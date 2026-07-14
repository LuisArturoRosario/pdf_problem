# 0002 — Generalized geometry-driven borderless engine

Status: accepted · 2026-07-14 · supersedes the detection approach in [0001](0001-borderless-table-extraction.md)

## Context

The borderless path has been generalized in response to successive real samples,
each of which broke an over-fitted assumption in the prior version:

- **Rome PD** (114 pp): grouped report with `Total:` subtotal lines; header labels
  *offset* from their data (`Address` label right of the data); sub-lines *below*
  the record (Weapon), the opposite of Walla Walla.
- **Austin (MN) PD** (1,806 pp): tall **multi-line records** (a single incident
  spans up to 4 lines as Location/Statute/IBR wrap); continuation lines *outnumber*
  record lines (so "the most common left edge is the record column" was wrong); the
  header labels run *closer together* than the data columns (the opposite of Rome).

Per-file "families" don't scale — every agency PDF has a different structure. The
goal is one engine that reads structure from geometry and works on native-text
tabular PDFs regardless of layout, or fails loud when it can't read the structure.

## Decision

Keep the router (ruled `find_tables()` first; borderless fallback when a page has
text but no ruled table; fail loud if nothing is extractable). The borderless
engine reads everything from geometry — no per-file logic, filenames, or
hard-coded positions:

1. **Lines** — cluster words by their top (`y`) coordinate.
2. **Find the header and record column from "wide" lines.** A line spanning most
   of the table width is a header or a full record row; narrow lines (titles,
   sub-lines, subtotals, footers) are set aside. The record column `col0` is the
   most common left edge among wide lines; the **header** is the topmost wide line
   starting there. Using *wide* lines (not raw frequency) is what makes this
   correct on Austin, where continuation lines outnumber record lines.
3. **Columns come from the data, not the header.** Cluster word-start positions;
   keep a cluster as a column edge only if, in the **majority of rows**, its words
   have whitespace to their left (gap > `_INTRA_CELL_GAP`). This drops mid-cell
   pile-ups — a street name after a house number, `Police` after `Rome`, a time
   after a date, a wrapped second word — while keeping true edges even in packed
   tables where an occasional long value from the left column reaches close. It
   handles headers offset from data (Rome) and headers narrower than data (Austin)
   because the header is not used for boundaries.
4. **Recover header-declared columns.** A column whose data is blank on the
   detection page (e.g. a mostly-empty Weapon column — Austin SO has no weapon on
   page 1) leaves no data cluster. Each header label whose left edge sits well
   away (> `_COL_MERGE_TOL`) from every data column is added as its own column, so
   the schema is complete; a label merely *offset* from its data (Rome's
   `Address`, ~12 pt) stays within tolerance and is not duplicated.
5. **Names** — bin the header words into the detected columns and join per column.
6. **Records group by anchor, with detected alignment.** An *anchor* line starts
   at `col0` (its key cell is populated). Continuation lines attach by:
   - **top-aligned** (tall records grow downward — detected when any anchor band
     holds ≥2 continuations, e.g. Austin): each continuation joins the nearest
     anchor *at or above* it — correct even when a lower anchor is geometrically
     nearer.
   - otherwise (short records — Walla Walla, Rome): nearest anchor, ties broken to
     the anchor *below*, which reconstructs cells that wrap upward as well as
     ordinary sub-lines below.
   Words are binned into columns by left edge and joined per column in reading
   order (top-to-bottom, then left-to-right).
7. **Skip non-record lines** — lines starting at no column edge (titles, `Total:`
   subtotals, footers) are excluded, never mixed into data.
8. **Fail loud** when no columns are found, or table text can't be attached to any
   record.

**Robustness & performance.** Detection uses wide lines and majority rules, so a
single odd row can't force a bogus layout (the earlier version mis-detected a
2-column layout mid-document and ran ~1,485 pages before failing). Once a document
is known borderless, the costly `find_tables()` probe is skipped on later pages —
Austin's 1,806 pages dropped from ~2m24s to ~6s.

## Tuning constants (PDF points)

`_WIDE_LINE_FRAC` 0.6 (record/header line width), `_X_CLUSTER_TOL` 3 (word-start
clustering), `_COL_SNAP_TOLERANCE` 5 (word "starts at" a column edge),
`_INTRA_CELL_GAP` 9 (gap below which a word joins the cell to its left rather than
starting a column — measured bimodal split: intra-cell ≤7.5, inter-column ≥10.8
across samples), `_COL_MERGE_TOL` 20 (a header label this far from every data
column is a distinct, sparsely-populated column), `_COL_MIN_LINES` 2,
`_MIN_BORDERLESS_COLS` 2, `_MIN_BORDERLESS_ROWS` 3.

## Consequences / limitations

- **Assumes left-aligned columns** and that the **first column is populated on
  every record** (used to find anchors). Holds for all samples.
- **Column layout and alignment are detected on the first borderless page and
  reused**; a document that changes structure mid-way is not handled. A column
  blank on page 1 is still recovered if the header names it (step 4); a column
  that is *both* unnamed in the header *and* blank on page 1 would be missed.
- **Alignment for uniform-grid tables**: if a document has *only* short (≤2-line)
  records it is treated with the nearest-anchor/tie-below rule; a genuinely
  tall **bottom-aligned** layout (continuations above a bottom anchor, 3+ lines)
  is not detected and would misgroup. Not seen in any sample.
- **Group `Total:` subtotal rows are dropped** as aggregates (one row per record).
- Still **native-text only** — scanned/image PDFs (OCR) and non-tabular documents
  are out of scope; unreadable structure fails loud rather than guessing.

## Verification (six samples, one engine, no per-file code)

| Sample | Structure | Result |
|---|---|---|
| Tallahassee | ruled grid | **byte-identical** to pre-change CSV |
| Walla Walla | borderless, sub-lines above | **byte-identical** to verified CSV; 74,044 tokens, 0 mismatched pages |
| Rome | borderless, grouped, offset header, sub-lines below | **byte-identical** to verified CSV; 39,004 tokens, 0 mismatched pages |
| synthetic (blind) | 5-col permits, new positions/names, wrap below | correct columns, wrapped cell rejoined, blanks preserved |
| Austin (MN) PD | borderless, 1,806 pp, tall multi-line records, top-aligned | 26,996 rows; 604,165 tokens, **0 mismatched pages**; wrapped Statute/Location/IBR rejoined |
| Austin (MN) SO | borderless, 758 pp, Weapon column blank on page 1 | 9,518 rows; 245,112 tokens, **0 mismatched pages**; Weapon recovered as its own column via the header |

Austin PD and SO were both genuine held-out tests (run on the unchanged engine
first): PD passed as-is; SO surfaced the sparse-column case, which became a
generalization (step 4), not a per-file patch. Token completeness checks
page-level presence and no-fabrication; column placement is checked by pattern
conformance on structured columns plus manual spot-checks — free-text columns are
not machine-verifiable and remain a spot-check item.
