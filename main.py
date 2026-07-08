import pymupdf as pdf
import pandas as pd
import argparse
from pathlib import Path

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
    output_path = f"{args.output}/{(args.file).split('/')[-1]}"

    # TODO: PROCESS LOGIC HERE

    # OUTPUT
    with open(output_path, "w") as f:
        print(f"Output saved to {output_path}")
    
    
elif args.files:
    path = Path(args.files)
    # Process multiple PDF files
    files = [f for f in path.iterdir() if f.is_file() and f.suffix == ".pdf"]

    for file in files:
        print(f"Processing {file.name}")
        
        
        # TODO: PROCESS LOGIC HERE
        
        
        # OUTPUT
        output_path = f"{args.output}/{file.name}.csv"

        with open(output_path, "w") as f:
            print(f"Saved processed {file.name} to {output_path}")

    pass