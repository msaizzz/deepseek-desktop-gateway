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

    def build_app(self) -> FastAPI:
        config = self.config_manager.load()
        upstream_key = self.config_manager.get_upstream_api_key()
        if not upstream_key:
            raise ValueError("尚未配置上游 API Key。")

        config_path = self._write_runtime_config(config=config, upstream_key=upstream_key)
        self._prepare_litellm_environment(config_path)

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

    def _prepare_litellm_environment(self, config_path: Path) -> None:
        os.environ["CONFIG_FILE_PATH"] = str(config_path)
        os.environ["LITELLM_DONT_SHOW_FEEDBACK_BOX"] = "true"
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")

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
        config_path = self._runtime_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            yaml.safe_dump(runtime_config, sort_keys=False, allow_unicode=False),
            encoding="utf-8",
        )
        LOGGER.info("已写入 LiteLLM 运行配置: %s", config_path)
        return config_path

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
