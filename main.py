import pymupdf as pdf
import pandas as pd
import argparse
from pathlib import Path


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


def extract_tables(pdf_path):
    """EXTRACT stage: read tables from a PDF into an intermediary DataFrame.

    Every output row carries a 1-based ``source_page`` column (provenance rule).
    The header is taken from the first table found; later rows that exactly
    repeat that header (continuation pages) are skipped. Fails loudly if no
    table is found or a row's column count doesn't match the header.
    """
    doc = pdf.open(str(pdf_path))
    header = None
    records = []
    try:
        for page_index, page in enumerate(doc):
            page_number = page_index + 1  # provenance: pages are 1-based to humans
            for table in page.find_tables().tables:
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