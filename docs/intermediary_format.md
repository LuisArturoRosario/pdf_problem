# Intermediary Format

The **intermediary format** is the structured object the extractor produces after
reading a PDF and before writing output:

```
pdf → [ intermediary format ] → schema mapping → csv
```

This document defines that format. Code that produces it lives in
`extract_tables()` in `main.py`.

## Format vs. schema (read this first)

The tool is meant to accept **any** records-response PDF an analyst uploads, and
different PDFs contain completely different data. So we separate two ideas that
are easy to confuse:

| | What it is | Changes per PDF? | Defined where? |
|---|---|---|---|
| **Format** | the *container*: a table with provenance, string cells, null blanks | **No** — identical for every PDF | this document |
| **Schema** | the *specific columns* (`Case Address`, `Amount`, …) | **Yes** — discovered from each PDF at runtime | not authored; see below |

We use **schema-on-read**: the columns are taken from the PDF's own table header
at runtime, not declared in advance. This document defines only the **format** —
the part that is the same no matter what PDF comes in.

## The container contract

The intermediary object is a **pandas DataFrame** with these guarantees:

1. **One row per extracted record.** No merging, splitting, or synthesizing of rows.
2. **`source_page` is the first column** and is present on every row. It is the
   1-based PDF page the record was extracted from. This satisfies the provenance
   rule: every value traces back to a source page.
3. **Remaining columns are discovered from the PDF.** The first table's first row
   becomes the column names (schema-on-read). Names are whitespace-normalized —
   e.g. a header cell `"Domestic\nViolence\nCode"` becomes `"Domestic Violence Code"`.
4. **All cell values are strings** (or null). No type inference happens at this
   stage — dates, numbers, and codes stay as the text the PDF contained. Typing is
   a later (schema-mapping) concern.
5. **Empty cells become null** (`None` → blank in CSV). A blank is represented
   explicitly; it is never dropped, and a missing value is never guessed or filled.
6. **Nothing is fabricated or interpolated.** The DataFrame contains only values
   the parser actually read from the PDF.

## Multi-page tables

A single table may span multiple pages with the header printed only once (this is
true of the Tallahassee sample). The header is taken from the first table found;
any later row that *exactly equals* that header is treated as a repeated
continuation header and skipped, not emitted as data.

## Fail-loud behavior

The extractor raises (rather than guessing or silently producing partial output)
when:

- **No table is found** in the PDF.
- **A row's column count does not match the header** — we refuse to guess how the
  cells align, because misaligned columns would silently corrupt data.

## Redactions

Redactions must be represented explicitly, never dropped or guessed. The current
sample (`Tallahassee PD 2026.pdf`) contains no redaction markers — its empty cells
are genuinely blank and are represented as null. If a document type uses redaction
markers, handling them (e.g. a flag or a preserved marker string) is defined per
type in the schema-mapping stage, not here.

## Output: CSV

The DataFrame is written to CSV with `df.to_csv(path, index=False)` — one CSV per
PDF. Because the format is schema-on-read, the CSV's columns are simply
`source_page` plus whatever the PDF's table contained.

### Consumer note: `N/A` and blanks are different

Some source cells contain the literal string `N/A` — a real recorded value, not a
blank. (Two rows in the Tallahassee sample's `Domestic Violence Code` do.) The CSV
preserves `N/A` and blanks as distinct. Pandas' default `read_csv` treats `N/A` as
missing, which would erase that distinction. Consumers who need the literal value
should read with:

```python
pd.read_csv(path, keep_default_na=False)
```

## What this format deliberately does NOT define

- **Per-type field names / clean labels** — output columns are the PDF's own headers.
- **Types** — everything is a string here.
- **Field-specific accuracy or validation** (e.g. the address-accuracy priority) —
  a schema-mapping concern for known document types.

These belong to the later **schema-mapping** stage, added per document type only
where normalization or accuracy guarantees are needed. Keeping them out of the
intermediary format is what lets the tool accept any PDF.
