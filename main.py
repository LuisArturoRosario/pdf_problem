import pymupdf as pdf
import pandas as pd
import argparse
import base64
import bisect
import json
import os
import sys
from collections import Counter
from pathlib import Path


# --- Borderless-table geometry knobs (PDF points; 1 pt = 1/72 inch) ----------
# These are the only tuning constants for the coordinate-based (borderless) path.
# They are documented, with the rationale for each value, in
# docs/decisions/0002-generalized-borderless-engine.md.
_ROW_Y_TOLERANCE = 3.0      # words within this vertical distance share one line
_X_CLUSTER_TOL = 3.0        # word starts within this horizontal distance are one column edge
_COL_SNAP_TOLERANCE = 5.0   # how close a word's start must be to a column edge to "start" there
_WIDE_LINE_FRAC = 0.6       # a line spanning >= this fraction of table width is a record/header line
_COL_MIN_LINES = 2          # a data start must recur in this many lines to anchor a column edge
_INTRA_CELL_GAP = 9.0       # words closer than this to a left neighbor are the same cell, not a new column
_COL_MERGE_TOL = 20.0       # a header label farther than this from every data column is its own (sparse) column
_MIN_BORDERLESS_COLS = 2    # fewer columns than this is not a table
_MIN_BORDERLESS_ROWS = 3    # need at least this many record rows to trust a detection

# A redaction box is painted (near-)black. Every RGB channel at or below this
# (0.0 = pure black, 1.0 = white) counts as an opaque black box.
_REDACTION_MAX_CHANNEL = 0.15
# A redaction box covers text, so it has real width AND height. Ruled table
# lines are also filled black but are hairline-thin in one dimension; requiring
# both sides to exceed this (PDF points) keeps boxes and excludes grid lines.
_REDACTION_MIN_BOX_PT = 3.0

# Quarter-turns (90 deg clockwise) that straighten each dominant text-writing
# direction. PyMuPDF reports a line's direction as a unit vector: (1, 0) is
# ordinary left-to-right text; the others are pages drawn sideways or upside
# down. Used to re-orient native-text pages before column detection.
_DIR_TO_TURNS = {(1, 0): 0, (0, -1): 1, (-1, 0): 2, (0, 1): 3}


# --- AI extraction fallback (Claude vision) ----------------------------------
# Runs ONLY when the deterministic engine finds no table AND the user opts in
# (--ai-fallback, or an interactive prompt). It sends each page as an image to
# the Claude API and asks for structured rows. This path is probabilistic, so
# its output is labelled ai_vision and must be spot-checked. Rationale, guard-
# rails, and the stakeholder data-sharing approval are in
# docs/decisions/0003-ai-extraction-fallback.md.
_AI_MODEL = "claude-opus-4-8"    # single knob; swap to "claude-sonnet-5" to trade cost for a small accuracy margin
_AI_RENDER_DPI = 200             # page-image resolution sent to the model
_AI_MAX_TOKENS = 16000           # per-page output ceiling (one page of rows is small)
_AI_METHOD_LABEL = "ai_vision"   # value written to the extraction_method provenance column

# The model must transcribe, never invent. Blacked-out/blank/illegible cells
# become null so redactions stay null (Rules #4/#5) and locations are never
# guessed (Rule #7). Cached across pages (it never changes) to cut cost.
_AI_SYSTEM = (
    "You transcribe a single page of a public-records table into structured rows.\n"
    "Rules you must follow exactly:\n"
    "- Transcribe every value EXACTLY as printed. Never correct spelling, expand "
    "abbreviations, reformat dates/numbers, or infer a value from context.\n"
    "- If a cell is blacked-out/redacted, blank, or illegible, output null. NEVER "
    "guess a value that is not clearly readable.\n"
    "- Return every data row on the page. Do NOT emit page titles, column-header "
    "rows, footers, or subtotal/total lines as data rows.\n"
    "- Conform to the JSON schema: a 'columns' array (the table's column headers, "
    "left to right) and a 'rows' array in which each row is an array of cells "
    "aligned to 'columns' in the same order. Each cell is a string or null."
)

# Structured-output schema: dynamic columns (we don't know them ahead of time),
# rows as arrays aligned to those columns, cells nullable. Enforced by the API so
# there is no free-text parsing and the model cannot drift off-format.
_AI_SCHEMA = {
    "type": "object",
    "properties": {
        "columns": {"type": "array", "items": {"type": "string"}},
        "rows": {
            "type": "array",
            "items": {
                "type": "array",
                "items": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            },
        },
    },
    "required": ["columns", "rows"],
    "additionalProperties": False,
}


def _redact_black_boxes(page):
    """Blank out text hidden under opaque black boxes (reversible redactions).

    Some records responses "redact" a value by drawing a filled black rectangle
    *over* text that is still present in the PDF's text layer. A naive extractor
    reads the value straight through the box -- the redaction is only visual.
    Here we find those black boxes and delete the text underneath them, so the
    cell extracts as blank (None -> null in the CSV) instead of leaking the
    hidden value. We remove only the covered text -- never ruled lines or images
    -- so table detection downstream is unaffected.

    Returns the number of black boxes found (for logging/provenance).

    Ruled table lines are also filled black but hairline-thin, so we keep only
    box-shaped fills (see ``_REDACTION_MIN_BOX_PT``) and leave grid lines alone.

    Known limit: a dark rectangle used purely as a design element (e.g. a header
    bar with light text on top) would also be treated as a redaction. These
    plain records tables don't use them; revisit if a real sample does.
    """
    boxes = []
    for drawing in page.get_drawings():
        fill = drawing.get("fill")
        # Keep only *filled*, (near-)black shapes that enclose real area.
        if fill is None or max(fill) > _REDACTION_MAX_CHANNEL:
            continue
        rect = drawing["rect"]
        # Require a box shape: skip hairline-thin shapes, which are ruled table
        # lines (also filled black) rather than redactions covering text.
        if rect.width < _REDACTION_MIN_BOX_PT or rect.height < _REDACTION_MIN_BOX_PT:
            continue
        boxes.append(rect)

    for rect in boxes:
        page.add_redact_annot(rect)
    if boxes:
        # Remove only text under the boxes; leave line art and images intact.
        page.apply_redactions(
            images=pdf.PDF_REDACT_IMAGE_NONE,
            graphics=pdf.PDF_REDACT_LINE_ART_NONE,
        )
    return len(boxes)


def _clean_cell(value):
    """Normalize one extracted cell.

    Collapses internal newlines/whitespace (PyMuPDF returns header cells like
    'Domestic\\nViolence\\nCode') and turns genuinely-empty cells into None so
    they land as blanks in the CSV. Never invents a value that wasn't there.
    """
    if value is None:
        return None
    text = " ".join(value.split())
    return text if text != "" else None


def _upright_turns(page):
    """How many 90-deg clockwise turns make this page's text read upright.

    A few native-text PDFs draw an entire table sideways -- e.g. a landscape
    report placed on a portrait page (San Rafael is the scanned look-alike of
    this). PyMuPDF then returns word coordinates in that sideways space, so the
    column detection -- which assumes text reads left-to-right -- mis-parses the
    page. We read the dominant writing direction across the page's text lines
    and return the turns needed to straighten it. Returns 0 for ordinary upright
    pages, so they are left completely untouched.
    """
    seen = Counter()
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            direction = (round(line["dir"][0]), round(line["dir"][1]))
            if direction in _DIR_TO_TURNS:
                seen[direction] += len(line.get("spans", []))
    if not seen:
        return 0
    dominant = seen.most_common(1)[0][0]
    return _DIR_TO_TURNS[dominant]


def _rotate_words_upright(words, page_rect, turns):
    """Rotate word boxes by ``turns`` 90-deg clockwise turns into upright space.

    Returns new ``(x0, y0, x1, y1, text)`` tuples so the existing row/column
    detection runs unchanged; ``turns == 0`` returns the words as-is. Only the
    geometry is rotated -- each word's text is left exactly as extracted.
    """
    if turns == 0:
        return words
    w, h = page_rect.width, page_rect.height

    def rotate(x, y):
        if turns == 1:          # 90 clockwise;      page becomes h x w
            return h - y, x
        if turns == 2:          # 180;               page stays  w x h
            return w - x, h - y
        return y, w - x         # 270 (turns == 3);  page becomes h x w

    rotated = []
    for word in words:
        ax, ay = rotate(word[0], word[1])
        bx, by = rotate(word[2], word[3])
        rotated.append((min(ax, bx), min(ay, by), max(ax, bx), max(ay, by), word[4]))
    return rotated


def _cluster_rows(words, tol=_ROW_Y_TOLERANCE):
    """Group PyMuPDF words into visual rows by their top (y0) coordinate.

    ``words`` are (x0, y0, x1, y1, text, ...) tuples. Returns rows sorted
    top-to-bottom; each row is ``(y0, [words sorted left-to-right])``.
    """
    rows = []
    for w in sorted(words, key=lambda w: w[1]):  # ascending y0
        if rows and abs(w[1] - rows[-1][0]) <= tol:
            rows[-1][1].append(w)
        else:
            rows.append([w[1], [w]])
    return [(y, sorted(ws, key=lambda w: w[0])) for y, ws in rows]


def _cluster_starts(values, tol=_X_CLUSTER_TOL):
    """Cluster 1-D word-start positions; return ``[(center, count), ...]``.

    Left-aligned columns make word starts pile up at each column's left edge, so
    a dense cluster marks a real edge. Used to snap header edges onto the data.
    """
    clusters = []
    for v in sorted(values):
        if clusters and v - clusters[-1][-1] <= tol:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    return [(sum(c) / len(c), len(c)) for c in clusters]


def _header_group_lefts(header_words, gap=_INTRA_CELL_GAP):
    """Left edge of each header *label* (words grouped by whitespace).

    Consecutive header words closer than ``gap`` are one label ("Reported
    Date/Time", "Case Number"); a wider gap starts a new label. Returns the left
    x of each label. Used to recover a column the header declares but whose data
    is blank on the detection page (e.g. a mostly-empty Weapon column).
    """
    lefts, run_x1 = [], None
    for w in sorted(header_words, key=lambda w: w[0]):
        if run_x1 is None or w[0] - run_x1 > gap:
            lefts.append(w[0])
            run_x1 = w[2]
        else:
            run_x1 = max(run_x1, w[2])
    return lefts


def _keep_column_starts(starts, data_lines, tol=_X_CLUSTER_TOL, gap=_INTRA_CELL_GAP):
    """Keep only start-clusters that begin a column (whitespace to their left).

    For each cluster, look at the lines where a word starts there and check
    whether another word ends just to its left (a gap <= ``gap`` means they're in
    the same cell). A cluster whose words usually touch a left neighbour is
    mid-cell — a street name right after a house number, or 'Police' after 'Rome',
    or a time after a date — and is dropped. A true column edge has whitespace
    before it in most rows and is kept. This works for packed tables (where an
    occasional long value from the left column reaches close) because it takes the
    majority across rows, not any single row.
    """
    kept = []
    for c in starts:
        total = touching = 0
        for _, ws in data_lines:
            for j, w in enumerate(ws):
                if abs(w[0] - c) <= tol:
                    total += 1
                    if j > 0 and w[0] - ws[j - 1][2] <= gap:
                        touching += 1
                    break
        if total == 0 or touching / total < 0.5:
            kept.append(c)
    return kept


def _starts_at_column(x0, col_lefts, tol=_COL_SNAP_TOLERANCE):
    """Index of the column whose left edge ``x0`` aligns with (within ``tol``).

    Returns None when ``x0`` is not near any column's left edge — the signal that
    a line is a centered title, an indented group subtotal, or a footer stamp,
    not a table row. Anchor (record) lines start at column 0; continuation and
    sub-lines start at a later column's edge.
    """
    for i, left in enumerate(col_lefts):
        if abs(x0 - left) <= tol:
            return i
    return None


def _assign_columns(row_words, col_lefts, tol=_COL_SNAP_TOLERANCE):
    """Bin words into columns and join the words within each column.

    A word belongs to the right-most column whose left edge is <= word.x0 + tol.
    ``row_words`` may span several lines of one record — a cell wrapped onto
    another line, or a sub-line printed above or below the row; within a column
    the words are joined in reading order (top-to-bottom, then left-to-right), so
    a two-line address rejoins intact. An untouched column stays None so blanks
    stay blank — never guessed or filled.
    """
    buckets = [[] for _ in col_lefts]
    for w in row_words:
        idx = 0
        for i, left in enumerate(col_lefts):
            if w[0] + tol >= left:
                idx = i
            else:
                break
        buckets[idx].append(w)
    cells = []
    for b in buckets:
        if not b:
            cells.append(None)
        else:
            b.sort(key=lambda w: (w[1], w[0]))  # reading order across a record's lines
            cells.append(_clean_cell(" ".join(w[4] for w in b)))
    return cells


def _line_span(ws):
    """Horizontal extent of a line (rightmost x1 minus leftmost x0)."""
    return max(w[2] for w in ws) - ws[0][0]


def _classify(lines, col_lefts):
    """Split lines into (anchors, continuations).

    An *anchor* starts at column 0 — its key cell (Case/Date/ID) is populated, so
    it begins a record. A *continuation* starts at a later column's edge — a
    wrapped cell or a sub-line. Lines starting at no column edge (titles, group
    subtotals, footers) are dropped. Each entry is ``(y, words)``.
    """
    anchors, continuations = [], []
    for y, ws in lines:
        si = _starts_at_column(ws[0][0], col_lefts)
        if si is None:
            continue
        (anchors if si == 0 else continuations).append((y, ws))
    return anchors, continuations


def _is_top_aligned(anchors, continuations):
    """Whether records grow downward from their anchor (top-aligned).

    True when any record spans several continuation lines: a band between two
    anchors holding >= 2 continuations is a tall record whose lines can only
    belong to the anchor that starts it, which is only consistent with
    top-alignment. Short (<=1 continuation) records stay ambiguous and are left
    to the nearest-anchor rule, so this returns False for them.
    """
    if not anchors:
        return False
    anchor_ys = sorted(y for y, _ in anchors)
    counts = [0] * len(anchor_ys)
    for y, _ in continuations:
        i = bisect.bisect_right(anchor_ys, y) - 1
        counts[max(i, 0)] += 1
    return any(c >= 2 for c in counts)


def _detect_borderless_layout(lines):
    """Detect a borderless table's columns, header, and record alignment.

    Returns ``(col_lefts, header_names, top_aligned)``, or None if the page
    doesn't look like a table. Strategy, all read from geometry (no per-file
    assumptions):

    * **Wide lines** — those spanning most of the table width — are the header and
      the full record rows; narrow lines (titles, sub-lines, subtotals) are set
      aside. The record column ``col0`` is the most common left edge among wide
      lines, and the header is the topmost wide line starting there.
    * **Columns come from the data**: dense clusters of word-start positions that
      have whitespace to their left (true left edges, not mid-cell pile-ups). This
      handles headers whose labels are offset from, or run closer together than,
      their data.
    * **Names** are the header words binned into those columns.
    * **Alignment** (top vs. ambiguous) is detected from tall records.
    """
    all_words = [w for _, ws in lines for w in ws]
    if not all_words:
        return None
    gmin = min(w[0] for w in all_words)
    gwidth = max(w[2] for w in all_words) - gmin
    if gwidth <= 0:
        return None
    wide = [(y, ws) for y, ws in lines if _line_span(ws) >= _WIDE_LINE_FRAC * gwidth]
    if len(wide) < _MIN_BORDERLESS_ROWS + 1:  # need a header plus enough record rows
        return None
    col0 = Counter(round(ws[0][0]) for _, ws in wide).most_common(1)[0][0]
    header_line = next(
        (ws for _, ws in wide if abs(ws[0][0] - col0) <= _COL_SNAP_TOLERANCE), None
    )
    if header_line is None:
        return None

    # Table lines start at or right of the record column; this drops centered
    # titles and left-of-table group subtotals from column detection.
    table_lines = [(y, ws) for y, ws in lines if ws[0][0] >= col0 - _COL_SNAP_TOLERANCE]
    data_lines = [(y, ws) for y, ws in table_lines if ws is not header_line]
    data_starts = [w[0] for _, ws in data_lines for w in ws]
    strong = [c for c, n in _cluster_starts(data_starts) if n >= _COL_MIN_LINES]
    col_lefts = _keep_column_starts(strong, data_lines)
    # Recover columns the header declares but whose data is blank on this page
    # (e.g. a mostly-empty Weapon column): add a header label only when it sits
    # well away from every data column, so an offset label isn't duplicated.
    for h in _header_group_lefts(header_line):
        if all(abs(h - c) > _COL_MERGE_TOL for c in col_lefts):
            col_lefts.append(h)
    col_lefts.sort()
    if len(col_lefts) < _MIN_BORDERLESS_COLS:
        return None

    anchors, continuations = _classify(data_lines, col_lefts)
    if len(anchors) < _MIN_BORDERLESS_ROWS:
        return None  # a header-like row but no real body; distrust it

    names = _assign_columns(header_line, col_lefts)
    header = [n if n else f"column_{i + 1}" for i, n in enumerate(names)]
    top_aligned = _is_top_aligned(anchors, continuations)
    return col_lefts, header, top_aligned


def _extract_borderless_page(pdf_path, words, page_number, col_lefts, header, top_aligned):
    """Extract one page with no ruled lines, using word x-positions.

    Columns/header/alignment are detected on the first borderless page and reused
    on later pages (the header prints once). A record is an *anchor* line — one
    that starts at the first column, so its key cell (Case/Date/ID) is populated —
    together with any *continuation* lines: a wrapped cell or a sub-line.

    Continuations are attached to a record two ways:
      * **top-aligned** (tall records grow downward, as in Austin): each
        continuation joins the nearest anchor **at or above** it — correct even
        when a lower anchor is geometrically nearer.
      * otherwise (short records, as in Walla Walla/Rome): the nearest anchor,
        ties broken to the anchor **below** — which reconstructs cells that wrap
        upward as well as ordinary sub-lines below.

    Lines starting at no column edge (titles, group ``Total:`` subtotals, footers)
    are skipped, never silently mixed into data. Returns
    ``(col_lefts, header, top_aligned, records)``.
    """
    lines = _cluster_rows(words)
    if not lines:
        return col_lefts, header, top_aligned, []

    if col_lefts is None:
        detected = _detect_borderless_layout(lines)
        if detected is None:
            return None, header, top_aligned, []  # not a table; leave state untouched
        col_lefts, header, top_aligned = detected
        print(
            f"  [borderless] page {page_number}: no ruled lines; inferred "
            f"{len(header)} columns from word positions"
            f"{' (multi-line records)' if top_aligned else ''}"
        )

    anchors, continuations = _classify(lines, col_lefts)
    if not anchors:
        if continuations:
            raise ValueError(
                f"{pdf_path}: page {page_number} has table text but no record rows "
                "to attach it to; refusing to guess."
            )
        return col_lefts, header, top_aligned, []

    anchor_ys = [y for y, _ in anchors]
    extra = [[] for _ in anchors]
    for y, ws in continuations:
        if top_aligned:
            i = bisect.bisect_right(anchor_ys, y) - 1  # nearest anchor at or above
            i = max(i, 0)
        else:
            i = min(range(len(anchors)),
                    key=lambda k: (abs(anchor_ys[k] - y), -anchor_ys[k]))
        extra[i].extend(ws)

    records = []
    for i, (_, ws) in enumerate(anchors):
        cells = _assign_columns(list(ws) + extra[i], col_lefts)
        if cells == header:
            continue  # the header line itself, or a repeated header on a later page
        record = {"source_page": page_number}
        record.update(zip(header, cells))
        records.append(record)
    return col_lefts, header, top_aligned, records


def extract_tables(pdf_path):
    """EXTRACT stage: read tables from a PDF into an intermediary DataFrame.

    Two extraction paths, chosen automatically per page:
      * ruled tables (clean) via PyMuPDF ``find_tables()`` line detection, and
      * borderless tables inferred from word x-positions when a page has text but
        no ruled table.
    Both produce the same container: one row per record, a 1-based
    ``source_page`` provenance column, string/None cells, nothing fabricated.
    Fails loudly if no table can be found or a ruled row's column count doesn't
    match the header.
    """
    doc = pdf.open(str(pdf_path))
    if doc.page_count < args.start_page:
        raise ValueError(f"{pdf_path}: --start_page is beyond the number of pages in the pdf.")

    header = None
    col_lefts = None        # borderless column left-edges, once detected
    top_aligned = False     # borderless record alignment, once detected
    mode = None             # None | "clean" | "borderless" — keep one doc consistent
    records = []
    try:
        for page_index, page in enumerate(doc):
            page_number = page_index + 1  # provenance: pages are 1-based to humans
            if page_number < args.start_page:
                continue
            # Blank text hidden under black boxes first, so both paths below read
            # a redacted cell as empty rather than leaking the value beneath it.
            boxes = _redact_black_boxes(page)
            if boxes:
                print(
                    f"  [redaction] page {page_number}: blanked text under "
                    f"{boxes} black box(es)"
                )
            # Once a doc is known borderless, skip the (costly) ruled-table probe.
            tables = [] if mode == "borderless" else page.find_tables().tables
            if tables:
                if mode is None:
                    mode = "clean"
                if mode != "clean":
                    continue  # a borderless doc with a stray ruled page; don't mix layouts
                # CLEAN PATH: ruled tables via PyMuPDF line detection (unchanged).
                for table in tables:
                    for row in table.extract():
                        cleaned = [_clean_cell(c) for c in row]
                        if header is None:
                            header = cleaned
                            continue
                        if cleaned == header:
                            continue  # repeated header on a continuation page
                        if len(cleaned) != len(header):
                            raise ValueError(
                                f"{pdf_path}: page {page_number} has a row with "
                                f"{len(cleaned)} cells but the header has {len(header)}; "
                                "refusing to guess how columns align."
                            )
                        record = {"source_page": page_number}
                        record.update(zip(header, cleaned))
                        records.append(record)
            else:
                # No ruled table on this page.
                if mode == "clean":
                    continue  # clean doc, a page without a table -> skip it
                words = page.get_text("words")
                if not words:
                    continue  # genuinely empty page
                # Straighten native-text pages whose table is drawn sideways, so
                # the column logic (which assumes left-to-right text) can read
                # it. Upright pages return turns == 0 and are left unchanged.
                turns = _upright_turns(page)
                if turns:
                    words = _rotate_words_upright(words, page.rect, turns)
                    print(
                        f"  [orientation] page {page_number}: text drawn "
                        "sideways; rotated upright before extraction"
                    )
                # BORDERLESS PATH: infer columns from word x-positions.
                col_lefts, header, top_aligned, page_records = _extract_borderless_page(
                    pdf_path, words, page_number, col_lefts, header, top_aligned
                )
                if col_lefts is not None and mode is None:
                    mode = "borderless"
                records.extend(page_records)
    finally:
        doc.close()

    if header is None:
        raise ValueError(f"{pdf_path}: no tables found in the PDF.")

    return pd.DataFrame(records, columns=["source_page"] + header)


def _ai_client():
    """Build a Claude client, failing loud if the SDK or API key is missing.

    We require ANTHROPIC_API_KEY explicitly rather than relying on other credential
    sources, so a misconfigured run says exactly what to fix instead of erroring
    deep inside the first request.
    """
    try:
        import anthropic
    except ModuleNotFoundError as exc:  # pragma: no cover - environment guard
        raise ValueError(
            "AI fallback needs the 'anthropic' package. Install it with: uv add anthropic"
        ) from exc
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ValueError(
            "AI fallback needs the ANTHROPIC_API_KEY environment variable to be set."
        )
    return anthropic.Anthropic()


def _ai_page_image_b64(page):
    """Blank redactions, then render the page to a base64 PNG for the model.

    ``_redact_black_boxes`` runs first so text hidden under a black box is deleted
    from the text layer; the black box itself stays drawn, so the model sees an
    opaque box (and emits null) rather than the value beneath it.
    """
    _redact_black_boxes(page)
    pixmap = page.get_pixmap(dpi=_AI_RENDER_DPI)
    return base64.standard_b64encode(pixmap.tobytes("png")).decode("ascii")


def _ai_header(columns):
    """Normalize the model's column names; fill any blank with a positional name."""
    names = [_clean_cell(c) for c in columns]
    return [n if n else f"column_{i + 1}" for i, n in enumerate(names)]


def _ai_align_row(row, header, page_number):
    """Align one model row to the established columns without guessing.

    Short rows are padded with None (a blank, never an invented value); over-long
    rows keep the leading cells. Both cases are logged, never silent, so a
    spot-checker can see where the model and the schema disagreed.
    """
    cells = [_clean_cell(c) for c in row]
    if len(cells) < len(header):
        print(
            f"  [ai-fallback] page {page_number}: row had "
            f"{len(cells)} cells for {len(header)} columns; padded the rest with null"
        )
        cells = cells + [None] * (len(header) - len(cells))
    elif len(cells) > len(header):
        print(
            f"  [ai-fallback] page {page_number}: row had "
            f"{len(cells)} cells for {len(header)} columns; kept the first {len(header)}"
        )
        cells = cells[: len(header)]
    return cells


def _ai_extract_page(client, image_b64, page_number, columns):
    """Ask the model for structured rows from one page image.

    On the first page ``columns`` is None and the model infers the header; on
    later pages the established columns are passed back so rows stay aligned to one
    schema. Returns ``(columns, rows)`` exactly as the model gave them; alignment
    and cleaning happen in the caller.
    """
    if columns is None:
        instruction = (
            "Identify the table's columns from its header row and return them in "
            "'columns'. Then return every data row on the page, each aligned to "
            "those columns."
        )
    else:
        instruction = (
            "Use EXACTLY these columns, in this order, and return them unchanged in "
            "'columns':\n" + " | ".join(columns) + "\n"
            "Return every data row as an array aligned to them. The column-header "
            "row may repeat at the top of this page; do not emit it as a data row."
        )
    response = client.messages.create(
        model=_AI_MODEL,
        max_tokens=_AI_MAX_TOKENS,
        system=[{"type": "text", "text": _AI_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": image_b64},
                },
                {"type": "text", "text": instruction},
            ],
        }],
        output_config={"format": {"type": "json_schema", "schema": _AI_SCHEMA}},
    )
    if response.stop_reason == "refusal":
        raise ValueError(f"page {page_number}: the model declined to process this page.")
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"page {page_number}: could not parse the model's structured output "
            f"(stop_reason={response.stop_reason}); the page may be too dense for one request."
        ) from exc
    return data.get("columns") or [], data.get("rows") or []


def extract_with_ai(pdf_path, start_page=1):
    """AI FALLBACK: read each page image into rows via the Claude API.

    Used only when the deterministic engine fails and the user opts in. Provenance
    is preserved (``source_page`` is assigned here, in code, never by the model)
    and every row is stamped ``extraction_method = ai_vision`` so it is visibly
    distinct from deterministic output. Columns are detected once and reused, so
    the whole document lands in one consistent schema. Fails loud if no columns
    can be read at all.
    """
    client = _ai_client()
    doc = pdf.open(str(pdf_path))
    if doc.page_count < start_page:
        doc.close()
        raise ValueError(f"{pdf_path}: --start_page is beyond the number of pages in the pdf.")

    header = None
    records = []
    try:
        for page_index, page in enumerate(doc):
            page_number = page_index + 1  # provenance: pages are 1-based to humans
            if page_number < start_page:
                continue
            image_b64 = _ai_page_image_b64(page)
            columns, rows = _ai_extract_page(client, image_b64, page_number, header)
            if header is None:
                if not columns:
                    continue  # no table detected on this page yet; keep looking
                header = _ai_header(columns)
                print(
                    f"  [ai-fallback] page {page_number}: inferred "
                    f"{len(header)} columns from the page image"
                )
            for row in rows:
                cells = _ai_align_row(row, header, page_number)
                record = {"source_page": page_number}
                record.update(zip(header, cells))
                record["extraction_method"] = _AI_METHOD_LABEL
                records.append(record)
    finally:
        doc.close()

    if header is None:
        raise ValueError(f"{pdf_path}: the AI extractor found no columns in the PDF.")

    return pd.DataFrame(
        records, columns=["source_page"] + header + ["extraction_method"]
    )


def _should_use_ai_fallback(reason):
    """Whether to run the AI fallback: opt-in only, never silent.

    The --ai-fallback flag forces it (for scripted/pipeline runs). Otherwise, only
    prompt when attached to a terminal; a non-interactive run without the flag
    keeps the original fail-loud behaviour rather than calling a paid API unasked.
    """
    if args.ai_fallback:
        return True
    if not sys.stdin.isatty():
        return False
    reply = input(
        f"Normal extraction failed: {reason}\n"
        "Try the AI extractor (Claude vision, sends page images to the Claude API)? [y/N] "
    )
    return reply.strip().lower() in ("y", "yes")


def extract_with_fallback(pdf_path):
    """Run the deterministic engine; on failure, offer the AI fallback."""
    try:
        return extract_tables(pdf_path)
    except ValueError as exc:
        if not _should_use_ai_fallback(exc):
            raise
        print(
            f"  [ai-fallback] using Claude vision ({_AI_MODEL}); output is AI-derived "
            "- spot-check it, addresses especially"
        )
        return extract_with_ai(pdf_path, args.start_page)


parser = argparse.ArgumentParser(description="Process PDF files")
parser.add_argument("--file", help="Path to the PDF file")
parser.add_argument("--files", nargs="+", help="Path to directory of PDF files")
parser.add_argument("--output", help="Path to the output CSV file") # Default to current dir of running processes
parser.add_argument("--start_page", type=int, default=1, help="Start page for processing")
parser.add_argument(
    "--ai-fallback",
    action="store_true",
    help=(
        "If normal extraction finds no table, use the Claude vision AI extractor "
        "(sends page images to the Claude API; output is AI-derived, spot-check it). "
        "Without this flag, an interactive run prompts y/N and a non-interactive run "
        "fails loud."
    ),
)
args = parser.parse_args()

if args.output is None:
    args.output = "."

if args.start_page < 1:
    raise ValueError("--start_page must be a positive integer")

if args.file:
    print(f"Processing {args.file}")
    # Process single PDF file
    input_path = Path(args.file)

    # EXTRACT: PDF -> intermediary DataFrame (one row per record, with provenance).
    # Falls back to the Claude vision extractor if the deterministic engine finds
    # no table and the user opts in (--ai-fallback or the interactive prompt).
    df = extract_with_fallback(input_path)

    # OUTPUT: write one CSV. Use the PDF's stem so the extension is .csv, and
    # build the path with pathlib so it works on Windows too.
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)  # create the output folder if missing
    output_path = output_dir / f"{input_path.stem}.csv"
    df.to_csv(output_path, index=False)
    print(
        f"Extracted {len(df)} rows across "
        f"{df['source_page'].nunique()} page(s) -> {output_path}"
    )
    
    
elif args.files:
    path = Path(args.files)
    # Process multiple PDF files
    files = [f for f in path.iterdir() if f.is_file() and f.suffix == ".pdf"]

    for file in files:
        print(f"Processing {file.name}")

        # EXTRACT: same stage as the single-file branch, applied per file.
        df = extract_with_fallback(file)

        # OUTPUT
        output_dir = Path(args.output)
        output_dir.mkdir(parents=True, exist_ok=True)  # create the output folder if missing
        output_path = output_dir / f"{file.stem}.csv"
        df.to_csv(output_path, index=False)
        print(f"Saved {len(df)} rows -> {output_path}")

    pass