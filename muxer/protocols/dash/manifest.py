"""DASH MPD 解析与分片计划展开。

只覆盖点播常见形态：SegmentTemplate（含 SegmentTimeline / 固定时长）、
SegmentList、以及单文件 BaseURL 回退。多 Period 取第一个。
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urljoin

ISO_DURATION_RE = re.compile(
    r"^P(?:(\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$"
)
TEMPLATE_RE = re.compile(r"\$(\w+)(?:%0(\d+)d)?\$")


class ManifestError(ValueError):
    """MPD 不合法或包含不可支持的形态。"""


@dataclass(frozen=True)
class TimelineSpec:
    """一个 <S> 元素；time 为空表示延续上一段。"""

    duration: int
    repeat: int = 0
    time: int | None = None


@dataclass(frozen=True)
class TemplateSpec:
    """SegmentTemplate 的合并结果。"""

    media: str = ""
    initialization: str = ""
    duration: int = 0
    timescale: int | None = None
    start_number: int | None = None
    timeline: tuple[TimelineSpec, ...] = ()

    def merged(self, override: TemplateSpec | None) -> TemplateSpec:
        """层级覆盖：子层非空字段优先。"""
        if override is None:
            return self
        return TemplateSpec(
            media=override.media or self.media,
            initialization=override.initialization or self.initialization,
            duration=override.duration or self.duration,
            timescale=(
                override.timescale if override.timescale is not None else self.timescale
            ),
            start_number=(
                override.start_number if override.start_number is not None else self.start_number
            ),
            timeline=override.timeline or self.timeline,
        )


@dataclass(frozen=True)
class SegmentListSpec:
    """SegmentList 的简化形态。"""

    initialization: str = ""
    media: tuple[str, ...] = ()
    duration: int = 0
    timescale: int = 1


@dataclass
class Representation:
    """一个码率表示。"""

    id: str
    bandwidth: int = 0
    width: int = 0
    height: int = 0
    codecs: str = ""
    mime_type: str = ""
    base_url: str = ""
    has_base_url: bool = False
    template: TemplateSpec | None = None
    segment_list: SegmentListSpec | None = None
    drm: bool = False


@dataclass
class AdaptationSet:
    """一组同类型媒体。"""

    content_type: str
    mime_type: str
    codecs: str
    lang: str
    representations: list[Representation]
    base_url: str = ""
    template: TemplateSpec | None = None
    segment_list: SegmentListSpec | None = None
    drm: bool = False


@dataclass
class Period:
    """MPD 周期；M4 只使用第一个。"""

    id: str
    duration: float | None
    base_url: str
    adaptations: list[AdaptationSet]
    template: TemplateSpec | None = None


@dataclass
class DashManifest:
    """解析后的 MPD。"""

    url: str
    type: str
    duration: float | None
    base_url: str
    periods: list[Period]
    template: TemplateSpec | None = None


@dataclass(frozen=True)
class SegmentPlan:
    """可直接下载的分片计划。"""

    init_urls: tuple[str, ...]
    media_urls: tuple[str, ...]
    is_fmp4: bool
    mime_type: str
    total_duration: float


# ---- 通用工具 ----


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local(child.tag) == name]


def _find(element: ET.Element, name: str) -> ET.Element | None:
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def _int(element: ET.Element | None, name: str, default: int = 0) -> int:
    if element is None:
        return default
    value = element.get(name)
    if value is None:
        return default
    try:
        return int(float(value))
    except ValueError:
        return default


def parse_iso_duration(value: str | None) -> float | None:
    """解析 ``PT1H2M3.5S`` 形式的时长。"""
    if not value:
        return None
    match = ISO_DURATION_RE.match(value.strip())
    if not match:
        return None
    days, hours, minutes, seconds = (float(part) if part else 0.0 for part in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _base_text(element: ET.Element) -> str:
    node = _find(element, "BaseURL")
    if node is None or not node.text:
        return ""
    return node.text.strip()


def _has_drm(element: ET.Element) -> bool:
    return any(_local(child.tag) == "ContentProtection" for child in element.iter())


def expand_template(template: str, values: dict[str, object]) -> str:
    """展开 ``$Number%05d$`` 一类的模板占位符。"""
    if not template:
        return template

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        width = match.group(2)
        if name not in values:
            raise ManifestError(f"模板占位符暂不支持: ${name}$")
        value = values[name]
        if width and isinstance(value, int):
            return f"{value:0{int(width)}d}"
        return str(value)

    return TEMPLATE_RE.sub(replace, template)


def _template_from(element: ET.Element) -> TemplateSpec | None:
    node = _find(element, "SegmentTemplate")
    if node is None:
        return None
    timeline: list[TimelineSpec] = []
    timeline_node = _find(node, "SegmentTimeline")
    if timeline_node is not None:
        previous_end: int | None = None
        for entry in _children(timeline_node, "S"):
            duration = _int(entry, "d")
            if duration <= 0:
                raise ManifestError("SegmentTimeline 的 S@d 必须为正数")
            start = _int(entry, "t", previous_end if previous_end is not None else 0)
            repeat = _int(entry, "r", 0)
            timeline.append(TimelineSpec(duration=duration, repeat=repeat, time=start))
            previous_end = start + duration * (repeat + 1 if repeat > 0 else 1)
    return TemplateSpec(
        media=node.get("media", ""),
        initialization=node.get("initialization", ""),
        duration=_int(node, "duration"),
        timescale=_int(node, "timescale") if node.get("timescale") else None,
        start_number=_int(node, "startNumber") if node.get("startNumber") else None,
        timeline=tuple(timeline),
    )


def _segment_list_from(element: ET.Element) -> SegmentListSpec | None:
    node = _find(element, "SegmentList")
    if node is None:
        return None
    initialization = ""
    init_node = _find(node, "Initialization")
    if init_node is not None:
        initialization = init_node.get("sourceURL", "")
    media = tuple(
        url.get("media", "") for url in _children(node, "SegmentURL") if url.get("media")
    )
    return SegmentListSpec(
        initialization=initialization,
        media=media,
        duration=_int(node, "duration"),
        timescale=max(1, _int(node, "timescale", 1)),
    )


def _expand_timeline(
    specs: tuple[TimelineSpec, ...], period_ticks: int | None
) -> list[tuple[int, int]]:
    """把 <S> 序列展开为 (time, duration) 列表，支持 r=-1。"""
    entries: list[tuple[int, int]] = []
    current = 0
    for index, spec in enumerate(specs):
        start = spec.time if spec.time is not None else current
        repeat = spec.repeat
        if repeat < 0:
            next_start = period_ticks
            if index + 1 < len(specs) and specs[index + 1].time is not None:
                next_start = specs[index + 1].time
            if next_start is None or next_start <= start:
                repeat = 0
            else:
                repeat = max(0, math.ceil((next_start - start) / spec.duration) - 1)
        for offset in range(repeat + 1):
            entries.append((start + offset * spec.duration, spec.duration))
        current = start + (repeat + 1) * spec.duration
    return entries


# ---- MPD 解析 ----


def parse_manifest(text: str, url: str) -> DashManifest:
    """解析 MPD 文本并解析出各层 URL 与模板。"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ManifestError(f"MPD XML 解析失败：{exc}") from exc
    if _local(root.tag) != "MPD":
        raise ManifestError("根节点不是 MPD")

    mpd_base = urljoin(url, _base_text(root))
    duration = parse_iso_duration(root.get("mediaPresentationDuration"))
    manifest_type = root.get("type", "static")
    manifest_template = _template_from(root)

    periods: list[Period] = []
    for period_node in _children(root, "Period"):
        period_base = urljoin(mpd_base, _base_text(period_node))
        period_duration = parse_iso_duration(period_node.get("duration")) or duration
        period_template = _template_from(period_node) or manifest_template

        adaptations: list[AdaptationSet] = []
        for adaptation_node in _children(period_node, "AdaptationSet"):
            adaptation_base = urljoin(period_base, _base_text(adaptation_node))
            adaptation_template = _template_from(adaptation_node) or period_template
            adaptation_list = _segment_list_from(adaptation_node)
            adaptation_drm = _has_drm(adaptation_node)
            adaptation_mime = adaptation_node.get("mimeType", "")
            adaptation_codecs = adaptation_node.get("codecs", "")
            content_type = adaptation_node.get("contentType", "")
            if not content_type and adaptation_mime:
                content_type = adaptation_mime.split("/", 1)[0]

            representations: list[Representation] = []
            for rep_node in _children(adaptation_node, "Representation"):
                rep_base_text = _base_text(rep_node)
                rep_base = urljoin(adaptation_base, rep_base_text)
                rep_template = _template_from(rep_node)
                merged_template = (
                    adaptation_template.merged(rep_template)
                    if adaptation_template
                    else rep_template
                )
                rep_mime = rep_node.get("mimeType", adaptation_mime)
                if not content_type and rep_mime:
                    content_type = rep_mime.split("/", 1)[0]
                representations.append(
                    Representation(
                        id=rep_node.get("id", ""),
                        bandwidth=_int(rep_node, "bandwidth"),
                        width=_int(rep_node, "width", _int(adaptation_node, "width")),
                        height=_int(rep_node, "height", _int(adaptation_node, "height")),
                        codecs=rep_node.get("codecs", adaptation_codecs),
                        mime_type=rep_mime,
                        base_url=rep_base,
                        has_base_url=bool(rep_base_text),
                        template=merged_template,
                        segment_list=_segment_list_from(rep_node) or adaptation_list,
                        drm=adaptation_drm or _has_drm(rep_node),
                    )
                )

            adaptations.append(
                AdaptationSet(
                    content_type=content_type,
                    mime_type=adaptation_mime,
                    codecs=adaptation_codecs,
                    lang=adaptation_node.get("lang", ""),
                    representations=representations,
                    base_url=adaptation_base,
                    template=adaptation_template,
                    segment_list=adaptation_list,
                    drm=adaptation_drm,
                )
            )

        periods.append(
            Period(
                id=period_node.get("id", ""),
                duration=period_duration,
                base_url=period_base,
                adaptations=adaptations,
                template=period_template,
            )
        )

    if not periods:
        raise ManifestError("MPD 中没有 Period")
    return DashManifest(
        url=url,
        type=manifest_type,
        duration=duration,
        base_url=mpd_base,
        periods=periods,
        template=manifest_template,
    )


def build_plan(
    manifest: DashManifest,
    period: Period,
    adaptation: AdaptationSet,
    representation: Representation,
) -> SegmentPlan | None:
    """把模板 / 列表 / BaseURL 展开成可下载的 URL 列表。"""
    base = (
        representation.base_url
        or adaptation.base_url
        or period.base_url
        or manifest.base_url
    )
    template = representation.template

    if template is not None and template.media:
        values_base = {
            "RepresentationID": representation.id,
            "Bandwidth": representation.bandwidth,
        }
        init_urls: tuple[str, ...] = ()
        if template.initialization:
            init_urls = (urljoin(base, expand_template(template.initialization, values_base)),)

        timescale = max(1, template.timescale or 1)
        period_ticks = int(period.duration * timescale) if period.duration else None
        if template.timeline:
            entries = _expand_timeline(template.timeline, period_ticks)
            timing = [(time, index) for index, (time, _duration) in enumerate(entries)]
        else:
            if template.duration <= 0 or not period.duration:
                raise ManifestError("SegmentTemplate 缺少 duration 或周期时长")
            segment_seconds = template.duration / timescale
            count = max(1, math.ceil(period.duration / segment_seconds - 1e-9))
            timing = [(index * template.duration, index) for index in range(count)]

        media_urls = tuple(
            urljoin(
                base,
                expand_template(
                    template.media,
                    {
                        **values_base,
                        "Number": (template.start_number or 1) + index,
                        "Time": time,
                    },
                ),
            )
            for time, index in timing
        )
        total_duration = (
            sum(duration for _, duration in _expand_timeline(template.timeline, period_ticks))
            / timescale
            if template.timeline
            else (period.duration or 0.0)
        )
        return SegmentPlan(
            init_urls=init_urls,
            media_urls=media_urls,
            is_fmp4=bool(template.initialization),
            mime_type=representation.mime_type or adaptation.mime_type,
            total_duration=total_duration,
        )

    segment_list = representation.segment_list
    if segment_list is not None and segment_list.media:
        init_urls = (
            (urljoin(base, segment_list.initialization),)
            if segment_list.initialization
            else ()
        )
        media_urls = tuple(urljoin(base, media) for media in segment_list.media)
        return SegmentPlan(
            init_urls=init_urls,
            media_urls=media_urls,
            is_fmp4=bool(init_urls),
            mime_type=representation.mime_type or adaptation.mime_type,
            total_duration=period.duration or 0.0,
        )

    if representation.has_base_url:
        is_fmp4 = representation.base_url.lower().endswith((".mp4", ".m4s", ".m4a"))
        return SegmentPlan(
            init_urls=(),
            media_urls=(representation.base_url,),
            is_fmp4=is_fmp4,
            mime_type=representation.mime_type or adaptation.mime_type,
            total_duration=period.duration or 0.0,
        )
    return None