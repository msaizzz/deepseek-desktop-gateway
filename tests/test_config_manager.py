from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.deepseek_gateway.config_manager import ConfigManager


class ConfigManagerTests(unittest.TestCase):
    def test_current_user_secret_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = ConfigManager(root)

            manager.set_upstream_api_key("sk-test-visible")

            payload = json.loads((root / "secrets.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 2)
            self.assertEqual(payload["scope"], "windows-current-user")
            self.assertNotIn("sk-test-visible", json.dumps(payload, ensure_ascii=True))
            self.assertEqual(manager.get_upstream_api_key(), "sk-test-visible")

    def test_load_recovers_config_when_bound_secret_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = ConfigManager(root)
            config = manager.load()
            manager.set_upstream_api_key("sk-test-visible")
            manager.save(config)

            with patch(
                "src.deepseek_gateway.config_manager._unprotect_for_current_user",
                side_effect=OSError("dpapi unavailable"),
            ):
                recovered_manager = ConfigManager(root)
                recovered_config = recovered_manager.load()
                self.assertEqual(recovered_config.device_id, config.device_id)
                self.assertEqual(recovered_manager.get_upstream_api_key(), "")
                recovered_manager.save(recovered_config)

            rewritten_payload = json.loads((root / "secrets.json").read_text(encoding="utf-8"))
            self.assertEqual(rewritten_payload["version"], 2)
            self.assertEqual(rewritten_payload["scope"], "windows-current-user")

            final_manager = ConfigManager(root)
            self.assertEqual(final_manager.get_upstream_api_key(), "")
            reloaded_config = final_manager.load()
            self.assertEqual(reloaded_config.device_id, config.device_id)

    def test_tampered_config_still_raises_when_secret_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = ConfigManager(root)
            config = manager.load()
            manager.set_upstream_api_key("sk-test-visible")
            manager.save(config)

            config_path = root / "config.json"
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            payload["host"] = "10.0.0.1"
            config_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "配置完整性校验失败"):
                ConfigManager(root).load()


if __name__ == "__main__":
    unittest.main()