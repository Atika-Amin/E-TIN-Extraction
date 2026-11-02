# -*- coding: utf-8 -*-
"""
extract_tin_certificate_mssql.py

Same e-TIN certificate extraction as before, but stores into SQL Server
instead of MySQL, using pyodbc.

Processes either a single PDF file or every PDF in a folder.

Usage:
    python extract_tin_certificate_mssql.py "C:\\path\\to\\file.pdf"
    python extract_tin_certificate_mssql.py "C:\\path\\to\\folder_of_pdfs"

Requirements:
    pip install pdfplumber requests pyodbc pymupdf
    Also requires "ODBC Driver 17 for SQL Server" installed on Windows.

Setup:
    1. Install SQL Server Express and run mssql_schema.sql against it
       (via SSMS or sqlcmd) to create the database and tables.
    2. Get a free OpenRouter API key at https://openrouter.ai/keys.
    3. Set these as environment variables before running (PowerShell):

       For Windows Authentication (simplest for local dev - uses your
       own Windows login, no separate SQL password needed):
           $env:MSSQL_SERVER = "localhost"
           $env:MSSQL_DB = "tin_certificates"
           $env:MSSQL_TRUSTED = "yes"
           $env:OPENROUTER_API_KEY = "your-key-here"

       For SQL Server Authentication instead (if you set up a SQL login):
           $env:MSSQL_SERVER = "localhost"
           $env:MSSQL_DB = "tin_certificates"
           $env:MSSQL_USER = "your-sql-username"
           $env:MSSQL_PASSWORD = "your-sql-password"
           $env:OPENROUTER_API_KEY = "your-key-here"

    If you're unsure which ODBC driver version you have installed, run
    this in Python to list them:
        import pyodbc; print(pyodbc.drivers())
    and set MSSQL_DRIVER to match exactly if it's not "ODBC Driver 17
    for SQL Server".
"""

import os
import sys
import re
import json
import time
import requests
import pdfplumber
import pyodbc

try:
    import fitz  # PyMuPDF
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False

MODEL = "qwen/qwen3.8-27b:free"
FALLBACK_MODELS = [
    MODEL,
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-3-27b-it:free",
    "deepseek/deepseek-chat-v3-0324:free",
]
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
API_KEY = os.environ.get("OPENROUTER_API_KEY")

MSSQL_CONFIG = {
    "server": os.environ.get("MSSQL_SERVER", "localhost"),
    "database": os.environ.get("MSSQL_DB", "tin_certificates"),
    "username": os.environ.get("MSSQL_USER", ""),
    "password": os.environ.get("MSSQL_PASSWORD", ""),
    "driver": os.environ.get("MSSQL_DRIVER", "ODBC Driver 17 for SQL Server"),
    "trusted": os.environ.get("MSSQL_TRUSTED", "no").lower() in ("1", "yes", "true"),
}

TAXPAYER_FIELDS = [
    "taxpayer_name", "purpose_of_tin", "tin", "issue_date", "old_tin", "taxes_zone",
    "taxes_circle", "status", "nationality", "nid", "smart_card_id",
    "dob", "gender", "father_name", "mother_name", "spouse_name",
    "mobile_number", "facsimile", "email", "login_username", "user_type",
]

ADDRESS_COLUMNS = ["address_line", "country", "district_state", "thana", "post_zip_code"]

PHOTO_OUTPUT_DIR = "photos"


def get_connection():
    if MSSQL_CONFIG["trusted"]:
        conn_str = (
            f"DRIVER={{{MSSQL_CONFIG['driver']}}};"
            f"SERVER={MSSQL_CONFIG['server']};"
            f"DATABASE={MSSQL_CONFIG['database']};"
            f"Trusted_Connection=yes;"
        )
    else:
        conn_str = (
            f"DRIVER={{{MSSQL_CONFIG['driver']}}};"
            f"SERVER={MSSQL_CONFIG['server']};"
            f"DATABASE={MSSQL_CONFIG['database']};"
            f"UID={MSSQL_CONFIG['username']};"
            f"PWD={MSSQL_CONFIG['password']};"
        )
    return pyodbc.connect(conn_str)


def extract_text_from_pdf(pdf_path):
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    return text


def extract_photo(pdf_path):
    """Best-effort: pulls the first embedded image out of the PDF and
    saves it, assuming it's the taxpayer photo. Returns the saved path,
    or None if PyMuPDF isn't installed or no image was found."""
    if not HAS_PYMUPDF:
        return None

    os.makedirs(PHOTO_OUTPUT_DIR, exist_ok=True)
    doc = fitz.open(pdf_path)
    photo_path = None
    try:
        for page in doc:
            images = page.get_images(full=True)
            if images:
                xref = images[0][0]
                base_image = doc.extract_image(xref)
                image_bytes = base_image["image"]
                ext = base_image["ext"]
                filename = os.path.splitext(os.path.basename(pdf_path))[0] + f"_photo.{ext}"
                photo_path = os.path.join(PHOTO_OUTPUT_DIR, filename)
                with open(photo_path, "wb") as f:
                    f.write(image_bytes)
                break
    finally:
        doc.close()
    return photo_path


def _iso_date(s):
    """dd/mm/yyyy -> yyyy-mm-dd (safe for DATE and VARCHAR columns)."""
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", s or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else s


def extract_fields_from_layout(pdf_path):
    """Reads the e-TIN certificate by word position (the page has a fixed
    layout), so no LLM / API key / rate limit is needed. Bengali text is
    skipped on purpose (its glyphs come out garbled and all DB fields are
    English). Returns the same dict shape the LLM function returned."""
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        words = [w for w in page.extract_words()
                 if all(ord(c) < 128 for c in w["text"])]

    def anchor(text, x0, x1, min_top=0):
        for w in words:
            if w["text"] == text and x0 <= w["x0"] < x1 and w["top"] >= min_top:
                return w["top"]
        return None

    def cell(top, x0, x1, y0, y1):
        picked = [w for w in words
                  if x0 <= w["x0"] < x1 and top + y0 <= w["top"] <= top + y1]
        picked.sort(key=lambda w: (round(w["top"]), w["x0"]))
        return " ".join(w["text"] for w in picked) or None

    LEFT, RIGHT = (100, 255), (318, 600)
    d = {f: None for f in TAXPAYER_FIELDS}

    t = anchor("Taxpayer's", 0, 100)
    if t is not None:
        d["taxpayer_name"] = cell(t, *LEFT, -2, 30)

    t = anchor("TIN", 0, 100)
    if t is not None:
        d["tin"] = cell(t, *LEFT, -2, 8)
        d["issue_date"] = _iso_date(cell(t, *RIGHT, -2, 8))

    t = anchor("Old", 0, 100)
    if t is not None:
        d["old_tin"] = cell(t, *LEFT, -2, 40)

    t = anchor("Taxes", 0, 100)
    if t is not None:
        d["taxes_zone"] = cell(t, *LEFT, -2, 22)
        d["taxes_circle"] = cell(t, *RIGHT, -2, 22)

    t = anchor("Country", 255, 320)
    if t is not None:
        status = cell(t, *LEFT, -2, 40)
        d["status"] = status.replace(" -> ", ", ").rstrip(" ->") if status else None
        d["nationality"] = cell(t, *RIGHT, -2, 40)

    t = anchor("SMART", 255, 320)
    if t is not None:
        d["nid"] = cell(t, *LEFT, -2, 20)
        d["smart_card_id"] = cell(t, *RIGHT, -2, 20)

    t = anchor("DoB", 0, 100)
    if t is not None:
        d["dob"] = _iso_date(cell(t, *LEFT, -2, 8))
        d["gender"] = cell(t, *RIGHT, -2, 8)

    for key, label in (("father_name", "Father's"), ("mother_name", "Mother's"),
                       ("spouse_name", "Spouse's")):
        t = anchor(label, 0, 100)
        if t is not None:
            d[key] = cell(t, *LEFT, 2, 27)

    t = anchor("Mobile", 0, 100)
    if t is not None:
        d["mobile_number"] = cell(t, 100, 195, 2, 22)
        d["facsimile"] = cell(t, 253, 318, 2, 22)
        d["email"] = cell(t, 420, 600, 2, 22)

    t = anchor("User", 0, 100, min_top=650)
    if t is not None:
        d["login_username"] = cell(t, *LEFT, -2, 35)
        d["user_type"] = cell(t, *RIGHT, -2, 35)

    # address table: one row per Current / Permanent / Other label
    rows = []
    for label, addr_type in (("Current", "current"), ("Permanent", "permanent"),
                             ("Other", "other")):
        top = anchor(label, 0, 100, min_top=480)
        if top is not None:
            rows.append((top - 14, addr_type))
    rows.sort()
    photo_top = anchor("Photo", 0, 100, min_top=600) or 670
    cols = {
        "address_line": (100, 196), "country": (196, 259),
        "district_state": (259, 318), "thana": (318, 424),
        "post_zip_code": (424, 600),
    }
    addresses = []
    for i, (start, addr_type) in enumerate(rows):
        end = rows[i + 1][0] if i + 1 < len(rows) else photo_top - 8
        addr = {"type": addr_type}
        for col, (cx0, cx1) in cols.items():
            addr[col] = cell(start, cx0, cx1, 0, end - start - 0.01)
        if any(addr[c] for c in cols):
            addresses.append(addr)
    d["addresses"] = addresses
    return d


def build_prompt(text):
    return f"""You are extracting data from a Bangladesh NBR e-TIN (Taxpayer
Identification Number) certificate. Field labels may appear in English,
Bengali, or both. Return ONLY a valid JSON object - no explanation, no
markdown formatting, nothing outside the JSON.

Return exactly this structure:
{{
  "taxpayer_name": null,
  "purpose_of_tin": null,
  "tin": null,
  "issue_date": null,
  "old_tin": null,
  "taxes_zone": null,
  "taxes_circle": null,
  "status": null,
  "nationality": null,
  "nid": null,
  "smart_card_id": null,
  "dob": null,
  "gender": null,
  "father_name": null,
  "mother_name": null,
  "spouse_name": null,
  "mobile_number": null,
  "facsimile": null,
  "email": null,
  "login_username": null,
  "user_type": null,
  "addresses": [
    {{"type": "current", "address_line": null, "country": null, "district_state": null, "thana": null, "post_zip_code": null}},
    {{"type": "permanent", "address_line": null, "country": null, "district_state": null, "thana": null, "post_zip_code": null}},
    {{"type": "other", "address_line": null, "country": null, "district_state": null, "thana": null, "post_zip_code": null}}
  ]
}}

Field label hints (either language may appear in the document):
- Father's Name = পিতার নাম
- Mother's Name = মাতার নাম
- Spouse's Name = স্বামী বা স্ত্রীর নাম
- Current Address = বর্তমান ঠিকানা
- Permanent Address = স্থায়ী ঠিকানা
- "status" combines the taxpayer category chain, e.g. "Individual, Bangladeshi, Having NID"
- "login_username" is the "User / Login Name" field; "user_type" is the value next to it (e.g. "Public")

For each address, combine house/holding number, village/road, ward number,
and post office details into a single "address_line" string. If the
document's address table has separate District/State, Thana, and
Post/Zip Code columns, put those into their matching fields directly.

If an entire address type (current/permanent/other) does not appear in the
document, remove that object from the "addresses" array entirely rather
than leaving it with all null fields. Use null for any single field that
is genuinely not present. Do not include any text outside the JSON object.

Document text:
{text}

JSON:"""


def extract_fields_with_llm(text, model=None):
    if not API_KEY:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Get a free key at "
            "https://openrouter.ai/keys and set it as an environment variable."
        )

    prompt = build_prompt(text)
    models = [model] if model else FALLBACK_MODELS

    for m in models:
        for attempt in range(4):
            response = requests.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": m,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                },
                timeout=120,
            )

            try:
                body = response.json()
            except ValueError:
                body = {}

            err = body.get("error") if isinstance(body, dict) else None
            err_code = err.get("code") if isinstance(err, dict) else None
            rate_limited = response.status_code in (429, 502, 503) or err_code in (429, 502, 503)

            if rate_limited:
                wait = response.headers.get("Retry-After")
                wait = int(wait) if wait and wait.isdigit() else min(60, 5 * (2 ** attempt))
                print(f"  {m} busy (attempt {attempt + 1}/4), waiting {wait}s...")
                time.sleep(wait)
                continue

            if response.status_code >= 400 or err:
                print(f"  {m} error {response.status_code}: {str(err or response.text)[:300]}")
                break  # not a rate limit, try the next model

            choices = body.get("choices") or []
            message = (choices[0].get("message") or {}) if choices else {}
            raw_output = (message.get("content") or "").strip()

            if not raw_output:
                print(f"  {m} returned empty content, trying next model")
                break

            if raw_output.startswith("```"):
                raw_output = raw_output.strip("`")
                if raw_output.lower().startswith("json"):
                    raw_output = raw_output[4:]
                raw_output = raw_output.strip()

            start, end = raw_output.find("{"), raw_output.rfind("}")
            if start != -1 and end > start:
                raw_output = raw_output[start:end + 1]

            try:
                return json.loads(raw_output)
            except json.JSONDecodeError:
                print(f"  {m} did not return valid JSON, trying next model")
                break
        else:
            print(f"  {m} still rate-limited, moving to next model")

    print("  All models failed or are rate-limited.")
    return None

def store_in_mssql(source_filename, data, photo_path=None):
    conn = get_connection()
    try:
        cursor = conn.cursor()

        columns = TAXPAYER_FIELDS + (["photo_path"] if photo_path else [])
        values = [data.get(f) for f in TAXPAYER_FIELDS] + ([photo_path] if photo_path else [])

        cursor.execute(
            "SELECT id FROM taxpayers WHERE source_filename = ?",
            (source_filename,),
        )
        row = cursor.fetchone()

        if row:
            taxpayer_id = row[0]
            set_clause = ", ".join(f"{c} = ?" for c in columns)
            cursor.execute(
                f"UPDATE taxpayers SET {set_clause} WHERE id = ?",
                values + [taxpayer_id],
            )
        else:
            col_list = ", ".join(columns)
            placeholders = ", ".join(["?"] * len(columns))
            cursor.execute(
                f"INSERT INTO taxpayers (source_filename, {col_list}) "
                f"OUTPUT INSERTED.id VALUES (?, {placeholders})",
                [source_filename] + values,
            )
            taxpayer_id = int(cursor.fetchone()[0])

        for addr in (data.get("addresses") or []):
            addr_type = addr.get("type")
            if not addr_type:
                continue

            addr_values = [addr.get(c) for c in ADDRESS_COLUMNS]

            cursor.execute(
                "SELECT id FROM taxpayer_addresses WHERE taxpayer_id = ? AND address_type = ?",
                (taxpayer_id, addr_type),
            )
            addr_row = cursor.fetchone()

            if addr_row:
                set_clause = ", ".join(f"{c} = ?" for c in ADDRESS_COLUMNS)
                cursor.execute(
                    f"UPDATE taxpayer_addresses SET {set_clause} WHERE id = ?",
                    addr_values + [addr_row[0]],
                )
            else:
                col_list = ", ".join(ADDRESS_COLUMNS)
                placeholders = ", ".join(["?"] * len(ADDRESS_COLUMNS))
                cursor.execute(
                    f"INSERT INTO taxpayer_addresses (taxpayer_id, address_type, {col_list}) "
                    f"VALUES (?, ?, {placeholders})",
                    [taxpayer_id, addr_type] + addr_values,
                )

        conn.commit()
    finally:
        conn.close()


def process_pdf(pdf_path):
    filename = os.path.basename(pdf_path)
    print(f"\nProcessing {filename}...", flush=True)

    data = None
    try:
        data = extract_fields_from_layout(pdf_path)
    except Exception as e:
        print(f"  Layout parser error: {e}", flush=True)

    if not data or not data.get("tin"):
        print("  Layout parser found no TIN, falling back to LLM...", flush=True)
        text = extract_text_from_pdf(pdf_path)
        if not text.strip():
            print("  Skipped - no extractable text (is this a scanned PDF?)")
            return
        data = extract_fields_with_llm(text)
        if not data:
            print("  Skipped - extraction failed")
            return

    photo_path = extract_photo(pdf_path)
    if photo_path:
        print(f"  Photo saved to {photo_path}")
    elif not HAS_PYMUPDF:
        print("  Note: pymupdf not installed, skipping photo extraction")

    print(f"  Extracted {len(data.get('addresses') or [])} address block(s)")
    store_in_mssql(filename, data, photo_path)
    print("  Saved to database")



if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python extract_tin_certificate_mssql.py "path\\to\\file_or_folder"')
        sys.exit(1)

    target = sys.argv[1]

    if os.path.isdir(target):
        pdf_files = [f for f in os.listdir(target) if f.lower().endswith(".pdf")]
        if not pdf_files:
            print("No PDF files found in that folder.")
            sys.exit(1)
        print(f"Found {len(pdf_files)} PDF(s) in {target}")
        for f in pdf_files:
            process_pdf(os.path.join(target, f))
    elif os.path.isfile(target):
        process_pdf(target)
    else:
        print(f"Path not found: {target}")
        sys.exit(1)

    print("\nDone.")