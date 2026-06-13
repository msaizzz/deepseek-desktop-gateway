from __future__ import annotations

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class SecurityDefaultsTests(unittest.TestCase):
    def test_default_blocked_words_are_limited_to_state_secret_and_basic_audit(self) -> None:
        payload = yaml.safe_load((ROOT / "security" / "blocked_words.yaml").read_text(encoding="utf-8"))
        blocked_words = payload["blocked_words"]
        keywords = {item["keyword"] for item in blocked_words}

        self.assertIn("国家秘密", keywords)
        self.assertIn("绝密", keywords)
        self.assertIn("管理员密码", keywords)

        self.assertNotIn("忽略之前的指令", keywords)
        self.assertNotIn("暴力", keywords)
        self.assertNotIn("军事基地", keywords)
        self.assertNotIn("DROP TABLE", keywords)

        # 高频通用词不应出现在默认 BLOCK 列表中，避免误拦 VS Code Copilot 正常请求
        for too_broad in ("机密", "涉密", "密级", "定密"):
            self.assertNotIn(too_broad, keywords)

    def test_default_regex_rules_only_keep_minimal_blocking_patterns(self) -> None:
        payload = yaml.safe_load((ROOT / "security" / "regex_patterns.yaml").read_text(encoding="utf-8"))
        patterns = payload["patterns"]
        pattern_names = {item["pattern_name"] for item in patterns}

        self.assertEqual(
            pattern_names,
            {"classified_marking", "credential_in_text", "openai_api_key"},
        )


if __name__ == "__main__":
    unittest.main()