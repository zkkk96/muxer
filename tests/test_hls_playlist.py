"""HLS 播放列表解析的单元测试。"""

from __future__ import annotations

import unittest

from muxer.protocols.hls.playlist import (
    MasterPlaylist,
    MediaPlaylist,
    PlaylistError,
    parse_playlist,
    select_rendition,
)

MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",LANGUAGE="en",DEFAULT=YES,AUTOSELECT=YES,URI="audio/en.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="Japanese",LANGUAGE="ja",URI="audio/ja.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=5000000,AVERAGE-BANDWIDTH=4500000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2",AUDIO="aud"
high/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2",AUDIO="aud"
low/index.m3u8
"""

MEDIA_ENCRYPTED = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:10
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x000102030405060708090a0b0c0d0e0f
#EXT-X-MAP:URI="init.mp4",BYTERANGE="800@0"
#EXTINF:3.5,
#EXT-X-BYTERANGE:1000@800
seg1.m4s
#EXTINF:3.5,
#EXT-X-BYTERANGE:500
seg2.m4s
#EXT-X-ENDLIST
"""

MEDIA_LIVE = """#EXTM3U
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:42
#EXTINF:2.0,
seg42.ts
#EXTINF:2.0,
seg43.ts
"""


class MasterPlaylistTests(unittest.TestCase):
    def test_parse_variants_and_audio(self) -> None:
        playlist = parse_playlist(MASTER, "https://cdn.example.com/vod/master.m3u8")
        self.assertIsInstance(playlist, MasterPlaylist)
        master: MasterPlaylist = playlist  # type: ignore[assignment]
        self.assertEqual(len(master.variants), 2)
        self.assertEqual(master.variants[0].bandwidth, 5_000_000)
        self.assertEqual(master.variants[0].resolution, "1920x1080")
        self.assertEqual(master.variants[0].audio_group, "aud")
        self.assertEqual(
            master.variants[0].uri, "https://cdn.example.com/vod/high/index.m3u8"
        )
        self.assertEqual(master.variants[0].label, "1080P · 5.0 Mbps · H.264")
        self.assertEqual(master.variants[1].label, "720P · 1.2 Mbps · H.264")

        audio = select_rendition(master, "aud")
        self.assertIsNotNone(audio)
        self.assertEqual(audio.name, "English")
        self.assertEqual(
            audio.uri, "https://cdn.example.com/vod/audio/en.m3u8"
        )


class MediaPlaylistTests(unittest.TestCase):
    def test_parse_keys_map_byterange(self) -> None:
        playlist = parse_playlist(MEDIA_ENCRYPTED, "https://cdn.example.com/vod/high/index.m3u8")
        self.assertIsInstance(playlist, MediaPlaylist)
        media: MediaPlaylist = playlist  # type: ignore[assignment]

        self.assertEqual(media.media_sequence, 10)
        self.assertEqual(media.target_duration, 4.0)
        self.assertFalse(media.live)
        self.assertTrue(media.has_map)
        self.assertTrue(media.encrypted)
        self.assertFalse(media.sample_aes)
        self.assertAlmostEqual(media.duration, 7.0)

        first, second = media.segments
        self.assertEqual(first.sequence, 10)
        self.assertEqual(second.sequence, 11)
        self.assertIsNotNone(first.key)
        self.assertEqual(first.key.method, "AES-128")
        self.assertEqual(first.key.uri, "https://cdn.example.com/vod/high/key.bin")
        self.assertEqual(first.key.iv, bytes(range(16)))
        self.assertEqual(first.init.uri, "https://cdn.example.com/vod/high/init.mp4")
        self.assertEqual(first.byterange.offset, 800)
        self.assertEqual(first.byterange.length, 1000)
        self.assertEqual(second.byterange.offset, 1800)

    def test_live_detection(self) -> None:
        playlist = parse_playlist(MEDIA_LIVE, "https://cdn.example.com/live/index.m3u8")
        media: MediaPlaylist = playlist  # type: ignore[assignment]
        self.assertTrue(media.live)
        self.assertEqual(media.segments[0].sequence, 42)

    def test_sample_aes_is_unsupported(self) -> None:
        text = """#EXTM3U
#EXT-X-TARGETDURATION:4
#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key.bin",KEYFORMAT="com.apple.streamingkeydelivery"
#EXTINF:4.0,
seg1.ts
#EXT-X-ENDLIST
"""
        media: MediaPlaylist = parse_playlist(text, "https://x/y.m3u8")  # type: ignore[assignment]
        self.assertTrue(media.sample_aes)

    def test_malformed_playlist(self) -> None:
        with self.assertRaises(PlaylistError):
            parse_playlist("#EXTM3U\n#EXTINF:4.0,\n", "https://x/y.m3u8")


if __name__ == "__main__":
    unittest.main()