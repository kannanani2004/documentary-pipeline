# Documentary Pipeline

Build documentary videos from a reviewed script, source ledger and visual assets. Track evidence, render narration and visuals, align captions, and check the finished export.

Extracted from the SilentDepth production workflow. The command-line filenames retain their original names to keep the existing tools and tests compatible.

## What it does

- Links narration beats to claims and saved source passages.
- Records per-asset provenance, credits and reuse rights.
- Distinguishes source material, context, diagrams, AI reconstructions and AI illustrations with visible labels.
- Builds still-image and footage sequences using measured narration timing.
- Reuses audio and render caches when their input hashes remain valid.
- Checks duplicate stills and overlapping footage intervals when strict editorial policy is enabled.
- Produces SRT captions, thumbnails, frame-audit pages, QA reports and upload copy.
- Requires recorded human reviews before packaging a release.

This is an operator-assisted toolkit. It does not autonomously research a topic, establish factual truth, determine copyright permissions or upload videos.

## Requirements

Use Python 3.11 or 3.12 and FFmpeg/FFprobe on PATH. The release is tested locally on Windows with Python 3.12; other platforms are not yet verified. FFmpeg needs libx264 for the default CPU encoder. NVIDIA NVENC is optional.

Create an isolated environment from the repository root:

~~~powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
ffmpeg -version
ffprobe -version
~~~

On Linux/macOS use .venv/bin/python in place of .venv/Scripts/python. Font lookup supports Windows Arial and Linux DejaVu, with a Pillow fallback; inspect typography on your platform.

No LLM API key is required by these tools. By default, narration uses edge-tts and sends the script to Microsoft's online speech service. Availability and applicable service terms are external to this repository. Set narration_file per beat to use your own recordings instead. Caption alignment uses a separately obtained local faster-whisper model; model downloads, licenses and hardware requirements are separate.

## Try the offline smoke test

~~~powershell
.venv/Scripts/python -B -m unittest discover -s tests -p "test_silentdepth*.py"
.venv/Scripts/python -B tests/silentdepth_smoke.py --output .local/smoke-01
~~~

The smoke test creates original diagrams and silent PCM test audio, renders a short 1080p video, performs QA and verifies render-cache reuse. It does not call TTS or download a speech model. Choose a new output directory for each run.

Its source ledger is a synthetic technical fixture, not validated research. The packager deliberately rejects smoke fixtures. The silent audio cannot demonstrate narration quality.

Use --video-fixture to exercise source-video decoding. Use --live-tts only if you intend to send the smoke narration to the online service.

## Start a real episode

1. Create a local episodes/my-documentary directory.
2. Copy examples/episode.template.json to episodes/my-documentary/episode.json.
3. Replace every placeholder, save source passages under research/, and add your reviewed assets under assets/.
4. Read [the workflow](docs/workflow.md) and [manifest reference](docs/manifest.md).
5. Validate inputs, perform actual reviews, then build and inspect the export.

~~~powershell
.venv/Scripts/python tools/silentdepth_pipeline.py --episode episodes/my-documentary check --stage assets
.venv/Scripts/python tools/silentdepth_pipeline.py --help
.venv/Scripts/python tools/silentdepth_captions.py --help
~~~

The template is intentionally incomplete and will fail validation until its sources, claims and files have been supplied.

## Layout

| Path | Purpose |
| --- | --- |
| tools/silentdepth_pipeline.py | Validation, narration, rendering, QA and packaging |
| tools/silentdepth_captions.py | Local ASR alignment and exact continuous-audio splitting |
| tests/ | Existing behavioral tests and offline smoke runner |
| examples/episode.template.json | Unfilled manifest template |
| docs/ | Workflow and manifest reference |

## Limits and contribution scope

Automated checks support editorial review; a saved review note is an operator assertion, not proof of inspection. Watch and listen to the complete export, inspect captions and evidence labels, and verify each asset's rights before publication. Caption matching currently normalizes English letters and digits; multilingual alignment is not established.

The downloader is for trusted operator-selected HTTPS asset URLs, not a public URL-fetching service. The renderer executes locally and processes media through FFmpeg/OpenCV. Use a controlled environment for unfamiliar inputs.

Keep episodes, source passages, recordings, model weights, credentials and outputs out of source control. The included ignore rules exclude common locations but are not a secret scanner.

Contributions improving reproducibility, portability or validation are welcome. Describe the behavior you change, add a focused regression where appropriate, and run the existing suite. Do not commit real episode data or credentials.

## License

Repository code and documentation are available under the [MIT license](LICENSE). Dependencies, speech services, models and user-supplied media retain their own licenses and terms.