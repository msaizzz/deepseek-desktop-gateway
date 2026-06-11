from __future__ import annotations

import logging
from typing import Any, Optional, Union, cast

from fastapi import HTTPException
from litellm.caching.caching import DualCache
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import UserAPIKeyAuth
from litellm.types.utils import CallTypesLiteral, StandardLoggingPayload

from .budgeting import month_key
from .config_manager import ConfigManager
from .database import UsageDatabase


LOGGER = logging.getLogger(__name__)


def _read_field(source: Any, field_name: str, default: Any = None) -> Any:
    if source is None:
        return default
    if isinstance(source, dict):
        return source.get(field_name, default)
    return getattr(source, field_name, default)


def _int_field(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _extract_cache_token_counts(response_obj: Any) -> tuple[int, int]:
    usage = _read_field(response_obj, "usage")
    prompt_token_details = _read_field(usage, "prompt_tokens_details")

    cache_read_input_tokens = _int_field(_read_field(prompt_token_details, "cached_tokens"))
    cache_creation_input_tokens = _int_field(
        _read_field(prompt_token_details, "cache_write_tokens")
        or _read_field(prompt_token_details, "cache_creation_tokens")
    )

    if cache_read_input_tokens <= 0:
        cache_read_input_tokens = _int_field(_read_field(usage, "cache_read_input_tokens"))
    if cache_creation_input_tokens <= 0:
        cache_creation_input_tokens = _int_field(_read_field(usage, "cache_creation_input_tokens"))

    return max(cache_read_input_tokens, 0), max(cache_creation_input_tokens, 0)


class LocalBudgetAndLoggingHandler(CustomLogger):
    def __init__(
        self,
        config_manager: Optional[ConfigManager] = None,
        database: Optional[UsageDatabase] = None,
    ) -> None:
        super().__init__()
        self.config_manager = config_manager or ConfigManager()
        self.database = database or UsageDatabase()

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: CallTypesLiteral,
    ) -> Optional[Union[Exception, str, dict]]:
        del user_api_key_dict
        del cache
        del call_type

        config = self.config_manager.load()
        monthly_budget = float(config.monthly_budget_usd or 0.0)
        if monthly_budget <= 0:
            return data

        spent = self.database.monthly_spend(config.device_id, month_key())
        if spent >= monthly_budget:
            raise HTTPException(status_code=402, detail="本机本月预算已超限。")

        return data

    async def async_log_success_event(
        self,
        kwargs: dict,
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        del start_time
        del end_time

        config = self.config_manager.load()
        standard_logging_object = cast(
            Optional[StandardLoggingPayload], kwargs.get("standard_logging_object", None)
        )
        if standard_logging_object is None:
            LOGGER.debug("跳过本地成功记账：standard_logging_object 缺失")
            return

        model_name = cast(
            str,
            standard_logging_object.get("model_group")
            or kwargs.get("model")
            or "",
        )
        upstream_model = cast(
            str,
            standard_logging_object.get("model") or kwargs.get("model") or model_name,
        )
        prompt_tokens = int(standard_logging_object.get("prompt_tokens") or 0)
        completion_tokens = int(standard_logging_object.get("completion_tokens") or 0)
        response_cost = float(standard_logging_object.get("response_cost") or 0.0)
        cache_read_input_tokens, cache_creation_input_tokens = _extract_cache_token_counts(response_obj)

        if not model_name:
            LOGGER.debug("跳过本地成功记账：无法确定模型别名")
            return

        self.database.log_request(
            device_id=config.device_id,
            model=model_name,
            upstream_model=upstream_model,
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cost_usd=response_cost,
            status="success",
        )

    async def async_post_call_failure_hook(
        self,
        request_data: dict,
        original_exception: Exception,
        user_api_key_dict: UserAPIKeyAuth,
        traceback_str: Optional[str] = None,
    ) -> Optional[HTTPException]:
        del user_api_key_dict
        del traceback_str

        config = self.config_manager.load()
        model_name = cast(str, request_data.get("model") or "")
        pricing = config.models.get(model_name)
        upstream_model = pricing.upstream_model if pricing is not None else model_name

        self.database.log_request(
            device_id=config.device_id,
            model=model_name or "unknown",
            upstream_model=upstream_model or "unknown",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            status="error",
            error=str(original_exception),
        )
        return None


local_budget_and_logging_handler = LocalBudgetAndLoggingHandler()