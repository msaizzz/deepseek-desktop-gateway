from __future__ import annotations

import sys
import tempfile
import unittest
import os
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from src.deepseek_gateway.config_manager import ConfigManager
from src.deepseek_gateway.gateway_service import GatewayService
from src.deepseek_gateway.security_config import SecuritySettings


class GatewayServiceSecurityToggleTests(unittest.TestCase):
    def test_diagnostic_logging_defaults_to_disabled(self) -> None:
        config = ConfigManager(Path(tempfile.gettempdir())).load()

        with patch.dict(os.environ, {}, clear=False):
            enabled = GatewayService._resolve_diagnostic_logging_enabled(config)

        self.assertFalse(enabled)

    def test_diagnostic_logging_env_override_enables_logging(self) -> None:
        config = ConfigManager(Path(tempfile.gettempdir())).load()

        with patch.dict(os.environ, {"SRW_GATEWAY_DIAGNOSTIC_LOGS": "true"}, clear=False):
            enabled = GatewayService._resolve_diagnostic_logging_enabled(config)

        self.assertTrue(enabled)

    def test_wait_for_litellm_router_ready_returns_true_when_router_exists(self) -> None:
        fake_proxy_server = SimpleNamespace(llm_router=object())

        with patch.dict(sys.modules, {"litellm.proxy.proxy_server": fake_proxy_server}, clear=False):
            ready = GatewayService._wait_for_litellm_router_ready(timeout_seconds=0.01)

        self.assertTrue(ready)

    def test_wait_for_litellm_router_ready_times_out_without_router(self) -> None:
        fake_proxy_server = SimpleNamespace(llm_router=None)

        with patch.dict(sys.modules, {"litellm.proxy.proxy_server": fake_proxy_server}, clear=False):
            ready = GatewayService._wait_for_litellm_router_ready(timeout_seconds=0.0)

        self.assertFalse(ready)

    def test_temporary_disable_skips_guardrail_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = GatewayService(ConfigManager(root), object())
            runtime_config: dict[str, object] = {}

            service.set_security_interception_temporarily_disabled(True)
            with patch(
                "src.deepseek_gateway.gateway_service.SecuritySettings.load",
                return_value=SecuritySettings(enabled=True),
            ):
                service._inject_guardrails_config(runtime_config)

        self.assertNotIn("guardrails", runtime_config)

    def test_enabled_session_keeps_guardrail_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = GatewayService(ConfigManager(root), object())
            runtime_config: dict[str, object] = {}

            with patch(
                "src.deepseek_gateway.gateway_service.SecuritySettings.load",
                return_value=SecuritySettings(enabled=True),
            ):
                service._inject_guardrails_config(runtime_config)

        self.assertIn("guardrails", runtime_config)

    def test_ensure_litellm_builtin_guardrails_registered_adds_content_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = GatewayService(ConfigManager(root), object())

            initializer = object()
            guardrail_class = object()
            fake_registry_module = SimpleNamespace(
                guardrail_initializer_registry={},
                guardrail_class_registry={},
            )
            fake_content_filter_module = SimpleNamespace(
                guardrail_initializer_registry={"litellm_content_filter": initializer},
                guardrail_class_registry={"litellm_content_filter": guardrail_class},
            )

            def fake_import_module(name: str) -> object:
                modules = {
                    "litellm.proxy.guardrails.guardrail_registry": fake_registry_module,
                    "litellm.proxy.guardrails.guardrail_hooks.litellm_content_filter": fake_content_filter_module,
                }
                return modules[name]

            with patch(
                "src.deepseek_gateway.gateway_service.importlib.import_module",
                side_effect=fake_import_module,
            ):
                service._ensure_litellm_builtin_guardrails_registered()

        self.assertIs(
            fake_registry_module.guardrail_initializer_registry["litellm_content_filter"],
            initializer,
        )
        self.assertIs(
            fake_registry_module.guardrail_class_registry["litellm_content_filter"],
            guardrail_class,
        )

    def test_ensure_litellm_guardrail_translation_mappings_registered_adds_openai_chat(self) -> None:
        import litellm.llms as llms_module
        import litellm.proxy.guardrails.guardrail_hooks.unified_guardrail.unified_guardrail as unified_guardrail_module
        from litellm.llms.openai.chat.guardrail_translation.handler import (
            OpenAIChatCompletionsHandler,
        )
        from litellm.types.utils import CallTypes

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = GatewayService(ConfigManager(root), object())
            original_llms_mappings = getattr(
                llms_module,
                "endpoint_guardrail_translation_mappings",
                None,
            )
            original_unified_mappings = getattr(
                unified_guardrail_module,
                "endpoint_guardrail_translation_mappings",
                None,
            )

            try:
                llms_module.endpoint_guardrail_translation_mappings = {}
                unified_guardrail_module.endpoint_guardrail_translation_mappings = None

                service._ensure_litellm_guardrail_translation_mappings_registered()

                self.assertIs(
                    llms_module.endpoint_guardrail_translation_mappings[CallTypes.acompletion],
                    OpenAIChatCompletionsHandler,
                )
                self.assertIs(
                    unified_guardrail_module.endpoint_guardrail_translation_mappings[CallTypes.acompletion],
                    OpenAIChatCompletionsHandler,
                )
                self.assertIs(
                    unified_guardrail_module.endpoint_guardrail_translation_mappings[CallTypes.completion],
                    OpenAIChatCompletionsHandler,
                )
            finally:
                llms_module.endpoint_guardrail_translation_mappings = original_llms_mappings
                unified_guardrail_module.endpoint_guardrail_translation_mappings = original_unified_mappings

    def test_reset_litellm_guardrail_state_clears_config_guardrails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = GatewayService(ConfigManager(root), object())

            deleted_ids: list[str] = []

            class FakeHandler:
                def __init__(self) -> None:
                    self._sources = {
                        "cfg-1": "config",
                        "db-1": "db",
                        "cfg-2": "config",
                    }

                def delete_in_memory_guardrail(self, guardrail_id: str) -> None:
                    deleted_ids.append(guardrail_id)

            fake_handler = FakeHandler()
            fake_litellm = SimpleNamespace(guardrail_name_config_map={"old": object()})
            fake_init_guardrails = SimpleNamespace(all_guardrails=["stale"])
            fake_proxy_server = SimpleNamespace(llm_router=SimpleNamespace(guardrail_list=["stale"]))
            fake_registry_module = SimpleNamespace(IN_MEMORY_GUARDRAIL_HANDLER=fake_handler)

            def fake_import_module(name: str) -> object:
                modules = {
                    "litellm": fake_litellm,
                    "litellm.proxy.guardrails.init_guardrails": fake_init_guardrails,
                    "litellm.proxy.guardrails.guardrail_registry": fake_registry_module,
                }
                return modules[name]

            with patch("src.deepseek_gateway.gateway_service.importlib.import_module", side_effect=fake_import_module):
                with patch.dict(sys.modules, {"litellm.proxy.proxy_server": fake_proxy_server}, clear=False):
                    service._reset_litellm_guardrail_state()

        self.assertEqual(fake_litellm.guardrail_name_config_map, {})
        self.assertEqual(fake_init_guardrails.all_guardrails, [])
        self.assertEqual(deleted_ids, ["cfg-1", "cfg-2"])
        self.assertEqual(fake_proxy_server.llm_router.guardrail_list, [])

    def test_reconcile_guardrails_after_startup_reinitializes_from_runtime_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = GatewayService(ConfigManager(root), object())
            runtime_config_path = root / "runtime" / "litellm_config.yaml"
            runtime_config_path.parent.mkdir(parents=True, exist_ok=True)
            runtime_config_path.write_text(
                "guardrails:\n"
                "  - guardrail_name: local-content-filter\n"
                "    litellm_params:\n"
                "      guardrail: litellm_content_filter\n"
                "      mode: pre_call\n"
                "      default_on: true\n",
                encoding="utf-8",
            )

            fake_router = SimpleNamespace(guardrail_list=[])
            fake_proxy_server = SimpleNamespace(llm_router=fake_router)
            fake_handler = SimpleNamespace(list_in_memory_guardrails=lambda: [])
            fake_registry_module = SimpleNamespace(IN_MEMORY_GUARDRAIL_HANDLER=fake_handler)
            init_calls: list[tuple[list[dict[str, object]], str, object]] = []

            def fake_init_guardrails_v2(*, all_guardrails: list[dict[str, object]], config_file_path: str, llm_router: object) -> None:
                init_calls.append((all_guardrails, config_file_path, llm_router))

            fake_init_module = SimpleNamespace(init_guardrails_v2=fake_init_guardrails_v2)

            def fake_import_module(name: str) -> object:
                modules = {
                    "litellm.proxy.guardrails.guardrail_registry": fake_registry_module,
                    "litellm.proxy.guardrails.init_guardrails": fake_init_module,
                }
                return modules[name]

            with patch("src.deepseek_gateway.gateway_service.importlib.import_module", side_effect=fake_import_module):
                with patch.dict(sys.modules, {"litellm.proxy.proxy_server": fake_proxy_server}, clear=False):
                    service._reconcile_guardrails_after_startup(runtime_config_path)

        self.assertEqual(len(init_calls), 1)
        self.assertEqual(init_calls[0][0][0]["guardrail_name"], "local-content-filter")
        self.assertEqual(init_calls[0][1], str(runtime_config_path))
        self.assertIs(init_calls[0][2], fake_router)

    def test_reconcile_guardrails_after_startup_clears_callback_capability_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = GatewayService(ConfigManager(root), object())
            runtime_config_path = root / "runtime" / "litellm_config.yaml"
            runtime_config_path.parent.mkdir(parents=True, exist_ok=True)
            runtime_config_path.write_text(
                "guardrails:\n"
                "  - guardrail_name: local-content-filter\n"
                "    litellm_params:\n"
                "      guardrail: litellm_content_filter\n"
                "      mode: pre_call\n"
                "      default_on: true\n",
                encoding="utf-8",
            )

            fake_router = SimpleNamespace(guardrail_list=[])
            fake_proxy_server = SimpleNamespace(llm_router=fake_router)
            fake_handler = SimpleNamespace(
                list_in_memory_guardrails=lambda: [{"guardrail_name": "local-content-filter"}]
            )
            fake_registry_module = SimpleNamespace(IN_MEMORY_GUARDRAIL_HANDLER=fake_handler)
            fake_cache: dict[object, object] = {(1, (2, 3)): object()}
            fake_proxy_utils = SimpleNamespace(
                ProxyLogging=SimpleNamespace(_callback_capabilities_cache=fake_cache)
            )

            def fake_populate_router_guardrails(*, guardrail_list: list[dict[str, object]]) -> None:
                fake_router.guardrail_list = guardrail_list

            fake_init_module = SimpleNamespace(
                _populate_router_guardrail_list=fake_populate_router_guardrails
            )

            def fake_import_module(name: str) -> object:
                modules = {
                    "litellm.proxy.guardrails.guardrail_registry": fake_registry_module,
                    "litellm.proxy.guardrails.init_guardrails": fake_init_module,
                    "litellm.proxy.utils": fake_proxy_utils,
                }
                return modules[name]

            with patch("src.deepseek_gateway.gateway_service.importlib.import_module", side_effect=fake_import_module):
                with patch.dict(sys.modules, {"litellm.proxy.proxy_server": fake_proxy_server}, clear=False):
                    service._reconcile_guardrails_after_startup(runtime_config_path)

        self.assertEqual(fake_router.guardrail_list, [{"guardrail_name": "local-content-filter"}])
        self.assertEqual(fake_cache, {})

    def test_reconcile_guardrails_after_startup_restores_missing_callbacks_from_router(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = GatewayService(ConfigManager(root), object())
            runtime_config_path = root / "runtime" / "litellm_config.yaml"
            runtime_config_path.parent.mkdir(parents=True, exist_ok=True)
            runtime_config_path.write_text("guardrails: []\n", encoding="utf-8")

            callback = object()
            fake_router = SimpleNamespace(
                guardrail_list=[
                    {
                        "guardrail_name": "local-content-filter",
                        "callback": callback,
                    }
                ]
            )
            fake_proxy_server = SimpleNamespace(llm_router=fake_router)
            fake_cache: dict[object, object] = {(1, (2, 3)): object()}
            added_callbacks: list[object] = []
            fake_litellm = SimpleNamespace(
                callbacks=[],
                logging_callback_manager=SimpleNamespace(
                    add_litellm_callback=lambda cb: (added_callbacks.append(cb), fake_litellm.callbacks.append(cb))
                ),
            )
            fake_proxy_utils = SimpleNamespace(
                ProxyLogging=SimpleNamespace(_callback_capabilities_cache=fake_cache)
            )

            def fake_import_module(name: str) -> object:
                modules = {
                    "litellm": fake_litellm,
                    "litellm.proxy.utils": fake_proxy_utils,
                }
                return modules[name]

            with patch("src.deepseek_gateway.gateway_service.importlib.import_module", side_effect=fake_import_module):
                with patch.dict(sys.modules, {"litellm.proxy.proxy_server": fake_proxy_server}, clear=False):
                    service._reconcile_guardrails_after_startup(runtime_config_path)

        self.assertEqual(added_callbacks, [callback])
        self.assertEqual(fake_litellm.callbacks, [callback])
        self.assertEqual(fake_cache, {})


if __name__ == "__main__":
    unittest.main()