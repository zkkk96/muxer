"""离屏渲染界面截图，用于开发期检查视觉稿。

用法：python tools/preview.py
输出：docs/preview-light-*.png、docs/preview-dark-*.png

预览不启动引擎，而是注入演示快照，保证页面有内容且不产生真实下载。
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from muxer.app import configure_logging, create_application  # noqa: E402
from muxer.config import AppConfig  # noqa: E402
from muxer.core.models import ProbeResult, TaskSnapshot, TaskStatus, VariantOption  # noqa: E402
from muxer.ui.main_window import MainWindow  # noqa: E402
from muxer.ui.log_dialog import LogDialog  # noqa: E402
from muxer.ui.onboarding import OnboardingDialog  # noqa: E402
from muxer.ui.theme import manager as theme_manager  # noqa: E402

OUTPUT_DIR = ROOT / "docs"
PAGE_NAMES = ("new", "tasks", "history", "sniffer", "settings")


def snapshot(
    task_id: str,
    title: str,
    protocol: str,
    status: TaskStatus,
    progress: float,
    *,
    quality: str = "",
    container: str = "",
    speed_bps: float = 0.0,
    eta_seconds: int | None = None,
    updated: datetime | None = None,
    error: str = "",
    segments_total: int = 0,
    live: bool = False,
    live_seconds: float = 0.0,
    bytes_total_override: int | None = None,
    bytes_done_override: int | None = None,
) -> TaskSnapshot:
    """构造预览用的任务快照。"""
    total = 800 * 1024 * 1024 if bytes_total_override is None else bytes_total_override
    moment = (updated or datetime.now()).isoformat(timespec="seconds")
    return TaskSnapshot(
        id=task_id,
        url=f"https://cdn.example.com/{task_id}.mp4",
        title=title,
        protocol=protocol,
        status=status,
        progress=progress,
        bytes_done=(
            int(total * progress) if bytes_done_override is None else bytes_done_override
        ),
        bytes_total=total,
        segments_done=int(segments_total * progress) if segments_total else 0,
        segments_total=segments_total,
        speed_bps=speed_bps,
        eta_seconds=eta_seconds,
        output_dir="D:/Downloads/Muxer",
        output_path=f"D:/Downloads/Muxer/{title}" if status is TaskStatus.COMPLETED else "",
        error=error,
        quality=quality,
        container=container,
        created_at=moment,
        updated_at=moment,
        live=live,
        live_seconds=live_seconds,
    )


def demo_snapshots() -> tuple[TaskSnapshot, ...]:
    """覆盖下载中、暂停、完成、失败、排队五种状态，外加两条历史记录。"""
    now = datetime.now()
    return (
        snapshot(
            "demo-1", "落日海岸延时摄影 4K.mp4", "hls", TaskStatus.DOWNLOADING, 0.68,
            quality="1080P", container="MP4", speed_bps=2.4 * 1024 * 1024, eta_seconds=192,
            segments_total=120,
        ),
        snapshot(
            "demo-2", "访谈实录 第 12 期.mp4", "dash", TaskStatus.DOWNLOADING, 0.24,
            quality="720P", container="MKV", speed_bps=1.1 * 1024 * 1024, eta_seconds=760,
            updated=now - timedelta(minutes=1), segments_total=240,
        ),
        snapshot(
            "demo-3", "线上课程 · 第三章.m4a", "hls", TaskStatus.PAUSED, 0.41,
            quality="仅音频", container="M4A", updated=now - timedelta(minutes=2),
        ),
        snapshot("demo-4", "发布会回放.mp4", "flv", TaskStatus.COMPLETED, 1.0,
                 updated=now - timedelta(minutes=3)),
        snapshot(
            "demo-5", "MV 高清版.mp4", "hls", TaskStatus.FAILED, 0.35,
            quality="1080P", container="MP4", error="分片连续失败：HTTP 403",
            updated=now - timedelta(minutes=4),
        ),
        snapshot(
            "demo-live", "电竞赛事直播 · 总决赛", "hls", TaskStatus.DOWNLOADING, 0.0,
            quality="1080P", container="MP4", speed_bps=3.2 * 1024 * 1024,
            live=True, live_seconds=3725.0,
            bytes_total_override=0, bytes_done_override=1250 * 1024 * 1024,
        ),
        snapshot("demo-6", "城市夜景航拍.mp4", "hls", TaskStatus.COMPLETED, 1.0,
                 updated=now - timedelta(days=1)),
        snapshot("demo-7", "音乐会现场.mkv", "dash", TaskStatus.COMPLETED, 1.0,
                 updated=now - timedelta(days=4)),
    )


def capture(window: MainWindow, mode: str) -> None:
    """逐页截图。"""
    for index, name in enumerate(PAGE_NAMES):
        window.go_to(index)
        QTest.qWait(260)
        path = OUTPUT_DIR / f"preview-{mode}-{name}.png"
        window.grab().save(str(path))
        print(f"saved {path}")


def main() -> int:
    configure_logging()
    app = create_application([])

    window = MainWindow(
        AppConfig(download_dir="D:/Downloads/Muxer"),
        start_engine=False,
        enable_tray=False,
    )
    window.resize(1180, 800)
    # WA_DontShowOnScreen 让窗口完成布局与绘制但不真出现在桌面上，
    # 相比 offscreen 平台插件，字体渲染与真实环境一致。
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    QTest.qWait(300)

    # 空白新建页（显示使用提示）
    theme_manager.apply("light")
    window.go_to(0)
    QTest.qWait(200)
    window.grab().save(str(OUTPUT_DIR / "preview-new-empty-light.png"))
    theme_manager.apply("dark")
    QTest.qWait(150)
    window.grab().save(str(OUTPUT_DIR / "preview-new-empty-dark.png"))

    snapshots = demo_snapshots()
    window._tasks_page.load_tasks(snapshots)  # noqa: SLF001 - 预览注入
    window._history_page.load_tasks(snapshots)  # noqa: SLF001 - 预览注入

    # 给嗅探页注入演示候选，便于检查列表与抽屉布局
    sniffer = window._sniffer_page  # noqa: SLF001 - 预览注入
    if sniffer.available:
        sniffer.blockSignals(True)
        try:
            demos = (
                ProbeResult(
                    request_id="",
                    url="https://cdn.example.com/vod/coastline/master.m3u8",
                    title="纪录片 · 海岸线",
                    protocol="hls",
                    supported=True,
                    size_bytes=734003200,
                    content_type="application/vnd.apple.mpegurl",
                    variants=(
                        VariantOption(uri="1080.m3u8", label="1080P · 5.2 Mbps · H.264", bandwidth=5_200_000),
                        VariantOption(uri="720.m3u8", label="720P · 2.8 Mbps · H.264", bandwidth=2_800_000),
                    ),
                    duration_seconds=223.0,
                ),
                ProbeResult(
                    request_id="",
                    url="https://live.example.com/match/index.m3u8",
                    title="电竞赛事直播 · 总决赛",
                    protocol="hls",
                    supported=True,
                    content_type="application/vnd.apple.mpegurl",
                    message="直播流：持续录制，点击任务卡的停止按钮完成并合并",
                    variants=(VariantOption(uri="live", label="1080P · 6.0 Mbps · H.264", bandwidth=6_000_000),),
                    live=True,
                ),
                ProbeResult(
                    request_id="",
                    url="https://cdn.example.com/trailer.mp4",
                    title="电影预告片 4K.mp4",
                    protocol="direct",
                    supported=True,
                    size_bytes=734003200,
                    content_type="video/mp4",
                ),
                ProbeResult(
                    request_id="",
                    url="https://drm.example.com/movie/manifest.mpd",
                    title="DRM 示例影片",
                    protocol="dash",
                    supported=False,
                    content_type="application/dash+xml",
                    message="检测到 DRM 保护（ContentProtection），暂不支持",
                ),
            )
            for demo in demos:
                request_id = sniffer.handle_candidate(demo.url, "https://example.com/watch/1", "")
                if request_id:
                    sniffer.on_probe_finished(
                        ProbeResult(
                            request_id=request_id,
                            url=demo.url,
                            title=demo.title,
                            protocol=demo.protocol,
                            supported=demo.supported,
                            size_bytes=demo.size_bytes,
                            content_type=demo.content_type,
                            message=demo.message,
                            variants=demo.variants,
                            duration_seconds=demo.duration_seconds,
                            live=demo.live,
                        )
                    )
            for row in sniffer._rows.values():  # noqa: SLF001
                if row.is_supported():
                    row.set_checked(True)
            sniffer._update_batch_button()  # noqa: SLF001
        finally:
            sniffer.blockSignals(False)

    # 给新建下载页注入一个已完成探测的状态，用于检查媒体信息卡
    page = window._new_page  # noqa: SLF001 - 预览注入
    page._request_id = "preview"  # noqa: SLF001
    page.url_input.set_text("https://cdn.example.com/vod/coastline/master.m3u8")
    page.on_probe_finished(
        ProbeResult(
            request_id="preview",
            url="https://cdn.example.com/vod/coastline/index.m3u8",
            title="纪录片 · 海岸线",
            protocol="hls",
            supported=True,
            size_bytes=734003200,
            content_type="application/vnd.apple.mpegurl",
            message="AES-128 加密，将自动解密",
            variants=(
                VariantOption(
                    uri="https://cdn.example.com/vod/coastline/1080/index.m3u8",
                    label="1080P · 5.2 Mbps · H.264",
                    bandwidth=5_200_000,
                    resolution="1920x1080",
                ),
                VariantOption(
                    uri="https://cdn.example.com/vod/coastline/720/index.m3u8",
                    label="720P · 2.8 Mbps · H.264",
                    bandwidth=2_800_000,
                    resolution="1280x720",
                ),
                VariantOption(
                    uri="https://cdn.example.com/vod/coastline/480/index.m3u8",
                    label="480P · 1.2 Mbps · H.264",
                    bandwidth=1_200_000,
                    resolution="854x480",
                ),
            ),
            duration_seconds=223.0,
            encrypted=True,
        )
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    theme_manager.apply("light")
    QTest.qWait(220)
    capture(window, "light")

    theme_manager.apply("dark")
    QTest.qWait(220)
    capture(window, "dark")

    # 首次引导对话框（不点击接受，避免改写本地配置）
    theme_manager.apply("light")
    QTest.qWait(150)
    dialog = OnboardingDialog(window._config, window)  # noqa: SLF001
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    dialog.show()
    QTest.qWait(200)
    dialog.grab().save(str(OUTPUT_DIR / "preview-onboarding-light.png"))
    theme_manager.apply("dark")
    QTest.qWait(150)
    dialog.grab().save(str(OUTPUT_DIR / "preview-onboarding-dark.png"))
    dialog.close()

    # 日志窗口
    demo_logger = logging.getLogger("muxer.preview")
    demo_logger.info("下载完成: 纪录片 · 海岸线.mp4")
    demo_logger.warning("分片重试 2/5: HTTP 403")
    demo_logger.error("合并失败: 未找到 ffmpeg")
    QTest.qWait(100)
    log_dialog = LogDialog(window._log_store, window)  # noqa: SLF001
    log_dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    log_dialog.show()
    theme_manager.apply("light")
    QTest.qWait(150)
    log_dialog.grab().save(str(OUTPUT_DIR / "preview-log-light.png"))
    theme_manager.apply("dark")
    QTest.qWait(150)
    log_dialog.grab().save(str(OUTPUT_DIR / "preview-log-dark.png"))
    log_dialog.close()

    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())