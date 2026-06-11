from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook

from .runtime_paths import 用户数据目录


@dataclass(slots=True)
class UsageRecord:
    timestamp: str
    device_id: str
    model: str
    upstream_model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    status: str
    error: str
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass(slots=True)
class MonthlySpendRecord:
    month: str
    total_cost: float
    success_count: int
    failure_count: int


@dataclass(slots=True)
class MonthlyUsageSnapshot:
    total_requests: int
    success_count: int
    failure_count: int
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    total_tokens: int
    total_cost: float
    last_request_time: str


class UsageDatabase:
    def __init__(self, root_dir: Path | None = None) -> None:
        self.root_dir = root_dir or 用户数据目录()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root_dir / "usage.db"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    upstream_model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            existing_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(usage_logs)").fetchall()
            }
            if "cache_read_input_tokens" not in existing_columns:
                connection.execute(
                    "ALTER TABLE usage_logs ADD COLUMN cache_read_input_tokens INTEGER NOT NULL DEFAULT 0"
                )
            if "cache_creation_input_tokens" not in existing_columns:
                connection.execute(
                    "ALTER TABLE usage_logs ADD COLUMN cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0"
                )

    def log_request(
        self,
        *,
        device_id: str,
        model: str,
        upstream_model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        cost_usd: float,
        status: str,
        error: str = "",
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO usage_logs (
                    timestamp, device_id, model, upstream_model, input_tokens,
                    output_tokens, cache_read_input_tokens, cache_creation_input_tokens,
                    cost_usd, status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    device_id,
                    model,
                    upstream_model,
                    input_tokens,
                    output_tokens,
                    cache_read_input_tokens,
                    cache_creation_input_tokens,
                    cost_usd,
                    status,
                    error,
                ),
            )

    def monthly_spend(self, device_id: str, month: str) -> float:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(cost_usd), 0)
                FROM usage_logs
                WHERE device_id = ? AND substr(timestamp, 1, 7) = ? AND status = 'success'
                """,
                (device_id, month),
            ).fetchone()
        return float(row[0] or 0.0)

    def monthly_records(self, device_id: str, month: str) -> list[UsageRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT timestamp, device_id, model, upstream_model, input_tokens,
                       output_tokens, cache_read_input_tokens, cache_creation_input_tokens,
                       cost_usd, status, error
                FROM usage_logs
                WHERE device_id = ? AND substr(timestamp, 1, 7) = ?
                ORDER BY timestamp DESC
                """,
                (device_id, month),
            ).fetchall()
        return [UsageRecord(**dict(row)) for row in rows]

    def recent_records(self, limit: int = 50) -> list[UsageRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT timestamp, device_id, model, upstream_model, input_tokens,
                       output_tokens, cache_read_input_tokens, cache_creation_input_tokens,
                       cost_usd, status, error
                FROM usage_logs
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [UsageRecord(**dict(row)) for row in rows]

    def available_months(self, device_id: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT substr(timestamp, 1, 7) AS month
                FROM usage_logs
                WHERE device_id = ?
                ORDER BY month DESC
                """,
                (device_id,),
            ).fetchall()
        return [str(row["month"]) for row in rows]

    def monthly_summaries(self, device_id: str) -> list[MonthlySpendRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    substr(timestamp, 1, 7) AS month,
                    COALESCE(SUM(CASE WHEN status = 'success' THEN cost_usd ELSE 0 END), 0) AS total_cost,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
                    SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END) AS failure_count
                FROM usage_logs
                WHERE device_id = ?
                GROUP BY substr(timestamp, 1, 7)
                ORDER BY month DESC
                """,
                (device_id,),
            ).fetchall()
        return [MonthlySpendRecord(**dict(row)) for row in rows]

    def monthly_usage_snapshot(self, device_id: str, month: str) -> MonthlyUsageSnapshot:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total_requests,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
                    SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END) AS failure_count,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_input_tokens,
                    COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_creation_input_tokens,
                    COALESCE(SUM(input_tokens + output_tokens), 0) AS total_tokens,
                    COALESCE(SUM(CASE WHEN status = 'success' THEN cost_usd ELSE 0 END), 0) AS total_cost,
                    COALESCE(MAX(timestamp), '') AS last_request_time
                FROM usage_logs
                WHERE device_id = ? AND substr(timestamp, 1, 7) = ?
                """,
                (device_id, month),
            ).fetchone()
        return MonthlyUsageSnapshot(**dict(row))

    def monthly_model_spend_breakdown(self, device_id: str, month: str) -> dict[str, float]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT model, COALESCE(SUM(cost_usd), 0) AS total_cost
                FROM usage_logs
                WHERE device_id = ? AND substr(timestamp, 1, 7) = ? AND status = 'success'
                GROUP BY model
                ORDER BY total_cost DESC, model ASC
                """,
                (device_id, month),
            ).fetchall()
        return {str(row["model"]): float(row["total_cost"] or 0.0) for row in rows}

    def reset_usage_logs(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM usage_logs")
            connection.execute("DELETE FROM sqlite_sequence WHERE name = 'usage_logs'")

    def export_month_csv(self, device_id: str, month: str, target_path: Path) -> Path:
        records = self.monthly_records(device_id, month)
        with target_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "时间戳",
                    "设备编号",
                    "网关模型",
                    "上游模型",
                    "输入Token",
                    "输出Token",
                    "费用_人民币",
                    "状态",
                    "错误信息",
                ]
            )
            for record in records:
                writer.writerow(
                    [
                        record.timestamp,
                        record.device_id,
                        record.model,
                        record.upstream_model,
                        record.input_tokens,
                        record.output_tokens,
                        record.cost_usd,
                        "成功" if record.status == "success" else "失败",
                        record.error,
                    ]
                )
        return target_path

    def export_month_xlsx(self, device_id: str, month: str, target_path: Path) -> Path:
        records = self.monthly_records(device_id, month)
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "用量明细"
        sheet.append(
            [
                "时间戳",
                "设备编号",
                "网关模型",
                "上游模型",
                "输入Token",
                "输出Token",
                "费用_人民币",
                "状态",
                "错误信息",
            ]
        )
        for record in records:
            sheet.append(
                [
                    record.timestamp,
                    record.device_id,
                    record.model,
                    record.upstream_model,
                    record.input_tokens,
                    record.output_tokens,
                    record.cost_usd,
                    "成功" if record.status == "success" else "失败",
                    record.error,
                ]
            )
        workbook.save(target_path)
        return target_path
