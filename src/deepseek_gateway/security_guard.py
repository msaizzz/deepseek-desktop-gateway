"""安全护栏管理器。

职责:
- 读取 SecuritySettings
- 生成 LiteLLM 原生 guardrails 运行时配置
- 解析配置文件路径（用户目录优先，默认目录回退）
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from .security_config import (
    ContentFilterSettings,
    InjectionDetectionSettings,
    OutputModerationSettings,
    PiiMaskingSettings,
    PiiPattern,
    SecuritySettings,
)

LOGGER = logging.getLogger(__name__)


class SecurityGuardManager:
    """管理安全护栏的配置生成和文件解析。"""

    def __init__(
        self,
        settings: SecuritySettings,
        config_dir: Path | None = None,
    ) -> None:
        self.settings = settings
        self.config_dir = config_dir or SecuritySettings.default_config_dir()

    # ----------------------------------------------------------------
    # 公开 API
    # ----------------------------------------------------------------

    def build_guardrails_config(self) -> dict[str, Any] | None:
        """生成 LiteLLM guardrails 配置段，可直接合并到运行时 YAML。

        返回 None 表示安全过滤已禁用或不需生成配置。
        """
        if not self.settings.enabled:
            LOGGER.info("安全过滤总开关已关闭，跳过 guardrails 配置生成。")
            return None

        guardrails: list[dict[str, Any]] = []

        # 第一期：内容过滤（关键词 + 正则 + PII）
        cf_config = self._build_content_filter_guardrail()
        if cf_config is not None:
            guardrails.append(cf_config)

        # 第二期：提示词注入检测
        inj_config = self._build_injection_detection_guardrail()
        if inj_config is not None:
            guardrails.append(inj_config)

        # 第二期：输出内容审核
        om_config = self._build_output_moderation_guardrail()
        if om_config is not None:
            guardrails.append(om_config)

        if not guardrails:
            LOGGER.warning("安全过滤已启用但未生成任何护栏配置。")
            return None

        return {"guardrails": guardrails}

    def resolve_config_files(self) -> dict[str, str | None]:
        """解析所有引用的配置文件绝对路径，供 GUI 展示。

        返回: { 文件名: 绝对路径或None(文件不存在) }
        """
        files: dict[str, str | None] = {}

        cf = self.settings.content_filter
        files["security.yaml"] = str(
            (self.config_dir / "security.yaml").resolve()
        )
        files[cf.blocked_words_file] = self._resolve_file(cf.blocked_words_file)
        files[cf.regex_patterns_file] = self._resolve_file(cf.regex_patterns_file)

        return files

    # ----------------------------------------------------------------
    # 内部：内容过滤护栏
    # ----------------------------------------------------------------

    def _build_content_filter_guardrail(self) -> dict[str, Any] | None:
        cf = self.settings.content_filter
        if not cf.enabled:
            return None

        # 收集所有 patterns（PII 脱敏 + regex_patterns.yaml + 内联 patterns）
        all_patterns = self._collect_all_patterns(cf)
        blocked_words = self._collect_blocked_words(cf)

        litellm_params: dict[str, Any] = {
            "guardrail": "litellm_content_filter",
            "mode": cf.mode,
            "default_on": True,
            "severity_threshold": cf.severity_threshold,
        }

        if blocked_words:
            litellm_params["blocked_words"] = blocked_words

        if all_patterns:
            litellm_params["patterns"] = all_patterns

        LOGGER.info(
            "内容过滤护栏已配置: mode=%s, blocked_words=%d条, patterns=%d条",
            cf.mode,
            len(blocked_words),
            len(all_patterns),
        )

        return {
            "guardrail_name": "local-content-filter",
            "litellm_params": litellm_params,
        }

    def _collect_all_patterns(self, cf: ContentFilterSettings) -> list[dict[str, str]]:
        """收集 LiteLLM 支持的正则规则。

        LiteLLM ContentFilterPattern 要求:
        - 必须提供 pattern_type=regex
        - action 仅支持 BLOCK / MASK
        """
        patterns: list[dict[str, str]] = []

        # PII 脱敏正则
        if self.settings.pii_masking.enabled:
            for p in self.settings.pii_masking.patterns:
                patterns.append(
                    {
                        "pattern": p.pattern,
                        "name": p.pattern_name,
                        "pattern_type": "regex",
                        "action": p.action,
                    }
                )
            LOGGER.info("PII 脱敏已启用: %d 条规则", len(self.settings.pii_masking.patterns))

        # regex_patterns.yaml 中的自定义正则
        regex_file_path = self._resolve_file(cf.regex_patterns_file)
        if regex_file_path is not None:
            try:
                raw = yaml.safe_load(Path(regex_file_path).read_text(encoding="utf-8")) or {}
                skipped_log_patterns = 0
                for p in raw.get("patterns", []):
                    if isinstance(p, dict):
                        action = str(p.get("action", "BLOCK")).upper()
                        if action not in {"BLOCK", "MASK"}:
                            skipped_log_patterns += 1
                            continue
                        pattern_type = str(p.get("pattern_type", "regex"))
                        entry = {
                            "pattern": str(p.get("pattern", "")),
                            "pattern_type": pattern_type,
                            "action": action,
                        }
                        if pattern_type == "prebuilt":
                            entry["pattern_name"] = str(p.get("pattern_name", ""))
                        else:
                            entry["name"] = str(p.get("pattern_name", ""))
                        patterns.append(
                            entry
                        )
                LOGGER.info(
                    "已加载正则文件 %s: 生效 %d 条，跳过不受支持的 LOG 规则 %d 条",
                    regex_file_path,
                    len(patterns),
                    skipped_log_patterns,
                )
            except Exception:
                LOGGER.exception("加载正则文件 %s 失败。", regex_file_path)

        # 内联 patterns
        for p in cf.patterns:
            if isinstance(p, dict):
                action = str(p.get("action", "BLOCK")).upper()
                if action not in {"BLOCK", "MASK"}:
                    continue
                pattern_type = str(p.get("pattern_type", "regex"))
                entry = {
                    "pattern": str(p.get("pattern", "")),
                    "pattern_type": pattern_type,
                    "action": action,
                }
                if pattern_type == "prebuilt":
                    entry["pattern_name"] = str(p.get("pattern_name", ""))
                else:
                    entry["name"] = str(
                        p.get("name", p.get("pattern_name", ""))
                    )
                patterns.append(
                    entry
                )

        return patterns

    def _collect_blocked_words(self, cf: ContentFilterSettings) -> list[dict[str, str]]:
        """收集 LiteLLM 支持的关键词规则。

        LiteLLM ContentFilterAction 仅支持 BLOCK / MASK。
        配置文件中的 LOG 规则保留在源文件中，但不会注入 LiteLLM runtime。
        """
        blocked_words_path = self._resolve_file(cf.blocked_words_file)
        if blocked_words_path is None:
            LOGGER.warning(
                "关键词黑名单文件 %s 不存在，内容过滤将仅使用正则规则。",
                cf.blocked_words_file,
            )
            return []

        try:
            raw = yaml.safe_load(Path(blocked_words_path).read_text(encoding="utf-8")) or {}
        except Exception:
            LOGGER.exception("加载关键词黑名单文件 %s 失败。", blocked_words_path)
            return []

        blocked_words: list[dict[str, str]] = []
        skipped_log_words = 0
        for item in raw.get("blocked_words", []):
            if not isinstance(item, dict):
                continue
            action = str(item.get("action", "BLOCK")).upper()
            if action not in {"BLOCK", "MASK"}:
                skipped_log_words += 1
                continue
            blocked_words.append(
                {
                    "keyword": str(item.get("keyword", "")),
                    "action": action,
                    "description": str(item.get("description", "")),
                }
            )

        LOGGER.info(
            "已加载关键词文件 %s: 生效 %d 条，跳过不受支持的 LOG 规则 %d 条",
            blocked_words_path,
            len(blocked_words),
            skipped_log_words,
        )
        return blocked_words

    # ----------------------------------------------------------------
    # 内部：注入检测护栏（第二期）
    # ----------------------------------------------------------------

    def _build_injection_detection_guardrail(self) -> dict[str, Any] | None:
        inj = self.settings.injection_detection
        if not inj.enabled:
            return None

        # 当前 LiteLLM 版本对 prompt injection 的原生能力并非通过与
        # content filter 相同的 v2 guardrail initializer 暴露；为避免启用后
        # 再次造成启动失败，这里先安全跳过，待确认官方可稳定配置路径后再接入。
        LOGGER.warning(
            "injection_detection.enabled=true，但当前版本未接入可稳定的 LiteLLM v2 guardrail 初始化路径；已跳过注入检测配置生成。"
        )
        return None

    # ----------------------------------------------------------------
    # 内部：输出审核护栏（第二期）
    # ----------------------------------------------------------------

    def _build_output_moderation_guardrail(self) -> dict[str, Any] | None:
        om = self.settings.output_moderation
        if not om.enabled:
            return None

        blocked_words_path = self._resolve_file(om.blocked_words_file)

        litellm_params: dict[str, Any] = {
            "guardrail": "litellm_content_filter",
            "mode": "post_call",
            "default_on": True,
        }

        if blocked_words_path is not None:
            litellm_params["blocked_words_file"] = blocked_words_path

        LOGGER.info("输出内容审核已启用: blocked_words=%s",
                     blocked_words_path or "未找到")

        return {
            "guardrail_name": "local-output-moderation",
            "litellm_params": litellm_params,
        }

    # ----------------------------------------------------------------
    # 辅助方法
    # ----------------------------------------------------------------

    def _resolve_file(self, relative_path: str) -> str | None:
        """解析配置文件路径。用户目录优先，默认目录回退。

        返回绝对路径字符串，或 None（文件不存在）。
        """
        result = SecuritySettings.resolve_config_file(relative_path, self.config_dir)
        if result is not None:
            return str(result)
        return None
