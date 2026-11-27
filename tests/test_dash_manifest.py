"""DASH MPD 解析与分片计划展开的单元测试。"""

from __future__ import annotations

import unittest

from muxer.protocols.dash.manifest import (
    ManifestError,
    build_plan,
    expand_template,
    parse_iso_duration,
    parse_manifest,
)

BASE_URL = "https://cdn.example.com/vod/manifest.mpd"


def wrap(period_body: str, duration: str = "PT6S", mpd_type: str = "static") -> str:
    return (
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" '
        f'type="{mpd_type}" mediaPresentationDuration="{duration}">'
        f"<Period id=\"0\">{period_body}</Period></MPD>"
    )


class TemplateExpansionTests(unittest.TestCase):
    def test_expand_number_with_width(self) -> None:
        value = expand_template(
            "chunk-$RepresentationID$-$Number%05d$.m4s",
            {"RepresentationID": "v1", "Number": 12},
        )
        self.assertEqual(value, "chunk-v1-00012.m4s")

    def test_unknown_placeholder(self) -> None:
        with self.assertRaises(ManifestError):
            expand_template("$Unknown$", {})

    def test_iso_duration(self) -> None:
        self.assertAlmostEqual(parse_iso_duration("PT1H2M3.5S") or 0, 3723.5)
        self.assertAlmostEqual(parse_iso_duration("PT6.024S") or 0, 6.024)
        self.assertIsNone(parse_iso_duration("oops"))


class ManifestTests(unittest.TestCase):
    def test_duration_based_template(self) -> None:
        text = wrap(
            """
            <AdaptationSet contentType="video" mimeType="video/mp4" codecs="avc1.640028">
              <Representation id="v1" bandwidth="2000000" width="1280" height="720">
                <SegmentTemplate timescale="1000" duration="2000" startNumber="1"
                  media="seg-$RepresentationID$-$Number%05d$.m4s"
                  initialization="init-$RepresentationID$.mp4"/>
              </Representation>
            </AdaptationSet>
            """
        )
        manifest = parse_manifest(text, BASE_URL)
        period = manifest.periods[0]
        adaptation = period.adaptations[0]
        representation = adaptation.representations[0]
        plan = build_plan(manifest, period, adaptation, representation)

        self.assertIsNotNone(plan)
        self.assertTrue(plan.is_fmp4)
        self.assertEqual(
            plan.init_urls, ("https://cdn.example.com/vod/init-v1.mp4",)
        )
        self.assertEqual(len(plan.media_urls), 3)
        self.assertEqual(
            plan.media_urls[0], "https://cdn.example.com/vod/seg-v1-00001.m4s"
        )
        self.assertEqual(representation.width, 1280)
        self.assertAlmostEqual(plan.total_duration, 6.0)

    def test_timeline_with_repeat_and_time(self) -> None:
        text = wrap(
            """
            <AdaptationSet contentType="video" mimeType="video/mp4">
              <Representation id="v1" bandwidth="1000000">
                <SegmentTemplate timescale="1000" startNumber="5"
                  media="chunk-$Time$-$Number$.m4s">
                  <SegmentTimeline>
                    <S t="0" d="2000" r="2"/>
                  </SegmentTimeline>
                </SegmentTemplate>
              </Representation>
            </AdaptationSet>
            """
        )
        manifest = parse_manifest(text, BASE_URL)
        plan = build_plan(
            manifest,
            manifest.periods[0],
            manifest.periods[0].adaptations[0],
            manifest.periods[0].adaptations[0].representations[0],
        )
        self.assertEqual(
            plan.media_urls,
            (
                "https://cdn.example.com/vod/chunk-0-5.m4s",
                "https://cdn.example.com/vod/chunk-2000-6.m4s",
                "https://cdn.example.com/vod/chunk-4000-7.m4s",
            ),
        )

    def test_timeline_repeat_until_end(self) -> None:
        text = wrap(
            """
            <AdaptationSet contentType="video" mimeType="video/mp4">
              <Representation id="v1" bandwidth="1000000">
                <SegmentTemplate timescale="1000" media="chunk-$Number$.m4s">
                  <SegmentTimeline>
                    <S t="0" d="1000" r="2"/>
                    <S t="3000" d="1000" r="-1"/>
                  </SegmentTimeline>
                </SegmentTemplate>
              </Representation>
            </AdaptationSet>
            """
        )
        manifest = parse_manifest(text, BASE_URL)
        plan = build_plan(
            manifest,
            manifest.periods[0],
            manifest.periods[0].adaptations[0],
            manifest.periods[0].adaptations[0].representations[0],
        )
        self.assertEqual(len(plan.media_urls), 6)
        self.assertEqual(plan.media_urls[-1], "https://cdn.example.com/vod/chunk-6.m4s")

    def test_inheritance_and_base_urls(self) -> None:
        text = (
            '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" '
            'mediaPresentationDuration="PT4S"><BaseURL>https://cdn.example.com/root/</BaseURL>'
            '<Period id="0"><BaseURL>p0/</BaseURL>'
            '<AdaptationSet contentType="video" mimeType="video/mp4"><BaseURL>video/</BaseURL>'
            '<SegmentTemplate timescale="1000" duration="2000" media="seg-$Number$.m4s"/>'
            '<Representation id="v1" bandwidth="900000"><BaseURL>v1/</BaseURL>'
            '<SegmentTemplate initialization="custom-$RepresentationID$.mp4"/>'
            "</Representation></AdaptationSet></Period></MPD>"
        )
        manifest = parse_manifest(text, BASE_URL)
        period = manifest.periods[0]
        adaptation = period.adaptations[0]
        representation = adaptation.representations[0]
        plan = build_plan(manifest, period, adaptation, representation)
        self.assertEqual(
            plan.init_urls,
            ("https://cdn.example.com/root/p0/video/v1/custom-v1.mp4",),
        )
        self.assertEqual(
            plan.media_urls[0], "https://cdn.example.com/root/p0/video/v1/seg-1.m4s"
        )
        self.assertEqual(len(plan.media_urls), 2)

    def test_segment_list(self) -> None:
        text = wrap(
            """
            <AdaptationSet contentType="audio" mimeType="audio/mp4">
              <Representation id="a1" bandwidth="128000">
                <SegmentList timescale="1000" duration="2000">
                  <Initialization sourceURL="audio-init.mp4"/>
                  <SegmentURL media="a1.m4s"/>
                  <SegmentURL media="a2.m4s"/>
                </SegmentList>
              </Representation>
            </AdaptationSet>
            """
        )
        manifest = parse_manifest(text, BASE_URL)
        period = manifest.periods[0]
        adaptation = period.adaptations[0]
        plan = build_plan(manifest, period, adaptation, adaptation.representations[0])
        self.assertEqual(plan.media_urls, (
            "https://cdn.example.com/vod/a1.m4s",
            "https://cdn.example.com/vod/a2.m4s",
        ))
        self.assertEqual(plan.init_urls, ("https://cdn.example.com/vod/audio-init.mp4",))

    def test_single_file_base_url(self) -> None:
        text = wrap(
            """
            <AdaptationSet contentType="video" mimeType="video/mp4">
              <Representation id="v1" bandwidth="1000">
                <BaseURL>movie.mp4</BaseURL>
              </Representation>
            </AdaptationSet>
            """
        )
        manifest = parse_manifest(text, BASE_URL)
        period = manifest.periods[0]
        adaptation = period.adaptations[0]
        plan = build_plan(manifest, period, adaptation, adaptation.representations[0])
        self.assertEqual(plan.media_urls, ("https://cdn.example.com/vod/movie.mp4",))
        self.assertEqual(plan.init_urls, ())
        self.assertTrue(plan.is_fmp4)

    def test_drm_detection(self) -> None:
        text = wrap(
            """
            <AdaptationSet contentType="video" mimeType="video/mp4">
              <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/>
              <Representation id="v1" bandwidth="1000">
                <BaseURL>movie.mp4</BaseURL>
              </Representation>
            </AdaptationSet>
            """
        )
        manifest = parse_manifest(text, BASE_URL)
        self.assertTrue(manifest.periods[0].adaptations[0].drm)
        self.assertTrue(manifest.periods[0].adaptations[0].representations[0].drm)

    def test_dynamic_manifest(self) -> None:
        text = wrap(
            """
            <AdaptationSet contentType="video" mimeType="video/mp4">
              <Representation id="v1" bandwidth="1000">
                <SegmentTemplate timescale="1000" media="chunk-$Number$.m4s"
                  initialization="init.mp4">
                  <SegmentTimeline><S t="0" d="1000" r="2"/></SegmentTimeline>
                </SegmentTemplate>
              </Representation>
            </AdaptationSet>
            """,
            duration="PT0S",
            mpd_type="dynamic",
        )
        manifest = parse_manifest(text, BASE_URL)
        self.assertEqual(manifest.type, "dynamic")

    def test_invalid_mpd(self) -> None:
        with self.assertRaises(ManifestError):
            parse_manifest("<not-mpd/>", BASE_URL)
        with self.assertRaises(ManifestError):
            parse_manifest("<MPD xmlns='urn:mpeg:dash:schema:mpd:2011'/>", BASE_URL)


if __name__ == "__main__":
    unittest.main()