# -*- coding: utf-8 -*-
"""
cli.py

Command-line batch runner for e-TIN certificate extraction. Processes
either a single PDF file or every PDF in a folder.

Usage:
    python cli.py "C:\\path\\to\\file.pdf"
    python cli.py "C:\\path\\to\\folder_of_pdfs"

Requires the same environment variables as app.py - see its docstring.
"""

import os
import sys
from tin_extractor import process_pdf


def run(target):
    if os.path.isdir(target):
        pdf_files = [f for f in os.listdir(target) if f.lower().endswith(".pdf")]
        if not pdf_files:
            print("No PDF files found in that folder.")
            sys.exit(1)
        print(f"Found {len(pdf_files)} PDF(s) in {target}")
        for f in pdf_files:
            path = os.path.join(target, f)
            print(f"\nProcessing {f}...")
            try:
                taxpayer_id = process_pdf(path)
                print(f"  Saved as taxpayer #{taxpayer_id}")
            except Exception as e:
                print(f"  Failed: {e}")
    elif os.path.isfile(target):
        print(f"Processing {os.path.basename(target)}...")
        try:
            taxpayer_id = process_pdf(target)
            print(f"  Saved as taxpayer #{taxpayer_id}")
        except Exception as e:
            print(f"  Failed: {e}")
    else:
        print(f"Path not found: {target}")
        sys.exit(1)

    print("\nDone.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python cli.py "path\\to\\file_or_folder"')
        sys.exit(1)
    run(sys.argv[1])
