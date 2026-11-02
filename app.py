# -*- coding: utf-8 -*-
"""
app.py

Local web interface for the e-TIN certificate registry.

Requirements:
    pip install pdfplumber requests pyodbc pymupdf flask werkzeug

Setup (same environment variables as before, set in the same PowerShell
window before running):
    $env:MSSQL_SERVER = ".\\SQLEXPRESS"
    $env:MSSQL_DB = "tin_certificates"
    $env:MSSQL_TRUSTED = "yes"
    $env:OPENROUTER_API_KEY = "your-real-key-here"

Run:
    python app.py

Then open http://localhost:5000 in your browser.
"""

import os
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, abort
from werkzeug.utils import secure_filename

import tin_extractor as ext

app = Flask(__name__)

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(ext.PHOTO_OUTPUT_DIR, exist_ok=True)


@app.route("/")
def dashboard():
    search = request.args.get("q", "").strip()
    try:
        taxpayers = ext.get_all_taxpayers(search)
        db_error = None
    except Exception as e:
        taxpayers = []
        db_error = str(e)
    return render_template("dashboard.html", taxpayers=taxpayers, search=search, db_error=db_error)


@app.route("/taxpayer/<int:taxpayer_id>")
def taxpayer_detail(taxpayer_id):
    taxpayer = ext.get_taxpayer_by_id(taxpayer_id)
    if not taxpayer:
        abort(404)
    return render_template("detail.html", t=taxpayer)


@app.route("/taxpayer/<int:taxpayer_id>/delete", methods=["POST"])
def remove_taxpayer(taxpayer_id):
    ext.delete_taxpayer(taxpayer_id)
    return redirect(url_for("dashboard"))


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "GET":
        return render_template("upload.html", results=None)

    files = request.files.getlist("pdfs")
    files = [f for f in files if f and f.filename]

    if not files:
        return render_template("upload.html", results=None, error="Choose at least one PDF first.")

    results = []
    for file in files:
        if not file.filename.lower().endswith(".pdf"):
            results.append({"filename": file.filename, "ok": False, "message": "Not a PDF file - skipped."})
            continue

        filename = secure_filename(file.filename)
        save_path = os.path.join(UPLOAD_DIR, filename)
        file.save(save_path)

        try:
            taxpayer_id = ext.process_pdf(save_path)
            results.append({"filename": filename, "ok": True, "taxpayer_id": taxpayer_id})
        except Exception as e:
            results.append({"filename": filename, "ok": False, "message": str(e)})

    return render_template("upload.html", results=results)


@app.route("/photos/<path:filename>")
def photo(filename):
    return send_from_directory(ext.PHOTO_OUTPUT_DIR, filename)


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(debug=True, port=5000)