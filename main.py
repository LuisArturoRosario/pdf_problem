import pymupdf as pdf
import pandas as pd
import argparse
import bisect
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
    header = None
    col_lefts = None        # borderless column left-edges, once detected
    top_aligned = False     # borderless record alignment, once detected
    mode = None             # None | "clean" | "borderless" — keep one doc consistent
    records = []
    try:
        for page_index, page in enumerate(doc):
            page_number = page_index + 1  # provenance: pages are 1-based to humans
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


parser = argparse.ArgumentParser(description="Process PDF files")
parser.add_argument("--file", help="Path to the PDF file")
parser.add_argument("--files", nargs="+", help="Path to directory of PDF files")
parser.add_argument("--output", help="Path to the output CSV file") # Default to current dir of running processes

args = parser.parse_args()

if args.output is None:
    args.output = "."

if args.file:
    print(f"Processing {args.file}")
    # Process single PDF file
    input_path = Path(args.file)

    # EXTRACT: PDF -> intermediary DataFrame (one row per record, with provenance)
    df = extract_tables(input_path)

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
        df = extract_tables(file)

        # OUTPUT
        output_dir = Path(args.output)
        output_dir.mkdir(parents=True, exist_ok=True)  # create the output folder if missing
        output_path = output_dir / f"{file.stem}.csv"
        df.to_csv(output_path, index=False)
        print(f"Saved {len(df)} rows -> {output_path}")

    pass