# Workflow

Run commands from the repository root in the environment described in README.md. These examples use python; substitute your virtual environment's interpreter.

## Prepare and validate

Create an episode directory containing episode.json, research/ source snapshots and assets/. Use examples/episode.template.json as an unfilled starting point.

Research, scripting, asset acquisition and factual/visual inspection are performed by an operator, optionally assisted by an AI tool of their choice. No research or image-generation service is bundled.

~~~powershell
python tools/silentdepth_pipeline.py --episode episodes/my-documentary check --stage research
python tools/silentdepth_pipeline.py --episode episodes/my-documentary check --stage script
python tools/silentdepth_pipeline.py --episode episodes/my-documentary check --stage assets
~~~

Only research, script and assets support read-only check. A successful check confirms the implemented structural rules, not the truth of the source or legal permission.

After inspecting the actual narration, sources and assets, record substantive review notes. Supply notes describing your own completed checks, not generic approval text.

~~~powershell
python tools/silentdepth_pipeline.py --episode episodes/my-documentary review --stage script --notes "Describe the source, attribution, measurement and title checks actually performed."
python tools/silentdepth_pipeline.py --episode episodes/my-documentary review --stage assets --notes "Describe the actual visual, provenance, rights and scene-correspondence checks."
~~~

## Narrate and render

~~~powershell
python tools/silentdepth_pipeline.py --episode episodes/my-documentary run --stage audio
python tools/silentdepth_pipeline.py --episode episodes/my-documentary run --stage render --encoder libx264 --resolution 1080p
~~~

Normal narration uses online edge-tts. For supplied recordings, set narration_file on each beat and verify the transcript yourself. The audio stage converts supplied audio to mono 48 kHz PCM. It caches per-beat audio by input/settings and bytes. Rendering uses cumulative sample timing and hashes; no bit-for-bit cross-machine reproducibility is promised.

Optional render choices: --encoder h264_nvenc and --resolution 4K. Resizing assets does not create source detail. If NVENC is unavailable, explicitly select libx264.

## Align captions

The caption tool needs an existing local faster-whisper-compatible model directory. Acquire it separately. It is loaded locally; the caption tool does not send audio to a remote ASR API.

~~~powershell
python tools/silentdepth_captions.py --episode episodes/my-documentary --model /path/to/local-whisper-model
python tools/silentdepth_captions.py --episode episodes/my-documentary --inspect
~~~

Alignment preserves script words, uses ASR anchors and records interpolated timings. Investigate flagged beats; matching scores do not prove pronunciation or good delivery. A successful alignment adds the alignment-file association to episode.json. Inspect its report and readability.

Without an alignment file, the pipeline emits approximate phrase timings. Do not describe those as word-aligned.

The advanced --split-continuous option accepts mono 48 kHz 16-bit WAV and imports consecutive exact source samples into beat files. It modifies narration_file associations, invalidating prior reviews. Refresh reviews after actual inspection, run audio, and align the resulting timeline.

## QA, inspect and package

~~~powershell
python tools/silentdepth_pipeline.py --episode episodes/my-documentary run --stage qa
~~~

QA fully decodes the video, checks timing and clipping, and creates midpoint/cut frame-audit pages, captions, a thumbnail and draft upload copy. Inspect every audit page, review the thumbnail and captions, and watch/listen through the complete export. Automated tests do not perform that review.

After the review, record the work you actually completed and package:

~~~powershell
python tools/silentdepth_pipeline.py --episode episodes/my-documentary review --stage final --notes "Describe your completed full playback, listening, caption, label and package review."
python tools/silentdepth_pipeline.py --episode episodes/my-documentary run --stage package
~~~

Review records are bound to input hashes. Changed sources, assignments or assets can invalidate reviews; changed audio invalidates caption alignment. Stale artifacts must be rebuilt, not waved through by editing hashes or lowering thresholds.

## Outputs and recovery

Main files include:

- output/master_1080p.mp4 (or master_4K.mp4)
- output/captions.srt and thumbnail_v2.jpg
- output/frame_audit/ and qa.json
- output/upload_package_draft.md; upload_package_v2.md after packaging
- audio/timeline.json, voiceover_v2.wav and per-beat caches
- reviews.json and pipeline_state.json

Rerun a failed stage after addressing the reported error. State records describe progress; byte hashes determine cache validity. run --stage all advances in order and stops at missing reviews. It does not generate research or perform reviews.

Nothing uploads automatically. Publication is a separate operator action.

## Asset download helper

The download command requires an episode.json to exist and an operator-selected HTTPS URL:

~~~powershell
python tools/silentdepth_pipeline.py --episode episodes/my-documentary download --url https://example.org/asset.jpg --destination assets/source.jpg
~~~

Downloads are bounded to 150 MB and can be pinned with --sha256. This helper does not establish authenticity or reuse permission. Use it only with trusted URLs; it is not hardened as a public web-fetch endpoint.