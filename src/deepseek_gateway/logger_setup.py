from __future__ import annotations

import logging
from pathlib import Path

from .runtime_paths import 日志目录


LOG_FILE_NAME = "srw_gateway.log"


def 日志文件路径() -> Path:
    return 日志目录() / LOG_FILE_NAME


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(日志文件路径(), encoding="utf-8"),
        ],
        force=True,
    )