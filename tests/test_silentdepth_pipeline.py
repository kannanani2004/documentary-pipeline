"""Behavioral regressions: evidence gates, byte caches, timing, and recovery."""
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import silentdepth_pipeline as p


def wav(path, samples=19201, amplitude=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        out.writeframes(struct.pack("<h", amplitude) * samples)


def fixture(root):
    from PIL import Image, ImageDraw
    (root / "research").mkdir(parents=True)
    (root / "assets").mkdir()
    (root / "research/source.txt").write_text("TEST FIXTURE ONLY. Ocean X's 2011 release reports a 197-foot diameter.", encoding="utf-8")
    for i in range(1, 3):
        im = Image.new("RGB", (1280, 720), "#102b40")
        d = ImageDraw.Draw(im)
        d.ellipse((390, 110, 890, 610), outline="#64cddb", width=8)
        d.text((65, 60), "PIPELINE TEST - illustrative scale diagram", fill="white", font=p.font(30))
        im.save(root / f"assets/test_{i}.png")
        wav(root / f"audio/input_{i}.wav", 19201 + i)
    m = {
        "episode_id": "pipeline_test", "title": "SilentDepth pipeline test", "summary": "A local technical fixture; not a documentary.",
        "packaging_check": "Test fixture uses only a reported source measurement and explicit illustrative framing.",
        "smoke_test": True,
        "sources": [{"id": "s1", "url": "https://www.oceanexplorer.se/press/", "title": "2011 Ocean X press release", "publisher": "Ocean X",
                     "date": "2011", "retrieved": "2026-10-13", "source_type": "primary", "excerpt": "A 197 feet diameter cylinder shaped object",
                     "snapshot": "research/source.txt"}],
        "claims": [{"id": "c1", "text": "Ocean X reported 197 feet in diameter.", "classification": "reported", "source_ids": ["s1"],
                    "support": "The release reports a 197-foot diameter; this establishes the expedition's report.", "status": "checked",
                    "attribution": "Ocean X reported", "measurement_check": "197 feet / 3.28084 is approximately 60 meters. This is a reported diameter, not an independently verified survey."}],
        "beats": [{"id": i, "text": "Ocean X reported an object about two hundred feet across. This is an illustrative pipeline test.",
                   "claim_ids": ["c1"], "scene": "Original circular scale illustration; no sonar, map, or measurement plot.",
                   "visual_kind": "diagram", "asset_id": f"a{i}", "narration_file": f"audio/input_{i}.wav"} for i in range(1, 3)],
        "assets": [{"id": f"a{i}", "file": f"assets/test_{i}.png", "kind": "diagram", "credit": "The Silent Depth - test diagram; Ocean X reported scale",
                    "description": "Original illustrative circle with test marking, not an instrument image.", "subject_check": "Checked original test diagram against scene intent.",
                    "creation_notes": "Original Pillow drawing; circle is illustrative, not measured morphology."} for i in range(1, 3)],
        "chapters": [{"beat": 1, "title": "Test begins"}]
    }
    p.save_json(root / "episode.json", m)
    return m


def reviews(m, root):
    p.review(m, root, "script", "Test source and reported measurement checked; illustrative framing is explicit.")
    p.review(m, root, "assets", "Test drawings checked for scene correspondence; original fixture art, no purported instrument records.")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.m = fixture(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_research_valid(self):
        self.assertEqual(len(p.research_check(self.m, self.root)[1]), 1)

    def test_unchecked_claim_blocks(self):
        self.m["claims"][0]["status"] = "unchecked"
        with self.assertRaisesRegex(p.PipelineError, "Unchecked"):
            p.script_check(self.m, self.root)

    def test_numeric_claim_requires_unit_audit(self):
        del self.m["claims"][0]["measurement_check"]
        with self.assertRaisesRegex(p.PipelineError, "measurement_check"):
            p.script_check(self.m, self.root)

    def test_attribution_required_in_narration(self):
        self.m["beats"][0]["text"] = "Electronics mysteriously died."
        with self.assertRaisesRegex(p.PipelineError, "attribution"):
            p.script_check(self.m, self.root)

    def test_unknown_claim_blocks(self):
        self.m["beats"][0]["claim_ids"] = ["invented"]
        with self.assertRaisesRegex(p.PipelineError, "claim reference"):
            p.script_check(self.m, self.root)

    def test_missing_source_snapshot_blocks(self):
        (self.root / "research/source.txt").unlink()
        with self.assertRaisesRegex(p.PipelineError, "snapshot"):
            p.research_check(self.m, self.root)

    def test_missing_asset_blocks(self):
        (self.root / "assets/test_1.png").unlink()
        with self.assertRaisesRegex(p.PipelineError, "Missing asset"):
            p.assets_check(self.m, self.root)

    def test_wrong_scene_classification_blocks(self):
        self.m["assets"][0]["kind"] = "site_evidence"
        with self.assertRaisesRegex(p.PipelineError, "classification mismatch"):
            p.assets_check(self.m, self.root)

    def test_unclear_license_blocks(self):
        b, a = self.m["beats"][0], self.m["assets"][0]
        b["visual_kind"] = a["kind"] = "site_evidence"
        a.update(source_url="https://example.org/asset", license="unknown", license_url="https://example.org/terms", license_evidence="research/source.txt", rights_checked=True)
        with self.assertRaisesRegex(p.PipelineError, "license"):
            p.assets_check(self.m, self.root)

    def test_generated_visual_requires_inspection(self):
        b, a = self.m["beats"][0], self.m["assets"][0]
        b["visual_kind"] = a["kind"] = "reconstruction"
        a.update(prompt="Atmospheric illustration", generator="test", not_evidence_checked=False)
        with self.assertRaisesRegex(p.PipelineError, "AI asset not reviewed"):
            p.assets_check(self.m, self.root)

    def test_paths_cannot_escape_episode(self):
        with self.assertRaisesRegex(p.PipelineError, "escapes"):
            p.local(self.root, "../elsewhere.txt")

    def test_source_change_invalidates_review(self):
        reviews(self.m, self.root)
        (self.root / "research/source.txt").write_text("Changed evidence", encoding="utf-8")
        with self.assertRaisesRegex(p.PipelineError, "review required"):
            p.require_review(self.m, self.root, "script")

    def test_asset_bytes_invalidate_review(self):
        reviews(self.m, self.root)
        (self.root / "assets/test_1.png").write_bytes(b"changed image")
        with self.assertRaisesRegex(p.PipelineError, "review required"):
            p.require_review(self.m, self.root, "assets")

    def test_audio_uses_samples_without_padding(self):
        reviews(self.m, self.root)
        p.audio(self.m, self.root)
        timeline = p.check_audio(self.m, self.root)
        self.assertEqual(timeline[0]["end_sample"], 19202)
        self.assertEqual(timeline[-1]["end_sample"], 38405)
        self.assertEqual(timeline[-1]["end_frame"], round(38405 * 30 / 48000))
        self.assertAlmostEqual(timeline[0]["duration"], 19202 / 48000)
        self.assertEqual(p.pcm_info(self.root / "audio/voiceover_v2.wav"), 38405)

    def test_edit_regenerates_only_changed_beat(self):
        reviews(self.m, self.root)
        p.audio(self.m, self.root)
        original = p.check_audio(self.m, self.root)
        unchanged = self.root / original[1]["audio_file"]
        stamp = unchanged.stat().st_mtime_ns
        self.m["beats"][0]["text"] += " A changed sentence."
        reviews(self.m, self.root)
        p.audio(self.m, self.root)
        updated = p.check_audio(self.m, self.root)
        self.assertNotEqual(original[0]["audio_file"], updated[0]["audio_file"])
        self.assertEqual(original[1]["audio_file"], updated[1]["audio_file"])
        self.assertEqual(stamp, unchanged.stat().st_mtime_ns)

    def test_voice_change_invalidates_audio(self):
        reviews(self.m, self.root)
        p.audio(self.m, self.root)
        self.m["voice"] = "different-voice"
        with self.assertRaisesRegex(p.PipelineError, "stale"):
            p.check_audio(self.m, self.root)

    def test_corrupt_cached_beat_is_rebuilt(self):
        reviews(self.m, self.root)
        p.audio(self.m, self.root)
        timeline = p.check_audio(self.m, self.root)
        first = self.root / timeline[0]["audio_file"]
        first.write_bytes(b"bad cached audio")
        p.audio(self.m, self.root)
        self.assertEqual(p.pcm_info(first), 19202)

    def test_failure_is_recorded_and_resume_works(self):
        with patch.object(p, "audio", side_effect=p.PipelineError("Interrupted synthesis")):
            with self.assertRaises(p.PipelineError):
                p.run_stage(self.m, self.root, "audio", "1080p", "libx264")
        self.assertEqual(p.read_json(self.root / "pipeline_state.json")["stages"]["audio"]["status"], "failed")
        reviews(self.m, self.root)
        p.run_stage(self.m, self.root, "audio", "1080p", "libx264")
        self.assertEqual(p.read_json(self.root / "pipeline_state.json")["stages"]["audio"]["status"], "complete")

    def test_chapters_are_ordered(self):
        self.m["chapters"] = [{"beat": 2, "title": "Wrong first chapter"}]
        with self.assertRaisesRegex(p.PipelineError, "Chapters"):
            p.script_check(self.m, self.root)

    def test_caption_text_and_chapters_derive_from_timeline(self):
        reviews(self.m, self.root)
        p.audio(self.m, self.root)
        self.m["chapters"].append({"beat": 2, "title": "Second"})
        p.write_materials(self.m, self.root, draft=True)
        text = (self.root / "output/captions.srt").read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if line and not line.isdigit() and " --> " not in line]
        self.assertEqual(" ".join(lines), " ".join(b["text"] for b in self.m["beats"]))
        self.assertIn("0:00 - Second", (self.root / "output/upload_package_draft.md").read_text(encoding="utf-8"))

    def test_download_rejects_insecure_url(self):
        with self.assertRaisesRegex(p.PipelineError, "HTTPS"):
            p.download(self.root, "http://example.org/asset", "assets/no.jpg")

    def test_final_review_needs_current_qa(self):
        with self.assertRaisesRegex(p.PipelineError, "QA report"):
            p.review(self.m, self.root, "final", "A review note without any actual current passing QA report.")

    def test_encoder_failure_preserves_previous_movie(self):
        reviews(self.m, self.root)
        p.audio(self.m, self.root)
        target = self.root / "output/master_1080p.mp4"
        target.parent.mkdir()
        target.write_bytes(b"previous completed export")
        with patch.object(p.subprocess, "Popen", side_effect=p.PipelineError("Encoder unavailable")):
            with self.assertRaisesRegex(p.PipelineError, "Encoder unavailable"):
                p.run_stage(self.m, self.root, "render", "1080p", "libx264")
        self.assertEqual(target.read_bytes(), b"previous completed export")
        self.assertEqual(p.read_json(self.root / "pipeline_state.json")["stages"]["render"]["status"], "failed")

    def test_tampered_timeline_blocks(self):
        reviews(self.m, self.root)
        p.audio(self.m, self.root)
        timeline = p.read_json(self.root / "audio/timeline.json")
        timeline[1]["start_frame"] += 10
        p.save_json(self.root / "audio/timeline.json", timeline)
        with self.assertRaisesRegex(p.PipelineError, "frame mismatch"):
            p.check_audio(self.m, self.root)

    def test_current_reviews_required_before_render(self):
        with self.assertRaisesRegex(p.PipelineError, "script review required"):
            p.render(self.m, self.root)


    def strict_policy(self):
        self.m["editorial_policy"] = {"version": 1, "prevent_unplanned_reuse": True,
                                      "intentional_repeats": []}

    def alignment(self):
        timeline = p.check_audio(self.m, self.root)
        cues = [{"start": t["start_time"], "end": t["end_time"], "text": t["text"]} for t in timeline]
        self.m["captions"] = {"alignment_file": "research/alignment.json"}
        data = {"input_key": p.caption_input_key(self.m, self.root), "cues_sha256": p.digest(cues),
                "method": "Reviewed fixture timing", "cues": cues}
        p.save_json(self.root / "research/alignment.json", data)
        return data

    def test_policy_rejects_renamed_identical_still(self):
        self.strict_policy()
        self.assertEqual(p.file_hash(self.root / "assets/test_1.png"), p.file_hash(self.root / "assets/test_2.png"))
        with self.assertRaisesRegex(p.PipelineError, "Unplanned visual reuse"):
            p.assets_check(self.m, self.root)

    def test_policy_allows_declared_bookend_and_reports_it(self):
        self.strict_policy()
        self.m["editorial_policy"]["intentional_repeats"] = [
            {"asset_id": "a1", "beats": [1, "last"], "reason": "Return to the opening view for comparison."}]
        p.assets_check(self.m, self.root)
        result = p.visual_reuse_audit(self.m, self.root)
        self.assertTrue(result["repeats"][0]["intentional"])
        self.assertEqual(result["unplanned_count"], 0)

    def test_policy_repeat_list_must_match_actual_still_uses(self):
        import copy
        self.strict_policy()
        beat = copy.deepcopy(self.m["beats"][0])
        beat["id"] = 3
        self.m["beats"].append(beat)
        self.m["editorial_policy"]["intentional_repeats"] = [
            {"asset_id": "a1", "beats": [1, 2], "reason": "Only these two appearances were planned."}]
        with self.assertRaisesRegex(p.PipelineError, "Unplanned visual reuse"):
            p.assets_check(self.m, self.root)

    def video_sources(self, second_offset=10.5, different_bytes=False):
        for i, a in enumerate(self.m["assets"]):
            a["file"] = f"assets/video_{i}.mp4"
            (self.root / a["file"]).write_bytes(b"source video" + (bytes([i]) if different_bytes else b""))
            a.update(source_id="original-dive", source_start_seconds=10 if not i else second_offset)
        return [{"start_frame": 0, "end_frame": 15}, {"start_frame": 15, "end_frame": 30}]

    def test_same_video_nonoverlapping_intervals_are_valid(self):
        self.strict_policy()
        timeline = self.video_sources()
        result = p.visual_reuse_audit(self.m, self.root, timeline)
        self.assertEqual(result["unplanned_count"], 0)
        self.assertTrue(result["video_intervals_checked"])

    def test_overlap_uses_rendered_duration_and_canonical_source_offsets(self):
        self.strict_policy()
        timeline = self.video_sources(second_offset=10.4, different_bytes=True)
        with self.assertRaisesRegex(p.PipelineError, "overlapping_source_video"):
            p.visual_reuse_audit(self.m, self.root, timeline)
        self.m["editorial_policy"]["prevent_unplanned_reuse"] = False
        self.assertAlmostEqual(p.visual_reuse_audit(self.m, self.root, timeline)["repeats"][0]["overlap_seconds"], .1)

    def test_video_interval_includes_local_playback_offset(self):
        self.strict_policy()
        timeline = self.video_sources(second_offset=10)
        self.m["assets"][1]["start_seconds"] = .5
        self.assertEqual(p.visual_reuse_audit(self.m, self.root, timeline)["unplanned_count"], 0)

    def test_render_version_does_not_invalidate_narration(self):
        reviews(self.m, self.root)
        p.audio(self.m, self.root)
        key = p.audio_hash(self.m, self.root)
        before = p.check_audio(self.m, self.root)
        with patch.object(p, "VERSION", "new-render-version"):
            self.assertEqual(p.audio_hash(self.m, self.root), key)
            self.assertEqual(p.check_audio(self.m, self.root), before)

    def test_opening_timing_gate_checks_measured_start(self):
        self.m["editorial_policy"] = {"version": 1, "opening_review": {
            "promise": "A documented measurement", "evidence_beat": 2, "payoff_beat": 2,
            "notes": "Evidence meaning is an editorial inspection, beyond the timestamp gate.",
            "max_evidence_seconds": .2, "max_payoff_seconds": 60}}
        reviews(self.m, self.root)
        p.audio(self.m, self.root)
        with self.assertRaisesRegex(p.PipelineError, "Opening evidence"):
            p.opening_timing_check(self.m, p.check_audio(self.m, self.root))
        self.m["editorial_policy"]["opening_review"]["max_evidence_seconds"] = 15
        result = p.opening_timing_check(self.m, p.check_audio(self.m, self.root))
        self.assertFalse(result["semantic_quality_checked"])

    def test_alignment_rejects_stale_audio_or_text(self):
        reviews(self.m, self.root)
        p.audio(self.m, self.root)
        data = self.alignment()
        self.m["beats"][0]["text"] += " Changed narration."
        reviews(self.m, self.root)
        p.audio(self.m, self.root)
        with self.assertRaisesRegex(p.PipelineError, "alignment is stale"):
            p.caption_data(self.m, self.root, p.check_audio(self.m, self.root))

    def test_alignment_rejects_corrupt_checksum_and_changed_words(self):
        reviews(self.m, self.root)
        p.audio(self.m, self.root)
        data = self.alignment()
        data["cues"][0]["text"] = "Invented words."
        p.save_json(self.root / "research/alignment.json", data)
        with self.assertRaisesRegex(p.PipelineError, "cues_sha256"):
            p.write_materials(self.m, self.root, draft=True)
        data["cues_sha256"] = p.digest(data["cues"])
        p.save_json(self.root / "research/alignment.json", data)
        with self.assertRaisesRegex(p.PipelineError, "complete current narration"):
            p.write_materials(self.m, self.root, draft=True)

    def test_alignment_rejects_overlap_and_nonfinite_timings(self):
        reviews(self.m, self.root)
        p.audio(self.m, self.root)
        data = self.alignment()
        data["cues"][1]["start"] = 0
        data["cues_sha256"] = p.digest(data["cues"])
        p.save_json(self.root / "research/alignment.json", data)
        with self.assertRaisesRegex(p.PipelineError, "timing invalid"):
            p.write_materials(self.m, self.root, draft=True)
        data["cues"][1]["start"] = float("nan")
        data["cues_sha256"] = p.digest(data["cues"])
        p.save_json(self.root / "research/alignment.json", data)
        with self.assertRaisesRegex(p.PipelineError, "timing invalid"):
            p.write_materials(self.m, self.root, draft=True)

    def test_alignment_preserved_through_real_qa_and_package(self):
        self.m["smoke_test"] = False  # Only this temporary technical fixture exercises package persistence.
        reviews(self.m, self.root)
        p.audio(self.m, self.root)
        data = self.alignment()
        reviews(self.m, self.root)
        p.write_materials(self.m, self.root, draft=True)
        before = (self.root / "output/captions.srt").read_bytes()
        p.render(self.m, self.root)
        result = p.qa(self.m, self.root, "1080p")
        self.assertEqual(result["frame_audit"]["shot_midpoints"], 2)
        self.assertEqual(result["frame_audit"]["cuts"], 1)
        self.assertEqual(len(p.read_json(self.root / "output/frame_audit/audit.json")["frames"]), 5)
        p.review(self.m, self.root, "final", "Technical fixture review record to test packaging; this is not a public episode review.")
        p.package(self.m, self.root)
        self.assertEqual(before, (self.root / "output/captions.srt").read_bytes())
        self.assertEqual(result["caption_timing"]["method"], "Reviewed fixture timing")

    def test_contain_preserves_full_image_edges(self):
        from PIL import Image, ImageDraw
        import numpy as np
        path = self.root / "assets/square.png"
        im = Image.new("RGB", (300, 300), "green")
        ImageDraw.Draw(im).rectangle((0, 0, 299, 299), outline="red", width=20)
        im.save(path)
        a = dict(self.m["assets"][0], file="assets/square.png", fit="contain")
        frame = p.asset_frame(self.root, a, 640, 360)
        self.assertTrue(np.array_equal(frame[180, 145], [0, 0, 255]))
        self.assertTrue(np.array_equal(frame[0, 0], [21, 14, 8]))

    def test_source_video_decoding_reuses_frames_across_frame_rates(self):
        import cv2
        import numpy as np
        class Capture:
            def __init__(self, fps):
                self.fps, self.position, self.reads, self.seeks = fps, 0, 0, []
            def get(self, key):
                return self.fps if key == cv2.CAP_PROP_FPS else self.position
            def set(self, key, value):
                self.seeks.append(value)
                self.position = int(value)
                return True
            def read(self):
                self.reads += 1
                value = self.position
                self.position += 1
                return True, np.full((10, 20, 3), value, dtype=np.uint8)
        for source_fps in [29.97002997, 23.97602398, 60]:
            capture = Capture(source_fps)
            for frame in range(30):
                result = p.footage_frame(capture, frame / 30, 20, 10)
                self.assertEqual(int(result[0, 0, 0]), int(frame / 30 * source_fps + 1e-6))
            self.assertEqual(len(capture.seeks), 1)
            reads = capture.reads
            p.footage_frame(capture, 29 / 30, 20, 10)
            self.assertEqual(capture.reads, reads)
            p.footage_frame(capture, 0, 20, 10)
            self.assertEqual(len(capture.seeks), 2)
            p.clear_footage_cache(capture)


    def test_caption_alignment_flags_require_resolution(self):
        reviews(self.m, self.root)
        p.audio(self.m, self.root)
        data = self.alignment()
        data["flagged_beats"] = [1]
        p.save_json(self.root / "research/alignment.json", data)
        with self.assertRaisesRegex(p.PipelineError, "unresolved flagged_beats"):
            p.write_materials(self.m, self.root, draft=True)

    def test_compact_overlay_blending_matches_full_surface(self):
        from PIL import Image, ImageDraw
        import numpy as np
        import cv2
        overlay = Image.new("RGBA", (640, 360))
        draw = ImageDraw.Draw(overlay)
        draw.rectangle((10, 10, 180, 35), fill=(0, 0, 0, 210))
        draw.rectangle((10, 320, 620, 350), fill=(255, 255, 255, 180))
        frame = np.full((360, 640, 3), 100, dtype=np.uint8)
        rgba = np.asarray(overlay)
        alpha = rgba[:, :, 3:4].astype(np.float32) / 255
        expected = (frame * (1 - alpha) + cv2.cvtColor(rgba[:, :, :3], cv2.COLOR_RGB2BGR) * alpha).astype(np.uint8)
        self.assertTrue(np.array_equal(p.compose(frame, overlay), expected))
        self.assertEqual(len(overlay._silentdepth_composite), 2)
        self.assertTrue(np.array_equal(p.compose(frame, overlay), expected))

    def test_tts_retry_error_omits_upstream_url_and_token(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        class UpstreamError(Exception):
            status = 403
        failure = AsyncMock(side_effect=UpstreamError("wss://service.invalid?token=do-not-print"))
        factory = unittest.mock.Mock(return_value=SimpleNamespace(save=failure))
        with patch.dict(sys.modules, {"edge_tts": SimpleNamespace(Communicate=factory)}):
            with patch.object(p.asyncio, "sleep", new=AsyncMock()):
                with self.assertRaises(p.PipelineError) as caught:
                    asyncio.run(p.synthesize("Test", "test-voice", "-2%", self.root / "audio/test.mp3"))
        self.assertEqual(factory.call_count, 3)
        self.assertIn("status 403", str(caught.exception))
        self.assertNotIn("token", str(caught.exception))
        self.assertNotIn("wss:", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
