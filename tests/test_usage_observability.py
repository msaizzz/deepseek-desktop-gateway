from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.deepseek_gateway.database import UsageDatabase
from src.deepseek_gateway.litellm_extensions import _extract_cache_token_counts


class _PromptTokenDetails:
    def __init__(self, *, cached_tokens: int = 0, cache_creation_tokens: int = 0) -> None:
        self.cached_tokens = cached_tokens
        self.cache_creation_tokens = cache_creation_tokens


class _Usage:
    def __init__(
        self,
        *,
        prompt_tokens_details: _PromptTokenDetails | None = None,
        cache_read_input_tokens: int | None = None,
        cache_creation_input_tokens: int | None = None,
    ) -> None:
        self.prompt_tokens_details = prompt_tokens_details
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens


class _Response:
    def __init__(self, usage: _Usage) -> None:
        self.usage = usage


class UsageObservabilityTests(unittest.TestCase):
    def test_extract_cache_token_counts_from_prompt_token_details(self) -> None:
        response = _Response(
            _Usage(
                prompt_tokens_details=_PromptTokenDetails(cached_tokens=120, cache_creation_tokens=30)
            )
        )

        cache_read_input_tokens, cache_creation_input_tokens = _extract_cache_token_counts(response)

        self.assertEqual(cache_read_input_tokens, 120)
        self.assertEqual(cache_creation_input_tokens, 30)

    def test_extract_cache_token_counts_from_direct_usage_fields(self) -> None:
        response = _Response(_Usage(cache_read_input_tokens=80, cache_creation_input_tokens=20))

        cache_read_input_tokens, cache_creation_input_tokens = _extract_cache_token_counts(response)

        self.assertEqual(cache_read_input_tokens, 80)
        self.assertEqual(cache_creation_input_tokens, 20)

    def test_monthly_usage_snapshot_and_model_breakdown_include_cache_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = UsageDatabase(Path(temp_dir))
            database.log_request(
                device_id="device-a",
                model="deepseek-v4-flash",
                upstream_model="deepseek-v4-flash",
                input_tokens=1000,
                output_tokens=300,
                cache_read_input_tokens=250,
                cache_creation_input_tokens=50,
                cost_usd=1.25,
                status="success",
            )
            database.log_request(
                device_id="device-a",
                model="deepseek-v4-pro",
                upstream_model="deepseek-v4-pro",
                input_tokens=2000,
                output_tokens=800,
                cache_read_input_tokens=500,
                cache_creation_input_tokens=0,
                cost_usd=3.5,
                status="success",
            )

            snapshot = database.monthly_usage_snapshot("device-a", database.available_months("device-a")[0])
            breakdown = database.monthly_model_spend_breakdown("device-a", database.available_months("device-a")[0])
            del database

        self.assertEqual(snapshot.input_tokens, 3000)
        self.assertEqual(snapshot.output_tokens, 1100)
        self.assertEqual(snapshot.cache_read_input_tokens, 750)
        self.assertEqual(snapshot.cache_creation_input_tokens, 50)
        self.assertAlmostEqual(snapshot.total_cost, 4.75)
        self.assertEqual(breakdown, {"deepseek-v4-pro": 3.5, "deepseek-v4-flash": 1.25})


if __name__ == "__main__":
    unittest.main()