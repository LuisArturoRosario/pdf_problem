# 0003 — AI extraction fallback (scanned & failed native-text PDFs)

Status: accepted · 2026-07-22 · extends the router in [0002](0002-generalized-borderless-engine.md)

## Context

The geometry-driven engine ([0002](0002-generalized-borderless-engine.md)) handles
native-text tabular PDFs and fails loud when it can't read the structure. Two real
gaps remain:

- **Scanned PDFs** have no text layer at all, so both the ruled and borderless
  paths find nothing. OCR/scanned support is in scope for the overall project
  (Immutable Rule #6) though it was deferred past the demo sprint.
- **Some native-text PDFs still fail** — layouts the geometry engine can't resolve
  (non-tabular, heavily irregular, or structure that changes mid-document). Today
  these fail loud with no recourse.

Stakeholders asked for a fallback that turns these into CSV using an AI model, and
**explicitly approved sending records-response data to Anthropic's Claude API** for
this purpose (clearing Immutable Rule #1 for this provider and this use only). They
reviewed and liked the approach.

The design tension: the deterministic engine never invents a value (Rules #4, #5,
#7). A generative model can — it may misread an address, "clean up" a smudge into a
plausible-but-wrong value, or read through a redaction. The fallback is therefore
wrapped in guardrails so it stays a *trustworthy* fallback, not a black box, and its
output is visibly distinct from the deterministic path so Maya knows which rows to
scrutinize.

## Decision

Add an **opt-in AI fallback** that runs only when the normal extraction fails. It
never fires silently.

**Trigger / opt-in.** When `extract_tables()` fails (no table found — which covers
both scanned PDFs with no text layer and native-text layouts the geometry engine
can't resolve), offer the fallback via two channels: a `--ai-fallback` flag
(scripted/pipeline runs) and an interactive `y/N` prompt (hand runs). Absent both,
behaviour is unchanged — the tool still fails loud.

**The fallback — Claude vision → structured rows.** Render each page to an image and
send it to the Claude API (`claude-opus-4-8` by default; the model ID is a single
constant so it can drop to `claude-sonnet-5` if accuracy holds). Page-at-a-time so
each row's `source_page` is assigned by us in code, never by the model — provenance
(Rule #3) is preserved and the per-page loop sidesteps the API's 600-page-per-request
limit on 700–800 page files.

**Considered and rejected: OCR-first (Tesseract → geometry engine).** A deterministic
OCR tier ahead of the model was considered. Rejected because OCR is not
accuracy-guaranteed either — it makes silent character-level errors (`0/O`, `1/l/I`,
`5/S`, digit runs in addresses), precisely on the highest-value field (Rule #7),
with no signal that it was unsure. It would add a system-binary dependency and a
second code path without buying the ~100% location accuracy that would justify it.
Claude vision reads messy scans at least as well and can be instructed to leave a
cell `null` when it can't read it confidently, which raw OCR cannot. May be revisited
if a real sample shows OCR clearly winning on clean scans.

**Guardrails (what keeps the fallback trustworthy):**

1. **Structured outputs** (`output_config.format` with a JSON schema) force
   well-formed rows with explicit `null`s — no fragile text parsing, and the model
   can't wander off-format.
2. **Transcribe-verbatim prompt**: emit `null` for any blacked-out, blank, or
   illegible cell; never guess (Rules #4, #5). The black-box redaction pass
   (`_redact_black_boxes`) still runs first so covered text can't reach the image.
3. **Columns detected once, reused.** The model returns the table's column names on
   the first page; those are pinned and passed back on every later page so rows stay
   aligned to one schema (mirroring the geometry engine's detect-once behaviour). A
   row whose cell count doesn't match is padded with `null` (never a guessed value)
   and logged, not silently realigned.
4. **Output is marked AI-derived** — an `extraction_method` column set to
   `ai_vision` — so it is visibly distinct from the deterministic engine's output and
   Maya knows which rows to scrutinize hardest.
5. **Location accuracy** (Rule #7): a double-pass on address fields, flagging any
   disagreement rather than picking one silently, is a planned follow-up (left out of
   v1 to keep the first cut focused).

**Provider / privacy.** Records data goes only to the Anthropic Claude API, per the
stakeholder approval above. No other third-party service. The Redlands PD sample
remains excluded from all paths (Immutable Rule #8).

## Consequences / limitations

- **Probabilistic by nature.** The AI output can be wrong in ways the deterministic
  engine cannot; the guardrails reduce but do not eliminate this. It is a labelled,
  opt-in fallback — not a replacement for the geometry engine, which stays the
  default and is tried first. No scanned extraction is 100% accurate; output must be
  spot-checked, addresses especially.
- **New dependency.** The `anthropic` SDK (added to `pyproject.toml`) plus an
  `ANTHROPIC_API_KEY` in the environment. The code fails loud with a clear message if
  either is missing — no silent skip.
- **Cost.** Bills per page (~$25–35 for an 800-page file on `claude-opus-4-8`,
  roughly half on `claude-sonnet-5`; prompt-caching the instruction/schema trims it).
  Acceptable under the project's workday-turnaround guidance, but non-zero — hence
  opt-in.
- **Scope.** The transcribe-only, null-on-uncertain contract is the mechanism that
  keeps the fallback inside Rule #5 (never fabricate). Anything it can't read
  confidently is left null, not guessed.

## Verification (to record once run against a real sample with an API key)

- On a scanned and a failed native-text sample: spot-check extracted values
  (addresses especially) against source pages; confirm redactions land as `null`;
  confirm `source_page` provenance on every row and the `ai_vision`
  `extraction_method` label.
- Confirm the deterministic path is byte-identical for PDFs that already worked (the
  fallback must not change existing behaviour) — e.g. re-run Tallahassee and diff.
