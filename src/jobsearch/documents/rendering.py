"""HTML -> LibreOffice DOCX -> US Letter -> PDF, matching the workspace workflow."""

import html
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup
from pypdf import PdfReader

from ..config import digest
from ..llm.runner import InferenceStopped
from .content import employment_facts, validate_cover_letter_text

VERSION = "html-letter-v4"
RESUME_CSS = """@page { size: Letter; margin: 0.42in 0.55in; }
body { margin: 0; color: #111; font-family: Arial, Helvetica, sans-serif; font-size: 9.6pt; line-height: 1.19; }
h1, h2 { font-family: Arial, Helvetica, sans-serif; }
h1 { margin: 0; font-size: 21pt; } .headline { margin: 2px 0 3px; font-size: 10.5pt; font-weight: bold; }
.contact { margin: 0 0 7px; font-size: 9pt; } h2 { margin: 7px 0 3px; padding-bottom: 2px; border-bottom: 1px solid #222; font-size: 11.5pt; text-transform: uppercase; }
p { margin: 0 0 3px; } .role { margin-top: 5px; } .role-header { font-size: 10.2pt; font-weight: bold; }
.role-meta { font-style: italic; } ul { margin: 2px 0 0 17px; padding: 0; } li { margin: 0 0 1.5px; }
a { color: #111; text-decoration: none; }"""
LETTER_CSS = """@page { size: Letter; margin: 0.65in 0.72in; }
body { margin: 0; color: #111; font-family: Arial, Helvetica, sans-serif; font-size: 11pt; line-height: 1.32; }
h1 { font-size: 20pt; margin: 0 0 5px; } .contact { font-size: 9.3pt; margin-bottom: 20px; }
p { margin: 0 0 13px; } .subject { font-weight: bold; }"""
LETTER_CLOSING = (
    "Thank you for your time and consideration. "
    "I look forward to learning more about your team's needs and discussing "
    "how I could best contribute in this role."
)
LETTER_SUBJECT = '<p class="subject">{employer} | {role}</p>'
TEMPLATE_HASH = digest([RESUME_CSS, LETTER_CSS, LETTER_SUBJECT, LETTER_CLOSING])


def check_dependencies():
    missing = [
        name
        for name in ("libreoffice", "pdfinfo", "pdftotext", "pdftoppm")
        if not shutil.which(name)
    ]
    if missing:
        raise ValueError(
            "Document tools missing: "
            + ", ".join(missing)
            + ". Install LibreOffice Writer and Poppler utilities."
        )


def document_html(kind, document, facts, profile, app):
    def esc(s):
        return html.escape(str(s), quote=True)

    resume = kind == "resume"
    parts = [
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{esc(profile["name"])} | {esc(app["role"])}</title><style>{RESUME_CSS if resume else LETTER_CSS}</style></head><body>',
        f"<h1>{esc(profile['name'])}</h1>",
    ]
    if resume:
        parts.append(f'<p class="headline">{esc(app["role"])}</p>')
    parts.append(f'<p class="contact">{esc(profile["contacts"])}</p>')
    if resume:
        parts += [
            "<h2>Professional Summary</h2>",
            f"<p>{esc(document['summary']['text'])}</p>",
            "<h2>Relevant Skills</h2>",
        ]
        parts += [
            f"<p><strong>{esc(g['label'])}:</strong> {esc(g['text'])}</p>"
            for g in document["skills"]
        ]
        parts.append("<h2>Professional Experience</h2>")
        entries = {r["employment_fact_id"]: r for r in document["experience"]}
        for fact in employment_facts(facts):
            value = fact["value"]
            parts += [
                f'<div class="role"><p class="role-header">{esc(value["title"])}</p>',
                f'<p class="role-meta">{esc(value["employer"])} | {esc(value["start"])} to {esc(value["end"])}</p><ul>',
            ]
            parts += [
                f"<li>{esc(b['text'])}</li>" for b in entries[fact["id"]]["bullets"]
            ]
            parts.append("</ul></div>")
        if document["education"]:
            parts += ["<h2>Education and Training</h2>"] + [
                f"<p>{esc(e['text'])}</p>" for e in document["education"]
            ]
    else:
        parts += [
            LETTER_SUBJECT.format(employer=esc(app["employer"]), role=esc(app["role"])),
            "<p>Dear Hiring Team,</p>",
        ]
        parts += [f"<p>{esc(p['text'])}</p>" for p in document["cover_letter"]]
        parts.append(f"<p>{esc(LETTER_CLOSING)}</p>")
        parts.append(f"<p>Sincerely,<br>{esc(profile['name'])}</p>")
    result = "\n".join(parts + ["</body></html>"])
    if not resume:
        validate_cover_letter_html(result, app)
    return result


def validate_cover_letter_html(source, app):
    body = BeautifulSoup(source, "html.parser").body
    if body is None:
        raise ValueError("Cover letter has no document body")
    validate_cover_letter_text(body.get_text(" "), app)


def command(args, cancelled, timeout=90):
    process = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancelled():
                raise InferenceStopped("Document preparation cancelled")
            if time.monotonic() > deadline:
                raise ValueError("Document conversion timed out")
            try:
                stdout, _ = process.communicate(timeout=0.25)
                if process.returncode:
                    raise ValueError("Document conversion failed")
                return stdout
            except subprocess.TimeoutExpired:
                pass
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()


def letter_size(docx):
    # Patch only page dimensions; preserve LibreOffice's imported HTML formatting.
    with zipfile.ZipFile(docx) as archive:
        items = [(i, archive.read(i.filename)) for i in archive.infolist()]
    patched = []
    for info, data in items:
        if info.filename == "word/document.xml":
            xml = data.decode()

            def size(match):
                tag = re.sub(r'w:w="\d+"', 'w:w="12240"', match[0])
                return re.sub(r'w:h="\d+"', 'w:h="15840"', tag)

            xml, count = re.subn(r"<w:pgSz\b[^>]*/>", size, xml)
            if not count:
                raise ValueError("Converted DOCX has no page dimensions")
            data = xml.encode()
        patched.append((info, data))
    temporary = docx.with_suffix(".tmp")
    with zipfile.ZipFile(temporary, "w") as archive:
        for info, data in patched:
            archive.writestr(info, data)
    temporary.replace(docx)


def render(source: Path, cancelled=lambda: False):
    docx, pdf = source.with_suffix(".docx"), source.with_suffix(".pdf")
    with tempfile.TemporaryDirectory(prefix="lo-", dir=source.parent) as profile:
        base = [
            "libreoffice",
            "-env:UserInstallation=" + Path(profile).as_uri(),
            "--headless",
        ]
        command(
            base
            + [
                "--convert-to",
                "docx:Office Open XML Text",
                "--outdir",
                str(source.parent),
                str(source),
            ],
            cancelled,
        )
        if not docx.exists():
            raise ValueError("HTML to DOCX conversion produced no document")
        letter_size(docx)
        command(
            base
            + [
                "--convert-to",
                "pdf:writer_pdf_Export",
                "--outdir",
                str(source.parent),
                str(docx),
            ],
            cancelled,
        )
    reader = PdfReader(pdf)
    if len(reader.pages) != 1:
        raise ValueError(
            "Document exceeds one page; revise the content before preparing attachments"
        )
    page = reader.pages[0]
    if (
        abs(float(page.mediabox.width) - 612) > 1
        or abs(float(page.mediabox.height) - 792) > 1
    ):
        raise ValueError("PDF is not US Letter")
    actual = page.extract_text()
    intended = BeautifulSoup(source.read_text(), "html.parser").body.get_text(" ")

    def normalize(s):
        return re.sub(r"\W+", "", s.lower())

    if not actual.strip() or normalize(actual) != normalize(intended):
        raise ValueError("PDF text differs from the reviewed HTML")
    info = command(["pdfinfo", str(pdf)], cancelled).decode()
    bbox = command(["pdftotext", "-bbox", str(pdf), "-"], cancelled)
    tree = ET.fromstring(bbox)
    words = [e for e in tree.iter() if e.tag.endswith("}word")]
    if not words or any(
        float(w.attrib["xMin"]) < 20
        or float(w.attrib["xMax"]) > 592
        or float(w.attrib["yMin"]) < 20
        or float(w.attrib["yMax"]) > 772
        for w in words
    ):
        raise ValueError("PDF text reaches the page edge; inspect the layout")
    bottom = max(float(w.attrib["yMax"]) for w in words)
    preview = source.with_suffix(".preview.png")
    command(
        [
            "pdftoppm",
            "-f",
            "1",
            "-singlefile",
            "-scale-to",
            "1200",
            "-png",
            str(pdf),
            str(preview.with_suffix("")),
        ],
        cancelled,
    )
    return {
        "pages": 1,
        "size": [612, 792],
        "text_matches_html": True,
        "bottom_points": round(bottom, 1),
        "warnings": ["Review the blank space below the content"]
        if bottom < 500
        else [],
        "preview": preview.name,
        "visual_review": "pending",
        "pdfinfo": info,
    }
