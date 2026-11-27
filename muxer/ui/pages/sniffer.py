"""嗅探页：内置浏览器 + 媒体请求捕获列表。

拦截命中的媒体地址后交给引擎探测，列表里可单个打开到新建下载页，
也可以勾选后批量解析并入队。
"""

from __future__ import annotations

import uuid

from PySide6.QtCore import QSize, QUrl, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from muxer.config import APP_DATA_DIR
from muxer.core.models import ProbeResult
from muxer.core.settings import DEFAULT_USER_AGENT
from muxer.sniff.interceptor import MediaRequestInterceptor, normalize_url
from muxer.ui.theme import manager as theme_manager
from muxer.ui.widgets import icons
from muxer.ui.widgets.empty_state import EmptyState
from muxer.ui.widgets.page import Page
from muxer.ui.widgets.pill import Pill
from muxer.ui.widgets.sniff_row import SniffRow

try:
    from PySide6.QtWebEngineCore import QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView

    WEBENGINE_AVAILABLE = True
except ImportError:  # pragma: no cover - 仅在缺少 QtWebEngine 时触发
    QWebEngineProfile = None  # type: ignore[assignment]
    QWebEngineView = None  # type: ignore[assignment]
    WEBENGINE_AVAILABLE = False

MAX_CANDIDATES = 200
def _home_html() -> str:
    """随主题变色的空白首页。"""
    theme = theme_manager.current()
    return (
        "<html><body style='font-family:Microsoft YaHei UI,Segoe UI,sans-serif;"
        f"color:{theme.text_secondary};background:{theme.bg};"
        "padding:28px;font-size:14px;line-height:1.8'>"
        f"<h3 style='color:{theme.text};font-weight:600'>在这里打开视频页面</h3>"
        "<p>播放后，页面发出的 m3u8 / mpd / flv / mp4 请求会自动出现在下方列表。</p>"
        "<p>下载时会自动带上页面的 Referer 与 Cookie。</p></body></html>"
    )


class SnifferPage(Page):
    """内置浏览器与媒体请求抽屉。"""

    probeRequested = Signal(dict)
    downloadRequested = Signal(str, dict, str)
    batchRequested = Signal(list)
    messageRequested = Signal(str, str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__("嗅探", "播放页面后自动捕获媒体地址，可直接下载或批量加入队列", parent)
        self.available = WEBENGINE_AVAILABLE
        self._rows: dict[str, SniffRow] = {}
        self._pending_probe: dict[str, str] = {}
        self._batch_pending: dict[str, str] = {}
        self._expanded = False

        if not WEBENGINE_AVAILABLE:
            state = EmptyState(
                "radar",
                "未安装嗅探组件",
                "当前环境缺少 QtWebEngine，内置浏览器不可用。"
                "核心下载功能不受影响；安装 PySide6-Addons 后重启即可启用。",
                parent=self,
            )
            self.content.addWidget(state, 1)
            return

        self._toolbar = self._build_toolbar()
        self._view = self._build_view()
        self._drawer = self._build_drawer()
        self.content.addWidget(self._toolbar)
        self.content.addWidget(self._view, 1)
        self.content.addWidget(self._drawer)
        self._set_expanded(False)

        theme_manager.subscribe(self._refresh_theme)
        self.destroyed.connect(lambda: theme_manager.unsubscribe(self._refresh_theme))
        self._refresh_theme()

    # ---- 构建 ----

    def _build_toolbar(self) -> QFrame:
        bar = QFrame(self)
        bar.setObjectName("sniffBar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)

        self._back_button = self._tool_button("arrow-left", "后退", self._go_back)
        self._forward_button = self._tool_button("arrow-right", "前进", self._go_forward)
        self._reload_button = self._tool_button("refresh", "刷新", self._reload)

        self._address = QLineEdit(bar)
        self._address.setPlaceholderText("输入网址，例如 https://example.com/watch/123")
        self._address.returnPressed.connect(self._navigate)

        open_button = QPushButton("打开", bar)
        open_button.setObjectName("primaryButton")
        open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        open_button.clicked.connect(self._navigate)

        layout.addWidget(self._back_button)
        layout.addWidget(self._forward_button)
        layout.addWidget(self._reload_button)
        layout.addWidget(self._address, 1)
        layout.addWidget(open_button)
        return bar

    def _tool_button(self, icon_name: str, tooltip: str, slot) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName("iconButton")
        button.setToolTip(tooltip)
        button.setFixedSize(30, 28)
        button.setIconSize(QSize(16, 16))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda: slot())
        button.setProperty("iconName", icon_name)
        return button

    def _build_view(self) -> QWebEngineView:
        # 先配置默认 Profile 的存储路径，再创建 View，避免 Qt 在
        # %LOCALAPPDATA%/Muxer/Muxer 下再建一套默认缓存目录
        profile = QWebEngineProfile.defaultProfile()
        profile.setHttpUserAgent(DEFAULT_USER_AGENT)
        storage = APP_DATA_DIR / "webengine"
        storage.mkdir(parents=True, exist_ok=True)
        profile.setPersistentStoragePath(str(storage))
        profile.setCachePath(str(storage / "cache"))
        profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
        )
        self._interceptor = MediaRequestInterceptor(self)
        self._interceptor.candidateFound.connect(self._on_candidate_signal)
        profile.setUrlRequestInterceptor(self._interceptor)

        view = QWebEngineView(self)
        view.setMinimumHeight(240)
        view.urlChanged.connect(self._on_url_changed)
        view.loadFinished.connect(lambda _ok: self._update_nav())
        view.setHtml(_home_html(), QUrl("about:blank"))
        return view

    def _build_drawer(self) -> QFrame:
        drawer = QFrame(self)
        drawer.setObjectName("sniffDrawer")
        drawer.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(drawer)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel("媒体请求", drawer)
        title.setObjectName("cardTitle")
        self._count_pill = Pill("0", "neutral", drawer)

        self._select_all_button = QPushButton("全选", drawer)
        self._select_all_button.setObjectName("ghostButton")
        self._select_all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._select_all_button.clicked.connect(self._select_all)

        self._batch_button = QPushButton("批量加入", drawer)
        self._batch_button.setObjectName("primaryButton")
        self._batch_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._batch_button.setEnabled(False)
        self._batch_button.clicked.connect(self._batch_download)

        self._clear_button = QPushButton("清空", drawer)
        self._clear_button.setObjectName("ghostButton")
        self._clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_button.clicked.connect(self._clear)

        self._toggle_button = QPushButton("展开", drawer)
        self._toggle_button.setObjectName("ghostButton")
        self._toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle_button.clicked.connect(self._toggle_expanded)

        header.addWidget(title)
        header.addWidget(self._count_pill)
        header.addStretch(1)
        header.addWidget(self._select_all_button)
        header.addWidget(self._batch_button)
        header.addWidget(self._clear_button)
        header.addWidget(self._toggle_button)
        layout.addLayout(header)

        self._scroll = QScrollArea(drawer)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFixedHeight(164)

        container = QWidget(self._scroll)
        self._list_layout = QVBoxLayout(container)
        self._list_layout.setContentsMargins(0, 0, 6, 0)
        self._list_layout.setSpacing(0)

        self._empty_hint = QLabel(
            "播放页面后，m3u8 / mpd / flv / mp4 请求会出现在这里。", container
        )
        self._empty_hint.setObjectName("caption")
        self._empty_hint.setContentsMargins(8, 12, 0, 0)
        self._list_layout.addWidget(self._empty_hint)
        self._list_layout.addStretch(1)
        self._scroll.setWidget(container)
        layout.addWidget(self._scroll)
        return drawer

    # ---- 候选与探测 ----

    def handle_candidate(self, url: str, referer: str = "", cookie: str = "") -> str | None:
        """新增一个候选地址并发起探测；返回探测请求 id。"""
        key = normalize_url(url)
        if not key or key in self._rows:
            return None
        if len(self._rows) >= MAX_CANDIDATES:
            self.messageRequested.emit("候选链接过多，已停止追加", "warning")
            return None

        row = SniffRow(url, referer, cookie, self)
        row.downloadRequested.connect(self._on_row_download)
        row.selectionChanged.connect(self._update_batch_button)
        self._rows[key] = row
        self._list_layout.insertWidget(0, row)

        request_id = uuid.uuid4().hex
        self._pending_probe[request_id] = key
        row.set_probing()
        self._refresh_count()
        self._update_batch_button()
        if not self._expanded:
            self._set_expanded(True)
        self.probeRequested.emit(
            {"request_id": request_id, "url": url, "headers": row.headers()}
        )
        return request_id

    def _on_candidate_signal(self, url: str, referer: str, cookie: str) -> None:
        self.handle_candidate(url, referer, cookie)

    def on_probe_finished(self, result: ProbeResult) -> None:
        """探测结果（单个候选或批量入队结果）。"""
        key = self._pending_probe.pop(result.request_id, None)
        if key is not None:
            row = self._rows.get(key)
            if row is not None:
                row.set_result(result)
            self._update_batch_button()
            return

        key = self._batch_pending.pop(result.request_id, None)
        if key is not None:
            row = self._rows.get(key)
            if row is not None:
                row.set_queued()
            self._update_batch_button()
            self.messageRequested.emit(f"已加入下载队列：{result.title}", "success")

    def on_probe_failed(self, request_id: str, message: str) -> None:
        key = self._pending_probe.pop(request_id, None)
        if key is not None:
            row = self._rows.get(key)
            if row is not None:
                row.set_failed(message)
            self._update_batch_button()
            return
        key = self._batch_pending.pop(request_id, None)
        if key is not None:
            row = self._rows.get(key)
            if row is not None:
                row.set_failed(message)
            self._update_batch_button()
            self.messageRequested.emit(message, "danger")

    # ---- 下载 ----

    def _on_row_download(self, url: str) -> None:
        row = next((item for item in self._rows.values() if item.url == url), None)
        if row is None:
            return
        self.downloadRequested.emit(url, row.headers(), row.title)

    def _batch_download(self) -> None:
        selected = [
            row for row in self._rows.values() if row.is_selected() and row.is_supported()
        ]
        if not selected:
            self.messageRequested.emit("请先勾选可下载的链接", "warning")
            return

        items = []
        for row in selected:
            request_id = uuid.uuid4().hex
            self._batch_pending[request_id] = normalize_url(row.url)
            row.set_submitting()
            items.append(
                {
                    "request_id": request_id,
                    "url": row.url,
                    "headers": row.headers(),
                    "title": row.title,
                }
            )
        self.batchRequested.emit(items)
        self.messageRequested.emit(f"正在解析并加入 {len(items)} 个任务…", "info")
        self._update_batch_button()

    def _select_all(self) -> None:
        for row in self._rows.values():
            if row.is_supported():
                row.set_checked(True)
        self._update_batch_button()

    def _clear(self) -> None:
        for row in self._rows.values():
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self._pending_probe.clear()
        self._batch_pending.clear()
        self._refresh_count()
        self._update_batch_button()

    # ---- 状态 ----

    def _refresh_count(self) -> None:
        count = len(self._rows)
        self._count_pill.setText(str(count))
        self._count_pill.set_tone("accent" if count else "neutral")
        self._empty_hint.setVisible(count == 0)
        self._select_all_button.setEnabled(count > 0)
        self._clear_button.setEnabled(count > 0)

    def _update_batch_button(self) -> None:
        selected = sum(
            1 for row in self._rows.values() if row.is_selected() and row.is_supported()
        )
        self._batch_button.setText(f"批量加入 ({selected})" if selected else "批量加入")
        self._batch_button.setEnabled(selected > 0)

    def _set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self._scroll.setVisible(expanded)
        self._toggle_button.setText("收起" if expanded else "展开")

    def _toggle_expanded(self) -> None:
        self._set_expanded(not self._expanded)

    # ---- 浏览器导航 ----

    def _navigate(self) -> None:
        text = self._address.text().strip()
        if not text:
            return
        if "://" not in text:
            text = "https://" + text
        self._view.load(QUrl(text))

    def _go_back(self) -> None:
        self._view.back()

    def _go_forward(self) -> None:
        self._view.forward()

    def _reload(self) -> None:
        self._view.reload()

    def _on_url_changed(self, url: QUrl) -> None:
        text = url.toString()
        if text and text != "about:blank":
            self._address.setText(text)
        self._update_nav()

    def _update_nav(self) -> None:
        history = self._view.history()
        self._back_button.setEnabled(history.canGoBack())
        self._forward_button.setEnabled(history.canGoForward())

    # ---- 退出 ----

    def release_webengine(self) -> None:
        """窗口关闭前释放 WebEngine 页面与拦截器，帮助进程干净退出。"""
        if not WEBENGINE_AVAILABLE:
            return
        try:
            self._view.stop()
            QWebEngineProfile.defaultProfile().setUrlRequestInterceptor(None)
            self._view.deleteLater()
        except RuntimeError:
            pass

    # ---- 主题 ----

    def _refresh_theme(self) -> None:
        if not WEBENGINE_AVAILABLE:
            return
        theme = theme_manager.current()
        self._view.page().setBackgroundColor(QColor(theme.bg))
        if self._view.url().toString() in ("about:blank", ""):
            self._view.setHtml(_home_html(), QUrl("about:blank"))
        for button, icon_name in (
            (self._back_button, "arrow-left"),
            (self._forward_button, "arrow-right"),
            (self._reload_button, "refresh"),
        ):
            button.setIcon(icons.icon(icon_name, theme.text_secondary, 16))