# -*- coding: utf-8 -*-
"""
tin_extractor.py

Core extraction + database logic for e-TIN certificates. Shared by
cli.py (batch command-line processing) and app.py (the web interface).

Requirements:
    pip install pdfplumber requests pyodbc pymupdf flask

Setup: see cli.py or app.py docstrings for environment variables.
"""

import os
import re
import json
import requests
import pdfplumber
import pyodbc

try:
    import fitz  # PyMuPDF
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False

from dotenv import load_dotenv
load_dotenv()  # reads a .env file in the same folder, if one exists

MODEL = "qwen/qwen3.8-27b:free"
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
    saves it, assuming it's the taxpayer photo. Returns the saved path
    (relative, for use as a URL), or None if unavailable."""
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
    """Reads an e-TIN certificate by word position, with NO LLM / API key.

    The certificate's columns and row heights change with the browser
    width it was printed at, so nothing here is a fixed page coordinate:
    every column edge and row band is derived from the label words
    (Taxpayer's, Issue, Country, Post/Zip, ...) found in that PDF.
    Bengali text is skipped (its glyphs come out garbled and every
    database field is English). Returns the same dict shape the LLM
    function returns; fields not present come back as None."""
    with pdfplumber.open(pdf_path) as pdf:
        words = [w for w in pdf.pages[0].extract_words()
                 if all(ord(c) < 128 for c in w["text"])]

    d = {f: None for f in TAXPAYER_FIELDS}
    d["addresses"] = []

    first = next((w for w in words if w["text"] == "Taxpayer's"), None)
    if first is None:
        return d
    L = first["x0"]                     # x of the left label column
    LV = L + 52                         # left value column starts here

    def left_label(text, top_min=0):
        for w in sorted(words, key=lambda w: w["top"]):
            if w["text"] == text and L - 4 <= w["x0"] <= L + 8 and w["top"] >= top_min:
                return w
        return None

    # right label column: x of Issue / Gender / Nationality / SMART / Circle
    right = [w["x0"] for w in words
             if w["text"] in ("Issue", "Gender", "Nationality", "SMART", "Circle")
             and w["top"] < 480]
    RL = min(right) if right else None

    def cell(top0, top1, x0, x1):
        picked = sorted((w for w in words
                         if x0 <= w["x0"] < x1 and top0 <= w["top"] < top1),
                        key=lambda w: w["top"])
        lines, cur, cur_top = [], [], None
        for w in picked:                # group words into lines by top
            if cur and w["top"] - cur_top > 3:
                lines.append(cur)
                cur = []
            if not cur:
                cur_top = w["top"]
            cur.append(w)
        if cur:
            lines.append(cur)
        text = " ".join(w["text"] for ln in lines for w in sorted(ln, key=lambda w: w["x0"]))
        return text or None

    # ---- row bands, from the left-hand labels ------------------------
    keys = [("name", "Taxpayer's"), ("purpose", "Purpose"), ("tin", "TIN"),
            ("old", "Old"), ("zone", "Taxes"), ("status", "Status"),
            ("nid", "NID"), ("dob", "DoB"), ("father", "Father's"),
            ("mother", "Mother's"), ("spouse", "Spouse's"), ("mobile", "Mobile")]
    found = []
    for key, text in keys:
        w = left_label(text)
        if w is not None:
            found.append((w["top"], key))
    found.sort()

    band = {}
    for i, (top, key) in enumerate(found):
        start = top - 8
        cap = 32 if key == "mobile" else 46
        nxt = found[i + 1][0] - 8 if i + 1 < len(found) else start + cap
        band[key] = (start, min(nxt, start + cap))

    LEFT = (LV, (RL - 5) if RL else 10000)
    RIGHT = (RL + 55, 10000) if RL else None

    def L_(key):
        return cell(*band[key], *LEFT) if key in band else None

    def R_(key):
        return cell(*band[key], *RIGHT) if key in band and RIGHT else None

    d["taxpayer_name"] = L_("name")
    d["purpose_of_tin"] = L_("purpose")
    d["tin"] = L_("tin")
    d["issue_date"] = _iso_date(R_("tin"))
    d["old_tin"] = L_("old")
    d["taxes_zone"] = L_("zone")
    d["taxes_circle"] = R_("zone")
    status = L_("status")
    d["status"] = status.replace(" -> ", ", ") if status else None
    d["nationality"] = R_("status")
    d["nid"] = L_("nid")
    d["smart_card_id"] = R_("nid")
    d["dob"] = _iso_date(L_("dob"))
    d["gender"] = R_("dob")
    d["father_name"] = L_("father")
    d["mother_name"] = L_("mother")
    d["spouse_name"] = L_("spouse")

    # ---- mobile / facsimile / email (one row, columns found by label) --
    if "mobile" in band:
        b0, b1 = band["mobile"]

        def in_row(text):
            return next((w for w in words
                         if w["text"] == text and b0 <= w["top"] < b1), None)

        def after_colon(label_word):
            if label_word is None:
                return None
            colon = next((w for w in words
                          if w["text"] == ":" and abs(w["top"] - label_word["top"]) <= 3
                          and w["x0"] >= label_word["x1"] - 1), None)
            return colon["x1"] if colon else label_word["x1"] + 8

        fac, mail = in_row("Facsimile"), in_row("Email")
        d["mobile_number"] = cell(b0, b1, LV, fac["x0"] if fac else 10000)
        if fac:
            d["facsimile"] = cell(b0, b1, after_colon(fac), mail["x0"] if mail else 10000)
        if mail:
            d["email"] = cell(b0, b1, after_colon(mail), 10000)
        mobile_top = b0 + 8
    else:
        mobile_top = 0

    # ---- login / user type (last row on the page) ---------------------
    user = left_label("User", top_min=mobile_top + 40)
    if user is not None:
        u0 = user["top"] - 8
        d["login_username"] = cell(u0, u0 + 46, *LEFT)
        if RIGHT:
            d["user_type"] = cell(u0, u0 + 46, *RIGHT)

    # ---- address table ---------------------------------------------------
    type_hdr = min((w for w in words if w["text"] == "Type" and w["top"] >= mobile_top + 20),
                   key=lambda w: w["top"], default=None)
    if type_hdr is not None and RL is not None:
        hdr_top = type_hdr["top"]
        hdr = [w for w in words if abs(w["top"] - hdr_top) <= 10]
        country_h = next((w for w in hdr if w["text"] == "Country"), None)
        post_h = next((w for w in hdr if w["text"].startswith("Post")), None)
        if country_h and post_h:
            cols = {
                "address_line":   (LV - 2, country_h["x0"] - 10),
                "country":        (country_h["x0"] - 10, RL - 3),
                "district_state": (RL - 3, RL + 52),
                "thana":          (RL + 52, post_h["x0"] - 50),
                "post_zip_code":  (post_h["x0"] - 50, 10000),
            }
            rows = []
            for label, addr_type in (("Current", "current"), ("Permanent", "permanent"),
                                     ("Other", "other")):
                w = left_label(label, top_min=hdr_top + 10)
                if w is not None:
                    rows.append((w["top"] - 14, addr_type))
            rows.sort()
            photo = left_label("Photo", top_min=hdr_top + 10)
            table_end = (photo["top"] - 8) if photo else (rows[-1][0] + 60 if rows else 0)
            for i, (a0, addr_type) in enumerate(rows):
                a1 = rows[i + 1][0] if i + 1 < len(rows) else table_end
                addr = {"type": addr_type}
                for col, (cx0, cx1) in cols.items():
                    addr[col] = cell(a0, a1, cx0, cx1)
                if any(addr[c] for c in cols):
                    d["addresses"].append(addr)
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


import time


def extract_fields_with_llm(text, model=MODEL, max_retries=4):
    if not API_KEY:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Get a free key at "
            "https://openrouter.ai/keys and set it as an environment variable."
        )

    for attempt in range(max_retries):
        response = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": build_prompt(text)}],
                "temperature": 0.1,
            },
        )

        if response.status_code == 429 and attempt < max_retries - 1:
            wait = 6 * (attempt + 1)  # 6s, 12s, 18s
            time.sleep(wait)
            continue

        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError:
            if response.status_code == 429:
                raise RuntimeError(
                    "Still rate-limited after several retries. The free model's shared "
                    "pool is busy - wait a minute and try again, or add your own provider "
                    "key at https://openrouter.ai/settings/integrations for higher limits."
                )
            raise RuntimeError(f"API error {response.status_code}: {response.text[:500]}")

        raw_output = response.json()["choices"][0]["message"]["content"].strip()

        if raw_output.startswith("```"):
            raw_output = raw_output.strip("`")
            if raw_output.lower().startswith("json"):
                raw_output = raw_output[4:]
            raw_output = raw_output.strip()

        try:
            return json.loads(raw_output)
        except json.JSONDecodeError:
            raise RuntimeError(f"Model did not return valid JSON: {raw_output[:300]}")


def _clean_nid(nid):
    nid = re.sub(r"\s+", "", nid or "")
    return nid or None


def _nid_conflict(cursor, source_filename, nid):
    """Returns (id, name) of ANOTHER record that already has this NID,
    or None. A record for the same source file is not a conflict
    (re-uploading a file updates it)."""
    if not nid:
        return None
    cursor.execute("SELECT id FROM taxpayers WHERE source_filename = ?", (source_filename,))
    own = cursor.fetchone()
    own_id = own[0] if own else None
    cursor.execute("SELECT id, taxpayer_name FROM taxpayers WHERE nid = ?", (nid,))
    for row in cursor.fetchall():
        if row[0] != own_id:
            return row[0], row[1]
    return None


def _nid_error(nid, conflict):
    return ValueError(
        f"NID {nid} is already in the database (record #{conflict[0]}, {conflict[1]}). "
        "Delete that record first if you want to replace it."
    )


def check_nid_available(source_filename, nid):
    """Raises ValueError if another record already uses this NID."""
    nid = _clean_nid(nid)
    conn = get_connection()
    try:
        conflict = _nid_conflict(conn.cursor(), source_filename, nid)
    finally:
        conn.close()
    if conflict:
        raise _nid_error(nid, conflict)


def store_in_mssql(source_filename, data, photo_path=None):
    """Inserts or updates a taxpayer record and its addresses. Returns
    the taxpayer's id. Raises ValueError if the NID already belongs to a
    different record (one NID = one record)."""
    data = dict(data)
    data["nid"] = _clean_nid(data.get("nid"))

    conn = get_connection()
    try:
        cursor = conn.cursor()

        conflict = _nid_conflict(cursor, source_filename, data["nid"])
        if conflict:
            raise _nid_error(data["nid"], conflict)

        columns = TAXPAYER_FIELDS + (["photo_path"] if photo_path else [])
        values = [data.get(f) for f in TAXPAYER_FIELDS] + ([photo_path] if photo_path else [])

        cursor.execute(
            "SELECT id FROM taxpayers WHERE source_filename = ?",
            (source_filename,),
        )
        row = cursor.fetchone()

        try:
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
        except pyodbc.IntegrityError:
            # unique index caught a race the pre-check could not
            raise ValueError(f"NID {data['nid']} is already in the database.")

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
        return taxpayer_id
    finally:
        conn.close()


def process_pdf(pdf_path):
    """Runs the full pipeline on one PDF and returns the taxpayer id.
    Uses the layout parser first (no API, no rate limits); falls back to
    the LLM only if the parser finds no TIN. Raises an exception (with a
    clear message) on any failure."""
    filename = os.path.basename(pdf_path)

    data = None
    try:
        data = extract_fields_from_layout(pdf_path)
    except Exception:
        data = None

    if not data or not data.get("tin"):
        text = extract_text_from_pdf(pdf_path)
        if not text.strip():
            raise ValueError("No extractable text found - is this a scanned image PDF?")
        try:
            data = extract_fields_with_llm(text)
        except Exception as e:
            raise RuntimeError(
                "The layout parser could not find a TIN in this PDF (unfamiliar "
                f"certificate layout), and the LLM fallback also failed: {e}"
            )

    check_nid_available(filename, data.get("nid"))  # before saving the photo file
    photo_path = extract_photo(pdf_path)

    return store_in_mssql(filename, data, photo_path)


def get_all_taxpayers(search=""):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        base = (
            "SELECT id, taxpayer_name, tin, mobile_number, email, photo_path, extracted_at "
            "FROM taxpayers"
        )
        if search:
            cursor.execute(
                base + " WHERE taxpayer_name LIKE ? OR tin LIKE ? ORDER BY extracted_at DESC",
                (f"%{search}%", f"%{search}%"),
            )
        else:
            cursor.execute(base + " ORDER BY extracted_at DESC")
        columns = [c[0] for c in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_taxpayer_by_id(taxpayer_id):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM taxpayers WHERE id = ?", (taxpayer_id,))
        row = cursor.fetchone()
        if not row:
            return None
        columns = [c[0] for c in cursor.description]
        taxpayer = dict(zip(columns, row))

        cursor.execute(
            "SELECT address_type, address_line, country, district_state, thana, post_zip_code "
            "FROM taxpayer_addresses WHERE taxpayer_id = ? ORDER BY "
            "CASE address_type WHEN 'current' THEN 1 WHEN 'permanent' THEN 2 ELSE 3 END",
            (taxpayer_id,),
        )
        addr_columns = [c[0] for c in cursor.description]
        taxpayer["addresses"] = [dict(zip(addr_columns, row)) for row in cursor.fetchall()]
        return taxpayer
    finally:
        conn.close()


def delete_taxpayer(taxpayer_id):
    """Deletes a taxpayer, their addresses, and their saved photo file.
    Returns True if a record was deleted, False if it didn't exist."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT photo_path FROM taxpayers WHERE id = ?", (taxpayer_id,))
        row = cursor.fetchone()
        if not row:
            return False
        photo_path = row[0]

        cursor.execute("DELETE FROM taxpayer_addresses WHERE taxpayer_id = ?", (taxpayer_id,))
        cursor.execute("DELETE FROM taxpayers WHERE id = ?", (taxpayer_id,))
        conn.commit()
    finally:
        conn.close()

    if photo_path:
        name = os.path.basename(photo_path.replace("\\", "/"))
        full = os.path.join(PHOTO_OUTPUT_DIR, name)
        if os.path.isfile(full):
            try:
                os.remove(full)
            except OSError:
                pass
    return True