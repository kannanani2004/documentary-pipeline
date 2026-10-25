# Manifest reference

Each episode has an episode.json. File references must be relative to its directory; paths escaping that directory are rejected. The included template is intentionally incomplete.

## Core records

| Record | Fields and rules |
| --- | --- |
| Episode | episode_id (lowercase directory-safe), title, summary, packaging_check, sources, claims, beats, assets, chapters |
| Source | id, url, title, publisher, date, retrieved, source_type, excerpt, snapshot |
| Claim | id, text, classification, source_ids, support, status |
| Beat | consecutive integer id starting at 1, text, claim_ids, scene, visual_kind, asset_id |
| Asset | id, file, kind, credit, description, subject_check |
| Chapter | beat, title; ordered unique starts beginning at beat 1 |

IDs must be unique within each record collection. Use real saved source context for snapshot. Source types: primary, direct-interview, secondary. An unknown publication date can be undated.

Claim classifications: documented, reported, interpretation, disputed. All claims must have status checked after review. Reported, interpretive and disputed claims require attribution; its literal text must occur in every narration beat that refers to the claim. Claims whose text contains digits require measurement_check covering units, calculations and source context. A checked field records an operator assertion; it does not validate a claim's truth.

## Assets and narration

Asset kinds and visible labels:

| Kind | Label |
| --- | --- |
| site_evidence | SOURCE MATERIAL |
| context | CONTEXT FOOTAGE |
| diagram | EXPLANATORY DIAGRAM |
| reconstruction | AI RECONSTRUCTION |
| atmosphere | AI ILLUSTRATION |

Each beat's visual_kind must match its selected asset's kind.

Evidence/context assets also require source_url, license, license_url, license_evidence (saved local file), and rights_checked: true. Accepted license identifiers are public-domain, CC0, CC-BY-4.0, CC-BY-SA-4.0 and explicit-commercial-permission. These identifiers do not resolve rights automatically; verify the specific file and follow its conditions.

Diagrams and generated assets require creation_notes. Reconstructions/atmosphere additionally require prompt, generator and not_evidence_checked: true after inspection. Do not present generated readings or diagrams without underlying measurements as instrument evidence.

Footage supports start_seconds. To track reuse across trimmed derivatives, supply source_id and source_start_seconds (the original-source timestamp corresponding to local file zero). fit can be cover (default) or contain (preserves the full frame without still-image zoom).

Optional voice and rate default to en-US-AndrewMultilingualNeural and -2%. A beat's optional narration_file points to supplied audio rather than synthesized speech. Check its transcript manually.

## Editorial policy

Strict reuse checking is opt-in:

~~~json
{
  "editorial_policy": {
    "version": 1,
    "prevent_unplanned_reuse": true,
    "intentional_repeats": [],
    "opening_review": {
      "promise": "The question the film promises to answer",
      "evidence_beat": 1,
      "payoff_beat": 2,
      "notes": "Describe the actual evidence and opening review",
      "max_evidence_seconds": 15,
      "max_payoff_seconds": 60
    }
  }
}
~~~

Choose valid beat IDs for your film; this snippet assumes at least two beats. Opening limits are configurable editorial choices, not platform rules.

An intentional repeat lists asset_id, beats (existing IDs, optionally last) and reason. Checks compare identical still bytes and overlapping source-video intervals. Crops, near-duplicates and repetitions hidden inside compilations still require inspection.

## Captions and optional packaging

captions.alignment_file points to a local alignment JSON with input_key, cues_sha256, method and cues. Each cue has start, end and text. The caption tool writes these fingerprints; do not hand-edit them to bypass stale-input failures. Cues must preserve all script words in order, remain within duration and not overlap. Unresolved flagged_beats block use.

Optional thumbnail fields: file, claim_check, credit and source_asset_id referencing a reviewed asset. Otherwise QA creates a labeled thumbnail from the selected visuals. Optional watch_next entries have actual title and url fields; only the currently implemented supported YouTube URL forms are emitted.

smoke_test: true marks technical fixtures and prevents publication packaging.

## Scope of validation

These tools validate the rules implemented in their Python checks, not a complete JSON Schema. They are designed for operator-authored manifests. Semantic source quality, legal rights, compelling editing, caption readability and complete listening remain human responsibilities.