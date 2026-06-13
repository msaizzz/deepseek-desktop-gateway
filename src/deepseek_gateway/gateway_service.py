from __future__ import annotations

import importlib
import logging
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from fastapi import FastAPI, Request
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
        self._diagnostic_logging_enabled = False

    def build_app(self) -> FastAPI:
        config = self.config_manager.load()
        self._diagnostic_logging_enabled = self._resolve_diagnostic_logging_enabled(config)
        upstream_key = self.config_manager.get_upstream_api_key()
        if not upstream_key:
            raise ValueError("尚未配置上游 API Key。")

        config_path = self._write_runtime_config(config=config, upstream_key=upstream_key)
        self._prepare_litellm_environment(config_path)
        self._reset_litellm_guardrail_state()
        self._ensure_litellm_builtin_guardrails_registered()
        self._ensure_litellm_guardrail_translation_mappings_registered()
        self._clear_litellm_callback_capabilities_cache("build-app")

        proxy_server = importlib.import_module("litellm.proxy.proxy_server")
        app = getattr(proxy_server, "app", None)
        if not isinstance(app, FastAPI):
            raise RuntimeError("LiteLLM gateway app 加载失败。")

        self._attach_guardrail_diagnostics_middleware(app)
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
        if self._wait_for_litellm_router_ready(timeout_seconds=5.0):
            self._reconcile_guardrails_after_startup(config_path)
            self._instrument_content_filter_guardrail()
            self._verify_guardrails_initialized()
        else:
            LOGGER.warning(
                "LiteLLM router 在启动后 5 秒内未就绪，暂无法校正安全护栏。"
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
            LOGGER.warning(
                "补注册 LiteLLM content filter 失败，安全护栏将不可用。"
                " 如果是发布版本，请确保 PyInstaller 包含了 guardrail_hooks 模块。",
                exc_info=True,
            )
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
            LOGGER.info(
                "已注册 LiteLLM content filter guardrail 初始化器，"
                "当前 registry 包含 %d 个 guardrail 类型: %s",
                len(initializer_registry),
                list(initializer_registry.keys()),
            )
        else:
            LOGGER.warning(
                "guardrail_initializer_registry 类型不匹配: "
                "registry=%s, content_filter=%s",
                type(initializer_registry).__name__,
                type(content_filter_initializers).__name__,
            )

        if isinstance(class_registry, dict) and isinstance(content_filter_classes, dict):
            class_registry.update(content_filter_classes)

        self._clear_litellm_callback_capabilities_cache("register-builtin-guardrails")

    def _ensure_litellm_guardrail_translation_mappings_registered(self) -> None:
        """补注册依赖目录扫描发现的 LiteLLM 护栏请求转换器。"""
        try:
            from litellm.llms.openai.chat.guardrail_translation.handler import (
                OpenAIChatCompletionsHandler,
            )
            from litellm.types.utils import CallTypes

            llms_module = importlib.import_module("litellm.llms")
        except Exception:
            LOGGER.warning(
                "补注册 LiteLLM OpenAI chat guardrail translation 失败，"
                "安全护栏可能无法在发布版执行。",
                exc_info=True,
            )
            return

        required_mappings = {
            CallTypes.completion: OpenAIChatCompletionsHandler,
            CallTypes.acompletion: OpenAIChatCompletionsHandler,
        }

        mappings = getattr(llms_module, "endpoint_guardrail_translation_mappings", None)
        if not isinstance(mappings, dict):
            mappings = {}
            setattr(llms_module, "endpoint_guardrail_translation_mappings", mappings)
        mappings.update(required_mappings)

        try:
            unified_guardrail_module = importlib.import_module(
                "litellm.proxy.guardrails.guardrail_hooks.unified_guardrail.unified_guardrail"
            )
            unified_mappings = getattr(
                unified_guardrail_module,
                "endpoint_guardrail_translation_mappings",
                None,
            )
            if not isinstance(unified_mappings, dict):
                unified_mappings = {}
                setattr(
                    unified_guardrail_module,
                    "endpoint_guardrail_translation_mappings",
                    unified_mappings,
                )
            unified_mappings.update(required_mappings)
        except Exception:
            LOGGER.debug(
                "同步 unified_guardrail translation mapping 缓存失败，稍后将由 LiteLLM 懒加载。",
                exc_info=True,
            )

        LOGGER.info(
            "已注册 LiteLLM OpenAI chat guardrail translation mappings: %s",
            [call_type.value for call_type in required_mappings],
        )

    def _reset_litellm_guardrail_state(self) -> None:
        """清理 LiteLLM 进程级 guardrail 状态，避免同进程重启沿用旧配置。"""
        try:
            litellm_module = importlib.import_module("litellm")
        except Exception:
            LOGGER.debug("LiteLLM 尚未加载，跳过 guardrail 状态清理。")
            return

        existing_map = getattr(litellm_module, "guardrail_name_config_map", None)
        if existing_map:
            LOGGER.info(
                "清理 guardrail_name_config_map（包含 %d 项）",
                len(existing_map),
            )
        setattr(litellm_module, "guardrail_name_config_map", {})

        try:
            init_guardrails_module = importlib.import_module(
                "litellm.proxy.guardrails.init_guardrails"
            )
            existing_all = getattr(init_guardrails_module, "all_guardrails", None)
            if existing_all:
                LOGGER.info("清理 all_guardrails 列表（包含 %d 项）", len(existing_all))
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
                if config_guardrail_ids:
                    LOGGER.info(
                        "清理 %d 个 config 来源的 in-memory guardrail: %s",
                        len(config_guardrail_ids),
                        config_guardrail_ids,
                    )
                    for guardrail_id in config_guardrail_ids:
                        handler.delete_in_memory_guardrail(guardrail_id)
        except Exception:
            LOGGER.debug("清理 LiteLLM v2 guardrail 注册表失败。", exc_info=True)

        proxy_server = sys.modules.get("litellm.proxy.proxy_server")
        if proxy_server is not None:
            llm_router = getattr(proxy_server, "llm_router", None)
            if llm_router is not None and hasattr(llm_router, "guardrail_list"):
                llm_router.guardrail_list = []

        self._clear_litellm_callback_capabilities_cache("reset-guardrail-state")

    def _verify_guardrails_initialized(self) -> None:
        """验证 LiteLLM guardrails 是否已成功初始化，输出诊断信息到日志。"""
        try:
            proxy_server = sys.modules.get("litellm.proxy.proxy_server")
            if proxy_server is None:
                LOGGER.warning("无法验证 guardrails: proxy_server 模块未加载。")
                return

            llm_router = getattr(proxy_server, "llm_router", None)
            if llm_router is None:
                LOGGER.warning("无法验证 guardrails: llm_router 未初始化。")
                return

            guardrail_list = getattr(llm_router, "guardrail_list", None)
            if guardrail_list is None:
                LOGGER.warning("无法验证 guardrails: llm_router.guardrail_list 为 None。")
                return

            if not guardrail_list:
                LOGGER.warning(
                    "⚠️ 安全护栏验证失败: llm_router.guardrail_list 为空！"
                    " 安全拦截将不会生效。请检查 LiteLLM guardrail 初始化日志。"
                )
            else:
                guardrail_names = [
                    g.get("guardrail_name", "unknown") for g in guardrail_list
                ]
                LOGGER.info(
                    "✅ 安全护栏验证通过: %d 个 guardrail 已激活 (%s)",
                    len(guardrail_list),
                    guardrail_names,
                )

            # 同时检查 IN_MEMORY_GUARDRAIL_HANDLER 中的状态
            try:
                registry_module = importlib.import_module(
                    "litellm.proxy.guardrails.guardrail_registry"
                )
                handler = getattr(registry_module, "IN_MEMORY_GUARDRAIL_HANDLER", None)
                if handler is not None:
                    in_memory_count = len(
                        getattr(handler, "IN_MEMORY_GUARDRAILS", {})
                    )
                    LOGGER.info(
                        "IN_MEMORY_GUARDRAILS 中有 %d 个 guardrail",
                        in_memory_count,
                    )
            except Exception:
                LOGGER.debug("检查 IN_MEMORY_GUARDRAILS 失败。", exc_info=True)

            self._log_guardrail_runtime_state("startup-verify")

        except Exception:
            LOGGER.exception("验证 guardrails 初始化状态时出错。")

    def _attach_guardrail_diagnostics_middleware(self, app: FastAPI) -> None:
        if not self._diagnostic_logging_enabled:
            return
        if getattr(app.state, "srw_guardrail_diagnostics_middleware_attached", False):
            return

        app.state.srw_guardrail_diagnostics_request_count = 0

        @app.middleware("http")
        async def guardrail_diagnostics_middleware(request: Request, call_next):
            if app.state.srw_guardrail_diagnostics_request_count < 10:
                app.state.srw_guardrail_diagnostics_request_count += 1
                LOGGER.info(
                    "HTTP 请求诊断[%d]: method=%s path=%s query=%s content_type=%s user_agent=%s content_length=%s",
                    app.state.srw_guardrail_diagnostics_request_count,
                    request.method,
                    request.url.path,
                    request.url.query,
                    request.headers.get("content-type", ""),
                    request.headers.get("user-agent", ""),
                    request.headers.get("content-length", ""),
                )
                if request.method.upper() == "POST":
                    self._log_guardrail_runtime_state(
                        f"request-preflight path={request.url.path}"
                    )
            return await call_next(request)

        app.state.srw_guardrail_diagnostics_middleware_attached = True

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
                self._reconcile_guardrails_after_startup(self._runtime_config_path())
                self._verify_guardrails_initialized()
                app_.router.routes = [route for route in app_.router.routes if is_gateway_route(route)]
                yield

        app.router.lifespan_context = gateway_lifespan
        app.state.srw_gateway_routes_trimmed = True

    @staticmethod
    def _clear_litellm_callback_capabilities_cache(context: str) -> None:
        try:
            proxy_utils = importlib.import_module("litellm.proxy.utils")
            proxy_logging_cls = getattr(proxy_utils, "ProxyLogging", None)
            cache = getattr(proxy_logging_cls, "_callback_capabilities_cache", None)
            if isinstance(cache, dict):
                cleared_entries = len(cache)
                cache.clear()
                LOGGER.info(
                    "已清理 LiteLLM callback 能力缓存: context=%s entries=%d",
                    context,
                    cleared_entries,
                )
        except Exception:
            LOGGER.debug(
                "清理 LiteLLM callback 能力缓存失败: context=%s",
                context,
                exc_info=True,
            )

    @staticmethod
    def _wait_for_litellm_router_ready(timeout_seconds: float) -> bool:
        started_at = time.monotonic()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            proxy_server = sys.modules.get("litellm.proxy.proxy_server")
            llm_router = None
            if proxy_server is not None:
                llm_router = getattr(proxy_server, "llm_router", None)
            if llm_router is not None:
                LOGGER.info(
                    "LiteLLM router 已就绪: elapsed=%.2fs router_id=%s",
                    time.monotonic() - started_at,
                    hex(id(llm_router)),
                )
                return True
            time.sleep(0.1)
        LOGGER.warning(
            "等待 LiteLLM router 超时: timeout=%.2fs proxy_server_loaded=%s",
            timeout_seconds,
            "litellm.proxy.proxy_server" in sys.modules,
        )
        return False

    def _reconcile_guardrails_after_startup(self, config_path: Path) -> None:
        """在 LiteLLM startup 完成后，按运行时 YAML 兜底恢复 guardrails。"""
        try:
            proxy_server = sys.modules.get("litellm.proxy.proxy_server")
            if proxy_server is None:
                LOGGER.warning("LiteLLM proxy_server 模块未加载，跳过 guardrail 启动后校正。")
                return

            llm_router = getattr(proxy_server, "llm_router", None)
            if llm_router is None:
                LOGGER.warning("LiteLLM llm_router 未初始化，跳过 guardrail 启动后校正。")
                return

            guardrail_list = getattr(llm_router, "guardrail_list", None)
            if guardrail_list:
                self._ensure_router_guardrail_callbacks_registered(guardrail_list)
                self._clear_litellm_callback_capabilities_cache(
                    "startup-reconcile-existing-router-guardrails"
                )
                self._recover_unresolved_guardrail_callbacks(config_path, llm_router)
                LOGGER.info(
                    "LiteLLM startup 后检测到 guardrail 已存在: router_guardrails=%d",
                    len(guardrail_list),
                )
                self._log_guardrail_runtime_state("startup-reconcile-skip")
                return

            if not config_path.exists():
                LOGGER.warning("LiteLLM 运行时配置不存在，无法校正 guardrail: %s", config_path)
                return

            runtime_config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            configured_guardrails = runtime_config.get("guardrails") or []
            if not configured_guardrails:
                LOGGER.info("运行时配置未声明 guardrails，跳过启动后校正。")
                return

            registry_module = importlib.import_module(
                "litellm.proxy.guardrails.guardrail_registry"
            )
            init_guardrails_module = importlib.import_module(
                "litellm.proxy.guardrails.init_guardrails"
            )
            handler = getattr(registry_module, "IN_MEMORY_GUARDRAIL_HANDLER", None)
            in_memory_guardrails = []
            if handler is not None and hasattr(handler, "list_in_memory_guardrails"):
                in_memory_guardrails = handler.list_in_memory_guardrails()
                LOGGER.info(
                    "LiteLLM startup 后诊断: in-memory guardrails=%d, router_guardrails=%d, configured_guardrails=%d",
                    len(in_memory_guardrails),
                    len(guardrail_list or []),
                    len(configured_guardrails),
                )

            if in_memory_guardrails:
                populate_router_guardrails = getattr(
                    init_guardrails_module,
                    "_populate_router_guardrail_list",
                    None,
                )
                if callable(populate_router_guardrails):
                    populate_router_guardrails(guardrail_list=in_memory_guardrails)
                    refreshed_guardrail_list = getattr(llm_router, "guardrail_list", None) or []
                    self._ensure_router_guardrail_callbacks_registered(refreshed_guardrail_list)
                    self._clear_litellm_callback_capabilities_cache(
                        "startup-reconcile-from-memory"
                    )
                    self._recover_unresolved_guardrail_callbacks(config_path, llm_router)
                    LOGGER.warning(
                        "LiteLLM startup 后检测到 guardrail_list 为空，已从 in-memory guardrails 恢复 %d 项。",
                        len(in_memory_guardrails),
                    )
                    self._log_guardrail_runtime_state("startup-reconcile-from-memory")
                return

            init_guardrails_v2 = getattr(init_guardrails_module, "init_guardrails_v2", None)
            if not callable(init_guardrails_v2):
                LOGGER.warning("LiteLLM init_guardrails_v2 不可用，无法执行 guardrail 启动后校正。")
                return

            init_guardrails_v2(
                all_guardrails=configured_guardrails,
                config_file_path=str(config_path),
                llm_router=llm_router,
            )
            refreshed_guardrail_list = getattr(llm_router, "guardrail_list", None) or []
            self._ensure_router_guardrail_callbacks_registered(refreshed_guardrail_list)
            self._clear_litellm_callback_capabilities_cache(
                "startup-reconcile-from-config"
            )
            self._recover_unresolved_guardrail_callbacks(config_path, llm_router)
            LOGGER.warning(
                "LiteLLM startup 后检测到 guardrail 未初始化，已按运行时配置重新加载 %d 项。",
                len(configured_guardrails),
            )
            self._log_guardrail_runtime_state("startup-reconcile-from-config")
        except Exception:
            LOGGER.exception("LiteLLM guardrail 启动后校正失败。")

    def _recover_unresolved_guardrail_callbacks(self, config_path: Path, llm_router: Any) -> None:
        snapshot = self._get_guardrail_callback_snapshot()
        if snapshot is None:
            return

        if self._diagnostic_logging_enabled:
            LOGGER.info(
                "Guardrail callback 诊断[startup-callback-snapshot]: raw=%s resolved=%s",
                snapshot["raw_content_filter_callbacks"],
                snapshot["resolved_content_filter_callbacks"],
            )

        has_executable_resolved_content_filter = any(
            bool(callback.get("is_custom_guardrail"))
            for callback in snapshot["resolved_content_filter_callbacks"]
        )

        if not snapshot["raw_content_filter_callbacks"] or has_executable_resolved_content_filter:
            return

        LOGGER.warning(
            "LiteLLM startup 后检测到 ContentFilterGuardrail 仅存在于 raw callbacks，未进入 resolved_callbacks；将按运行时配置重新初始化 guardrails。"
        )
        self._reinitialize_guardrails_from_runtime_config(config_path, llm_router)

        refreshed_snapshot = self._get_guardrail_callback_snapshot()
        if self._diagnostic_logging_enabled and refreshed_snapshot is not None:
            LOGGER.info(
                "Guardrail callback 诊断[startup-callback-refresh]: raw=%s resolved=%s",
                refreshed_snapshot["raw_content_filter_callbacks"],
                refreshed_snapshot["resolved_content_filter_callbacks"],
            )

    @staticmethod
    def _get_guardrail_callback_snapshot() -> dict[str, Any] | None:
        try:
            litellm_module = importlib.import_module("litellm")
            proxy_utils = importlib.import_module("litellm.proxy.utils")
            custom_guardrail_module = importlib.import_module(
                "litellm.integrations.custom_guardrail"
            )
            custom_logger_module = importlib.import_module(
                "litellm.integrations.custom_logger"
            )
        except Exception:
            LOGGER.debug("读取 Guardrail callback 快照失败：LiteLLM 模块不可用。", exc_info=True)
            return None

        proxy_logging_cls = getattr(proxy_utils, "ProxyLogging", None)
        if proxy_logging_cls is None or not hasattr(proxy_logging_cls, "_callback_capabilities"):
            return None

        callbacks = list(getattr(litellm_module, "callbacks", []) or [])
        caps = proxy_logging_cls._callback_capabilities()
        resolved_callbacks = list(getattr(caps, "resolved_callbacks", ()) or ())
        custom_guardrail_cls = getattr(custom_guardrail_module, "CustomGuardrail", None)
        custom_logger_cls = getattr(custom_logger_module, "CustomLogger", None)

        def serialize_callback(callback: Any) -> dict[str, Any]:
            return {
                "id": hex(id(callback)),
                "type": type(callback).__name__,
                "module": type(callback).__module__,
                "default_on": getattr(callback, "default_on", None),
                "event_hook": str(getattr(callback, "event_hook", None)),
                "wrapped": bool(getattr(callback, "_srw_diagnostics_wrapped", False)),
                "is_custom_logger": bool(custom_logger_cls and isinstance(callback, custom_logger_cls)),
                "is_custom_guardrail": bool(custom_guardrail_cls and isinstance(callback, custom_guardrail_cls)),
            }

        raw_content_filter_callbacks = [
            serialize_callback(callback)
            for callback in callbacks
            if type(callback).__name__ == "ContentFilterGuardrail"
        ]
        resolved_content_filter_callbacks = [
            serialize_callback(callback)
            for callback in resolved_callbacks
            if type(callback).__name__ == "ContentFilterGuardrail"
        ]
        return {
            "raw_content_filter_callbacks": raw_content_filter_callbacks,
            "resolved_content_filter_callbacks": resolved_content_filter_callbacks,
        }

    def _reinitialize_guardrails_from_runtime_config(self, config_path: Path, llm_router: Any) -> None:
        if not config_path.exists():
            LOGGER.warning("LiteLLM 运行时配置不存在，无法重新初始化 guardrails: %s", config_path)
            return

        runtime_config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        configured_guardrails = runtime_config.get("guardrails") or []
        if not configured_guardrails:
            LOGGER.warning("运行时配置未声明 guardrails，无法重新初始化可执行 callbacks。")
            return

        litellm_module = importlib.import_module("litellm")
        registry_module = importlib.import_module(
            "litellm.proxy.guardrails.guardrail_registry"
        )
        init_guardrails_module = importlib.import_module(
            "litellm.proxy.guardrails.init_guardrails"
        )
        handler = getattr(registry_module, "IN_MEMORY_GUARDRAIL_HANDLER", None)

        if handler is not None and hasattr(handler, "delete_in_memory_guardrail"):
            sources = getattr(handler, "_sources", {}) or {}
            in_memory = getattr(handler, "IN_MEMORY_GUARDRAILS", {}) or {}
            guardrail_ids = [
                guardrail_id
                for guardrail_id, source in sources.items()
                if source == "config"
            ]
            if not guardrail_ids:
                guardrail_ids = list(in_memory.keys())
            for guardrail_id in guardrail_ids:
                handler.delete_in_memory_guardrail(guardrail_id)

        guardrail_name_config_map = getattr(litellm_module, "guardrail_name_config_map", None)
        if isinstance(guardrail_name_config_map, dict):
            guardrail_name_config_map.clear()

        all_guardrails = getattr(init_guardrails_module, "all_guardrails", None)
        if isinstance(all_guardrails, list):
            all_guardrails.clear()

        if hasattr(llm_router, "guardrail_list"):
            llm_router.guardrail_list = []

        init_guardrails_v2 = getattr(init_guardrails_module, "init_guardrails_v2", None)
        if not callable(init_guardrails_v2):
            LOGGER.warning("LiteLLM init_guardrails_v2 不可用，无法重新初始化 guardrails。")
            return

        init_guardrails_v2(
            all_guardrails=configured_guardrails,
            config_file_path=str(config_path),
            llm_router=llm_router,
        )
        refreshed_guardrail_list = getattr(llm_router, "guardrail_list", None) or []
        self._ensure_router_guardrail_callbacks_registered(refreshed_guardrail_list)
        self._clear_litellm_callback_capabilities_cache(
            "startup-reinitialize-unresolved-guardrails"
        )
        LOGGER.warning(
            "已按运行时配置重新初始化 guardrails，以恢复可执行的 ContentFilterGuardrail callbacks: count=%d",
            len(configured_guardrails),
        )

    @staticmethod
    def _ensure_router_guardrail_callbacks_registered(guardrail_list: list[Any]) -> None:
        try:
            litellm_module = importlib.import_module("litellm")
        except Exception:
            LOGGER.debug("同步 router guardrail callbacks 失败：LiteLLM 模块不可用。", exc_info=True)
            return

        callbacks = getattr(litellm_module, "callbacks", None)
        if not isinstance(callbacks, list):
            LOGGER.debug("同步 router guardrail callbacks 失败：litellm.callbacks 不可写。")
            return

        logging_callback_manager = getattr(litellm_module, "logging_callback_manager", None)
        added_guardrail_names: list[str] = []
        for guardrail in guardrail_list:
            if not isinstance(guardrail, dict):
                continue
            callback = guardrail.get("callback")
            if callback is None or callback in callbacks:
                continue

            if logging_callback_manager is not None and hasattr(logging_callback_manager, "add_litellm_callback"):
                logging_callback_manager.add_litellm_callback(callback)
            else:
                callbacks.append(callback)

            added_guardrail_names.append(str(guardrail.get("guardrail_name", type(callback).__name__)))

        if added_guardrail_names:
            LOGGER.warning(
                "LiteLLM startup 后补注册了 %d 个 router guardrail callbacks 到 litellm.callbacks: %s",
                len(added_guardrail_names),
                added_guardrail_names,
            )

    @staticmethod
    def _is_guardrail_relevant_path(path: str) -> bool:
        normalized = path.lower()
        return any(
            marker in normalized
            for marker in (
                "/chat/completions",
                "/completions",
                "/responses",
                "/messages",
            )
        )

    def _log_guardrail_runtime_state(self, context: str) -> None:
        if not self._diagnostic_logging_enabled:
            return
        try:
            proxy_server = sys.modules.get("litellm.proxy.proxy_server")
            llm_router = getattr(proxy_server, "llm_router", None) if proxy_server is not None else None
            router_guardrail_list = getattr(llm_router, "guardrail_list", None) if llm_router is not None else None

            callback_types: list[str] = []
            guardrail_callback_types: list[str] = []
            resolved_callback_types: list[str] = []
            try:
                litellm_module = importlib.import_module("litellm")
                proxy_utils = importlib.import_module("litellm.proxy.utils")
                callbacks = list(getattr(litellm_module, "callbacks", []) or [])
                callback_types = [type(callback).__name__ for callback in callbacks]
                guardrail_callback_types = [
                    type(callback).__name__
                    for callback in callbacks
                    if hasattr(callback, "should_run_guardrail")
                ]
                proxy_logging_cls = getattr(proxy_utils, "ProxyLogging", None)
                if proxy_logging_cls is not None and hasattr(proxy_logging_cls, "_callback_capabilities"):
                    caps = proxy_logging_cls._callback_capabilities()
                    resolved_callback_types = [
                        type(callback).__name__
                        for callback in list(getattr(caps, "resolved_callbacks", ()) or ())
                    ]
            except Exception:
                LOGGER.debug("读取 LiteLLM callbacks 失败。", exc_info=True)

            in_memory_guardrail_names: list[str] = []
            try:
                registry_module = importlib.import_module(
                    "litellm.proxy.guardrails.guardrail_registry"
                )
                handler = getattr(registry_module, "IN_MEMORY_GUARDRAIL_HANDLER", None)
                if handler is not None:
                    in_memory_guardrails = handler.list_in_memory_guardrails()
                    in_memory_guardrail_names = [
                        str(guardrail.get("guardrail_name", "unknown"))
                        for guardrail in in_memory_guardrails
                    ]
            except Exception:
                LOGGER.debug("读取 in-memory guardrails 失败。", exc_info=True)

            LOGGER.info(
                "Guardrail 运行态诊断[%s]: router_ready=%s router_guardrails=%s in_memory_guardrails=%s guardrail_callbacks=%s callbacks=%s resolved_callbacks=%s",
                context,
                llm_router is not None,
                list(router_guardrail_list or []) if isinstance(router_guardrail_list, list) else router_guardrail_list,
                in_memory_guardrail_names,
                guardrail_callback_types,
                callback_types,
                resolved_callback_types,
            )
        except Exception:
            LOGGER.exception("记录 Guardrail 运行态诊断失败: context=%s", context)

    def _instrument_content_filter_guardrail(self) -> None:
        if not self._diagnostic_logging_enabled:
            return
        try:
            litellm_module = importlib.import_module("litellm")
            callbacks = list(getattr(litellm_module, "callbacks", []) or [])
        except Exception:
            LOGGER.debug("挂载 ContentFilterGuardrail 诊断包装器失败：LiteLLM callbacks 不可读。", exc_info=True)
            return

        instrumented_count = 0
        for callback in callbacks:
            if type(callback).__name__ != "ContentFilterGuardrail":
                continue
            if getattr(callback, "_srw_diagnostics_wrapped", False):
                instrumented_count += 1
                continue

            original_hook = getattr(callback, "apply_guardrail", None)
            if original_hook is None:
                continue

            async def wrapped_apply_guardrail(*args: Any, __original_hook=original_hook, __callback=callback, **kwargs: Any):
                request_data = kwargs.get("request_data")
                if request_data is None and len(args) >= 2:
                    request_data = args[1]

                joined_text = self._extract_request_text_for_diagnostics(request_data if isinstance(request_data, dict) else {})
                blocked_keywords = sorted(
                    keyword
                    for keyword in getattr(__callback, "blocked_words", {}).keys()
                    if isinstance(keyword, str) and keyword and keyword in joined_text.lower()
                )

                LOGGER.info(
                    "ContentFilterGuardrail 诊断: entered text_length=%d matched_blocked_keywords=%s contains_target=%s preview=%s",
                    len(joined_text),
                    blocked_keywords[:10],
                    "国家秘密" in joined_text,
                    joined_text[:200],
                )
                try:
                    result = await __original_hook(*args, **kwargs)
                    LOGGER.info(
                        "ContentFilterGuardrail 诊断: completed matched_blocked_keywords=%s",
                        blocked_keywords[:10],
                    )
                    return result
                except Exception as exc:
                    LOGGER.warning(
                        "ContentFilterGuardrail 诊断: raised %s matched_blocked_keywords=%s message=%s",
                        type(exc).__name__,
                        blocked_keywords[:10],
                        str(exc),
                    )
                    raise

            setattr(callback, "apply_guardrail", wrapped_apply_guardrail)
            setattr(callback, "_srw_diagnostics_wrapped", True)
            instrumented_count += 1

        if instrumented_count:
            LOGGER.info(
                "已挂载 ContentFilterGuardrail 诊断包装器: count=%d",
                instrumented_count,
            )
        else:
            LOGGER.warning("未找到可挂载的 ContentFilterGuardrail 诊断包装器实例。")

    @staticmethod
    def _extract_request_text_for_diagnostics(data: dict[str, Any]) -> str:
        parts: list[str] = []

        messages = data.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if not isinstance(message, dict):
                    continue
                content = message.get("content", "")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    parts.append(str(content))

        input_data = data.get("input")
        if isinstance(input_data, str):
            parts.append(input_data)
        elif isinstance(input_data, list):
            parts.append(str(input_data))

        return "\n".join(part for part in parts if part)

    @staticmethod
    def _resolve_diagnostic_logging_enabled(config: AppConfig) -> bool:
        env_value = os.environ.get("SRW_GATEWAY_DIAGNOSTIC_LOGS", "").strip().lower()
        if env_value in {"1", "true", "yes", "on"}:
            return True
        if env_value in {"0", "false", "no", "off"}:
            return False
        return bool(getattr(config, "enable_diagnostic_logs", False))

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

            # 预检查: 确保 LiteLLM content_filter 护栏模块可导入
            # 在 PyInstaller 打包环境中，此模块可能未被包含，导致后续启动崩溃
            if not self._can_load_content_filter_guardrail():
                LOGGER.warning(
                    "LiteLLM content_filter 护栏模块不可用，跳过护栏配置注入。"
                    " 网关将以无安全过滤模式运行。"
                )
                return

            manager = SecurityGuardManager(security_settings)
            guardrails_config = manager.build_guardrails_config()
            if guardrails_config is not None:
                runtime_config.update(guardrails_config)
                LOGGER.info("安全护栏配置已注入运行时配置。")
        except Exception:
            LOGGER.exception("安全护栏配置注入失败，网关将以无安全过滤模式运行。")

    @staticmethod
    def _can_load_content_filter_guardrail() -> bool:
        """检测 LiteLLM content_filter 护栏模块是否可加载。"""
        try:
            importlib.import_module(
                "litellm.proxy.guardrails.guardrail_hooks.litellm_content_filter"
            )
            return True
        except Exception:
            return False

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
