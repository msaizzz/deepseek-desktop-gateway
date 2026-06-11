from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ctypes import wintypes

from .runtime_paths import 用户数据目录


UPSTREAM_KEY_ENTRY = "upstream-api-key"
SIGNING_SECRET_ENTRY = "config-signing-secret"
LOCAL_SECRETS_FILE = "secrets.json"
LOCAL_SECRETS_VERSION = 2
LOCAL_SECRETS_SCOPE = "windows-current-user"
CRYPTPROTECT_UI_FORBIDDEN = 0x1

LEGACY_MODEL_DEFAULTS = {
    "deepseek-v4-flash": (0.14, 0.28),
    "deepseek-v4-pro": (0.435, 0.87),
}


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _bytes_to_blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _blob_to_bytes(blob: DATA_BLOB) -> bytes:
    if not blob.cbData or not blob.pbData:
        return b""
    return ctypes.string_at(blob.pbData, blob.cbData)


def _protect_for_current_user(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("当前用户绑定密钥存储仅支持 Windows。")

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    input_blob, input_buffer = _bytes_to_blob(data)
    output_blob = DATA_BLOB()
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "SRW DeepSeek Desktop Gateway secrets",
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError()
    try:
        return _blob_to_bytes(output_blob)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))


def _unprotect_for_current_user(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("当前用户绑定密钥存储仅支持 Windows。")

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    input_blob, input_buffer = _bytes_to_blob(data)
    output_blob = DATA_BLOB()
    description = wintypes.LPWSTR()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        ctypes.byref(description),
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError()
    try:
        return _blob_to_bytes(output_blob)
    finally:
        if description:
            kernel32.LocalFree(ctypes.cast(description, wintypes.HLOCAL))
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))


@dataclass(slots=True)
class ModelPricing:
    upstream_model: str
    input_per_million_usd: float
    output_per_million_usd: float
    cache_read_input_per_million: float = 0.0


@dataclass(slots=True)
class AppConfig:
    device_id: str
    device_name: str
    host: str = "127.0.0.1"
    port: int = 8765
    monthly_budget_usd: float = 50.0
    start_with_windows: bool = False
    upstream_base_url: str = "https://api.deepseek.com"
    admin_password_hash: str = ""
    admin_password_salt: str = ""
    models: dict[str, ModelPricing] = field(default_factory=dict)

    @classmethod
    def default(cls) -> "AppConfig":
        machine_name = os.environ.get("COMPUTERNAME", "unknown-device")
        return cls(
            device_id=machine_name,
            device_name=machine_name,
            models={
                "deepseek-v4-flash": ModelPricing(
                    upstream_model="deepseek-v4-flash",
                    input_per_million_usd=1.0,
                    output_per_million_usd=2.0,
                    cache_read_input_per_million=0.02,
                ),
                "deepseek-v4-pro": ModelPricing(
                    upstream_model="deepseek-v4-pro",
                    input_per_million_usd=3.0,
                    output_per_million_usd=6.0,
                    cache_read_input_per_million=0.025,
                ),
            },
        )


class ConfigManager:
    def __init__(self, root_dir: Path | None = None) -> None:
        self.root_dir = root_dir or 用户数据目录()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.root_dir / "config.json"
        self.local_secrets_path = self.root_dir / LOCAL_SECRETS_FILE

    def load(self) -> AppConfig:
        if not self.config_path.exists():
            config = AppConfig.default()
            self.save(config)
            return config

        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        signature = payload.pop("signature", "")
        try:
            self._verify_signature(payload, signature)
        except ValueError:
            if self._load_local_secret(SIGNING_SECRET_ENTRY):
                raise
        models = {
            name: ModelPricing(**details)
            for name, details in payload.get("models", {}).items()
        }
        changed = self._normalize_model_pricing(models)
        payload["models"] = models
        config = AppConfig(**payload)
        if changed:
            self.save(config)
        return config

    def save(self, config: AppConfig) -> None:
        payload = asdict(config)
        payload["models"] = {
            name: asdict(details) for name, details in config.models.items()
        }
        payload["signature"] = self._sign_payload(payload)
        self.config_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )

    def get_upstream_api_key(self) -> str:
        return self._load_local_secret(UPSTREAM_KEY_ENTRY)

    def set_upstream_api_key(self, api_key: str) -> None:
        self._save_local_secret(UPSTREAM_KEY_ENTRY, api_key)

    def _get_signing_secret(self) -> str:
        secret = self._load_local_secret(SIGNING_SECRET_ENTRY)
        if secret:
            return secret
        secret = secrets.token_hex(32)
        self._save_local_secret(SIGNING_SECRET_ENTRY, secret)
        return secret

    def _load_local_secrets(self) -> dict[str, str]:
        if not self.local_secrets_path.exists():
            return {}
        try:
            payload = json.loads(self.local_secrets_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}

        if payload.get("version") != LOCAL_SECRETS_VERSION or payload.get("scope") != LOCAL_SECRETS_SCOPE:
            return {}

        encrypted_payload = payload.get("payload")
        if not isinstance(encrypted_payload, str):
            return {}

        try:
            plaintext = _unprotect_for_current_user(base64.b64decode(encrypted_payload))
            decoded_payload = json.loads(plaintext.decode("utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

        if not isinstance(decoded_payload, dict):
            return {}
        return {str(key): str(value) for key, value in decoded_payload.items()}

    def _load_local_secret(self, key: str) -> str:
        return self._load_local_secrets().get(key, "")

    def _peek_local_secret(self, key: str) -> str:
        return self._load_local_secrets().get(key, "")

    def _save_local_secret(self, key: str, value: str) -> None:
        secrets_payload = self._load_local_secrets()
        if value:
            secrets_payload[key] = value
        else:
            secrets_payload.pop(key, None)

        serialized_payload = json.dumps(
            secrets_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        encrypted_payload = _protect_for_current_user(serialized_payload)
        self.local_secrets_path.write_text(
            json.dumps(
                {
                    "version": LOCAL_SECRETS_VERSION,
                    "scope": LOCAL_SECRETS_SCOPE,
                    "payload": base64.b64encode(encrypted_payload).decode("ascii"),
                },
                indent=2,
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )

    def _sign_payload(self, payload: dict[str, Any], *, allow_create: bool = True) -> str:
        if allow_create:
            signing_secret = self._get_signing_secret()
        else:
            signing_secret = self._peek_local_secret(SIGNING_SECRET_ENTRY)
            if not signing_secret:
                raise ValueError("缺少配置签名密钥。")

        secret = signing_secret.encode("utf-8")
        normalized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        return hmac.new(secret, normalized, hashlib.sha256).hexdigest()

    def _verify_signature(self, payload: dict[str, Any], signature: str) -> None:
        expected = self._sign_payload(payload, allow_create=False)
        if not hmac.compare_digest(expected, signature):
            raise ValueError("配置完整性校验失败。")

    def _normalize_model_pricing(self, models: dict[str, ModelPricing]) -> bool:
        default_models = AppConfig.default().models
        changed = False
        for model_name, default_pricing in default_models.items():
            if model_name not in models:
                models[model_name] = default_pricing
                changed = True
                continue

            pricing = models[model_name]
            legacy_defaults = LEGACY_MODEL_DEFAULTS.get(model_name)
            if legacy_defaults and (
                pricing.input_per_million_usd == legacy_defaults[0]
                and pricing.output_per_million_usd == legacy_defaults[1]
            ):
                pricing.input_per_million_usd = default_pricing.input_per_million_usd
                pricing.output_per_million_usd = default_pricing.output_per_million_usd
                changed = True

            if pricing.cache_read_input_per_million == 0.0:
                pricing.cache_read_input_per_million = default_pricing.cache_read_input_per_million
                changed = True

        return changed
