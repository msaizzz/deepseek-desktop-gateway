from __future__ import annotations

import sys
import winreg
from pathlib import Path

from .runtime_paths import 是否已打包


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "SRWDeepSeekDesktopGateway"


def _command() -> str:
    executable = Path(sys.executable)
    if 是否已打包():
        return f'"{executable}"'
    if executable.name.lower() == "python.exe":
        return f'"{executable}" -m src.deepseek_gateway.main'
    return f'"{executable}"'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(value)
    except FileNotFoundError:
        return False


def set_enabled(enabled: bool) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
