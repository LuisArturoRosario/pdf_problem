# PDF Data Extractor (CPAL)

Turns public-records-response PDFs (e.g. police records responses) into queryable
CSV data. Every extracted value is traceable to its source page, redactions are
kept as blanks rather than guessed, and nothing the parser didn't read is invented.

The pipeline is: `PDF input → intermediary table → CSV output`.

There are two extraction paths, chosen automatically:

1. **Deterministic engine (default).** Reads native-text PDFs — both ruled tables
   and borderless (coordinate-inferred) tables. This is the trustworthy default and
   is tried first on every file.
2. **AI fallback (opt-in).** If the deterministic engine finds no table — a scanned
   PDF with no text layer, or a native-text layout it can't resolve — you can opt in
   to a Claude vision extractor that reads each page image into rows. Its output is
   **AI-derived and probabilistic**: it is labelled as such and must be spot-checked.
   See [docs/decisions/0003-ai-extraction-fallback.md](docs/decisions/0003-ai-extraction-fallback.md).

---

## Requirements

- **Python 3.14+** (pinned in `.python-version`).
- **[uv](https://docs.astral.sh/uv/)** for dependency management (reads the committed
  `uv.lock`). Install it from the link if you don't have it.
- For the **AI fallback only**: an Anthropic API key (see [AI fallback](#ai-fallback-optional) below).

## Setup (from a fresh clone)

```powershell
# From the repo root — installs the exact locked dependencies into a local .venv
uv sync
```

That's the whole setup for the default (deterministic) path. Verify it works:

```powershell
uv run python main.py --file "samples/Tallahassee PD 2026.pdf" --output output
```

You should see `Extracted 114 rows across 2 page(s) -> output\Tallahassee PD 2026.csv`.

> Sample PDFs and extracted CSVs are **git-ignored** (they may contain PII — see
> [CLAUDE.md](CLAUDE.md) rules 1–2). Put source PDFs somewhere local; the tool never
> commits inputs or outputs.

---

## Usage

```powershell
uv run python main.py --file "<path to PDF>" [--output <dir>] [--start_page N] [--ai-fallback]
```

| Argument         | Meaning                                                                 |
| ---------------- | ----------------------------------------------------------------------- |
| `--file`         | Path to a single PDF to process.                                        |
| `--files`        | A directory of PDFs to process in batch.                                |
| `--output`       | Output folder for the CSV(s). Defaults to the current directory (`.`).  |
| `--start_page N` | Begin extraction at page N (1-based). Useful for skipping cover pages.  |
| `--ai-fallback`  | If normal extraction finds no table, use the AI extractor instead of failing. See below. |

One CSV is written per PDF, named after the PDF (e.g. `Tallahassee PD 2026.csv`).

### Output schema

- One row per extracted record.
- A **`source_page`** column (1-based) on every row — the page each value came from
  (provenance is a core trust requirement).
- Column names come from the table's own header. Empty / redacted cells are left
  **blank (null)**, never guessed.
- Rows produced by the **AI fallback** carry an extra **`extraction_method`** column
  set to `ai_vision`, so AI-derived data is always distinguishable from deterministic
  output. (Deterministic output has no such column.)

---

## AI fallback (optional)

The deterministic engine fails loud (`no tables found`) on PDFs it can't read —
scanned documents with no text layer, or unusual layouts. The AI fallback sends each
page image to the Anthropic Claude API and asks for structured rows.

**It never runs silently.** It only runs when extraction fails *and* you opt in:

- Add **`--ai-fallback`** to the command (for scripts / batch runs), **or**
- run interactively without the flag and answer **`y`** to the prompt.

Without either, a failed extraction still fails loud (it will not call a paid API
unasked).

> ⚠️ **AI output is probabilistic — spot-check it, addresses especially.** No scanned
> extraction is 100% accurate. The model is instructed to transcribe exactly and to
> leave any redacted/blank/illegible cell null, but it can still misread. Always
> verify AI-derived rows against the source pages before trusting them.

### One-time setup: your Anthropic API key

The AI fallback reads your key from the **`ANTHROPIC_API_KEY`** environment variable.
Each person sets their own key — it is never stored in this repo.

1. **Get a key.** Log in at [console.anthropic.com](https://console.anthropic.com)
   with your CPAL account → **API Keys** → **Create Key** → copy the `sk-ant-...`
   value. The CPAL Anthropic organization must have API credits enabled, and your
   account must be a member of it. If login is blocked ("access is blocked by IT
   administrator for this domain"), ask whoever administers CPAL's Anthropic account
   to invite you to the organization or issue you a key.
2. **Save it as a Windows environment variable** (do this once):
   - Press **Windows**, type `environment variables`, open
     **"Edit the environment variables for your account"**.
   - Under **User variables** → **New…** → Name `ANTHROPIC_API_KEY`, Value your
     `sk-ant-...` key → **OK**.
   - Or, in PowerShell:
     ```powershell
     [Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-your-key", "User")
     ```
   - Open a **new** terminal afterwards so the variable is picked up.

Treat the key like a password — never paste it into code, commit it, or share it.

### Running the AI fallback

```powershell
uv run python main.py --file "samples/San Rafael PD pt 1.pdf" --output output --ai-fallback
```

**Cost note:** the AI fallback bills per page (roughly $25–35 for an 800-page file on
the default model; about half that on the cheaper model). Turnaround is minutes, not
seconds. It is opt-in for this reason.

The model is set by one constant in `main.py` (`_AI_MODEL`) if you ever need to switch
between the higher-accuracy and cheaper models.

---

## How it works (for maintainers)

- `main.py` is the whole tool. `extract_tables()` is the deterministic engine;
  `extract_with_ai()` is the AI fallback; `extract_with_fallback()` wires them
  together (deterministic first, AI on opt-in).
- Design decisions and rationale live in [docs/decisions/](docs/decisions/):
  - `0001` / `0002` — the deterministic borderless-table engine.
  - `0003` — the AI extraction fallback (this feature), including the stakeholder
    approval to send records data to the Claude API and the guardrails that keep it
    trustworthy.
- Project constraints and immutable rules are in [CLAUDE.md](CLAUDE.md). Notably: the
  **Redlands PD sample is excluded** from all use, source PDFs must be sanitized of
  PII before being committed, and redactions must always be represented explicitly
  (blank/null), never guessed.
