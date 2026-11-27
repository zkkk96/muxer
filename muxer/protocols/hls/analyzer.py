"""HLS 探测：拉取并解析播放列表，判断变体、时长、加密与直播。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from muxer.core.http import HttpClient
from muxer.core.models import VariantOption
from muxer.protocols.hls.playlist import (
    MasterPlaylist,
    MediaPlaylist,
    PlaylistError,
    Rendition,
    Variant,
    parse_playlist,
    select_rendition,
)
from muxer.protocols.naming import title_from_url

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class HlsAnalysis:
    """探测结果，供 scheduler 组装 ProbeResult。"""

    url: str
    title: str
    master: MasterPlaylist | None
    variant: Variant | None
    media: MediaPlaylist
    audio: Rendition | None
    audio_playlist: MediaPlaylist | None
    variants: tuple[VariantOption, ...]

    @property
    def live(self) -> bool:
        return self.media.live or bool(self.audio_playlist and self.audio_playlist.live)

    @property
    def encrypted(self) -> bool:
        return self.media.encrypted or bool(self.audio_playlist and self.audio_playlist.encrypted)

    @property
    def sample_aes(self) -> bool:
        return self.media.sample_aes or bool(self.audio_playlist and self.audio_playlist.sample_aes)

    @property
    def duration(self) -> float:
        return self.media.duration


async def analyze(
    http: HttpClient,
    url: str,
    headers: dict[str, str] | None = None,
    preferred_variant: str = "",
) -> HlsAnalysis:
    """解析 URL 指向的主 / 媒体播放列表；可指定优先变体。"""
    headers = headers or {}
    text = await http.fetch_text(url, headers)
    parsed = parse_playlist(text, url)

    if isinstance(parsed, MediaPlaylist):
        return HlsAnalysis(
            url=url,
            title=title_from_url(url),
            master=None,
            variant=None,
            media=parsed,
            audio=None,
            audio_playlist=None,
            variants=(VariantOption(uri=url, label="源播放列表"),),
        )

    master = parsed
    variant, media = await _load_any_variant(http, master, headers, preferred_variant)
    audio: Rendition | None = None
    audio_playlist: MediaPlaylist | None = None
    audio = select_rendition(master, variant.audio_group) if master.renditions else None
    if audio is not None:
        try:
            audio_playlist = await _load_media(http, audio.uri, headers)
        except (PlaylistError, OSError) as exc:
            LOGGER.warning("备用音轨解析失败，忽略: %s", exc)
            audio = None

    variants = tuple(
        VariantOption(
            uri=item.uri,
            label=item.label,
            bandwidth=item.bandwidth,
            resolution=item.resolution,
            codecs=item.codecs,
        )
        for item in master.variants
    )
    return HlsAnalysis(
        url=url,
        title=title_from_url(url),
        master=master,
        variant=variant,
        media=media,
        audio=audio,
        audio_playlist=audio_playlist,
        variants=variants,
    )


async def _load_any_variant(
    http: HttpClient,
    master: MasterPlaylist,
    headers: dict[str, str],
    preferred_variant: str = "",
) -> tuple[Variant, MediaPlaylist]:
    """优先尝试指定变体，其次按码率从高到低尝试。"""
    ordered = sorted(master.variants, key=lambda item: item.bandwidth, reverse=True)
    if preferred_variant:
        preferred = [item for item in ordered if item.uri == preferred_variant]
        if preferred:
            ordered = preferred + [item for item in ordered if item.uri != preferred_variant]
    last_error: Exception | None = None
    for variant in ordered:
        try:
            return variant, await _load_media(http, variant.uri, headers)
        except Exception as exc:  # noqa: BLE001 - 继续尝试下一个变体
            last_error = exc
            LOGGER.warning("变体解析失败，尝试下一个: %s (%s)", variant.uri, exc)
    raise PlaylistError(f"所有变体都无法解析：{last_error}")


async def _load_media(http: HttpClient, url: str, headers: dict[str, str]) -> MediaPlaylist:
    text = await http.fetch_text(url, headers)
    playlist = parse_playlist(text, url)
    if not isinstance(playlist, MediaPlaylist):
        raise PlaylistError("期望媒体播放列表，实际拿到主播放列表")
    return playlist


def estimate_bytes(analysis: HlsAnalysis) -> int | None:
    """按码率 × 时长估算总大小，仅用于界面提示。"""
    duration = analysis.duration
    if duration <= 0:
        return None
    bitrate = analysis.variant.bandwidth if analysis.variant else 0
    if not bitrate:
        return None
    audio_bitrate = 128_000 if analysis.audio_playlist else 0
    return int((bitrate + audio_bitrate) * duration / 8)