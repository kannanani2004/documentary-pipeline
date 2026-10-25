"""SilentDepth v2: Codex supplies reviewed evidence; this tool builds reproducibly.

Use --help. Never imports or rewrites legacy episodes automatically.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.parse
import urllib.request
import wave

# Optional D:-local dependency installation; never alter C: site-packages.
_VENDOR = Path(__file__).resolve().parent / "vendor"
if (_VENDOR / "edge_tts").is_dir():
    sys.path.insert(0, str(_VENDOR))

VERSION = "2.1.0"
# Preserve existing narration caches when only rendering/editorial checks change.
AUDIO_VERSION = "2.0.0"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
_VIDEO_DECODE_CACHE = {}
FPS, SAMPLE_RATE = 30, 48000
STAGES = ("research", "script", "assets", "audio", "render", "qa", "package")
KINDS = {"site_evidence", "context", "diagram", "reconstruction", "atmosphere"}
LABELS = {"site_evidence": "SOURCE MATERIAL", "context": "CONTEXT FOOTAGE",
          "diagram": "EXPLANATORY DIAGRAM", "reconstruction": "AI RECONSTRUCTION",
          "atmosphere": "AI ILLUSTRATION"}
RIGHTS = {"public-domain", "CC0", "CC-BY-4.0", "CC-BY-SA-4.0", "explicit-commercial-permission"}


class PipelineError(Exception):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".partial")
    temp.write_bytes(data)
    os.replace(temp, path)


def save_json(path, value):
    atomic_bytes(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode())


def read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def local(root, relative):
    if not isinstance(relative, str) or not relative:
        raise PipelineError("Expected a nonempty episode-relative path")
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise PipelineError(f"Path escapes episode: {relative}")
    return target


def command(args):
    try:
        result = subprocess.run([str(x) for x in args], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", check=True)
        return result.stdout
    except FileNotFoundError as e:
        raise PipelineError(f"Required executable missing: {args[0]}") from e
    except subprocess.CalledProcessError as e:
        raise PipelineError(f"{args[0]} failed ({e.returncode}): {e.stderr[-3000:]}") from e


def probe(path):
    return json.loads(command(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path]))


def required(record, fields, context):
    missing = [key for key in fields if record.get(key) in (None, "", [])]
    if missing:
        raise PipelineError(f"{context}: missing {', '.join(missing)}")


def indexed(records, context):
    result = {}
    for record in records:
        required(record, ["id"], context)
        if record["id"] in result:
            raise PipelineError(f"Duplicate {context} ID: {record['id']}")
        result[record["id"]] = record
    return result


def research_check(m, root):
    sources = indexed(m.get("sources", []), "source")
    claims = indexed(m.get("claims", []), "claim")
    if not sources or not claims:
        raise PipelineError("Research requires sources and a claim ledger")
    for s in sources.values():
        required(s, ["url", "title", "publisher", "date", "retrieved", "source_type", "excerpt"], f"source {s['id']}")
        if urllib.parse.urlparse(s["url"]).scheme not in {"https", "http"}:
            raise PipelineError(f"Invalid source URL: {s['id']}")
        if s["source_type"] not in {"primary", "direct-interview", "secondary"}:
            raise PipelineError(f"Invalid source_type: {s['id']}")
        required(s, ["snapshot"], f"source {s['id']}")
        if not local(root, s["snapshot"]).is_file():
            raise PipelineError(f"Missing source snapshot: {s['id']}")
    for c in claims.values():
        required(c, ["text", "classification", "source_ids", "support", "status"], f"claim {c['id']}")
        if c["classification"] not in {"documented", "reported", "interpretation", "disputed"}:
            raise PipelineError(f"Invalid claim classification: {c['id']}")
        if c["status"] != "checked":
            raise PipelineError(f"Unchecked claim: {c['id']}")
        if any(s not in sources for s in c["source_ids"]):
            raise PipelineError(f"Unknown source in claim {c['id']}")
        if c["classification"] in {"reported", "disputed", "interpretation"}:
            required(c, ["attribution"], f"claim {c['id']}")
        if re.search(r"\d", c["text"]):
            required(c, ["measurement_check"], f"numeric claim {c['id']}")
    return sources, claims


def script_check(m, root):
    sources, claims = research_check(m, root)
    required(m, ["episode_id", "title", "summary", "beats", "chapters", "packaging_check"], "episode")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", m["episode_id"]):
        raise PipelineError("episode_id must be a lowercase directory-safe identifier")
    ids = [b.get("id") for b in m["beats"]]
    if ids != list(range(1, len(ids) + 1)):
        raise PipelineError("Beat IDs must be consecutive integers beginning at 1")
    for b in m["beats"]:
        required(b, ["text", "claim_ids", "scene", "visual_kind"], f"beat {b['id']}")
        if b["visual_kind"] not in KINDS or any(c not in claims for c in b["claim_ids"]):
            raise PipelineError(f"Invalid visual kind or claim reference: beat {b['id']}")
        for cid in b["claim_ids"]:
            c = claims[cid]
            if c.get("attribution") and c["attribution"].casefold() not in b["text"].casefold():
                raise PipelineError(f"Beat {b['id']} must explicitly include attribution: {c['attribution']}")
    starts = [c.get("beat") for c in m["chapters"]]
    if starts != sorted(set(starts)) or starts[0] != 1 or any(s not in ids for s in starts):
        raise PipelineError("Chapters must start at beat 1 and use ordered unique beat IDs")
    for chapter in m["chapters"]:
        required(chapter, ["title"], "chapter")
    editorial_policy_check(m)
    return sources, claims


def assets_check(m, root):
    script_check(m, root)
    assets = indexed(m.get("assets", []), "asset")
    for b in m["beats"]:
        if b.get("asset_id") not in assets:
            raise PipelineError(f"No selected asset for beat {b['id']}")
        a = assets[b["asset_id"]]
        if a["kind"] != b["visual_kind"]:
            raise PipelineError(f"Asset classification mismatch at beat {b['id']}")
    for a in assets.values():
        required(a, ["file", "kind", "credit", "description", "subject_check"], f"asset {a['id']}")
        if a["kind"] not in KINDS:
            raise PipelineError(f"Unknown asset classification: {a['id']}")
        path = local(root, a["file"])
        if not path.is_file():
            raise PipelineError(f"Missing asset: {path}")
        if a.get("fit", "cover") not in {"cover", "contain"}:
            raise PipelineError(f"Unsupported asset fit: {a['id']}")
        for key in ["start_seconds", "source_start_seconds"]:
            if not finite_number(a.get(key, 0)) or a.get(key, 0) < 0:
                raise PipelineError(f"Invalid {key}: {a['id']}")
        if a.get("source_id") is not None and (not isinstance(a["source_id"], str) or not a["source_id"].strip()):
            raise PipelineError(f"Invalid source_id: {a['id']}")
        if a["kind"] in {"site_evidence", "context"}:
            required(a, ["source_url", "license", "license_url", "license_evidence", "rights_checked"], f"asset {a['id']}")
            if a["license"] not in RIGHTS or a["rights_checked"] is not True:
                raise PipelineError(f"Unverified or unsupported license: {a['id']}")
            if not local(root, a["license_evidence"]).is_file():
                raise PipelineError(f"Missing license evidence: {a['id']}")
        else:
            required(a, ["creation_notes"], f"original asset {a['id']}")
            if a["kind"] in {"reconstruction", "atmosphere"}:
                required(a, ["prompt", "generator", "not_evidence_checked"], f"AI asset {a['id']}")
                if a["not_evidence_checked"] is not True:
                    raise PipelineError(f"AI asset not reviewed: {a['id']}")
    thumb = m.get("thumbnail")
    if thumb:
        required(thumb, ["file", "claim_check", "credit", "source_asset_id"], "thumbnail")
        if thumb["source_asset_id"] not in assets:
            raise PipelineError("Thumbnail must reference a provenance-checked source asset")
        if not local(root, thumb["file"]).is_file():
            raise PipelineError("Missing thumbnail")
    visual_reuse_audit(m, root)
    return assets


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def editorial_policy_check(m):
    policy = m.get("editorial_policy")
    if policy is None:
        return
    if not isinstance(policy, dict) or policy.get("version") != 1:
        raise PipelineError("editorial_policy requires version: 1")
    if not isinstance(policy.get("prevent_unplanned_reuse", False), bool):
        raise PipelineError("prevent_unplanned_reuse must be boolean")
    repeats = policy.get("intentional_repeats", [])
    if not isinstance(repeats, list):
        raise PipelineError("intentional_repeats must be a list")
    for item in repeats:
        required(item, ["asset_id", "beats", "reason"], "intentional repeat")
        ids = [len(m["beats"]) if b == "last" else b for b in item["beats"]]
        if (len(ids) < 2 or any(type(i) is not int or not 1 <= i <= len(m["beats"]) for i in ids)
                or len(set(ids)) != len(ids)):
            raise PipelineError("Intentional repeat beats must be distinct existing IDs (or last)")
        if not any(b["id"] in ids and b.get("asset_id") == item["asset_id"] for b in m["beats"]):
            raise PipelineError("Intentional repeat asset_id must be selected in its declared beats")
    opening = policy.get("opening_review")
    if opening:
        required(opening, ["promise", "evidence_beat", "payoff_beat", "notes"], "opening review")
        for key in ["evidence_beat", "payoff_beat"]:
            if type(opening[key]) is not int or not 1 <= opening[key] <= len(m["beats"]):
                raise PipelineError(f"Invalid opening {key}")
        for key, default in [("max_evidence_seconds", 15), ("max_payoff_seconds", 60)]:
            if not finite_number(opening.get(key, default)) or opening.get(key, default) <= 0:
                raise PipelineError(f"Invalid opening {key}")


def opening_timing_check(m, timeline):
    opening = m.get("editorial_policy", {}).get("opening_review")
    if not opening:
        return {"status": "not configured", "semantic_quality_checked": False}
    result = {"promise": opening["promise"], "notes": opening["notes"],
              "semantic_quality_checked": False,
              "limit": "Beat timestamps only; an operator must assess evidence, appeal and payoff"}
    for kind, default in [("evidence", 15), ("payoff", 60)]:
        t = timeline[opening[f"{kind}_beat"] - 1]
        seconds = t["start_frame"] / FPS
        maximum = opening.get(f"max_{kind}_seconds", default)
        if seconds > maximum + 1 / FPS:
            raise PipelineError(f"Opening {kind} begins at {seconds:.2f}s, after {maximum}s limit")
        result[f"{kind}_seconds"] = seconds
    result["status"] = "timestamp limits passed"
    return result


def visual_reuse_audit(m, root, timeline=None):
    """Find repeated bytes and overlapping source-video intervals, not semantic similarity."""
    assets = indexed(m.get("assets", []), "asset")
    policy = m.get("editorial_policy", {})
    strict = policy.get("prevent_unplanned_reuse", False)
    declared = []
    for item in policy.get("intentional_repeats", []):
        declared.append((item["asset_id"], sorted(len(m["beats"]) if x == "last" else x for x in item["beats"])))
    hashes, groups, timed, shots = {}, {}, [], []
    for index, beat in enumerate(m["beats"]):
        a = assets[beat["asset_id"]]
        path = local(root, a["file"])
        if not path.is_file():
            continue  # assets_check gives the more useful missing-file error first.
        if a["file"] not in hashes:
            hashes[a["file"]] = file_hash(path)
        sha = hashes[a["file"]]
        record = {"beat": beat["id"], "asset_id": a["id"], "file": a["file"], "sha256": sha}
        if path.suffix.lower() in VIDEO_EXTENSIONS:
            record["source_id"] = a.get("source_id", sha)
            if timeline:
                duration = (timeline[index]["end_frame"] - timeline[index]["start_frame"]) / FPS
                start = a.get("source_start_seconds", 0) + a.get("start_seconds", 0)
                record.update(source_start=start, source_end=start + duration, duration=duration)
                timed.append(record)
        else:
            groups.setdefault(sha, []).append(record)
        shots.append(record)
    repeats = []
    for group in groups.values():
        if len(group) > 1:
            beats = sorted(x["beat"] for x in group)
            planned = any(aid in {x["asset_id"] for x in group} and ids == beats for aid, ids in declared)
            repeats.append({"type": "identical_still_bytes", "beats": beats,
                            "asset_ids": sorted({x["asset_id"] for x in group}), "intentional": planned})
    for i, first in enumerate(timed):
        for second in timed[i + 1:]:
            # Identical bytes always identify the same source, even if the supplied IDs differ.
            if first["source_id"] != second["source_id"] and first["sha256"] != second["sha256"]:
                continue
            overlap = min(first["source_end"], second["source_end"]) - max(first["source_start"], second["source_start"])
            if overlap > 1e-6:
                pair = [first["beat"], second["beat"]]
                # A planned multi-shot source reuse may cover several overlapping pairs.
                planned = any(aid in {first["asset_id"], second["asset_id"]} and set(pair).issubset(ids)
                              for aid, ids in declared)
                repeats.append({"type": "overlapping_source_video", "beats": pair,
                                "overlap_seconds": overlap, "intentional": planned})
    unplanned = [r for r in repeats if not r["intentional"]]
    if strict and unplanned:
        raise PipelineError("Unplanned visual reuse: " + "; ".join(f"{x['type']} at beats {x['beats']}" for x in unplanned))
    return {"strict": strict, "shots": shots, "repeats": repeats, "unplanned_count": len(unplanned),
            "video_intervals_checked": timeline is not None,
            "limit": "Byte identity and declared source intervals only; near-duplicates and scene relevance need visual review"}


def fingerprint(m, root, stage):
    scopes = {
        "script": ["sources", "claims", "title", "summary", "beats", "chapters", "packaging_check", "editorial_policy"],
        "assets": ["sources", "claims", "beats", "assets", "thumbnail", "editorial_policy"],
        "final": list(m),
    }
    data = {k: m.get(k) for k in scopes[stage]}
    paths = [s.get("snapshot") for s in m.get("sources", [])]
    if stage != "script":
        paths += [a.get("file") for a in m.get("assets", [])]
        paths += [a.get("license_evidence") for a in m.get("assets", [])]
        paths += [b.get("narration_file") for b in m.get("beats", [])]
        if m.get("thumbnail"):
            paths.append(m["thumbnail"].get("file"))
    if stage == "final":
        paths += [m.get("captions", {}).get("alignment_file")]
        paths += ["output/master_1080p.mp4", "output/master_4K.mp4", "audio/timeline.json", "output/qa.json",
                  "output/captions.srt", "output/thumbnail_v2.jpg", "output/upload_package_draft.md"]
    return digest({"version": VERSION, "manifest": data, "files": {
        p: file_hash(local(root, p)) if local(root, p).is_file() else "missing"
        for p in paths if p}})


def require_review(m, root, stage):
    review = read_json(root / "reviews.json", {}).get(stage, {})
    if review.get("fingerprint") != fingerprint(m, root, stage) or not review.get("notes"):
        raise PipelineError(f"Current {stage} review required. Codex must inspect it, then run review --stage {stage} --notes ...")


def review(m, root, stage, notes):
    {"script": script_check, "assets": assets_check, "final": assets_check}[stage](m, root)
    if stage == "final":
        report = read_json(root / "output/qa.json", {})
        if not report.get("passed") or report.get("input_hash") != build_hash(m, root):
            raise PipelineError("A current passing QA report is required for final review")
    if len(notes.strip()) < 30:
        raise PipelineError("Record a substantive review, including what was checked")
    records = read_json(root / "reviews.json", {})
    records[stage] = {"fingerprint": fingerprint(m, root, stage), "notes": notes, "reviewer": "Codex/operator", "time": time.time()}
    save_json(root / "reviews.json", records)


def build_hash(m, root):
    return digest({"manifest": m, "assets": fingerprint(m, root, "assets"), "version": VERSION,
                   "engine": file_hash(Path(__file__))})


def download(root, url, destination, expected_hash=None):
    if urllib.parse.urlparse(url).scheme != "https":
        raise PipelineError("Asset download requires HTTPS")
    path = local(root, destination)
    request = urllib.request.Request(url, headers={"User-Agent": "SilentDepthProduction/2.0 (asset research)"})
    with urllib.request.urlopen(request, timeout=45) as response:
        if urllib.parse.urlparse(response.url).scheme != "https":
            raise PipelineError("Refusing insecure redirect")
        data = response.read(150 * 1024 * 1024 + 1)
        if len(data) > 150 * 1024 * 1024:
            raise PipelineError("Asset exceeds 150 MB download limit")
        if "text/html" in response.headers.get("Content-Type", ""):
            raise PipelineError("Asset URL returned an HTML page")
    if expected_hash and hashlib.sha256(data).hexdigest() != expected_hash:
        raise PipelineError("Downloaded asset hash mismatch")
    atomic_bytes(path, data)
    return {"url": url, "file": destination, "sha256": file_hash(path), "bytes": len(data)}


def pcm_info(path):
    with wave.open(str(path), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (SAMPLE_RATE, 1, 2):
            raise PipelineError(f"Expected mono 48 kHz 16-bit PCM: {path}")
        return w.getnframes()


async def synthesize(text, voice, rate, target):
    import edge_tts
    for attempt in range(3):
        try:
            await edge_tts.Communicate(text, voice, rate=rate).save(str(target))
            if target.stat().st_size < 100:
                raise PipelineError("Empty narration response")
            return
        except Exception as error:
            if attempt == 2:
                status = getattr(error, "status", None)
                detail = type(error).__name__ + (f", status {status}" if isinstance(status, int) else "")
                # Upstream errors may embed a full websocket URI and public client token.
                raise PipelineError(f"Edge TTS failed after 3 attempts ({detail}); cached narration is retained. Check the service and supported edge-tts version.") from None
            await asyncio.sleep(2 * (attempt + 1))


def audio(m, root):
    require_review(m, root, "script")
    voice = m.get("voice", "en-US-AndrewMultilingualNeural")
    rate = m.get("rate", "-2%")
    folder = root / "audio/v2_beats"
    folder.mkdir(parents=True, exist_ok=True)
    timeline, files, cumulative = [], [], 0
    for b in m["beats"]:
        imported = local(root, b["narration_file"]) if b.get("narration_file") else None
        key = digest({"text": b["text"], "voice": voice, "rate": rate, "version": AUDIO_VERSION,
                      "import": file_hash(imported) if imported else None})
        target = folder / f"beat_{b['id']:04d}_{key[:20]}.wav"
        meta = target.with_suffix(".json")
        record = read_json(meta, {})
        if not target.exists() or record.get("key") != key or record.get("sha256") != file_hash(target):
            with tempfile.TemporaryDirectory(dir=folder) as temp_dir:
                temp = Path(temp_dir)
                source = imported
                if source is None:
                    source = temp / "tts.mp3"
                    asyncio.run(synthesize(b["text"], voice, rate, source))
                pcm = temp / "beat.wav"
                command(["ffmpeg", "-v", "error", "-y", "-i", source, "-ac", "1", "-ar", SAMPLE_RATE, "-c:a", "pcm_s16le", pcm])
                if pcm_info(pcm) < SAMPLE_RATE // 10:
                    raise PipelineError(f"Narration too short: beat {b['id']}")
                os.replace(pcm, target)
            save_json(meta, {"key": key, "sha256": file_hash(target)})
        samples = pcm_info(target)
        start_frame = round(cumulative * FPS / SAMPLE_RATE)
        end_frame = round((cumulative + samples) * FPS / SAMPLE_RATE)
        timeline.append({"beat": b["id"], "text": b["text"], "samples": samples,
                         "start_sample": cumulative, "end_sample": cumulative + samples,
                         "start_time": cumulative / SAMPLE_RATE, "end_time": (cumulative + samples) / SAMPLE_RATE,
                         "raw_duration": samples / SAMPLE_RATE, "duration": samples / SAMPLE_RATE,
                         "start_frame": start_frame, "end_frame": end_frame,
                         "audio_file": target.relative_to(root).as_posix()})
        files.append(target)
        cumulative += samples
    output = root / "audio/voiceover_v2.wav"
    temp = output.with_suffix(".partial.wav")
    with wave.open(str(temp), "wb") as out:
        out.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        for path in files:
            with wave.open(str(path), "rb") as beat:
                out.writeframes(beat.readframes(beat.getnframes()))
    os.replace(temp, output)
    save_json(root / "audio/timeline.json", timeline)
    save_json(root / "audio/audio_state.json", {"key": audio_hash(m, root), "sha256": file_hash(output)})


def audio_hash(m, root):
    return digest({"beats": [{"id": b["id"], "text": b["text"],
                              "import": file_hash(local(root, b["narration_file"])) if b.get("narration_file") else None}
                             for b in m["beats"]], "voice": m.get("voice", "en-US-AndrewMultilingualNeural"),
                   "rate": m.get("rate", "-2%"), "version": AUDIO_VERSION})


def check_audio(m, root):
    state = read_json(root / "audio/audio_state.json", {})
    voiceover = root / "audio/voiceover_v2.wav"
    if state.get("key") != audio_hash(m, root) or not voiceover.exists() or state.get("sha256") != file_hash(voiceover):
        raise PipelineError("Narration missing or stale; run audio")
    timeline = read_json(root / "audio/timeline.json", [])
    if len(timeline) != len(m["beats"]) or timeline[-1]["end_sample"] != pcm_info(voiceover):
        raise PipelineError("Invalid audio timeline; regenerate audio")
    for i, t in enumerate(timeline):
        if t["beat"] != m["beats"][i]["id"] or t["text"] != m["beats"][i]["text"]:
            raise PipelineError("Timeline does not match script")
        if t["start_sample"] != (timeline[i-1]["end_sample"] if i else 0):
            raise PipelineError("Non-contiguous audio timeline")
        if (t["end_sample"] - t["start_sample"] != t["samples"] or t["samples"] <= 0
                or t["start_frame"] != round(t["start_sample"] * FPS / SAMPLE_RATE)
                or t["end_frame"] != round(t["end_sample"] * FPS / SAMPLE_RATE)
                or t["end_frame"] <= t["start_frame"]):
            raise PipelineError("Timeline frame mismatch")
    return timeline


def font(size):
    from PIL import ImageFont
    for candidate in ["C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)


def label_overlay(width, height, asset):
    """A small stable classification badge and full readable attribution."""
    from PIL import Image, ImageDraw
    overlay = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(overlay)
    margin = round(width * .022)
    size = max(12, round(height * .021))
    f = font(size)
    badge = LABELS[asset["kind"]]
    if asset["kind"] in {"site_evidence", "context"}:
        is_video = Path(asset["file"]).suffix.lower() in VIDEO_EXTENSIONS
        if asset.get("_thumbnail_frame") and is_video:
            badge = "EXPEDITION FRAME"
        else:
            prefix = "EXPEDITION" if asset["kind"] == "site_evidence" else "CONTEXT"
            badge = prefix + (" FOOTAGE" if is_video else " PHOTO")
    badge_width = round(draw.textlength(badge, font=f)) + 24
    draw.rounded_rectangle((margin - 8, margin - 5, margin + badge_width, margin + size + 13),
                           radius=5, fill=(0, 0, 0, 210))
    draw.text((margin + 4, margin), badge, font=f, fill="white")
    # Wrap by measured pixels; never silently truncate attribution required by a license.
    lines, line = [], ""
    for word in asset["credit"].split():
        candidate = (line + " " + word).strip()
        if line and draw.textlength(candidate, font=f) > width - 2 * margin:
            lines.append(line)
            line = word
        else:
            line = candidate
    lines.append(line)
    line_h = round(size * 1.35)
    top = height - margin - line_h * len(lines)
    right = min(width - margin + 8, margin + max(draw.textlength(x, font=f) for x in lines) + 10)
    draw.rectangle((margin - 8, top - 5, right, height - margin + 4), fill=(0, 0, 0, 190))
    for i, line in enumerate(lines):
        draw.text((margin, top + i * line_h), line, font=f, fill="white")
    return overlay


def asset_frame(root, a, width, height):
    import cv2
    import numpy as np
    from PIL import Image, ImageOps
    path = local(root, a["file"])
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise PipelineError("Unsupported image type")
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im = ImageOps.pad(im, (width, height), color="#080e15") if a.get("fit") == "contain" else ImageOps.fit(im, (width, height))
        return cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)


def compose(frame, overlay):
    import cv2
    import numpy as np
    # Top classification + bottom attribution should not blend a full 1080p surface
    # for every frame. Cache only the occupied horizontal bands for this overlay.
    regions = getattr(overlay, "_silentdepth_composite", None)
    if regions is None:
        pixels = np.asarray(overlay)
        occupied = np.flatnonzero(np.any(pixels[:, :, 3], axis=1))
        groups = np.split(occupied, np.flatnonzero(np.diff(occupied) > 1) + 1)
        regions = []
        for rows in groups:
            if not len(rows):
                continue
            top, bottom = int(rows[0]), int(rows[-1]) + 1
            columns = np.flatnonzero(np.any(pixels[top:bottom, :, 3], axis=0))
            left, right = int(columns[0]), int(columns[-1]) + 1
            rgba = pixels[top:bottom, left:right]
            alpha = rgba[:, :, 3:4].astype(np.float32) / 255
            rgb = cv2.cvtColor(rgba[:, :, :3], cv2.COLOR_RGB2BGR)
            regions.append((left, top, right, bottom, alpha, rgb))
        overlay._silentdepth_composite = regions
    result = frame.copy()
    for left, top, right, bottom, alpha, rgb in regions:
        result[top:bottom, left:right] = (result[top:bottom, left:right] * (1 - alpha) + rgb * alpha).astype(np.uint8)
    return result


def clear_footage_cache(capture):
    _VIDEO_DECODE_CACHE.pop(capture, None)


def footage_frame(capture, seconds, width, height, fit="cover"):
    import cv2
    source_fps = capture.get(cv2.CAP_PROP_FPS)
    if not finite_number(source_fps) or source_fps <= 0:
        raise PipelineError("Source footage has no valid frame rate")
    # Hold or advance source frames for 29.97/24 fps material in a 30 fps output.
    # Seek once for an interval start, and only again for a backwards/large jump.
    target_index = max(0, math.floor(seconds * source_fps + 1e-6))
    cached = _VIDEO_DECODE_CACHE.get(capture)
    if cached and target_index == cached[0]:
        frame = cached[1]
    else:
        previous_index = cached[0] if cached else -1
        if cached and 0 < target_index - previous_index <= 8:
            frame = None
            for _ in range(target_index - previous_index):
                ok, frame = capture.read()
                if not ok:
                    raise PipelineError(f"Cannot decode source footage at {seconds:.3f}s; select a longer segment")
        else:
            capture.set(cv2.CAP_PROP_POS_FRAMES, target_index)
            ok, frame = capture.read()
            if not ok:
                raise PipelineError(f"Cannot decode source footage at {seconds:.3f}s; select a longer segment")
        _VIDEO_DECODE_CACHE[capture] = (target_index, frame)
    h, w = frame.shape[:2]
    if fit == "contain":
        import numpy as np
        scale = min(width / w, height / h)
        resized = cv2.resize(frame, (max(1, round(w * scale)), max(1, round(h * scale))))
        canvas = np.full((height, width, 3), (21, 14, 8), dtype=np.uint8)
        y, x = (height - resized.shape[0]) // 2, (width - resized.shape[1]) // 2
        canvas[y:y + resized.shape[0], x:x + resized.shape[1]] = resized
        return canvas
    scale = max(width / w, height / h)
    resized = cv2.resize(frame, (math.ceil(w * scale), math.ceil(h * scale)))
    y, x = (resized.shape[0] - height) // 2, (resized.shape[1] - width) // 2
    return resized[y:y + height, x:x + width]


def render(m, root, resolution="1080p", encoder="libx264"):
    import cv2
    assets = assets_check(m, root)
    require_review(m, root, "script")
    require_review(m, root, "assets")
    timeline = check_audio(m, root)
    visual_reuse_audit(m, root, timeline)
    opening_timing_check(m, timeline)
    width, height = (3840, 2160) if resolution == "4K" else (1920, 1080)
    outdir = root / "output"
    outdir.mkdir(exist_ok=True)
    target = outdir / f"master_{resolution}.mp4"
    key = digest({"build": build_hash(m, root), "audio": file_hash(root / "audio/voiceover_v2.wav"),
                  "timeline": timeline, "resolution": resolution, "encoder": encoder})
    cache = read_json(outdir / f"render_{resolution}.json", {})
    if target.exists() and cache.get("key") == key and cache.get("sha256") == file_hash(target):
        return
    frames = timeline[-1]["end_frame"]
    duration = frames / FPS
    temp = outdir / f"master_{resolution}.partial.mp4"
    cmd = ["ffmpeg", "-v", "warning", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}",
           "-r", str(FPS), "-i", "-", "-i", str(root / "audio/voiceover_v2.wav"),
           "-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude=0.015:sample_rate=48000:duration={duration + 1}",
           "-filter_complex", f"[2:a]lowpass=f=180[bg];[1:a][bg]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95:level=false,apad,atrim=end_sample={frames * SAMPLE_RATE // FPS}[a]",
           "-map", "0:v", "-map", "[a]", "-c:v", encoder, "-preset", "p4" if encoder == "h264_nvenc" else "veryfast",
           *( ["-cq", "19", "-b:v", "0"] if encoder == "h264_nvenc" else ["-crf", "19"] ),
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(temp)]
    previous, previous_kind = None, None
    with open(outdir / f"ffmpeg_{resolution}.log", "w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log,
                                creationflags=0x00004000 if sys.platform == "win32" else 0)
        try:
            for b, t in zip(m["beats"], timeline):
                a = assets[b["asset_id"]]
                path = local(root, a["file"])
                capture = cv2.VideoCapture(str(path)) if path.suffix.lower() in VIDEO_EXTENSIONS else None
                if capture is not None and not capture.isOpened():
                    raise PipelineError(f"Cannot open source footage: {path}")
                base = None if capture is not None else asset_frame(root, a, width, height)
                overlay = label_overlay(width, height, a)
                n = t["end_frame"] - t["start_frame"]
                fade = min(9, n) if previous_kind == a["kind"] and a["kind"] not in {"site_evidence", "context"} else 0
                for i in range(n):
                    if capture is not None:
                        frame = footage_frame(capture, a.get("start_seconds", 0) + i / FPS, width, height, a.get("fit", "cover"))
                    else:
                        scale = 1 if a.get("fit") == "contain" else 1 + .04 * i / max(n - 1, 1)
                        matrix = cv2.getRotationMatrix2D((width / 2, height / 2), 0, scale)
                        frame = cv2.warpAffine(base, matrix, (width, height), flags=cv2.INTER_LINEAR)
                    if i < fade:
                        alpha = (i + 1) / fade
                        frame = cv2.addWeighted(previous, 1 - alpha, frame, alpha, 0)
                    proc.stdin.write(compose(frame, overlay).tobytes())
                previous, previous_kind = frame, a["kind"]
                if capture is not None:
                    clear_footage_cache(capture)
                    capture.release()
            proc.stdin.close()
            code = proc.wait(timeout=120)
            if code:
                raise PipelineError(f"Encoder exited {code}; see {log.name}")
        except Exception:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            raise
    verify = probe(temp)
    video = next(s for s in verify["streams"] if s["codec_type"] == "video")
    aud = next(s for s in verify["streams"] if s["codec_type"] == "audio")
    if abs(float(video["duration"]) - float(aud["duration"])) > 1 / FPS:
        raise PipelineError("Rendered audio/video duration differs by more than one frame")
    os.replace(temp, target)
    save_json(outdir / f"render_{resolution}.json", {"key": key, "build_hash": build_hash(m, root),
                                                   "sha256": file_hash(target), "resolution": resolution})


def timecode(seconds, srt=False):
    millis = round(seconds * 1000)
    hours, remainder = divmod(millis, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    secs, ms = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}" if srt else f"{hours * 60 + minutes}:{secs:02}"


def qa(m, root, resolution):
    from PIL import Image, ImageDraw
    assets = assets_check(m, root)
    require_review(m, root, "script")
    require_review(m, root, "assets")
    timeline = check_audio(m, root)
    output = root / "output"
    movie = output / f"master_{resolution}.mp4"
    cache = read_json(output / f"render_{resolution}.json", {})
    if cache.get("build_hash") != build_hash(m, root) or not movie.exists() or cache.get("sha256") != file_hash(movie):
        raise PipelineError("Render missing, changed, or stale")
    write_materials(m, root, draft=True)
    make_thumbnail(m, root)
    info = probe(movie)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    drift = abs(float(v["duration"]) - float(a["duration"]))
    if drift > 1 / FPS or abs(float(v["duration"]) - timeline[-1]["end_frame"] / FPS) > 1 / FPS:
        raise PipelineError("Timeline or A/V duration mismatch")
    # Decode the entire export: container metadata alone cannot detect corrupt frames.
    command(["ffmpeg", "-v", "error", "-xerror", "-i", movie, "-f", "null", "-"])
    # Measure actual encoded audio; loudness is reported for the final listening review.
    loudness = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(movie), "-af", "volumedetect", "-vn", "-f", "null", "-"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stderr
    peak = re.search(r"max_volume: ([\-\d.]+) dB", loudness)
    if peak and float(peak.group(1)) >= 0:
        raise PipelineError("Encoded audio reaches clipping level")
    frame_audit = make_frame_audit(m, root, movie, timeline)
    reuse = visual_reuse_audit(m, root, timeline)
    opening = opening_timing_check(m, timeline)
    caption_report = caption_data(m, root, timeline)[1]
    report = {"passed": True, "input_hash": build_hash(m, root), "resolution": resolution,
              "duration": float(v["duration"]), "av_drift_seconds": drift,
              "peak_db": float(peak.group(1)) if peak else None, "full_decode": "passed",
              "caption_timing": caption_report,
              "visual_reuse": reuse, "opening_review": opening,
              "frame_audit": frame_audit,
              "semantic_limit": "Automated checks do not establish truth, licensing, or visual accuracy; current Codex reviews required",
              "captions_sha256": file_hash(output / "captions.srt"),
              "draft_package_sha256": file_hash(output / "upload_package_draft.md"),
              "thumbnail_sha256": file_hash(output / "thumbnail_v2.jpg"),
              "final_review_required": True}
    save_json(output / "qa.json", report)
    return report


def write_materials(m, root, draft=False):
    assets = assets_check(m, root)
    timeline = check_audio(m, root)
    chapter_lines = [f"{timecode(timeline[c['beat'] - 1]['start_time'])} - {c['title']}" for c in m["chapters"]]
    sources = [f"- {s['title']} — {s['url']} (accessed {s['retrieved']})" for s in m["sources"]]
    credits = [f"- {a['id']}: {a['credit']}" + (f" | {a['license']} | {a['license_url']}" if a.get("license") else " | Original illustrative asset") for a in assets.values()]
    if m.get("thumbnail"):
        credits.append(f"- Thumbnail: {m['thumbnail']['credit']}")
    links = []
    for link in m.get("watch_next", []):
        if re.fullmatch(r"https://(?:youtu\.be/[A-Za-z0-9_-]{11}|www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{11})", link.get("url", "")):
            links.append(f"- {link['title']}: {link['url']}")
    disclosure = "AI-generated illustrations and reconstructions are labeled on screen. Context footage and explanatory diagrams are distinguished from site evidence."
    text = f"# {m['title']}\n\n{m['summary']}\n\n## Chapters\n" + "\n".join(chapter_lines)
    text += "\n\n## Sources\n" + "\n".join(sources) + "\n\n## Visual credits\n" + "\n".join(credits)
    text += "\n\n## Visual disclosure\n" + disclosure
    if links:
        text += "\n\n## Watch next\n" + "\n".join(links)
    text += "\n\nLocal review package only. Confirm current platform disclosure settings during any separately requested upload.\n"
    atomic_bytes(root / "output" / ("upload_package_draft.md" if draft else "upload_package_v2.md"), text.encode())
    cues, _ = caption_data(m, root, timeline)
    subtitles = [f"{i}\n{timecode(c['start'], True)} --> {timecode(c['end'], True)}\n{c['text']}\n"
                 for i, c in enumerate(cues, 1)]
    atomic_bytes(root / "output/captions.srt", "\n".join(subtitles).encode())
    atomic_bytes(root / "script_v2.txt", "\n".join(f"[BEAT_{b['id']:02d}] {b['text']}" for b in m["beats"]).encode())


def caption_input_key(m, root):
    """Stable across render-only edits, invalidated by narration text/bytes/timing changes."""
    timeline = check_audio(m, root)
    return digest({"schema": 1, "audio_sha256": file_hash(root / "audio/voiceover_v2.wav"),
                   "audio_key": audio_hash(m, root),
                   "timeline": [{k: t[k] for k in ["beat", "text", "start_sample", "end_sample", "start_frame", "end_frame"]}
                                for t in timeline]})


def caption_data(m, root, timeline):
    config = m.get("captions", {})
    if not isinstance(config, dict):
        raise PipelineError("captions must be an object")
    if config.get("alignment_file"):
        path = local(root, config["alignment_file"])
        if not path.is_file():
            raise PipelineError("Caption alignment file missing")
        data = read_json(path, {})
        required(data, ["input_key", "cues_sha256", "method", "cues"], "caption alignment")
        if data["input_key"] != caption_input_key(m, root):
            raise PipelineError("Caption alignment is stale for current narration/text/timing")
        if data.get("flagged_beats"):
            raise PipelineError("Caption alignment has unresolved flagged_beats; inspect and repair before QA/package")
        cues = data["cues"]
        if not isinstance(cues, list) or data["cues_sha256"] != digest(cues):
            raise PipelineError("Caption alignment cues_sha256 mismatch")
        previous = 0
        duration = timeline[-1]["end_frame"] / FPS
        for c in cues:
            required(c, ["start", "end", "text"], "caption cue")
            if (not finite_number(c["start"]) or not finite_number(c["end"]) or not isinstance(c["text"], str)
                    or c["start"] < previous - .0001 or c["start"] < 0 or c["end"] <= c["start"]
                    or c["end"] > duration + 1 / FPS
                    or round(c["end"] * 1000) <= round(c["start"] * 1000)):
                raise PipelineError("Caption cue timing invalid, overlapping, or outside narration")
            previous = c["end"]
        expected = " ".join(" ".join(b["text"].split()) for b in m["beats"])
        actual = " ".join(" ".join(c["text"].split()) for c in cues)
        if actual != expected:
            raise PipelineError("Caption text does not preserve the complete current narration")
        return cues, {"method": data["method"], "alignment_file": config["alignment_file"],
                      "input_key": data["input_key"], "cues_sha256": data["cues_sha256"],
                      "limit": "Imported alignment timings require human readability/listening review"}
    cues = []
    for t in timeline:
        words = t["text"].split()
        used = 0
        for i in range(0, len(words), 12):
            chunk = words[i:i + 12]
            start = t["start_time"] + (t["end_time"] - t["start_time"]) * used / len(words)
            used += len(chunk)
            end = t["start_time"] + (t["end_time"] - t["start_time"]) * used / len(words)
            cues.append({"start": start, "end": end, "text": " ".join(chunk)})
    return cues, {"method": "approximate beat interpolation", "limit": "Not word-aligned; inspect for readability"}


def make_frame_audit(m, root, movie, timeline):
    """Every shot midpoint and every cut, including before/after/settled frames."""
    import cv2
    from PIL import Image, ImageDraw
    folder = root / "output/frame_audit"
    folder.mkdir(parents=True, exist_ok=True)
    capture = cv2.VideoCapture(str(movie))
    if not capture.isOpened():
        raise PipelineError("Cannot open finished export for frame audit")
    boards, records = [], []
    def extract(frame, name, label):
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame)
        ok, pixels = capture.read()
        if not ok:
            raise PipelineError(f"Cannot decode audit frame {frame}")
        im = Image.fromarray(cv2.cvtColor(pixels, cv2.COLOR_BGR2RGB))
        im.thumbnail((480, 270))
        tile = Image.new("RGB", (480, 298), "#10151d")
        tile.paste(im, ((480 - im.width) // 2, 0))
        ImageDraw.Draw(tile).text((8, 275), label, font=font(15), fill="white")
        path = folder / name
        tile.save(path, quality=92)
        records.append({"file": path.relative_to(root).as_posix(), "frame": frame,
                        "seconds": frame / FPS, "label": label})
        return tile
    def pages(tiles, prefix):
        for offset in range(0, len(tiles), 12):
            batch = tiles[offset:offset + 12]
            board = Image.new("RGB", (1440, math.ceil(len(batch) / 3) * 298), "#10151d")
            for i, tile in enumerate(batch):
                board.paste(tile, ((i % 3) * 480, (i // 3) * 298))
            path = folder / f"{prefix}_{offset // 12 + 1}.jpg"
            board.save(path, quality=92)
            boards.append({"file": path.relative_to(root).as_posix(), "sha256": file_hash(path)})
            if prefix == "midpoints" and offset == 0:
                board.save(root / "output/contact_sheet.jpg", quality=92)
    try:
        midpoints = [extract((t["start_frame"] + t["end_frame"] - 1) // 2,
                             f"beat_{t['beat']:04d}.jpg", f"Beat {t['beat']} midpoint") for t in timeline]
        pages(midpoints, "midpoints")
        cuts = []
        for previous, following in zip(timeline, timeline[1:]):
            boundary = following["start_frame"]
            frames = [max(previous["start_frame"], boundary - 2), boundary,
                      min(following["end_frame"] - 1, boundary + 2 * FPS)]
            for suffix, frame in zip(["before", "after", "settled"], frames):
                cuts.append(extract(frame, f"cut_{following['beat']:04d}_{suffix}.jpg",
                                    f"Cut {previous['beat']} to {following['beat']}: {suffix}"))
        pages(cuts, "cuts")
    finally:
        capture.release()
    report = {"shot_midpoints": len(timeline), "cuts": max(0, len(timeline) - 1),
              "frames": records, "boards": boards,
              "limit": "Frames support inspection; they do not establish continuous playback or audio listening"}
    save_json(folder / "audit.json", report)
    return {k: report[k] for k in ["shot_midpoints", "cuts", "boards", "limit"]}


def make_thumbnail(m, root):
    from PIL import Image, ImageDraw
    import cv2
    if m.get("thumbnail"):
        source = local(root, m["thumbnail"]["file"])
        with Image.open(source) as im:
            from PIL import ImageOps
            im = ImageOps.fit(im.convert("RGB"), (1280, 720))
            asset = indexed(m["assets"], "asset")[m["thumbnail"]["source_asset_id"]]
            asset = dict(asset, credit=m["thumbnail"]["credit"], _thumbnail_frame=True)
            im = Image.alpha_composite(im.convert("RGBA"), label_overlay(1280, 720, asset))
            im.convert("RGB").save(root / "output/thumbnail_v2.jpg", quality=95)
        return
    assets = indexed(m["assets"], "asset")
    used = [assets[b["asset_id"]] for b in m["beats"]]
    a = next((a for kind in ["site_evidence", "diagram", "context", "reconstruction", "atmosphere"] for a in used if a["kind"] == kind), None)
    if local(root, a["file"]).suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}:
        cap = cv2.VideoCapture(str(local(root, a["file"])))
        try:
            frame = footage_frame(cap, a.get("start_seconds", 0), 1280, 720, a.get("fit", "cover"))
        finally:
            clear_footage_cache(cap)
            cap.release()
    else:
        frame = asset_frame(root, a, 1280, 720)
    im = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(im)
    lines = textwrap.wrap(m["title"], width=29)
    size = min(64, 220 // max(len(lines), 1))
    draw.rectangle((30, 30, 1250, 70 + (size + 12) * len(lines)), fill="#10151d")
    for i, line in enumerate(lines):
        draw.text((55, 50 + i * (size + 12)), line, fill="#ffdf55", font=font(size))
    im = Image.alpha_composite(im.convert("RGBA"), label_overlay(1280, 720, a))
    im.convert("RGB").save(root / "output/thumbnail_v2.jpg", quality=95)


def package(m, root):
    assets_check(m, root)
    for stage in ["script", "assets", "final"]:
        require_review(m, root, stage)
    report = read_json(root / "output/qa.json", {})
    if report.get("input_hash") != build_hash(m, root):
        raise PipelineError("QA is stale")
    for file, key in [("captions.srt", "captions_sha256"), ("upload_package_draft.md", "draft_package_sha256"), ("thumbnail_v2.jpg", "thumbnail_sha256")]:
        if file_hash(root / "output" / file) != report.get(key):
            raise PipelineError(f"Reviewed output changed: {file}")
    resolution = report["resolution"]
    cache = read_json(root / "output" / f"render_{resolution}.json", {})
    if file_hash(root / "output" / f"master_{resolution}.mp4") != cache.get("sha256"):
        raise PipelineError("Reviewed video changed")
    if m.get("smoke_test"):
        raise PipelineError("Smoke-test fixtures cannot be packaged for publication")
    write_materials(m, root)


def prepare_video(root, source, destination, start, duration):
    # Retain source footage; extract a representative still for the legacy-compatible renderer.
    if start < 0 or duration <= 0:
        raise PipelineError("Invalid footage interval")
    target = local(root, destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.stem + ".partial" + target.suffix)
    command(["ffmpeg", "-v", "error", "-y", "-ss", str(start + duration / 2), "-i", local(root, source), "-frames:v", "1", temp])
    os.replace(temp, target)


def run_stage(m, root, stage, resolution, encoder):
    functions = {"research": research_check, "script": script_check, "assets": assets_check,
                 "audio": audio, "render": lambda m, r: render(m, r, resolution, encoder),
                 "qa": lambda m, r: qa(m, r, resolution), "package": package}
    state_path = root / "pipeline_state.json"
    state = read_json(state_path, {"version": VERSION, "stages": {}})
    state["stages"][stage] = {"status": "running", "time": time.time()}
    save_json(state_path, state)
    try:
        functions[stage](m, root)
    except Exception as e:
        state["stages"][stage] = {"status": "failed", "error": str(e), "time": time.time()}
        save_json(state_path, state)
        raise
    state["stages"][stage] = {"status": "complete", "manifest_hash": digest(m), "time": time.time()}
    save_json(state_path, state)
    print(f"Complete: {stage}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", required=True, type=Path, help="New episode directory containing episode.json")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ["run", "check"]:
        p = sub.add_parser(action)
        p.add_argument("--stage", choices=STAGES + (("all",) if action == "run" else ()), default="all" if action == "run" else "assets")
        p.add_argument("--resolution", choices=["1080p", "4K"], default="1080p")
        p.add_argument("--encoder", choices=["libx264", "h264_nvenc"], default="libx264")
    p = sub.add_parser("review", help="Record a completed semantic inspection; never a substitute for inspecting")
    p.add_argument("--stage", choices=["script", "assets", "final"], required=True)
    p.add_argument("--notes", required=True)
    p = sub.add_parser("download")
    p.add_argument("--url", required=True)
    p.add_argument("--destination", required=True)
    p.add_argument("--sha256")
    p = sub.add_parser("prepare-video")
    p.add_argument("--source", required=True)
    p.add_argument("--destination", required=True)
    p.add_argument("--start", type=float, default=0)
    p.add_argument("--duration", type=float, required=True)
    args = parser.parse_args()
    root = args.episode.resolve()
    try:
        m = read_json(root / "episode.json")
        if m is None:
            raise PipelineError("episode.json missing; Codex must prepare research and manifest first. Legacy episodes are not auto-migrated.")
        if args.action == "review":
            review(m, root, args.stage, args.notes)
        elif args.action == "download":
            print(json.dumps(download(root, args.url, args.destination, args.sha256), indent=2))
        elif args.action == "prepare-video":
            prepare_video(root, args.source, args.destination, args.start, args.duration)
        elif args.action == "check":
            if args.stage not in {"research", "script", "assets"}:
                raise PipelineError("Read-only check supports research, script, assets; use run for build stages")
            {"research": research_check, "script": script_check, "assets": assets_check}[args.stage](m, root)
            print(f"Valid: {args.stage}")
        else:
            for stage in STAGES if args.stage == "all" else [args.stage]:
                run_stage(m, root, stage, args.resolution, args.encoder)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
