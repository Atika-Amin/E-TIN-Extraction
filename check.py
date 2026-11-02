import sys
import pdfplumber
import fitz  # pymupdf

path = sys.argv[1]

with pdfplumber.open(path) as pdf:
    print("pages:", len(pdf.pages))
    for i, p in enumerate(pdf.pages):
        t = p.extract_text() or ""
        print(f"pdfplumber page {i+1}: {len(t)} chars, {len(p.chars)} raw chars")

doc = fitz.open(path)
for i, p in enumerate(doc):
    t = p.get_text()
    print(f"pymupdf page {i+1}: {len(t)} chars")
    print(t[:1500])