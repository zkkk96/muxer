"""生成 Muxer 应用图标（PNG 多尺寸 + ICO）。

设计说明
--------
图形语义：左右两条流（上、下）向中间汇聚成一条流，末端以播放三角结束，
对应 "把多路分片/音视频轨 mux 成一个可播放文件"。

配色沿用 Claude Desktop 的暖色调：
    背景渐变 #E08A6E -> #C45F3E（珊瑚色）
    前景     #FAF9F5（米白）

几何以 1024 画布为基准，所有尺寸乘以 SUPERSAMPLE 后再渲染，
最后用 LANCZOS 降采样，得到平滑边缘。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

CANVAS = 1024                 # 设计基准画布
SUPERSAMPLE = 2               # 超采样倍数（2048 渲染 -> 1024 输出）
SIZE = CANVAS * SUPERSAMPLE   # 实际渲染边长

BG_TOP = (224, 138, 110)      # #E08A6E
BG_BOTTOM = (196, 95, 62)     # #C45F3E
FG = (250, 249, 245)          # #FAF9F5
CORNER_RADIUS = 230           # 圆角半径（squircle 近似）
STROKE = 76                   # 流线宽度
PLAY_POINTS = [(676, 392), (830, 512), (676, 632)]  # 播放三角
MERGE_X = 560                 # 汇聚点 x
CENTER_Y = 512                # 汇聚点 y
STREAM_TOP_Y = 330            # 上方流起点 y
STREAM_BOTTOM_Y = 694         # 下方流起点 y
STREAM_START_X = 220          # 流的起点 x


def scale_point(point: tuple[float, float]) -> tuple[float, float]:
    """把设计坐标映射到超采样画布坐标。"""
    return (point[0] * SUPERSAMPLE, point[1] * SUPERSAMPLE)


def cubic_bezier(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    steps: int = 320,
) -> list[tuple[float, float]]:
    """采样三次贝塞尔曲线，返回折线点集。"""
    t = np.linspace(0.0, 1.0, steps)[:, None]
    points = (
        (1 - t) ** 3 * np.array(p0)
        + 3 * (1 - t) ** 2 * t * np.array(p1)
        + 3 * (1 - t) * t**2 * np.array(p2)
        + t**3 * np.array(p3)
    )
    return [tuple(point) for point in points]


def draw_background(image: Image.Image) -> None:
    """绘制圆角矩形背景 + 垂直渐变。"""
    gradient = np.linspace(np.array(BG_TOP), np.array(BG_BOTTOM), SIZE)
    background = np.zeros((SIZE, SIZE, 4), dtype=np.uint8)
    background[..., :3] = np.round(gradient).astype(np.uint8)[:, None, :]
    background[..., 3] = 255

    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, SIZE - 1, SIZE - 1], radius=CORNER_RADIUS * SUPERSAMPLE, fill=255
    )
    image.paste(Image.fromarray(background, "RGBA"), (0, 0), mask)


def draw_glyph_mask() -> Image.Image:
    """绘制前景图形遮罩：两条汇聚流 + 主干 + 播放三角。"""
    mask = Image.new("L", (SIZE, SIZE), 0)
    drawer = ImageDraw.Draw(mask)
    radius = (STROKE / 2) * SUPERSAMPLE

    # 两条流：起点水平，收敛到汇聚点后切线水平
    curves = [
        cubic_bezier(
            (STREAM_START_X, STREAM_TOP_Y),
            (400, STREAM_TOP_Y),
            (400, CENTER_Y),
            (MERGE_X, CENTER_Y),
        ),
        cubic_bezier(
            (STREAM_START_X, STREAM_BOTTOM_Y),
            (400, STREAM_BOTTOM_Y),
            (400, CENTER_Y),
            (MERGE_X, CENTER_Y),
        ),
    ]
    for curve in curves:
        for point in curve:
            x, y = scale_point(point)
            drawer.ellipse([x - radius, y - radius, x + radius, y + radius], fill=255)

    # 主干：从汇聚点延伸到播放三角左侧
    trunk_start = scale_point((MERGE_X, CENTER_Y))
    trunk_end = scale_point((660, CENTER_Y))
    drawer.line([trunk_start, trunk_end], fill=255, width=int(STROKE * SUPERSAMPLE))
    for center in (trunk_start, trunk_end):
        drawer.ellipse(
            [center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius],
            fill=255,
        )

    # 播放三角（顶点做小圆角，避免过尖）
    drawer.polygon([scale_point(point) for point in PLAY_POINTS], fill=255)
    for point in PLAY_POINTS:
        x, y = scale_point(point)
        corner = 14 * SUPERSAMPLE
        drawer.ellipse([x - corner, y - corner, x + corner, y + corner], fill=255)

    return mask


def build_master() -> Image.Image:
    """生成 1024x1024 母版图标。"""
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw_background(image)

    foreground = Image.new("RGBA", (SIZE, SIZE), FG + (255,))
    image.paste(foreground, (0, 0), draw_glyph_mask())

    return image.resize((CANVAS, CANVAS), Image.LANCZOS)


def main() -> None:
    """输出 SVG 之外的所有位图资源。"""
    output_dir = Path(__file__).resolve().parent.parent / "muxer" / "assets" / "icons"
    output_dir.mkdir(parents=True, exist_ok=True)

    master = build_master()
    master.save(output_dir / "app-1024.png")

    for size in (512, 256, 128, 64, 48, 32, 16):
        master.resize((size, size), Image.LANCZOS).save(output_dir / f"app-{size}.png")

    master.resize((256, 256), Image.LANCZOS).save(
        output_dir / "muxer.ico",
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"图标已生成: {output_dir}")


if __name__ == "__main__":
    main()