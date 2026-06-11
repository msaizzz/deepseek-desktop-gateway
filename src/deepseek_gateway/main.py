from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.deepseek_gateway.config_manager import ConfigManager
from src.deepseek_gateway.database import UsageDatabase
from src.deepseek_gateway.gateway_service import GatewayService
from src.deepseek_gateway.gui import build_application
from src.deepseek_gateway.logger_setup import configure_logging


def main() -> int:
    configure_logging()
    config_manager = ConfigManager()
    database = UsageDatabase()
    gateway_service = GatewayService(config_manager, database)
    app = build_application(config_manager, database, gateway_service)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
