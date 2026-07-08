# CLAUDE.md — PDF Data Extractor (CPAL)

## Project Overview
- Tool that transforms public-records-response PDFs into queryable data for the Child Poverty Action Lab (CPAL).
- Core pipeline shape: `pdf_input → intermediary format → schema mapping → csv_output`.
- Input is ALWAYS a PDF — no other input types exist or will exist. Do not build ingestion abstractions for other formats.
- Long-term: a drop-in component of CPAL data pipelines (`pipeline_start → pdf_data_extractor → further_processing`).
- Timeline: 1-month project. Week 1 ends in a stakeholder demo — one real PDF flowing end-to-end.
- Vision and goals: see `project_management/vision/vision_goal.md`. Primary persona: `project_management/personas/maya_torres.md`.

## Who We Build For
- Maya Torres — nonprofit data analyst. She trusts tools that show their work and spot-checks everything.
- Deliverables must be runnable and maintainable by a Python-literate data analyst after this team leaves. No orphan infrastructure.

## Tech Stack
- Python 3.11+ with a pinned version; use a virtual environment and a committed requirements/lock file.
- PDF parsing: evaluate pdfplumber / PyMuPDF / Camelot against real samples before committing; record the decision in a short note in `docs/decisions/`.
- Intermediary format: normalized structure (JSON or pandas DataFrame) — must be documented before building on it.
- Output: CSV files — this is the confirmed landing target. One CSV per extracted table, with a documented schema (field names, types, nullable fields).

## Immutable Rules
1. NEVER send records-response data to third-party APIs or paid services without explicit stakeholder approval — responses may contain PII.
2. Sample PDFs committed to the repo MUST be sanitized of PII first.
3. Every extracted value MUST be traceable to a source page number (provenance is a core trust requirement).
4. Represent redactions explicitly (flag or null) — never silently drop or guess redacted values.
5. Accuracy over cleverness: prefer a smaller extraction that is verifiably correct to a broader one that is not. Never fabricate or interpolate values the parser did not extract.
6. Scope discipline: OCR for scanned PDFs, multi-format coverage, and pipeline packaging are OUT of scope for the demo sprint unless explicitly re-scoped.

## Conventions
- Scripts accept a PDF path as input and fail loudly with clear error messages — no silent failures.
- Keep changes minimal and single-purpose; separate commits per logical change with clear messages.
- Document schema decisions (field names, types, nullable fields) in the repo before writing code that depends on them.
- When unsure between two approaches, present both with tradeoffs and let the team choose.

## Definition of Done (demo sprint)
- One real records-response PDF runs raw input → intermediary format → CSV output in a single documented, repeatable run.
- Extracted demo values have been manually spot-checked against source pages, with the result recorded.
- Either teammate can run the demo from a fresh clone using only the README.