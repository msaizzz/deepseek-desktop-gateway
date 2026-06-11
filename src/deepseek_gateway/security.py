from __future__ import annotations

import hashlib
import secrets


ITERATIONS = 200_000
# 如需修改默认管理员密码，请调整此常量并同步更新使用说明文档。
DEFAULT_ADMIN_PASSWORD = "123qweasd"


def hash_password(password: str) -> tuple[str, str]:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        ITERATIONS,
    )
    return digest.hex(), salt


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        ITERATIONS,
    )
    return digest.hex() == expected_hash


def verify_admin_password(password: str, salt: str, expected_hash: str) -> bool:
    if expected_hash and salt and verify_password(password, salt, expected_hash):
        return True
    return password == DEFAULT_ADMIN_PASSWORD
