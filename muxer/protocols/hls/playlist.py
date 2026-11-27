"""HLS 播放列表解析（master / media）。

纯文本解析，不做任何 IO，便于用固定样本做回归测试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from muxer.protocols.naming import variant_label as format_variant_label

ATTRIBUTE_RE = re.compile(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))')

class PlaylistError(ValueError):
    """播放列表格式不合法。"""


@dataclass(frozen=True)
class ByteRange:
    """EXT-X-BYTERANGE 描述的范围。"""

    length: int
    offset: int = 0

    @property
    def end(self) -> int:
        return self.offset + self.length - 1


@dataclass(frozen=True)
class KeyInfo:
    """EXT-X-KEY。IV 为 None 时由分片序号推导。"""

    method: str
    uri: str = ""
    iv: bytes | None = None
    keyformat: str = "identity"


@dataclass(frozen=True)
class InitSection:
    """EXT-X-MAP 初始化段（fMP4）。"""

    uri: str
    byterange: ByteRange | None = None


@dataclass(frozen=True)
class Segment:
    """一个媒体分片。"""

    index: int
    uri: str
    duration: float
    sequence: int
    byterange: ByteRange | None = None
    key: KeyInfo | None = None
    init: InitSection | None = None
    discontinuity: bool = False


@dataclass
class MediaPlaylist:
    """媒体播放列表。"""

    url: str
    target_duration: float
    media_sequence: int
    segments: list[Segment]
    endlist: bool
    playlist_type: str = ""

    @property
    def live(self) -> bool:
        return not self.endlist

    @property
    def duration(self) -> float:
        return sum(segment.duration for segment in self.segments)

    @property
    def has_map(self) -> bool:
        return any(segment.init is not None for segment in self.segments)

    @property
    def encrypted(self) -> bool:
        return any(segment.key is not None for segment in self.segments)

    @property
    def sample_aes(self) -> bool:
        return any(
            segment.key is not None and segment.key.method.upper() != "AES-128"
            for segment in self.segments
        )


@dataclass(frozen=True)
class Variant:
    """主播放列表里的码率变体。"""

    uri: str
    bandwidth: int = 0
    average_bandwidth: int = 0
    resolution: str = ""
    codecs: str = ""
    audio_group: str = ""
    subtitle_group: str = ""
    frame_rate: str = ""

    @property
    def label(self) -> str:
        return variant_label(self)


@dataclass(frozen=True)
class Rendition:
    """EXT-X-MEDIA 描述的备用音轨 / 字幕。"""

    type: str
    group_id: str
    name: str = ""
    language: str = ""
    uri: str = ""
    default: bool = False
    autoselect: bool = False


@dataclass
class MasterPlaylist:
    """主播放列表。"""

    url: str
    variants: list[Variant]
    renditions: list[Rendition] = field(default_factory=list)


def parse_attributes(payload: str) -> dict[str, str]:
    """解析 HLS 属性列表，正确处理引号内的逗号。"""
    attributes: dict[str, str] = {}
    for match in ATTRIBUTE_RE.finditer(payload):
        key = match.group(1)
        value = match.group(2) if match.group(2) is not None else match.group(3)
        attributes[key] = value.strip()
    return attributes


def parse_byterange(value: str, default_offset: int = 0) -> ByteRange | None:
    """解析 ``length[@offset]``。"""
    value = value.strip()
    if not value:
        return None
    try:
        if "@" in value:
            length_text, offset_text = value.split("@", 1)
            return ByteRange(int(length_text), int(offset_text))
        return ByteRange(int(value), default_offset)
    except ValueError as exc:
        raise PlaylistError(f"EXT-X-BYTERANGE 格式错误: {value}") from exc


def _parse_iv(value: str | None) -> bytes | None:
    """把 0x 开头的十六进制 IV 补齐到 16 字节。"""
    if not value:
        return None
    text = value.strip()
    if text.lower().startswith("0x"):
        text = text[2:]
    if not text:
        return None
    text = text.rjust(32, "0")
    try:
        return bytes.fromhex(text)
    except ValueError:
        return None


def _int(value: str | None, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _float(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def parse_playlist(text: str, url: str) -> MasterPlaylist | MediaPlaylist:
    """根据内容判定主 / 媒体播放列表并解析。"""
    lines = text.lstrip("﻿").splitlines()
    if any(line.strip().startswith("#EXT-X-STREAM-INF") for line in lines):
        return _parse_master(lines, url)
    return _parse_media(lines, url)


def _parse_master(lines: list[str], url: str) -> MasterPlaylist:
    variants: list[Variant] = []
    renditions: list[Rendition] = []
    pending: dict[str, str] | None = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = parse_attributes(line.split(":", 1)[1])
            renditions.append(
                Rendition(
                    type=attrs.get("TYPE", "").upper(),
                    group_id=attrs.get("GROUP-ID", ""),
                    name=attrs.get("NAME", ""),
                    language=attrs.get("LANGUAGE", ""),
                    uri=urljoin(url, attrs.get("URI", "")) if attrs.get("URI") else "",
                    default=attrs.get("DEFAULT", "").upper() == "YES",
                    autoselect=attrs.get("AUTOSELECT", "").upper() == "YES",
                )
            )
        elif line.startswith("#EXT-X-STREAM-INF:"):
            pending = parse_attributes(line.split(":", 1)[1])
        elif line.startswith("#"):
            continue
        elif pending is not None:
            variants.append(
                Variant(
                    uri=urljoin(url, line),
                    bandwidth=_int(pending.get("BANDWIDTH")),
                    average_bandwidth=_int(pending.get("AVERAGE-BANDWIDTH")),
                    resolution=pending.get("RESOLUTION", ""),
                    codecs=pending.get("CODECS", ""),
                    audio_group=pending.get("AUDIO", ""),
                    subtitle_group=pending.get("SUBTITLES", ""),
                    frame_rate=pending.get("FRAME-RATE", ""),
                )
            )
            pending = None

    if not variants:
        raise PlaylistError("主播放列表中没有可用变体")
    return MasterPlaylist(url=url, variants=variants, renditions=renditions)


def _parse_media(lines: list[str], url: str) -> MediaPlaylist:
    target_duration = 0.0
    media_sequence = 0
    playlist_type = ""
    endlist = False
    segments: list[Segment] = []

    current_key: KeyInfo | None = None
    current_init: InitSection | None = None
    pending_duration: float | None = None
    pending_range: ByteRange | None = None
    pending_discontinuity = False
    previous_range_end = 0

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            target_duration = _float(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_sequence = _int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-PLAYLIST-TYPE:"):
            playlist_type = line.split(":", 1)[1].strip()
        elif line.startswith("#EXT-X-ENDLIST"):
            endlist = True
        elif line.startswith("#EXT-X-KEY:"):
            attrs = parse_attributes(line.split(":", 1)[1])
            method = attrs.get("METHOD", "NONE").upper()
            if method == "NONE":
                current_key = None
            else:
                key_uri = attrs.get("URI", "")
                current_key = KeyInfo(
                    method=method,
                    uri=urljoin(url, key_uri) if key_uri else "",
                    iv=_parse_iv(attrs.get("IV")),
                    keyformat=attrs.get("KEYFORMAT", "identity"),
                )
        elif line.startswith("#EXT-X-MAP:"):
            attrs = parse_attributes(line.split(":", 1)[1])
            current_init = InitSection(
                uri=urljoin(url, attrs.get("URI", "")),
                byterange=parse_byterange(attrs.get("BYTERANGE", "")),
            )
        elif line.startswith("#EXT-X-BYTERANGE:"):
            pending_range = parse_byterange(line.split(":", 1)[1], previous_range_end)
        elif line.startswith("#EXTINF:"):
            value = line.split(":", 1)[1]
            pending_duration = _float(value.split(",", 1)[0])
        elif line.startswith("#EXT-X-DISCONTINUITY"):
            pending_discontinuity = True
        elif line.startswith("#"):
            continue
        else:
            if pending_duration is None:
                raise PlaylistError(f"分片缺少 EXTINF: {line}")
            segment = Segment(
                index=len(segments),
                uri=urljoin(url, line),
                duration=pending_duration,
                sequence=media_sequence + len(segments),
                byterange=pending_range,
                key=current_key,
                init=current_init,
                discontinuity=pending_discontinuity,
            )
            segments.append(segment)
            if pending_range is not None:
                previous_range_end = pending_range.end + 1
            pending_duration = None
            pending_range = None
            pending_discontinuity = False

    if not segments:
        raise PlaylistError("媒体播放列表中没有分片")
    return MediaPlaylist(
        url=url,
        target_duration=target_duration,
        media_sequence=media_sequence,
        segments=segments,
        endlist=endlist,
        playlist_type=playlist_type,
    )


def select_rendition(master: MasterPlaylist, group_id: str, kind: str = "AUDIO") -> Rendition | None:
    """挑选默认音轨；没有 DEFAULT 时取第一个带 URI 的。"""
    candidates = [
        rendition
        for rendition in master.renditions
        if rendition.type == kind and rendition.uri and (not group_id or rendition.group_id == group_id)
    ]
    if not candidates:
        return None
    for rendition in candidates:
        if rendition.default:
            return rendition
    return candidates[0]


def variant_label(variant: Variant) -> str:
    """生成类似 ``1080P · 5.2 Mbps · H.264`` 的展示标签。"""
    return format_variant_label(variant.resolution, variant.bandwidth, variant.codecs)
