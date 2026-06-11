from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


应用目录名 = "SRWDeepSeekDesktopGatewayData"
旧应用目录名 = "DeepSeekDesktopGateway"


def 项目根目录() -> Path:
    return Path(__file__).resolve().parents[2]


def 是否已打包() -> bool:
    return bool(getattr(sys, "frozen", False))


def 可执行文件目录() -> Path:
    if 是否已打包():
        return Path(sys.executable).resolve().parent
    return 项目根目录()


def _当前文档数据目录() -> Path:
    文档目录 = Path.home() / "Documents"
    基础目录 = 文档目录 if 文档目录.exists() else Path.home()
    return 基础目录 / 应用目录名


def _旧数据目录列表() -> list[Path]:
    目录列表: list[Path] = []
    本地应用数据目录 = os.environ.get("LOCALAPPDATA")
    if 本地应用数据目录:
        目录列表.append(Path(本地应用数据目录) / 旧应用目录名)
    目录列表.append(Path.home() / ".deepseek-desktop-gateway")
    目录列表.append(_当前文档数据目录())
    return 目录列表


def _迁移旧数据(目标目录: Path) -> None:
    for 旧目录 in _旧数据目录列表():
        if not 旧目录.exists():
            continue
        目标目录.mkdir(parents=True, exist_ok=True)
        for 子项 in 旧目录.iterdir():
            目标路径 = 目标目录 / 子项.name
            if 目标路径.exists():
                continue
            if 子项.is_dir():
                shutil.copytree(子项, 目标路径)
            else:
                shutil.copy2(子项, 目标路径)
        return


def 用户数据目录() -> Path:
    自定义目录 = os.environ.get("SRW_GATEWAY_DATA_DIR")
    if 自定义目录:
        路径 = Path(自定义目录)
    elif 是否已打包():
        路径 = 可执行文件目录()
    else:
        路径 = _当前文档数据目录()
    if not 路径.exists():
        _迁移旧数据(路径)
    路径.mkdir(parents=True, exist_ok=True)
    return 路径


def 日志目录() -> Path:
    路径 = 用户数据目录() / "logs"
    路径.mkdir(parents=True, exist_ok=True)
    return 路径


def 日志文件路径() -> Path:
    return 日志目录() / "srw_gateway.log"


def 报表目录() -> Path:
    路径 = 用户数据目录() / "reports"
    路径.mkdir(parents=True, exist_ok=True)
    return 路径


def 打包输出目录() -> Path:
    路径 = 项目根目录() / "dist" / "release"
    路径.mkdir(parents=True, exist_ok=True)
    return 路径