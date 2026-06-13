from __future__ import annotations

import importlib
import logging
import os
import sys
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from fastapi import FastAPI
from fastapi.routing import Mount

from .runtime_paths import 项目根目录

if not getattr(sys, "frozen", False):
    本地_litellm_目录 = 项目根目录() / "vendor" / "litellm"
    if 本地_litellm_目录.exists():
        sys.path.insert(0, str(本地_litellm_目录))

from .budgeting import BudgetStatus, month_key
from .config_manager import AppConfig, ConfigManager
from .database import UsageDatabase
from .runtime_paths import 用户数据目录
from .security_config import SecuritySettings
from .security_guard import SecurityGuardManager


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class GatewayRuntime:
    app: FastAPI
    server: uvicorn.Server
    thread: threading.Thread
    config_path: Path


class GatewayService:
    def __init__(self, config_manager: ConfigManager, database: UsageDatabase) -> None:
        self.config_manager = config_manager
        self.database = database
        self.runtime: GatewayRuntime | None = None
        self._security_interception_temporarily_disabled = False

    def build_app(self) -> FastAPI:
        config = self.config_manager.load()
        upstream_key = self.config_manager.get_upstream_api_key()
        if not upstream_key:
            raise ValueError("尚未配置上游 API Key。")

        config_path = self._write_runtime_config(config=config, upstream_key=upstream_key)
        self._prepare_litellm_environment(config_path)
        self._ensure_litellm_builtin_guardrails_registered()
        self._reset_litellm_guardrail_state()

        proxy_server = importlib.import_module("litellm.proxy.proxy_server")
        app = getattr(proxy_server, "app", None)
        if not isinstance(app, FastAPI):
            raise RuntimeError("LiteLLM gateway app 加载失败。")

        self._trim_to_gateway_routes(app)

        if self.runtime is not None:
            self.runtime = GatewayRuntime(
                app=app,
                server=self.runtime.server,
                thread=self.runtime.thread,
                config_path=config_path,
            )
        return app

    def current_budget_status(self, config: AppConfig) -> BudgetStatus:
        spent = self.database.monthly_spend(config.device_id, month_key())
        return BudgetStatus(spent_usd=spent, limit_usd=config.monthly_budget_usd)

    def start(self) -> None:
        if self.runtime:
            return
        config = self.config_manager.load()
        app = self.build_app()
        config_path = self._runtime_config_path()
        server_config = uvicorn.Config(
            app=app,
            host=config.host,
            port=config.port,
            log_level="warning",
            log_config=None,
            access_log=False,
        )
        server = uvicorn.Server(server_config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        self.runtime = GatewayRuntime(
            app=app,
            server=server,
            thread=thread,
            config_path=config_path,
        )

    def stop(self) -> None:
        if not self.runtime:
            return
        self.runtime.server.should_exit = True
        self.runtime.thread.join(timeout=5)
        self.runtime = None

    def is_running(self) -> bool:
        return bool(self.runtime and self.runtime.thread.is_alive())

    def set_security_interception_temporarily_disabled(self, disabled: bool) -> None:
        self._security_interception_temporarily_disabled = disabled

    def is_security_interception_temporarily_disabled(self) -> bool:
        return self._security_interception_temporarily_disabled

    def _prepare_litellm_environment(self, config_path: Path) -> None:
        os.environ["CONFIG_FILE_PATH"] = str(config_path)
        os.environ["LITELLM_DONT_SHOW_FEEDBACK_BOX"] = "true"
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")

    def _ensure_litellm_builtin_guardrails_registered(self) -> None:
        """补注册依赖目录扫描发现的 LiteLLM 内建护栏。"""
        try:
            registry_module = importlib.import_module(
                "litellm.proxy.guardrails.guardrail_registry"
            )
            content_filter_module = importlib.import_module(
                "litellm.proxy.guardrails.guardrail_hooks.litellm_content_filter"
            )
        except Exception:
            LOGGER.debug("补注册 LiteLLM content filter 失败。", exc_info=True)
            return

        initializer_registry = getattr(
            registry_module,
            "guardrail_initializer_registry",
            None,
        )
        class_registry = getattr(
            registry_module,
            "guardrail_class_registry",
            None,
        )
        content_filter_initializers = getattr(
            content_filter_module,
            "guardrail_initializer_registry",
            None,
        )
        content_filter_classes = getattr(
            content_filter_module,
            "guardrail_class_registry",
            None,
        )

        if isinstance(initializer_registry, dict) and isinstance(
            content_filter_initializers,
            dict,
        ):
            initializer_registry.update(content_filter_initializers)

        if isinstance(class_registry, dict) and isinstance(content_filter_classes, dict):
            class_registry.update(content_filter_classes)

    def _reset_litellm_guardrail_state(self) -> None:
        """清理 LiteLLM 进程级 guardrail 状态，避免同进程重启沿用旧配置。"""
        try:
            litellm_module = importlib.import_module("litellm")
        except Exception:
            LOGGER.debug("LiteLLM 尚未加载，跳过 guardrail 状态清理。")
            return

        setattr(litellm_module, "guardrail_name_config_map", {})

        try:
            init_guardrails_module = importlib.import_module(
                "litellm.proxy.guardrails.init_guardrails"
            )
            setattr(init_guardrails_module, "all_guardrails", [])
        except Exception:
            LOGGER.debug("清理 LiteLLM legacy guardrail 列表失败。", exc_info=True)

        try:
            registry_module = importlib.import_module(
                "litellm.proxy.guardrails.guardrail_registry"
            )
            handler = getattr(registry_module, "IN_MEMORY_GUARDRAIL_HANDLER", None)
            if handler is not None:
                config_guardrail_ids = [
                    guardrail_id
                    for guardrail_id, source in getattr(handler, "_sources", {}).items()
                    if source == "config"
                ]
                for guardrail_id in config_guardrail_ids:
                    handler.delete_in_memory_guardrail(guardrail_id)
        except Exception:
            LOGGER.debug("清理 LiteLLM v2 guardrail 注册表失败。", exc_info=True)

        proxy_server = sys.modules.get("litellm.proxy.proxy_server")
        if proxy_server is not None:
            llm_router = getattr(proxy_server, "llm_router", None)
            if llm_router is not None and hasattr(llm_router, "guardrail_list"):
                llm_router.guardrail_list = []

    def _trim_to_gateway_routes(self, app: FastAPI) -> None:
        if getattr(app.state, "srw_gateway_routes_trimmed", False):
            return

        allowlist = importlib.import_module("gateway.routes.allowlist")
        exact_paths = getattr(allowlist, "GATEWAY_EXACT_PATHS")
        path_prefixes = getattr(allowlist, "GATEWAY_PATH_PREFIXES")
        proxy_lifespan = app.router.lifespan_context

        def is_gateway_route(route: Any) -> bool:
            path = getattr(route, "path", None)
            if path is None:
                return False
            if isinstance(route, Mount):
                return False
            if path in exact_paths:
                return True
            return any(path.startswith(prefix) for prefix in path_prefixes)

        @asynccontextmanager
        async def gateway_lifespan(app_: FastAPI):
            async with proxy_lifespan(app_):
                app_.router.routes = [route for route in app_.router.routes if is_gateway_route(route)]
                yield

        app.router.lifespan_context = gateway_lifespan
        app.state.srw_gateway_routes_trimmed = True

    def _write_runtime_config(self, *, config: AppConfig, upstream_key: str) -> Path:
        self._write_runtime_callback_module()
        runtime_config = {
            "model_list": [
                {
                    "model_name": model_name,
                    "litellm_params": {
                        "model": self._build_upstream_model_name(pricing.upstream_model),
                        "api_key": upstream_key,
                        "api_base": config.upstream_base_url,
                    },
                    "model_info": {
                        "input_cost_per_token": pricing.input_per_million_usd / 1_000_000,
                        "output_cost_per_token": pricing.output_per_million_usd / 1_000_000,
                        "cache_creation_input_token_cost": pricing.input_per_million_usd / 1_000_000,
                        "cache_read_input_token_cost": pricing.cache_read_input_per_million / 1_000_000,
                    },
                }
                for model_name, pricing in config.models.items()
            ],
            "general_settings": {
                "disable_spend_logs": True,
            },
            "litellm_settings": {
                "drop_params": False,
                "callbacks": ["runtime_callbacks.local_budget_and_logging_handler"],
            },
        }

        # 注入安全护栏配置
        self._inject_guardrails_config(runtime_config)

        config_path = self._runtime_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            yaml.safe_dump(runtime_config, sort_keys=False, allow_unicode=False),
            encoding="utf-8",
        )
        LOGGER.info("已写入 LiteLLM 运行配置: %s", config_path)
        return config_path

    def _inject_guardrails_config(self, runtime_config: dict) -> None:
        """加载安全配置并注入到运行时 YAML 中。"""
        try:
            if self._security_interception_temporarily_disabled:
                LOGGER.warning("安全拦截已被本次会话临时关闭，跳过护栏配置注入。")
                return

            security_settings = SecuritySettings.load()
            if not security_settings.enabled:
                LOGGER.info("安全过滤已禁用，跳过护栏配置注入。")
                return

            manager = SecurityGuardManager(security_settings)
            guardrails_config = manager.build_guardrails_config()
            if guardrails_config is not None:
                runtime_config.update(guardrails_config)
                LOGGER.info("安全护栏配置已注入运行时配置。")
        except Exception:
            LOGGER.exception("安全护栏配置注入失败，网关将以无安全过滤模式运行。")

    @staticmethod
    def _runtime_config_path() -> Path:
        return 用户数据目录() / "runtime" / "litellm_config.yaml"

    @classmethod
    def _runtime_callback_module_path(cls) -> Path:
        return cls._runtime_config_path().parent / "runtime_callbacks.py"

    def _write_runtime_callback_module(self) -> None:
        callback_module_path = self._runtime_callback_module_path()
        callback_module_path.parent.mkdir(parents=True, exist_ok=True)
        callback_module_path.write_text(
            "from src.deepseek_gateway.litellm_extensions import local_budget_and_logging_handler\n",
            encoding="utf-8",
        )

    @staticmethod
    def _build_upstream_model_name(upstream_model: str) -> str:
        if "/" in upstream_model:
            return upstream_model
        return f"deepseek/{upstream_model}"
