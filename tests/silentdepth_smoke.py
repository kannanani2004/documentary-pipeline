"""End-to-end smoke test; fixture cannot be packaged for publication."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import silentdepth_pipeline as p
from test_silentdepth_pipeline import fixture, reviews


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live-tts", action="store_true", help="Use edge-tts instead of silent PCM fixtures")
    parser.add_argument("--resolution", choices=["1080p", "4K"], default="1080p")
    parser.add_argument("--video-fixture", action="store_true", help="Exercise source-video decoding using an original test animation")
    args = parser.parse_args()
    root = args.output.resolve()
    if root.exists():
        raise p.PipelineError("Smoke output must be a new directory")
    root.mkdir(parents=True)
    m = fixture(root)
    if args.live_tts:
        m["beats"][0]["text"] = "Ocean X reported an object approximately two hundred feet across. This is a labeled explanatory diagram."
        m["beats"][1]["text"] = "Ocean X reported the measurement in its original release. This local smoke test checks narration, timing, and evidence labels."
        for beat in m["beats"]:
            del beat["narration_file"]
    if args.video_fixture:
        video = root / "assets/test_video.mp4"
        p.command(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", root / "assets/test_2.png",
                   "-t", "30", "-r", "30", "-vf", "scale=640:360", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", video])
        m["assets"][1]["file"] = "assets/test_video.mp4"
        m["assets"][1]["description"] = "Original test diagram encoded as video to exercise footage decoding."
    p.save_json(root / "episode.json", m)
    reviews(m, root)
    for stage in ["research", "script", "assets", "audio", "render", "qa"]:
        p.run_stage(m, root, stage, args.resolution, "libx264")
    before = (root / "output" / f"master_{args.resolution}.mp4").stat().st_mtime_ns
    p.render(m, root, args.resolution)
    assert before == (root / "output" / f"master_{args.resolution}.mp4").stat().st_mtime_ns, "Render cache failed"
    # Exercise release guard without claiming the operator has watched/listened.
    with __import__("unittest.mock", fromlist=["patch"]).patch.object(p, "require_review"):
        try:
            p.package(m, root)
            raise AssertionError("Smoke publication guard failed")
        except p.PipelineError as e:
            assert "Smoke-test" in str(e), str(e)
    result = {"render": "passed", "qa": p.read_json(root / "output/qa.json"), "resume_cache": "passed",
              "publication_guard": "passed", "live_tts": args.live_tts,
              "video_fixture": args.video_fixture,
              "final_semantic_review": "not recorded; smoke fixture is not a release"}
    p.save_json(root / "smoke_result.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
