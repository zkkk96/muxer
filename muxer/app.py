"""应用启动：日志、QApplication、单实例、首次引导与主窗口。"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from muxer import APP_DESCRIPTION, APP_NAME, APP_VERSION
from muxer.config import APP_DATA_DIR, AppConfig, load_config
from muxer.ui.main_window import MainWindow
from muxer.ui.onboarding import OnboardingDialog
from muxer.ui.theme import manager as theme_manager
from muxer.ui.widgets import icons

LOGGER = logging.getLogger(__name__)

SINGLE_INSTANCE_NAME = "Muxer-SingleInstance"
SMOKE_ARG = "--smoke-test"


def configure_logging() -> None:
    """日志同时写文件与标准错误，文件按 5MB 滚动保留 3 份。"""
    log_dir = APP_DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        log_dir / "app.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)


def create_application(argv: list[str] | None = None) -> QApplication:
    """创建并配置 QApplication（已存在则复用）。"""
    app = QApplication.instance()
    if app is None:
        app = QApplication(argv or sys.argv)

    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(APP_NAME)
    app.setWindowIcon(icons.app_icon())
    # Fusion 作为 QSS 基底，避免各平台原生样式插件带来的差异
    app.setStyle("Fusion")
    app.setFont(QFont(theme_manager.current().font_sans, 10))
    return app


def _acquire_single_instance(
    app: QApplication,
) -> tuple[QLocalServer | None, QLockFile | None, bool]:
    """文件锁保证唯一实例；已有实例时通过本地套接字唤醒它。

    Windows 下 QLocalServer 的命名管道在同一名字上可能重复监听，
    因此互斥改用 QLockFile，套接字只负责激活已有窗口。
    """
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(APP_DATA_DIR / "muxer.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        socket = QLocalSocket()
        socket.connectToServer(SINGLE_INSTANCE_NAME)
        if socket.waitForConnected(800):
            socket.write(b"show")
            socket.flush()
            socket.waitForBytesWritten(500)
            socket.disconnectFromServer()
            return None, None, False
        # 没有实例响应：清理上次崩溃残留的锁再试一次
        if lock.removeStaleLockFile() and lock.tryLock(0):
            LOGGER.info("已清理残留的单实例锁")
        else:
            return None, None, False

    server = QLocalServer(app)
    if not server.listen(SINGLE_INSTANCE_NAME):
        LOGGER.warning("单实例激活通道创建失败: %s", server.errorString())
        server = None
    return server, lock, True


def _activate_existing(server: QLocalServer, window: MainWindow) -> None:
    """第二个实例发来唤醒消息。"""
    while server.hasPendingConnections():
        connection = server.nextPendingConnection()
        if connection is not None:
            connection.readAll()
            connection.disconnectFromServer()
    window.show_from_tray()


def main() -> int:
    """程序入口。"""
    smoke = SMOKE_ARG in sys.argv
    argv = [argument for argument in sys.argv if argument != SMOKE_ARG]

    configure_logging()
    app = create_application(argv)

    config: AppConfig = load_config()
    theme_manager.apply(config.theme)
    app.setFont(QFont(theme_manager.current().font_sans, 10))

    hints = app.styleHints()
    if hasattr(hints, "colorSchemeChanged"):
        hints.colorSchemeChanged.connect(_on_color_scheme_changed)

    server: QLocalServer | None = None
    instance_lock: QLockFile | None = None
    if not smoke:
        server, instance_lock, primary = _acquire_single_instance(app)
        if not primary:
            LOGGER.info("检测到已有 Muxer 实例，激活后退出")
            return 0
        if instance_lock is not None:
            app._muxer_instance_lock = instance_lock  # type: ignore[attr-defined]

    smoke_dir = Path(tempfile.mkdtemp(prefix="muxer-smoke-")) if smoke else None
    window = MainWindow(
        config,
        enable_tray=not smoke,
        engine_data_dir=smoke_dir,
    )
    if server is not None:
        server.newConnection.connect(lambda: _activate_existing(server, window))
        app._muxer_single_instance = server  # type: ignore[attr-defined]
        if server.hasPendingConnections():
            _activate_existing(server, window)

    window.show()
    LOGGER.info("%s %s 启动：%s", APP_NAME, APP_VERSION, APP_DESCRIPTION)

    if smoke:
        # 走与托盘退出相同的收尾路径，验证打包产物能干净退出
        QTimer.singleShot(1500, window.quit_application)
    elif not config.first_run_completed:
        dialog = OnboardingDialog(config, window)
        dialog.exec()
        window.apply_download_dir(config.download_dir)

    exit_code = app.exec()
    LOGGER.info("%s 已退出，代码 %s", APP_NAME, exit_code)
    if smoke_dir is not None:
        shutil.rmtree(smoke_dir, ignore_errors=True)
    return exit_code


def _on_color_scheme_changed(_scheme: Qt.ColorScheme) -> None:
    """系统外观变化时，仅在"跟随系统"模式下重设主题。"""
    if theme_manager.mode() == "system":
        theme_manager.apply("system")


if __name__ == "__main__":
    raise SystemExit(main())