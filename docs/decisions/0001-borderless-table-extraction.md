# 0001 — Borderless (no-ruled-lines) table extraction

Status: accepted · 2026-07-13 · **detection approach superseded by
[0002](0002-generalized-borderless-engine.md)** (the routing, provenance,
fail-loud, and format decisions here still stand; the header-anchored column
detection and wraps-above-only grouping were generalized in 0002)

## Context

The extractor's clean path uses PyMuPDF `find_tables()`, whose default strategy
builds a table grid from **vector graphics** (drawn lines/rectangles). That works
on the Tallahassee PD sample, whose cells are boxed by 81 drawn rectangles.

Many agency PDFs are *borderless*: the data is in clear rows and columns but there
are **no ruled lines**. On the Walla Walla PD sample, pages have 0–10 vector
drawings and none form a grid, so `find_tables()` returns **0 tables** and the
tool fails loudly with `no tables found` — correct, but useless.

We evaluated three options (see the session investigation):

1. `find_tables(strategy="text")` — built in, but infers column boundaries per
   row from whitespace gaps. On Walla Walla it produced inconsistent column
   counts (16 on p1, 13 on p2) and **split addresses** (`906 N 9TH` | `AVE`),
   violating the location-accuracy rule. Rejected.
2. A purpose-built library (Camelot `stream`, tabula, pdfplumber). Camelot's
   `stream` flavor is designed for this, but it drags in heavy dependencies
   (Ghostscript/OpenCV, or Java) that conflict with "runnable by a Python-literate
   analyst from a fresh clone." Deferred; revisit only if the custom path proves
   insufficient.
3. Custom column inference from PyMuPDF word coordinates. Full control, no new
   dependency, and it lets us enforce the project's accuracy/fail-loud rules.

## Decision

Add a **borderless path** (option 3) as an automatic **fallback**, keeping the
clean ruled-table path unchanged.

### Routing signal (automatic, per page)

For each page: run `find_tables()` first.
- **Tables found** → the existing clean path, byte-for-byte unchanged.
- **No tables but the page has text** → the borderless path.
- **Whole document yields nothing** → the existing `no tables found` error.

A document sticks to one mode once chosen, so a clean doc's non-table page (e.g. a
cover page) is skipped exactly as before rather than misinterpreted.

Rationale for *automatic* over an explicit `--borderless` flag: the tool must
accept any PDF and process directories via `--files`; a single flag can't describe
a mixed batch, and forcing the analyst to pre-classify each file is the manual work
the tool exists to remove. The path is announced on stdout
(`[borderless] page N: … inferred K columns …`) so the tool still shows its work,
and it fails loud when structure can't be read reliably.

### How the borderless path works

1. **Cluster words into visual lines** by their top (`y0`) coordinate.
2. **Detect columns once, from the header row** (the topmost line flush with the
   table's left edge). Header words are grouped into columns by horizontal
   whitespace; each column's **left edge** and joined **name** are recorded. The
   header prints only on page 1 of this document, so the layout is carried to
   continuation pages. Column names become the schema-on-read header, exactly as
   in the clean path.
3. **Assign each word to a column** by its left edge (the right-most column whose
   left edge is `<= x0 + tol`). This keeps multi-word cells (full street
   addresses, long descriptions) intact and leaves untouched columns blank.
4. **Reassemble wrapped (multi-line) cells.** A record is an *anchor* line (its
   first cell — Report Date — is populated, so a word sits at the first column's
   edge) plus any *wrapped* continuation lines above it. Example: an intersection
   address `COTTONWOOD RD / PIKES PEAK` (one line) + `RD` (anchor line) rejoins as
   `COTTONWOOD RD / PIKES PEAK RD`. Words in a column are joined in reading order
   (top-to-bottom, then left-to-right).
5. **Skip page titles and footers.** Lines whose first word does not start at any
   column's left edge (the repeated `NIBRS …` title and `COPY MADE FOR …` stamp,
   which are centered/indented) are not table data and are excluded.

### Fail-loud gates (accuracy over cleverness)

- No header row flush with the table's left edge → not treated as a table.
- Header groups into fewer than `_MIN_BORDERLESS_COLS` (2) columns → not a table.
- Fewer than `_MIN_BORDERLESS_ROWS` (3) anchor rows align under the header → the
  detection is distrusted (guards against false positives on prose/cover pages).
- A wrapped line that cannot be attached to an anchor (unexpected layout) →
  **raise**, rather than silently drop text.
- Whole document produces no rows → the existing `no tables found` error.

### Tuning constants (PDF points; 1 pt = 1/72 inch)

Defined at the top of `main.py`; values chosen from the Walla Walla geometry:

| Constant | Value | Meaning |
|---|---|---|
| `_ROW_Y_TOLERANCE` | 3.0 | words within this vertical distance share a line (rows are ~9 pt apart) |
| `_HEADER_GAP` | 6.0 | header words closer than this are one label; wider starts a new column (intra-label gaps ≤4 pt, inter-column ≥9 pt) |
| `_COL_SNAP_TOLERANCE` | 5.0 | a word may start this far left of a column edge and still belong to it (smallest inter-column gap is ~35 pt, so no cross-column risk) |
| `_LEFT_EDGE_TOLERANCE` | 4.0 | how close a line's start must be to the table's left edge to be the header |
| `_MIN_BORDERLESS_COLS` | 2 | fewer detected columns is not a table |
| `_MIN_BORDERLESS_ROWS` | 3 | anchor rows required to trust a detection |

## Consequences / limitations

- **Assumes left-aligned columns** with stable per-column left edges, and that the
  **first column (Report Date) is always populated** on a data row (used to find
  record anchors). A data row with a blank first cell would be attached to the next
  record. True for these records-response tables; revisit if a future document
  violates it.
- **Assumes wrapped lines sit above their anchor** (bottom-aligned rows, as in this
  document). A cell wrapping *below* its anchor would leave an unattached line and
  **raise** — loud, not silent.
- **Column layout is carried across pages** from the page-1 header; a document that
  changes column structure mid-way is not handled (would misalign or raise).
- Very tight columns where one cell's text bleeds past the next column's left edge
  would merge those columns; not observed in this sample.

Rotated pages and very long PDFs are **out of scope** for this change (separate
tasks).

## Verification (Walla Walla PD.pdf, 85 pages)

- Clean path unchanged: Tallahassee output is **byte-identical** to the prior CSV.
- 4,471 rows extracted across all 85 pages; every row has all 10 columns + `source_page`.
- Line accounting is exhaustive: 4,833 clustered lines = 1 header + 4,471 anchors +
  21 merged wrapped lines + 340 skipped (exactly 2 title + 2 footer lines × 85
  pages). **Nothing table-like is dropped.**
- Addresses (highest-value field) spot-checked, including reconstructed
  intersections (`COTTONWOOD RD / PIKES PEAK RD`, `N ROOSEVELT ST / WALLA WALLA AVE`).
- Genuine blanks preserved: 5 rows have an empty Case Address that is empty in the
  source; Report Date and Case Number are never blank.
