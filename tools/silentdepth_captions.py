"""Create exact-script captions using local ASR anchors and explicit interpolation.

No audio leaves the machine. This measures word matching and timing only; it does
not certify subjective narration quality, pronunciation, or a full listening review.
"""
from __future__ import annotations
import argparse
import difflib
import math
import re
from pathlib import Path
import sys
import wave

import silentdepth_pipeline as p

VERSION = "1.0.0"
SETTINGS = {"language": "en", "beam_size": 5, "word_timestamps": True,
            "vad_filter": False, "condition_on_previous_text": False}
MIN_SIMILARITY = .80
MIN_COVERAGE = .65
MAX_LINE = 44
METHOD = ("Local faster-whisper word timestamps aligned to the exact current script; "
          "unmatched tokens interpolated between matched anchors; segment-only timing "
          "fallback and local beat retries recorded. No external ASR transfer. "
          "This is not subjective listening or pronunciation review.")


def normalized(word):
    return re.sub(r"[^a-z0-9]", "", word.lower())


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def format_two_lines(tokens, width=MAX_LINE):
    """Return a balanced, at-most-two-line phrase, or None when it cannot fit."""
    text = " ".join(tokens)
    if len(text) <= width:
        return text
    options = []
    for split in range(1, len(tokens)):
        left, right = " ".join(tokens[:split]), " ".join(tokens[split:])
        if len(left) <= width and len(right) <= width:
            options.append((abs(len(left)-len(right)), left+"\n"+right))
    return min(options, key=lambda item: item[0])[1] if options else None


def phrase_ranges(tokens):
    """Partition globally so a final one-word fragment is not created greedily."""
    size = len(tokens)
    costs = [float("inf")] * (size+1)
    previous = [None] * (size+1)
    costs[0] = 0
    for end in range(1, size+1):
        for start in range(max(0, end-12), end):
            group = tokens[start:end]
            if format_two_lines(group) is None and len(group) != 1:
                continue
            words = end-start
            penalty = .6*(words-8)**2 + (25 if words == 1 and size > 1 else 0)
            penalty += 6 if end < size and not re.search(r"[.,;:!?]$", group[-1]) else 0
            candidate = costs[start]+penalty
            if candidate < costs[end]:
                costs[end], previous[end] = candidate, start
    ranges = []
    end = size
    while end:
        start = previous[end]
        if start is None:
            raise p.PipelineError("Cannot partition caption text")
        ranges.append((start, end))
        end = start
    return list(reversed(ranges))


def segment_records(segment, offset=0.0):
    """Prefer real ASR word times; record a segment-interpolation fallback honestly."""
    words = []
    asr_words = getattr(segment, "words", None) or []
    if asr_words:
        for word in asr_words:
            if not finite(word.start) or not finite(word.end) or word.end <= word.start:
                continue
            words.append({"word": word.word, "start": float(word.start)+offset,
                          "end": float(word.end)+offset,
                          "probability": float(getattr(word, "probability", 0.0)),
                          "timestamp_method": "asr-word"})
    if words:
        return words
    tokens = segment.text.split()
    start, end = float(segment.start)+offset, float(segment.end)+offset
    if not tokens or end <= start:
        return []
    weights = [max(1, len(normalized(token))) for token in tokens]
    total, used = sum(weights), 0
    for token, weight in zip(tokens, weights):
        words.append({"word": token, "start": start+(end-start)*used/total,
                      "end": start+(end-start)*(used+weight)/total,
                      "probability": None, "timestamp_method": "segment-interpolation"})
        used += weight
    return words


def align_words(text, heard, start, end):
    tokens = text.split()
    expected_keys = [normalized(word) for word in tokens]
    valid = [word for word in heard if finite(word.get("start")) and finite(word.get("end"))
             and word["end"] > word["start"] and normalized(word["word"])]
    valid.sort(key=lambda word: (word["start"], word["end"]))
    matching = difflib.SequenceMatcher(None, expected_keys,
                                      [normalized(word["word"]) for word in valid], autojunk=False)
    anchored = {}
    previous_end = start
    for block in matching.get_matching_blocks():
        for step in range(block.size):
            index = block.a+step
            word = valid[block.b+step]
            lo, hi = max(start, word["start"], previous_end), min(end, word["end"])
            if hi > lo:
                anchored[index] = {"start": lo, "end": hi,
                                   "timing": word.get("timestamp_method", "asr-word")}
                previous_end = hi
    times = [None] * len(tokens)
    for index, word in anchored.items():
        times[index] = word
    index = 0
    unmatched_runs = []
    while index < len(tokens):
        if times[index] is not None:
            index += 1
            continue
        first = index
        while index < len(tokens) and times[index] is None:
            index += 1
        last = index
        lo = times[first-1]["end"] if first else start
        hi = times[last]["start"] if last < len(tokens) else end
        hi = max(lo, hi)
        weights = [max(1, len(normalized(word))) for word in tokens[first:last]]
        total, used = sum(weights), 0
        for current, weight in zip(range(first, last), weights):
            times[current] = {"start": lo+(hi-lo)*used/total,
                              "end": lo+(hi-lo)*(used+weight)/total,
                              "timing": "unmatched-interpolation"}
            used += weight
        unmatched_runs.append({"first_word": first, "last_word_exclusive": last,
                               "text": " ".join(tokens[first:last]), "start": lo, "end": hi})
    covered = len(anchored)/len(tokens) if tokens else 0
    ratio = matching.ratio()
    flags = []
    if not valid:
        flags.append("no_recognized_words")
    if ratio < MIN_SIMILARITY:
        flags.append("low_transcript_similarity")
    if covered < MIN_COVERAGE:
        flags.append("insufficient_matching_anchors")
    if any(run["last_word_exclusive"]-run["first_word"] > max(6, len(tokens)*.30) for run in unmatched_runs):
        flags.append("long_unmatched_span")
    if any(word["end"] <= word["start"] for word in times):
        flags.append("unresolved_zero_duration_word")
    check = {"similarity": round(ratio, 6), "matched_word_coverage": round(covered, 6),
             "expected": text, "recognized": " ".join(word["word"].strip() for word in valid),
             "expected_word_count": len(tokens), "recognized_word_count": len(valid),
             "matched_word_count": len(anchored), "unmatched_runs": unmatched_runs,
             "timestamp_fallback_words": sum(word["timing"] == "segment-interpolation" for word in times),
             "flags": flags}
    return tokens, times, check


def cues_for_beat(tokens, times, start, end):
    if not tokens:
        return [], []
    ranges = phrase_ranges(tokens)
    groups = []
    for first, last in ranges:
        groups.append({"first": first, "last": last,
                       "start": max(start, times[first]["start"]-.04),
                       "end": min(end, times[last-1]["end"]+.20)})
    for index, cue in enumerate(groups[:-1]):
        cue["end"] = max(cue["start"], groups[index+1]["start"]-.02)
    # Merge a short phrase only if the resulting text still fits two readable lines.
    index = 0
    while index < len(groups):
        group = groups[index]
        if group["end"]-group["start"] >= 1 or len(groups) == 1:
            index += 1
            continue
        candidates = []
        for neighbor in (index-1, index+1):
            if 0 <= neighbor < len(groups):
                first, last = sorted((index, neighbor))
                a, b = groups[first], groups[last]
                if format_two_lines(tokens[a["first"]:b["last"]]) is not None:
                    candidates.append((len(tokens[a["first"]:b["last"]]), first, last))
        if candidates:
            _, first, last = min(candidates)
            groups[first] = {"first": groups[first]["first"], "last": groups[last]["last"],
                             "start": groups[first]["start"], "end": groups[last]["end"]}
            groups.pop(last)
            index = max(0, first-1)
        else:
            index += 1
    notes, cues = [], []
    for index, group in enumerate(groups):
        if group["end"]-group["start"] < 1:
            lower = groups[index-1]["end"]+.001 if index else start
            upper = groups[index+1]["start"]-.001 if index+1 < len(groups) else end
            group["end"] = min(upper, max(group["end"], group["start"]+1))
            group["start"] = max(lower, min(group["start"], group["end"]-1))
        text = format_two_lines(tokens[group["first"]:group["last"]])
        if text is None:
            text = " ".join(tokens[group["first"]:group["last"]])
            notes.append("unbreakable_word_exceeds_line_width")
        cue = {"start": round(group["start"], 6), "end": round(group["end"], 6), "text": text}
        if cue["end"]-cue["start"] < 1:
            notes.append("short_cue_constrained_by_spoken_timing")
        cues.append(cue)
    return cues, sorted(set(notes))


def build_alignment(beats, timeline, words):
    cues, checks = [], []
    for beat, timing in zip(beats, timeline):
        start, end = timing["start_time"], timing["end_time"]
        heard = [word for word in words if start <= (word["start"]+word["end"])/2 < end]
        tokens, times, check = align_words(beat["text"], heard, start, end)
        local, notes = cues_for_beat(tokens, times, start, end)
        check.update(beat=beat["id"], start=start, end=end, readability_notes=notes,
                     interpolated_word_count=sum(word["timing"] == "unmatched-interpolation" for word in times))
        if "unbreakable_word_exceeds_line_width" in notes:
            check["flags"].append("caption_line_too_long")
        cues.extend(local)
        checks.append(check)
    return cues, checks


def validate_report(report, input_key, script, duration):
    if report.get("input_key") != input_key:
        raise p.PipelineError("Caption alignment is stale for narration/text/timing")
    cues = report.get("cues")
    if not isinstance(cues, list) or not cues or report.get("cues_sha256") != p.digest(cues):
        raise p.PipelineError("Caption cues missing or digest does not match")
    actual = " ".join(" ".join(cue["text"].split()) for cue in cues)
    if actual != " ".join(script.split()):
        raise p.PipelineError("Captions do not preserve the complete current script")
    previous = 0
    for cue in cues:
        if (not finite(cue.get("start")) or not finite(cue.get("end"))
                or cue["start"] < previous-.0001 or cue["start"] < 0
                or cue["end"] <= cue["start"] or cue["end"] > duration+1/p.FPS
                or round(cue["end"]*1000) <= round(cue["start"]*1000)):
            raise p.PipelineError("Caption times overlap, have no duration, or exceed audio")
        previous = cue["end"]
    if report.get("flagged_beats"):
        raise p.PipelineError("Unresolved caption alignment beats: "+", ".join(map(str,report["flagged_beats"])))


def model_identity(path):
    if not path.is_dir() or not (path/"model.bin").is_file():
        raise p.PipelineError("--model must point to an existing local faster-whisper model folder; downloads are disabled")
    names = ("model.bin", "config.json", "tokenizer.json", "vocabulary.json", "vocabulary.txt")
    hashes = {name: p.file_hash(path/name) for name in names if (path/name).is_file()}
    return {"path": str(path.resolve()), "files_sha256": hashes}


def transcribe(model, audio, offset=0.0):
    segments, _ = model.transcribe(str(audio), **SETTINGS)
    words, transcripts = [], []
    for segment in segments:
        words.extend(segment_records(segment, offset))
        transcripts.append({"start":float(segment.start)+offset,"end":float(segment.end)+offset,"text":segment.text})
        print(f"ASR {float(segment.end)+offset:.1f}s",flush=True)
    return words, transcripts


def slice_wave(source, destination, start, end):
    with wave.open(str(source), "rb") as wav:
        params = wav.getparams()
        first, last = round(start*params.framerate), round(end*params.framerate)
        wav.setpos(first)
        frames = wav.readframes(last-first)
    with wave.open(str(destination), "wb") as target:
        target.setparams(params)
        target.writeframes(frames)


def report_summary(report):
    return {"method":report["method"],"cues":len(report["cues"]),
            "min_cue_seconds":report["min_cue_seconds"],"flagged_beats":report["flagged_beats"],
            "local_retried_beats":report.get("local_retried_beats",[]),
            "input_key":report["input_key"],"cues_sha256":report["cues_sha256"],
            "listening_review":"Not performed by ASR alignment"}


def run(root, model_path, inspect=False):
    root = root.resolve()
    manifest = p.read_json(root/"episode.json", {})
    if not manifest:
        raise p.PipelineError("episode.json missing")
    timeline = p.check_audio(manifest, root)
    input_key = p.caption_input_key(manifest, root)
    config = manifest.get("captions", {})
    if not isinstance(config, dict):
        raise p.PipelineError("captions must be an object")
    report_path = p.local(root, config.get("alignment_file", "research/caption_alignment.json"))
    script = " ".join(beat["text"] for beat in manifest["beats"])
    duration = timeline[-1]["end_frame"]/p.FPS
    if inspect:
        report = p.read_json(report_path,{})
        validate_report(report,input_key,script,duration)
        print(__import__('json').dumps(report_summary(report),indent=2))
        return
    if model_path is None:
        raise p.PipelineError("--model is required unless --inspect is used")
    identity = model_identity(model_path.resolve())
    cache_key = p.digest({"version":VERSION,"input_key":input_key,"model":identity,"settings":SETTINGS})
    cache_dir = root/"research/asr_cache"/cache_key
    cache_dir.mkdir(parents=True,exist_ok=True)
    cache = p.read_json(cache_dir/"recognized.json",{})
    model = None
    def local_model():
        nonlocal model
        if model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise p.PipelineError("faster-whisper is unavailable in this Python runtime") from exc
            model = WhisperModel(str(model_path.resolve()),device="cpu",compute_type="int8",
                                 cpu_threads=4,local_files_only=True)
        return model
    if cache.get("key") == cache_key and cache.get("words_sha256") == p.digest(cache.get("words")):
        words = cache["words"]
        print("Using matching local ASR cache",flush=True)
    else:
        continuous=p.read_json(root/'research/continuous_split.json',{})
        reusable=(continuous.get('manifest_updated') is True and not continuous.get('flags')
                  and not continuous.get('flagged_beats') and continuous.get('model')==identity
                  and continuous.get('settings')==SETTINGS and continuous.get('script_sha256')==p.digest(script)
                  and continuous.get('pcm_sha256')==pcm_identity(root/'audio/voiceover_v2.wav'))
        source_cache=p.read_json(p.local(root,continuous['recognized_cache']),{}) if reusable else {}
        if reusable and source_cache.get('words_sha256')==p.digest(source_cache.get('words')):
            words,segments=source_cache['words'],source_cache.get('segments',[])
            print('Reusing continuous narration ASR: PCM samples and script match',flush=True)
        else:
            words, segments = transcribe(local_model(),root/"audio/voiceover_v2.wav")
        p.save_json(cache_dir/"recognized.json",{"key":cache_key,"words_sha256":p.digest(words),
                                               "words":words,"segments":segments})
    cues, checks = build_alignment(manifest["beats"],timeline,words)
    retries=[]
    final_words=list(words)
    for beat,timing,check in zip(manifest["beats"],timeline,checks):
        if not check["flags"]:
            continue
        start,end=timing["start_time"],timing["end_time"]
        retry_path=cache_dir/f"beat_{beat['id']:04d}.json"
        retry=p.read_json(retry_path,{})
        if retry.get("key")==cache_key and retry.get("words_sha256")==p.digest(retry.get("words")):
            replacement=retry["words"]
        else:
            chunk=cache_dir/f"beat_{beat['id']:04d}.wav"
            slice_wave(root/"audio/voiceover_v2.wav",chunk,start,end)
            replacement,segments=transcribe(local_model(),chunk,offset=start)
            p.save_json(retry_path,{"key":cache_key,"words_sha256":p.digest(replacement),
                                    "words":replacement,"segments":segments})
        _,_,candidate=align_words(beat["text"],replacement,start,end)
        if (len(candidate["flags"]),-candidate["similarity"],-candidate["matched_word_coverage"]) < (len(check["flags"]),-check["similarity"],-check["matched_word_coverage"]):
            final_words=[word for word in final_words if not start <= (word["start"]+word["end"])/2 < end]+replacement
            retries.append(beat["id"])
    final_words.sort(key=lambda word:(word["start"],word["end"]))
    cues,checks=build_alignment(manifest["beats"],timeline,final_words)
    report={"schema":1,"tool_version":VERSION,"method":METHOD,"input_key":input_key,
            "cues_sha256":p.digest(cues),"cues":cues,"checks":checks,
            "flagged_beats":[check["beat"] for check in checks if check["flags"]],
            "min_cue_seconds":min((cue["end"]-cue["start"] for cue in cues),default=0),
            "model":identity,"settings":SETTINGS,"cache_key":cache_key,
            "recognized_cache":(cache_dir/"recognized.json").relative_to(root).as_posix(),
            "local_retried_beats":retries,"subjective_listening_performed":False,
            "manifest_opt_in":{"captions":{"alignment_file":report_path.relative_to(root).as_posix()}}}
    # Always retain diagnostics, including unresolved beats; fail before generating usable SRT.
    p.save_json(report_path,report)
    validate_report(report,input_key,script,duration)
    output=root/"output/captions.srt"
    subtitles=[f"{index}\n{p.timecode(cue['start'],True)} --> {p.timecode(cue['end'],True)}\n{cue['text']}\n" for index,cue in enumerate(cues,1)]
    p.atomic_bytes(output,"\n".join(subtitles).encode())
    print(__import__('json').dumps(report_summary(report),indent=2))
    if not config.get("alignment_file"):
        print('Set manifest captions.alignment_file to "research/caption_alignment.json" to opt in; manifest was not modified.')



def pcm_identity(path):
    """Hash PCM samples independently of WAV metadata and container header layout."""
    import hashlib
    p.pcm_info(path)
    digest=hashlib.sha256()
    with wave.open(str(path),'rb') as source:
        while frames:=source.readframes(48000):
            digest.update(frames)
    return digest.hexdigest()


def continuous_boundaries(beats,words,duration):
    script=' '.join(beat['text'] for beat in beats)
    tokens,times,whole_check=align_words(script,words,0.0,duration)
    boundaries=[0.0]
    boundary_notes=[]
    used=0
    flags=[]
    for beat in beats[:-1]:
        used+=len(beat['text'].split())
        left,right=times[used-1],times[used]
        value=(left['end']+right['start'])/2
        nearby_anchors_left=[index for index in range(max(0,used-3),used) if times[index]['timing']!='unmatched-interpolation']
        nearby_anchors_right=[index for index in range(used,min(len(times),used+3)) if times[index]['timing']!='unmatched-interpolation']
        if not nearby_anchors_left or not nearby_anchors_right:
            flags.append(f"boundary_after_beat_{beat['id']}_lacks_nearby_anchors")
        if value<=boundaries[-1] or value>=duration:
            flags.append(f"boundary_after_beat_{beat['id']}_not_increasing")
        method='adjacent-word-midpoint' if left['timing']!='unmatched-interpolation' and right['timing']!='unmatched-interpolation' else 'adjacent-scriptword-interpolation-midpoint'
        boundary_notes.append({'after_beat':beat['id'],'seconds':value,'previous_word':tokens[used-1],
                               'next_word':tokens[used],'method':method})
        boundaries.append(value)
    boundaries.append(duration)
    timeline=[{'start_time':boundaries[index],'end_time':boundaries[index+1]} for index in range(len(beats))]
    _,checks=build_alignment(beats,timeline,words)
    return boundaries,boundary_notes,whole_check,checks,flags


def split_continuous(root,source,model_path):
    root,source=root.resolve(),source.resolve()
    manifest=p.read_json(root/'episode.json',{})
    if not manifest or not manifest.get('beats'):
        raise p.PipelineError('episode.json with reviewed beat text is required')
    if model_path is None:
        raise p.PipelineError('--model is required for --split-continuous')
    sample_count=p.pcm_info(source)
    duration=sample_count/p.SAMPLE_RATE
    identity=model_identity(model_path.resolve())
    script=' '.join(beat['text'] for beat in manifest['beats'])
    script_hash=p.digest(script)
    pcm_hash=pcm_identity(source)
    cache_key=p.digest({'version':VERSION,'pcm_sha256':pcm_hash,'script_sha256':script_hash,'model':identity,'settings':SETTINGS})
    cache_dir=root/'research/asr_cache'/('continuous_'+cache_key)
    cache_dir.mkdir(parents=True,exist_ok=True)
    cache_path=cache_dir/'recognized.json'
    cache=p.read_json(cache_path,{})
    if cache.get('key')==cache_key and cache.get('words_sha256')==p.digest(cache.get('words')):
        words=cache['words']
        print('Using matching continuous narration ASR cache',flush=True)
    else:
        from faster_whisper import WhisperModel
        model=WhisperModel(str(model_path.resolve()),device='cpu',compute_type='int8',cpu_threads=4,local_files_only=True)
        words,segments=transcribe(model,source)
        p.save_json(cache_path,{'key':cache_key,'words_sha256':p.digest(words),'words':words,'segments':segments})
    boundaries,notes,whole,checks,flags=continuous_boundaries(manifest['beats'],words,duration)
    # A whole-track decode can skip isolated words or collapse their timestamps.
    # Retry only flagged provisional beats locally before deciding a split is unsafe.
    raw_cache_path=cache_path
    local_retries=[]
    model=None
    for index,(beat,check) in enumerate(zip(manifest['beats'],checks)):
        if not check['flags']:
            continue
        start,end=boundaries[index],boundaries[index+1]
        retry_path=cache_dir/f"split_retry_{beat['id']:04d}.json"
        retry=p.read_json(retry_path,{})
        if retry.get('key')==cache_key and retry.get('words_sha256')==p.digest(retry.get('words')):
            replacement=retry['words']
        else:
            if model is None:
                from faster_whisper import WhisperModel
                model=WhisperModel(str(model_path.resolve()),device='cpu',compute_type='int8',cpu_threads=4,local_files_only=True)
            chunk=cache_dir/f"split_retry_{beat['id']:04d}.wav"
            slice_wave(source,chunk,start,end)
            replacement,retry_segments=transcribe(model,chunk,offset=start)
            p.save_json(retry_path,{'key':cache_key,'words_sha256':p.digest(replacement),'words':replacement,'segments':retry_segments})
        _,_,candidate=align_words(beat['text'],replacement,start,end)
        if (len(candidate['flags']),-candidate['similarity'],-candidate['matched_word_coverage']) < (len(check['flags']),-check['similarity'],-check['matched_word_coverage']):
            words=[word for word in words if not start <= (word['start']+word['end'])/2 < end]+replacement
            words.sort(key=lambda word:(word['start'],word['end']))
            local_retries.append(beat['id'])
    if local_retries:
        cache_path=cache_dir/'recognized_checked.json'
        p.save_json(cache_path,{'key':cache_key,'words_sha256':p.digest(words),'words':words,'segments':[],'raw_recognized_cache':raw_cache_path.relative_to(root).as_posix(),'local_retried_beats':local_retries})
        boundaries,notes,whole,checks,flags=continuous_boundaries(manifest['beats'],words,duration)
    cuts=[round(value*p.SAMPLE_RATE) for value in boundaries]
    cuts[0],cuts[-1]=0,sample_count
    if any(last-first<p.SAMPLE_RATE//10 for first,last in zip(cuts,cuts[1:])):
        flags.append('beat_split_has_less_than_100ms')
    flagged_beats=[check['beat'] for check in checks if check['flags']]
    if whole['flags']:
        flags.append('whole_transcript_alignment_unresolved')
    report={'schema':1,'tool_version':VERSION,'method':METHOD,'source':str(source),'source_sha256':p.file_hash(source),
            'pcm_sha256':pcm_hash,'script_sha256':script_hash,'model':identity,'settings':SETTINGS,
            'cache_key':cache_key,'recognized_cache':cache_path.relative_to(root).as_posix(),
            'words_sha256':p.digest(words),'whole_check':whole,'checks':checks,
            'flagged_beats':flagged_beats,'flags':flags,'boundaries':notes,'cut_samples':cuts,
            'source_samples':sample_count,'manifest_updated':False,'subjective_listening_performed':False,
            'raw_recognized_cache':raw_cache_path.relative_to(root).as_posix(),'local_retried_beats':local_retries}
    report_path=root/'research/continuous_split.json'
    p.save_json(report_path,report)
    if flags or flagged_beats:
        raise p.PipelineError('Continuous narration has unresolved transcript or boundary checks; see research/continuous_split.json. Manifest and imports were not changed.')
    destination=root/'audio/imported'
    destination.mkdir(parents=True,exist_ok=True)
    imported=[]
    import hashlib
    concatenated=hashlib.sha256()
    with wave.open(str(source),'rb') as wav:
        params=wav.getparams()
        for beat,first,last in zip(manifest['beats'],cuts,cuts[1:]):
            wav.setpos(first)
            frames=wav.readframes(last-first)
            concatenated.update(frames)
            path=destination/f"beat{beat['id']:02d}.wav"
            partial=path.with_suffix('.partial.wav')
            with wave.open(str(partial),'wb') as target:
                target.setparams(params)
                target.writeframes(frames)
            partial.replace(path)
            imported.append({'beat':beat['id'],'file':path.relative_to(root).as_posix(),
                             'start_sample':first,'end_sample':last,'samples':last-first,'sha256':p.file_hash(path)})
    if concatenated.hexdigest()!=pcm_hash:
        raise p.PipelineError('Consecutive PCM splits do not reproduce source samples; manifest not changed')
    for beat,item in zip(manifest['beats'],imported):
        beat['narration_file']=item['file']
    report.update(manifest_updated=True,imports=imported,concatenated_pcm_sha256=concatenated.hexdigest(),
                  imported_manifest_sha256=p.digest(manifest),review_status='Script and asset reviews must be regenerated after narration_file associations changed.')
    p.save_json(root/'episode.json',manifest)
    p.save_json(report_path,report)
    print(__import__('json').dumps({'beats':len(imported),'source_seconds':duration,'source_samples':sample_count,
                                  'pcm_samples_identical':True,'flagged_beats':flagged_beats,'report':str(report_path),
                                  'next':'Regenerate manifest reviews, run pipeline audio, then run caption alignment.'},indent=2))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode",type=Path,required=True)
    parser.add_argument("--model",type=Path)
    parser.add_argument("--split-continuous",type=Path,help="Align a continuous mono48k PCM source and import exact consecutive beat samples")
    parser.add_argument("--inspect",action="store_true",help="Validate saved alignment against current audio/script without ASR")
    args=parser.parse_args()
    try:
        if args.split_continuous:
            if args.inspect:
                raise p.PipelineError("--inspect cannot be combined with --split-continuous")
            split_continuous(args.episode,args.split_continuous,args.model)
        else:
            run(args.episode,args.model,args.inspect)
    except (p.PipelineError,OSError,ValueError,KeyError) as exc:
        print(f"Caption alignment failed: {exc}",file=sys.stderr)
        return 1
    return 0

if __name__=="__main__":
    raise SystemExit(main())
