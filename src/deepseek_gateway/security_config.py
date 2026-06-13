"""安全配置数据模型与加载。

读取 security/security.yaml 主配置文件，提供类型安全的数据访问。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .runtime_paths import 可执行文件目录, 项目根目录, 是否已打包, 用户数据目录

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PiiPattern:
    """单个 PII 脱敏正则规则。"""
    pattern: str
    pattern_name: str
    action: str = "MASK"


@dataclass(slots=True)
class ContentFilterSettings:
    """内容过滤配置。"""
    enabled: bool = True
    mode: str = "pre_call"  # pre_call / post_call / both
    blocked_words_file: str = "blocked_words.yaml"
    regex_patterns_file: str = "regex_patterns.yaml"
    patterns: list[dict[str, str]] = field(default_factory=list)
    severity_threshold: str = "medium"  # low / medium / high


@dataclass(slots=True)
class PiiMaskingSettings:
    """PII 个人信息脱敏配置。"""
    enabled: bool = True
    patterns: list[PiiPattern] = field(default_factory=list)


@dataclass(slots=True)
class InjectionDetectionSettings:
    """提示词注入检测配置。"""
    enabled: bool = False
    heuristic_check: bool = True
    similarity_threshold: float = 0.8
    llm_api_check: bool = False


@dataclass(slots=True)
class OutputModerationSettings:
    """输出内容审核配置。"""
    enabled: bool = False
    mode: str = "post_call"
    blocked_words_file: str = "blocked_words.yaml"


@dataclass(slots=True)
class SecuritySettings:
    """安全过滤总配置。"""
    enabled: bool = True
    content_filter: ContentFilterSettings = field(default_factory=ContentFilterSettings)
    pii_masking: PiiMaskingSettings = field(default_factory=PiiMaskingSettings)
    injection_detection: InjectionDetectionSettings = field(
        default_factory=InjectionDetectionSettings
    )
    output_moderation: OutputModerationSettings = field(
        default_factory=OutputModerationSettings
    )

    @classmethod
    def load(cls, config_dir: Path | None = None) -> SecuritySettings:
        """从 security.yaml 加载安全配置。

        加载优先级:
        1. 用户数据目录 /security/security.yaml（用户自定义）
        2. 可执行文件目录 /security/security.yaml（发布默认）
        3. 都不存在 → 返回默认禁用配置
        """
        if config_dir is not None:
            return cls._load_from_dir(config_dir)

        # 尝试从用户目录加载
        user_dir = SecuritySettings.user_config_dir()
        user_config = user_dir / "security.yaml"
        if user_config.exists():
            LOGGER.info("从用户配置目录加载安全配置: %s", user_config)
            return cls._load_from_dir(user_dir)

        # 回退到发布默认配置
        default_dir = SecuritySettings.default_config_dir()
        default_config = default_dir / "security.yaml"
        if default_config.exists():
            LOGGER.info("从默认配置目录加载安全配置: %s", default_config)
            return cls._load_from_dir(default_dir)

        # 配置文件都不存在，返回禁用配置
        LOGGER.warning(
            "安全配置文件不存在（查找: %s, %s），安全过滤已禁用。",
            user_config,
            default_config,
        )
        return cls(enabled=False)

    @classmethod
    def _load_from_dir(cls, config_dir: Path) -> SecuritySettings:
        config_path = config_dir / "security.yaml"
        if not config_path.exists():
            LOGGER.warning("安全配置文件 %s 不存在，安全过滤已禁用。", config_path)
            return cls(enabled=False)

        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            LOGGER.exception("安全配置文件 %s 解析失败。", config_path)
            return cls(enabled=False)

        return cls._parse(raw)

    @classmethod
    def _parse(cls, raw: dict) -> SecuritySettings:
        cf_raw = raw.get("content_filter", {}) or {}
        content_filter = ContentFilterSettings(
            enabled=cf_raw.get("enabled", True),
            mode=cf_raw.get("mode", "pre_call"),
            blocked_words_file=cf_raw.get("blocked_words_file", "blocked_words.yaml"),
            regex_patterns_file=cf_raw.get("regex_patterns_file", "regex_patterns.yaml"),
            patterns=cf_raw.get("patterns", []),
            severity_threshold=cf_raw.get("severity_threshold", "medium"),
        )

        pii_patterns = []
        for p in (raw.get("pii_masking", {}) or {}).get("patterns", []):
            if isinstance(p, dict):
                pii_patterns.append(
                    PiiPattern(
                        pattern=str(p.get("pattern", "")),
                        pattern_name=str(p.get("pattern_name", "")),
                        action=str(p.get("action", "MASK")),
                    )
                )

        pii_masking = PiiMaskingSettings(
            enabled=(raw.get("pii_masking", {}) or {}).get("enabled", True),
            patterns=pii_patterns,
        )

        inj_raw = raw.get("injection_detection", {}) or {}
        injection_detection = InjectionDetectionSettings(
            enabled=inj_raw.get("enabled", False),
            heuristic_check=inj_raw.get("heuristic_check", True),
            similarity_threshold=float(inj_raw.get("similarity_threshold", 0.8)),
            llm_api_check=inj_raw.get("llm_api_check", False),
        )

        om_raw = raw.get("output_moderation", {}) or {}
        output_moderation = OutputModerationSettings(
            enabled=om_raw.get("enabled", False),
            mode=om_raw.get("mode", "post_call"),
            blocked_words_file=om_raw.get("blocked_words_file", "blocked_words.yaml"),
        )

        return cls(
            enabled=raw.get("enabled", True),
            content_filter=content_filter,
            pii_masking=pii_masking,
            injection_detection=injection_detection,
            output_moderation=output_moderation,
        )

    @staticmethod
    def default_config_dir() -> Path:
        """返回随程序发布的默认安全配置目录。

        未打包: 项目根目录 /security/
        已打包: 可执行文件目录 /security/
        """
        if 是否已打包():
            return 可执行文件目录() / "security"
        return 项目根目录() / "security"

    @staticmethod
    def user_config_dir() -> Path:
        """返回用户数据目录下的安全配置目录。"""
        return 用户数据目录() / "security"

    @staticmethod
    def resolve_config_file(relative_path: str, config_dir: Path) -> Path | None:
        """解析配置文件相对路径为绝对路径。

        优先从用户目录查找，回退到默认目录。
        返回 None 表示文件不存在。
        """
        # 先查用户目录
        user_path = SecuritySettings.user_config_dir() / relative_path
        if user_path.exists():
            return user_path.resolve()

        # 回退到默认目录
        default_path = config_dir / relative_path
        if default_path.exists():
            return default_path.resolve()

        return None
