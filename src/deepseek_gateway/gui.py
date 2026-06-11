from __future__ import annotations

import logging
import shutil
import tempfile
import zipfile
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QDoubleValidator, QIntValidator
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .autostart import is_enabled as autostart_enabled
from .autostart import set_enabled as set_autostart_enabled
from .budgeting import month_key
from .config_manager import ConfigManager
from .database import UsageDatabase
from .gateway_service import GatewayService
from .logger_setup import 日志文件路径
from .runtime_paths import 可执行文件目录, 报表目录, 用户数据目录, 项目根目录
from .security import verify_admin_password


APP_VERSION = "0.1.0"
APP_DISPLAY_NAME = "SRW DeepSeek 本地桌面网关"
LOGGER = logging.getLogger(__name__)


def _format_token_millions(tokens: int) -> str:
    return f"{tokens / 1_000_000:.2f} M"


def _format_percentage(value: float) -> str:
    return f"{value * 100:.1f}%"


def _model_display_name(model_name: str) -> str:
    if model_name == "deepseek-v4-flash":
        return "Flash"
    if model_name == "deepseek-v4-pro":
        return "Pro"
    return model_name


def _format_model_spend_breakdown(model_spend: dict[str, float], ordered_models: list[str]) -> str:
    breakdown_parts: list[str] = []
    for model_name in ordered_models:
        breakdown_parts.append(f"{_model_display_name(model_name)} RMB {model_spend.get(model_name, 0.0):.4f}")

    remaining_models = sorted(model_name for model_name in model_spend if model_name not in ordered_models)
    for model_name in remaining_models:
        breakdown_parts.append(f"{_model_display_name(model_name)} RMB {model_spend[model_name]:.4f}")

    return " | ".join(breakdown_parts)


def _cache_prompt_ratios(input_tokens: int, cache_read_input_tokens: int, cache_creation_input_tokens: int) -> tuple[float, float]:
    prompt_volume = input_tokens
    if cache_read_input_tokens + cache_creation_input_tokens > input_tokens:
        prompt_volume = input_tokens + cache_read_input_tokens + cache_creation_input_tokens

    if prompt_volume <= 0:
        return 0.0, 0.0

    cache_hit_ratio = min(max(cache_read_input_tokens / prompt_volume, 0.0), 1.0)
    cache_miss_ratio = min(max(1.0 - cache_hit_ratio, 0.0), 1.0)
    return cache_hit_ratio, cache_miss_ratio


class MainWindow(QMainWindow):
    def __init__(
        self,
        config_manager: ConfigManager,
        database: UsageDatabase,
        gateway_service: GatewayService,
    ) -> None:
        super().__init__()
        self.config_manager = config_manager
        self.database = database
        self.gateway_service = gateway_service
        self.setWindowTitle(APP_DISPLAY_NAME)
        self.resize(760, 520)

        self.config = self.config_manager.load()
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setToolTip(APP_DISPLAY_NAME)
        self._is_exiting = False
        self._tray_notice_shown = False
        self._settings_unlocked = False
        self._tab_switching = False
        self._previous_tab_index = 0
        self._session_admin_password = ""
        self._setup_menu_bar()
        self._setup_tray_icon()

        root = QWidget()
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_overview_tab(), "概览")
        self.tabs.addTab(self._build_settings_tab(), "设置")
        self.tabs.addTab(self._build_reports_tab(), "报表")
        self.tabs.addTab(self._build_history_tab(), "历史")
        self.tabs.addTab(self._build_operations_tab(), "运维")
        self.tabs.currentChanged.connect(self._handle_tab_changed)

        layout = QVBoxLayout(root)
        layout.addWidget(self.tabs)
        self.setCentralWidget(root)
        self.statusBar().showMessage("准备就绪")
        self.refresh_overview()

    def _setup_menu_bar(self) -> None:
        file_menu = self.menuBar().addMenu("文件")
        show_requests_action = QAction("最近请求", self)
        show_requests_action.triggered.connect(self._show_recent_requests)
        export_csv_action = QAction("导出本月 CSV", self)
        export_csv_action.triggered.connect(lambda: self._export("csv"))
        export_xlsx_action = QAction("导出本月 XLSX", self)
        export_xlsx_action.triggered.connect(lambda: self._export("xlsx"))
        exit_action = QAction("退出程序", self)
        exit_action.triggered.connect(self._exit_application)
        for action in (show_requests_action, export_csv_action, export_xlsx_action, exit_action):
            file_menu.addAction(action)

        view_menu = self.menuBar().addMenu("查看")
        refresh_action = QAction("刷新状态", self)
        refresh_action.triggered.connect(self.refresh_overview)
        restore_action = QAction("显示主界面", self)
        restore_action.triggered.connect(self._show_main_window)
        for action in (refresh_action, restore_action):
            view_menu.addAction(action)

        help_menu = self.menuBar().addMenu("帮助")
        docs_action = QAction("打开使用说明", self)
        docs_action.triggered.connect(lambda: self._open_doc("使用说明.md"))
        deploy_action = QAction("打开部署说明", self)
        deploy_action.triggered.connect(lambda: self._open_doc("部署说明.md"))
        vscode_action = QAction("打开 VS Code 接入说明", self)
        vscode_action.triggered.connect(lambda: self._open_doc("VS Code接入说明.md"))
        about_action = QAction("关于", self)
        about_action.triggered.connect(self._show_about)
        for action in (docs_action, deploy_action, vscode_action, about_action):
            help_menu.addAction(action)

    def _setup_tray_icon(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        tray_menu = QMenu(self)
        show_action = QAction("显示主界面", self)
        show_action.triggered.connect(self._show_main_window)
        start_action = QAction("启动网关", self)
        start_action.triggered.connect(self._start_gateway)
        stop_action = QAction("停止网关", self)
        stop_action.triggered.connect(self._stop_gateway)
        exit_action = QAction("退出程序", self)
        exit_action.triggered.connect(self._exit_application)

        for action in (show_action, start_action, stop_action, exit_action):
            tray_menu.addAction(action)

        self.tray_icon.setIcon(self.style().standardIcon(QStyle.SP_ComputerIcon))
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._handle_tray_activation)
        self.tray_icon.show()

    def _handle_tray_activation(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.DoubleClick:
            self._show_main_window()

    def _show_main_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _hide_to_tray(self) -> None:
        self.hide()
        if not self._tray_notice_shown and self.tray_icon.isVisible():
            self.tray_icon.showMessage(
                APP_DISPLAY_NAME,
                "程序已最小化到系统托盘，可通过托盘图标重新打开。",
                QSystemTrayIcon.Information,
                3000,
            )
            self._tray_notice_shown = True

    def _exit_application(self) -> None:
        self._is_exiting = True
        self.gateway_service.stop()
        self.tray_icon.hide()
        QApplication.instance().quit()

    def _show_about(self) -> None:
        QMessageBox.information(
            self,
            "关于",
            "\n".join(
                [
                    APP_DISPLAY_NAME,
                    f"版本：{APP_VERSION}",
                    f"数据目录：{用户数据目录()}",
                    f"程序目录：{可执行文件目录()}",
                ]
            ),
        )

    def _handle_tab_changed(self, index: int) -> None:
        if self._tab_switching:
            return
        settings_index = 1
        if index == settings_index and not self._settings_unlocked:
            if not self._request_settings_unlock():
                self._tab_switching = True
                self.tabs.setCurrentIndex(self._previous_tab_index)
                self._tab_switching = False
                return
            self._settings_unlocked = True
        self._previous_tab_index = index

    def _request_settings_unlock(self) -> bool:
        password, accepted = QInputDialog.getText(
            self,
            "管理员验证",
            "请输入管理员密码后进入设置页：",
            QLineEdit.Password,
        )
        if not accepted:
            return False
        if verify_admin_password(password, self.config.admin_password_salt, self.config.admin_password_hash):
            self._session_admin_password = password
            return True
        QMessageBox.warning(self, "认证失败", "管理员密码不正确，无法打开设置页。")
        return False

    def _show_recent_requests(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("最近请求")
        dialog.resize(760, 420)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit(dialog)
        text.setReadOnly(True)

        records = self.database.recent_records(50)
        if not records:
            text.setPlainText("当前还没有请求记录。")
        else:
            lines = []
            for record in records:
                status = "成功" if record.status == "success" else "失败"
                lines.append(
                    f"{record.timestamp} | {record.device_id} | {record.model} -> {record.upstream_model} | "
                    f"输入 {record.input_tokens} | 输出 {record.output_tokens} | 费用 RMB {record.cost_usd:.6f} | {status} | {record.error}"
                )
            text.setPlainText("\n".join(lines))

        layout.addWidget(text)
        dialog.exec()

    def _open_doc(self, file_name: str) -> None:
        docs_root = 可执行文件目录().parent if (可执行文件目录() / "_internal").exists() else 项目根目录() / "docs"
        doc_path = docs_root / file_name
        if not doc_path.exists():
            QMessageBox.warning(self, "文件不存在", f"未找到文档：{doc_path}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(doc_path)))

    def _open_directory(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _build_overview_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(14)

        cards_layout = QGridLayout()
        cards_layout.setHorizontalSpacing(12)
        cards_layout.setVerticalSpacing(12)

        status_card, self.status_value_label, self.status_detail_label = self._create_overview_card("网关状态")
        budget_card, self.budget_value_label, self.budget_detail_label = self._create_overview_card("本月费用（人民币）")
        request_card, self.request_value_label, self.request_detail_label = self._create_overview_card("本月调用")
        token_card, self.token_value_label, self.token_detail_label = self._create_overview_card("本月 Token")
        endpoint_card, self.endpoint_value_label, self.endpoint_detail_label = self._create_overview_card("本地接入地址")
        upstream_card, self.upstream_value_label, self.upstream_detail_label = self._create_overview_card("上游配置")

        cards = [
            status_card,
            budget_card,
            request_card,
            token_card,
            endpoint_card,
            upstream_card,
        ]
        for index, card in enumerate(cards):
            cards_layout.addWidget(card, index // 2, index % 2)

        self.device_label = QLabel()
        self.device_label.setStyleSheet("font-size: 13px; color: #94a3b8;")
        self.last_request_label = QLabel()
        self.last_request_label.setStyleSheet("font-size: 13px; color: #94a3b8;")

        actions = QHBoxLayout()
        start_button = QPushButton("启动网关")
        stop_button = QPushButton("停止网关")
        refresh_button = QPushButton("刷新状态")
        start_button.clicked.connect(self._start_gateway)
        stop_button.clicked.connect(self._stop_gateway)
        refresh_button.clicked.connect(self.refresh_overview)
        actions.addWidget(start_button)
        actions.addWidget(stop_button)
        actions.addWidget(refresh_button)

        layout.addLayout(cards_layout)
        layout.addWidget(self.device_label)
        layout.addWidget(self.last_request_label)
        layout.addLayout(actions)
        layout.addStretch(1)
        return widget

    def _create_overview_card(self, title: str) -> tuple[QFrame, QLabel, QLabel]:
        frame = QFrame(self)
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setObjectName("overviewCard")
        frame.setStyleSheet(
            "QFrame#overviewCard {"
            "background: #262d37;"
            "border: 1px solid #384150;"
            "border-radius: 10px;"
            "padding: 12px;"
            "}"
        )
        layout = QVBoxLayout(frame)
        layout.setSpacing(6)

        title_label = QLabel(title, frame)
        title_label.setStyleSheet("font-size: 12px; color: #9fb0c7; font-weight: 600;")
        value_label = QLabel("-", frame)
        value_label.setStyleSheet("font-size: 20px; color: #f8fafc; font-weight: 700;")
        detail_label = QLabel("-", frame)
        detail_label.setWordWrap(True)
        detail_label.setStyleSheet("font-size: 12px; color: #cbd5e1;")

        layout.addWidget(title_label)
        layout.addWidget(value_label)
        layout.addWidget(detail_label)
        return frame, value_label, detail_label

    def _build_settings_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        form = QFormLayout()
        self.model_price_inputs: dict[str, dict[str, QLineEdit]] = {}

        self.device_name_input = QLineEdit(self.config.device_name)
        self.device_id_input = QLineEdit(self.config.device_id)
        self.host_input = QLineEdit(self.config.host)
        self.port_input = QLineEdit(str(self.config.port))
        self.port_input.setValidator(QIntValidator(1024, 65535, self.port_input))
        self.port_input.setPlaceholderText("例如 8765")
        self.budget_input = QLineEdit(f"{self.config.monthly_budget_usd:.2f}")
        budget_validator = QDoubleValidator(0.0, 100000.0, 2, self.budget_input)
        budget_validator.setNotation(QDoubleValidator.StandardNotation)
        self.budget_input.setValidator(budget_validator)
        self.budget_input.setPlaceholderText("例如 50.00")
        self.base_url_input = QLineEdit(self.config.upstream_base_url)
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.api_key_input.setPlaceholderText("保存后将写入当前运行模式对应的本地安全存储")
        self.startup_checkbox = QCheckBox("开机自动启动")
        self.startup_checkbox.setChecked(autostart_enabled())
        self.admin_password_input = QLineEdit()
        self.admin_password_input.setEchoMode(QLineEdit.Password)
        self.new_password_input = QLineEdit()
        self.new_password_input.setEchoMode(QLineEdit.Password)

        form.addRow("设备名称", self.device_name_input)
        form.addRow("设备编号", self.device_id_input)
        form.addRow("监听地址", self.host_input)
        form.addRow("监听端口", self.port_input)
        form.addRow("月度预算（人民币）", self.budget_input)
        form.addRow("上游基础地址", self.base_url_input)
        form.addRow("上游 API Key", self.api_key_input)
        form.addRow("", self.startup_checkbox)
        form.addRow("当前管理员密码", self.admin_password_input)
        form.addRow("设置新管理员密码", self.new_password_input)

        for model_name, pricing in self.config.models.items():
            cache_input = QLineEdit(f"{pricing.cache_read_input_per_million:.4f}")
            normal_input = QLineEdit(f"{pricing.input_per_million_usd:.4f}")
            output_input = QLineEdit(f"{pricing.output_per_million_usd:.4f}")
            for widget_input in (cache_input, normal_input, output_input):
                validator = QDoubleValidator(0.0, 1000000.0, 6, widget_input)
                validator.setNotation(QDoubleValidator.StandardNotation)
                widget_input.setValidator(validator)
            self.model_price_inputs[model_name] = {
                "cache": cache_input,
                "input": normal_input,
                "output": output_input,
            }
            form.addRow(f"{model_name} 缓存命中输入 / 百万 tokens（人民币）", cache_input)
            form.addRow(f"{model_name} 输入 / 百万 tokens（人民币）", normal_input)
            form.addRow(f"{model_name} 输出 / 百万 tokens（人民币）", output_input)

        tip_label = QLabel("进入设置页需要管理员密码。预算和模型单价统一按人民币填写；LiteLLM 将按缓存命中、输入、输出三档价格参与本地预算与报表记账。")
        tip_label.setWordWrap(True)
        layout.addWidget(tip_label)

        save_button = QPushButton("保存设置")
        save_button.clicked.connect(self._save_settings)
        layout.addLayout(form)
        layout.addWidget(save_button, alignment=Qt.AlignLeft)
        layout.addStretch(1)
        return widget

    def _build_reports_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.report_status = QLabel("可选择月份导出 CSV 或 XLSX 报表。")
        month_row = QHBoxLayout()
        month_row.addWidget(QLabel("选择月份"))
        self.report_month_combo = QComboBox()
        month_row.addWidget(self.report_month_combo)
        csv_button = QPushButton("导出 CSV")
        xlsx_button = QPushButton("导出 XLSX")
        csv_button.clicked.connect(lambda: self._export("csv"))
        xlsx_button.clicked.connect(lambda: self._export("xlsx"))
        layout.addWidget(self.report_status)
        layout.addLayout(month_row)
        layout.addWidget(csv_button, alignment=Qt.AlignLeft)
        layout.addWidget(xlsx_button, alignment=Qt.AlignLeft)
        layout.addStretch(1)
        return widget

    def _build_history_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.history_status = QLabel("历史月度消费记录如下。")
        self.history_text = QPlainTextEdit(widget)
        self.history_text.setReadOnly(True)
        refresh_button = QPushButton("刷新历史记录")
        refresh_button.clicked.connect(self._refresh_history_view)
        layout.addWidget(self.history_status)
        layout.addWidget(self.history_text)
        layout.addWidget(refresh_button, alignment=Qt.AlignLeft)
        return widget

    def _build_operations_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.ops_status = QLabel("可在此查看日志、备份数据、恢复数据，或在保留配置的前提下重置数据库。")
        self.ops_log_text = QPlainTextEdit(widget)
        self.ops_log_text.setReadOnly(True)

        buttons = QHBoxLayout()
        refresh_logs_button = QPushButton("刷新日志")
        refresh_logs_button.clicked.connect(self._refresh_logs_view)
        open_data_button = QPushButton("打开数据目录")
        open_data_button.clicked.connect(lambda: self._open_directory(用户数据目录()))
        open_logs_button = QPushButton("打开日志目录")
        open_logs_button.clicked.connect(lambda: self._open_directory(日志文件路径().parent))
        backup_button = QPushButton("备份数据")
        backup_button.clicked.connect(self._backup_data)
        restore_button = QPushButton("恢复数据")
        restore_button.clicked.connect(self._restore_data)
        reset_database_button = QPushButton("重置数据库")
        reset_database_button.clicked.connect(self._reset_database)

        for button in (
            refresh_logs_button,
            open_data_button,
            open_logs_button,
            backup_button,
            restore_button,
            reset_database_button,
        ):
            buttons.addWidget(button)

        layout.addWidget(self.ops_status)
        layout.addWidget(QLabel(f"当前数据目录：{用户数据目录()}"))
        layout.addLayout(buttons)
        layout.addWidget(self.ops_log_text)
        return widget

    def _selected_report_month(self) -> str:
        month = self.report_month_combo.currentText().strip()
        return month or month_key()

    def _refresh_report_months(self) -> None:
        months = self.database.available_months(self.config.device_id)
        current_month = month_key()
        if current_month not in months:
            months.insert(0, current_month)
        current_selection = self.report_month_combo.currentText() if hasattr(self, "report_month_combo") else current_month
        self.report_month_combo.clear()
        self.report_month_combo.addItems(months)
        index = self.report_month_combo.findText(current_selection)
        self.report_month_combo.setCurrentIndex(index if index >= 0 else 0)

    def _refresh_history_view(self) -> None:
        summaries = self.database.monthly_summaries(self.config.device_id)
        if not summaries:
            self.history_text.setPlainText("当前还没有任何历史消费记录。")
            return
        lines = []
        for summary in summaries:
            lines.append(
                f"{summary.month} | 累计费用 RMB {summary.total_cost:.4f} | 成功请求 {summary.success_count} 次 | 失败请求 {summary.failure_count} 次"
            )
        self.history_text.setPlainText("\n".join(lines))

    def _refresh_logs_view(self) -> None:
        log_path = 日志文件路径()
        if not log_path.exists():
            self.ops_log_text.setPlainText("当前还没有日志文件。")
            return
        content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        self.ops_log_text.setPlainText("\n".join(content[-300:]))

    def _backup_data(self) -> None:
        backup_default = 用户数据目录() / f"srw-gateway-backup-{month_key()}.zip"
        selected_path, _ = QFileDialog.getSaveFileName(
            self,
            "备份数据",
            str(backup_default),
            "ZIP 文件 (*.zip)",
        )
        if not selected_path:
            return
        target = Path(selected_path)
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in 用户数据目录().rglob("*"):
                if item.is_file():
                    archive.write(item, item.relative_to(用户数据目录()))
        LOGGER.info("已完成数据备份: %s", target)
        self.ops_status.setText(f"数据备份完成：{target}")

    def _restore_data(self) -> None:
        selected_path, _ = QFileDialog.getOpenFileName(
            self,
            "恢复数据",
            str(用户数据目录()),
            "ZIP 文件 (*.zip)",
        )
        if not selected_path:
            return
        reply = QMessageBox.question(
            self,
            "确认恢复",
            "恢复数据会覆盖同名配置、数据库和报表文件。是否继续？",
        )
        if reply != QMessageBox.Yes:
            return
        self.gateway_service.stop()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            with zipfile.ZipFile(selected_path, "r") as archive:
                archive.extractall(temp_path)
            for item in temp_path.rglob("*"):
                target = 用户数据目录() / item.relative_to(temp_path)
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
        LOGGER.info("已从备份恢复数据: %s", selected_path)
        self.ops_status.setText(f"数据恢复完成：{selected_path}。建议重启程序后继续使用。")
        self.refresh_overview()
        self._refresh_logs_view()

    def _request_admin_password_for_action(self, action_name: str) -> bool:
        config = self.config_manager.load()
        password, accepted = QInputDialog.getText(
            self,
            f"确认{action_name}",
            f"请输入管理员密码后继续{action_name}：",
            QLineEdit.Password,
        )
        if not accepted:
            return False
        if verify_admin_password(password, config.admin_password_salt, config.admin_password_hash):
            return True
        QMessageBox.warning(self, "认证失败", "管理员密码不正确。")
        return False

    def _reset_database(self) -> None:
        if not self._request_admin_password_for_action("重置数据库"):
            return
        reply = QMessageBox.question(
            self,
            "确认重置",
            "该操作会清空本地 usage.db 中的请求记录，但保留当前配置、API Key、日志和导出文件。是否继续？",
        )
        if reply != QMessageBox.Yes:
            return

        was_running = self.gateway_service.is_running()
        try:
            if was_running:
                self.gateway_service.stop()
            self.database.reset_usage_logs()
            if was_running:
                self.gateway_service.start()
            LOGGER.info("已按管理员指令重置本地数据库")
            self.ops_status.setText("本地数据库已重置，当前配置保持不变。")
            self.refresh_overview()
        except Exception as exc:
            LOGGER.exception("重置数据库失败")
            QMessageBox.critical(self, "重置失败", f"重置数据库失败：{exc}")

    def refresh_overview(self) -> None:
        self.config = self.config_manager.load()
        current_month = month_key()
        snapshot = self.database.monthly_usage_snapshot(self.config.device_id, current_month)
        model_spend = self.database.monthly_model_spend_breakdown(self.config.device_id, current_month)
        spend = snapshot.total_cost
        running = self.gateway_service.is_running()
        api_key_configured = bool(self.config_manager.get_upstream_api_key())
        cache_hit_ratio, cache_miss_ratio = _cache_prompt_ratios(
            snapshot.input_tokens,
            snapshot.cache_read_input_tokens,
            snapshot.cache_creation_input_tokens,
        )
        self.status_value_label.setText("运行中" if running else "已停止")
        self.status_detail_label.setText(
            "LiteLLM 本地内核正在监听请求。" if running else "当前未监听请求，VS Code 将无法通过本地 LiteLLM 内核访问模型。"
        )
        self.budget_value_label.setText(f"RMB {spend:.4f}")
        self.budget_detail_label.setText(
            "\n".join(
                [
                    f"预算上限 RMB {self.config.monthly_budget_usd:.2f} | 本月 {current_month}",
                    _format_model_spend_breakdown(model_spend, list(self.config.models.keys())),
                ]
            )
        )
        self.request_value_label.setText(str(snapshot.total_requests))
        self.request_detail_label.setText(
            f"成功 {snapshot.success_count} 次 | 失败 {snapshot.failure_count} 次"
        )
        self.token_value_label.setText(_format_token_millions(snapshot.total_tokens))
        self.token_detail_label.setText(
            "\n".join(
                [
                    f"输入 {_format_token_millions(snapshot.input_tokens)} | 输出 {_format_token_millions(snapshot.output_tokens)}",
                    f"缓存命中 {_format_percentage(cache_hit_ratio)} | 未命中 {_format_percentage(cache_miss_ratio)}",
                ]
            )
        )
        self.endpoint_value_label.setText(f"{self.config.host}:{self.config.port}")
        self.endpoint_detail_label.setText(f"http://{self.config.host}:{self.config.port}/v1")
        self.upstream_value_label.setText("已配置" if api_key_configured else "未配置")
        self.upstream_detail_label.setText(
            f"上游地址 {self.config.upstream_base_url}" if api_key_configured else "请在设置页录入上游 API Key 后再启动网关。"
        )
        self.device_label.setText(f"当前设备：{self.config.device_name}（{self.config.device_id}）")
        self.last_request_label.setText(
            f"最近一次调用：{snapshot.last_request_time or '当前还没有请求记录'}"
        )
        self.statusBar().showMessage(
            f"设备 {self.config.device_name} | {'网关运行中' if running else '网关已停止'} | {self.config.host}:{self.config.port} | 本月费用 RMB {spend:.4f} | 调用 {snapshot.total_requests} 次"
        )
        self._refresh_report_months()
        self._refresh_history_view()
        self._refresh_logs_view()

    def _start_gateway(self) -> None:
        try:
            self.gateway_service.start()
            LOGGER.info("网关已启动")
            self.refresh_overview()
        except Exception as exc:
            LOGGER.exception("网关启动失败")
            QMessageBox.critical(self, "启动失败", f"网关启动失败：{exc}")

    def _stop_gateway(self) -> None:
        try:
            self.gateway_service.stop()
            LOGGER.info("网关已停止")
            self.refresh_overview()
        except Exception as exc:
            LOGGER.exception("网关停止失败")
            QMessageBox.critical(self, "停止失败", f"网关停止失败：{exc}")

    def _save_settings(self) -> None:
        config = self.config_manager.load()
        current_password = self.admin_password_input.text() or self._session_admin_password
        new_password = self.new_password_input.text().strip()

        if not verify_admin_password(current_password, config.admin_password_salt, config.admin_password_hash):
            QMessageBox.warning(self, "认证失败", "管理员密码不正确。")
            return

        config.device_name = self.device_name_input.text().strip() or config.device_name
        config.device_id = self.device_id_input.text().strip() or config.device_id
        config.host = self.host_input.text().strip() or config.host
        port_text = self.port_input.text().strip()
        budget_text = self.budget_input.text().strip()
        if not port_text or not budget_text:
            QMessageBox.warning(self, "输入不完整", "请完整填写监听端口和月度预算。")
            return
        config.port = int(port_text)
        config.monthly_budget_usd = float(budget_text)
        config.upstream_base_url = self.base_url_input.text().strip() or config.upstream_base_url
        for model_name, inputs in self.model_price_inputs.items():
            config.models[model_name].cache_read_input_per_million = float(inputs["cache"].text().strip() or 0.0)
            config.models[model_name].input_per_million_usd = float(inputs["input"].text().strip() or 0.0)
            config.models[model_name].output_per_million_usd = float(inputs["output"].text().strip() or 0.0)

        if new_password:
            password_hash, salt = hash_password(new_password)
            config.admin_password_hash = password_hash
            config.admin_password_salt = salt

        api_key = self.api_key_input.text().strip()
        if api_key:
            self.config_manager.set_upstream_api_key(api_key)
            self.api_key_input.clear()

        try:
            self.config_manager.save(config)
            set_autostart_enabled(self.startup_checkbox.isChecked())
            self._session_admin_password = current_password
            LOGGER.info("设置已更新，设备=%s 端口=%s 预算=%.2f", config.device_id, config.port, config.monthly_budget_usd)
            self.refresh_overview()
            QMessageBox.information(self, "保存成功", "设置已经更新。")
        except Exception as exc:
            LOGGER.exception("设置保存失败")
            QMessageBox.critical(self, "保存失败", f"设置保存失败：{exc}")

    def _export(self, file_type: str) -> None:
        suffix = ".csv" if file_type == "csv" else ".xlsx"
        export_month = self._selected_report_month()
        default_target = 报表目录() / f"usage-{export_month}{suffix}"
        selected_path, _ = QFileDialog.getSaveFileName(
            self,
            f"导出{file_type.upper()}",
            str(default_target),
            f"{file_type.upper()} 文件 (*{suffix})",
        )
        if not selected_path:
            return
        target = Path(selected_path)
        if file_type == "csv":
            self.database.export_month_csv(self.config.device_id, export_month, target)
        else:
            self.database.export_month_xlsx(self.config.device_id, export_month, target)
        LOGGER.info("已导出 %s 月报: %s", export_month, target)
        self.report_status.setText(f"{export_month} 报表已导出到：{target}")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._is_exiting or not self.tray_icon.isVisible():
            super().closeEvent(event)
            return
        event.ignore()
        self._hide_to_tray()


def build_application(
    config_manager: ConfigManager,
    database: UsageDatabase,
    gateway_service: GatewayService,
) -> QApplication:
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow(config_manager, database, gateway_service)
    window.show()
    app.window = window
    return app
