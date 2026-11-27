"""DASH 探测：解析 MPD、选择音视频表示、生成界面变体。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from muxer.core.http import HttpClient
from muxer.core.models import VariantOption
from muxer.protocols.dash.manifest import (
    AdaptationSet,
    DashManifest,
    ManifestError,
    Period,
    Representation,
    SegmentPlan,
    build_plan,
    parse_manifest,
)
from muxer.protocols.naming import title_from_url, variant_label

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DashAnalysis:
    """MPD 解析结果。"""

    url: str
    title: str
    manifest: DashManifest
    period: Period
    video_set: AdaptationSet | None
    video_rep: Representation | None
    audio_set: AdaptationSet | None
    audio_rep: Representation | None
    video_plan: SegmentPlan | None
    audio_plan: SegmentPlan | None
    variants: tuple[VariantOption, ...]

    @property
    def live(self) -> bool:
        return self.manifest.type == "dynamic"

    @property
    def drm(self) -> bool:
        return any(
            adaptation.drm or any(item.drm for item in adaptation.representations)
            for adaptation in self.period.adaptations
        )

    @property
    def duration(self) -> float:
        for plan in (self.video_plan, self.audio_plan):
            if plan is not None and plan.total_duration > 0:
                return plan.total_duration
        return self.period.duration or self.manifest.duration or 0.0


async def analyze(
    http: HttpClient,
    url: str,
    headers: dict[str, str] | None = None,
    preferred_variant: str = "",
) -> DashAnalysis:
    """拉取 MPD 并解析出可下载的音视频计划。"""
    text = await http.fetch_text(url, headers or {})
    manifest = parse_manifest(text, url)
    period = manifest.periods[0]

    video_set = next(
        (item for item in period.adaptations if _kind(item) == "video" and item.representations),
        None,
    )
    audio_set = next(
        (item for item in period.adaptations if _kind(item) == "audio" and item.representations),
        None,
    )
    if video_set is None and audio_set is None:
        raise ManifestError("MPD 中没有音视频表示")

    video_rep = (
        _pick_representation(video_set.representations, preferred_variant)
        if video_set is not None
        else None
    )
    audio_rep = (
        max(audio_set.representations, key=lambda item: item.bandwidth)
        if audio_set is not None
        else None
    )

    video_plan = (
        build_plan(manifest, period, video_set, video_rep)
        if video_set is not None and video_rep is not None
        else None
    )
    audio_plan = (
        build_plan(manifest, period, audio_set, audio_rep)
        if audio_set is not None and audio_rep is not None
        else None
    )
    if video_plan is None and audio_plan is None:
        raise ManifestError("无法从 MPD 生成分片计划")

    variants = _build_variants(video_set or audio_set)
    return DashAnalysis(
        url=url,
        title=title_from_url(url),
        manifest=manifest,
        period=period,
        video_set=video_set,
        video_rep=video_rep,
        audio_set=audio_set,
        audio_rep=audio_rep,
        video_plan=video_plan,
        audio_plan=audio_plan,
        variants=variants,
    )


def _kind(adaptation: AdaptationSet) -> str:
    """判断 AdaptationSet 是视频还是音频。"""
    if adaptation.content_type in ("video", "audio"):
        return adaptation.content_type
    if adaptation.mime_type.startswith("video/"):
        return "video"
    if adaptation.mime_type.startswith("audio/"):
        return "audio"
    if any(item.width or item.height for item in adaptation.representations):
        return "video"
    return ""


def _pick_representation(
    representations: list[Representation], preferred_variant: str
) -> Representation:
    """优先使用界面选择的表示，否则取最高码率。"""
    if preferred_variant:
        for item in representations:
            if item.id == preferred_variant:
                return item
        LOGGER.warning("未找到指定的 DASH 表示: %s", preferred_variant)
    return max(representations, key=lambda item: item.bandwidth)


def _build_variants(adaptation: AdaptationSet | None) -> tuple[VariantOption, ...]:
    if adaptation is None:
        return ()
    variants: list[VariantOption] = []
    for item in adaptation.representations:
        resolution = f"{item.width}x{item.height}" if item.width and item.height else ""
        variants.append(
            VariantOption(
                uri=item.id,
                label=variant_label(resolution, item.bandwidth, item.codecs),
                bandwidth=item.bandwidth,
                resolution=resolution,
                codecs=item.codecs,
            )
        )
    return tuple(variants)


def estimate_bytes(analysis: DashAnalysis) -> int | None:
    """按表示码率 × 时长估算总大小。"""
    duration = analysis.duration
    if duration <= 0:
        return None
    video_bitrate = analysis.video_rep.bandwidth if analysis.video_rep else 0
    audio_bitrate = analysis.audio_rep.bandwidth if analysis.audio_rep else 0
    total = video_bitrate + audio_bitrate
    if total <= 0:
        return None
    return int(total * duration / 8)