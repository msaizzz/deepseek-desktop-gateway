from __future__ import annotations

import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from src.deepseek_gateway.config_manager import ConfigManager
from src.deepseek_gateway.gateway_service import GatewayService
from src.deepseek_gateway.security_config import SecuritySettings


class GatewayServiceSecurityToggleTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()