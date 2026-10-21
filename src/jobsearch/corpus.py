from __future__ import annotations
from email import policy
from email.parser import BytesParser
import mailbox
import logging
import json
import re
from pathlib import Path
from dataclasses import dataclass
from bs4 import BeautifulSoup
from docx import Document
from pypdf import PdfReader
from .config import digest, file_hash
from .db import uid, js
from .providers import Providers

SUPPORTED = {
    ".pdf",
    ".docx",
    ".ods",
    ".html",
    ".htm",
    ".md",
    ".txt",
    ".json",
    ".eml",
    ".mbox",
}
EXCLUDED = {
    "_Backups",
    ".git",
    ".venv",
    "node_modules",
    "profiles",
    "browser-profile",
    "automation",
    "__pycache__",
}
SECRET_KEYS = re.compile(
    r"password|verification.?code|session.?token|access.?token|refresh.?token|cookie|secret|api.?key",
    re.I,
)


def strip_secrets(value):
    if isinstance(value, dict):
        return {
            k: "[excluded secret]" if SECRET_KEYS.search(k) else strip_secrets(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [strip_secrets(x) for x in value]
    return value


@dataclass
class Piece:
    text: str
    section: str
    locator: dict


def classification(path: Path):
    name = path.name.lower()
    if any(
        x in str(path).lower()
        for x in ("source_backup", "before_app_scope", "superseded")
    ):
        return "historical_source"
    if "receipt" in name or "submission_confirmation" in name:
        return "submission_evidence"
    if "answers" in name and path.suffix == ".json":
        return "confirmation_record"
    if "resume-sources" in str(path).lower():
        return "historical_source"
    if "posting" in name or "listing" in name or "official_" in name:
        return "employer_source"
    if any(
        x in name for x in ["resume", "cover_letter", "narrative", "draft", "review"]
    ):
        return "generated_draft"
    if path.suffix == ".ods":
        return "tracker"
    return "imported_claim"


def extract(path: Path) -> tuple[list[Piece], list[str]]:
    suffix = path.suffix.lower()
    out, warnings = [], []
    if suffix == ".pdf":
        reader = PdfReader(path)

        class Capture(logging.Handler):
            def emit(self, record):
                if record.levelno >= logging.WARNING:
                    warnings.append("PDF extraction: " + record.getMessage()[:240])

        capture = Capture()
        logger = logging.getLogger("pypdf")
        logger.addHandler(capture)
        for i, page in enumerate(reader.pages, 1):
            text = page.extract_text(extraction_mode="layout") or ""
            out.append(Piece(text, f"Page {i}", {"page": i}))
            if len(text.strip()) < 15:
                warnings.append(
                    f"Page {i}: little or no text; OCR or manual extraction required"
                )
        logger.removeHandler(capture)
    elif suffix == ".docx":
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        doc = Document(path)
        section, page = "Document", 1
        for i, element in enumerate(doc.element.body):
            if element.tag.endswith("}p"):
                p = Paragraph(element, doc)
                if p.style and p.style.name.startswith("Heading"):
                    section = p.text
                if p.text:
                    out.append(
                        Piece(p.text, section, {"paragraph": i + 1, "page_hint": page})
                    )
                if "lastRenderedPageBreak" in p._p.xml or 'w:type="page"' in p._p.xml:
                    page += 1
            elif element.tag.endswith("}tbl"):
                table = Table(element, doc)
                rows = [[c.text for c in r.cells] for r in table.rows]
                header = " | ".join(rows[0]) if rows else ""
                for n, row in enumerate(rows[1:] or rows, 2):
                    out.append(
                        Piece(
                            header + "\n" + " | ".join(row),
                            section,
                            {"table": i + 1, "row": n},
                        )
                    )
        warnings.append(
            "DOCX page numbers are layout hints; use paragraph and table references"
        )
    elif suffix == ".ods":
        from .tracker import read_ods

        for sheet in read_ods(path)["sheets"]:
            header = (
                sheet["rows"][sheet["header_index"]]
                if sheet["header_index"] is not None
                else []
            )
            for i, row in enumerate(sheet["rows"], 1):
                if any(row):
                    out.append(
                        Piece(
                            " | ".join(header) + "\n" + " | ".join(row),
                            sheet["name"],
                            {"sheet": sheet["name"], "row": i},
                        )
                    )
    elif suffix in {".html", ".htm"}:
        soup = BeautifulSoup(path.read_text(errors="replace"), "html.parser")
        for e in soup(["script", "style", "noscript"]):
            e.decompose()
        section = "Document"
        for i, e in enumerate(
            soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "table"])
        ):
            if e.find_parent(["li", "table"]):
                continue
            if e.name.startswith("h"):
                section = e.get_text(" ", strip=True)
            if e.name == "table":
                rows = e.find_all("tr")
                header = rows[0].get_text(" | ", strip=True) if rows else ""
                for n, row in enumerate(rows[1:] or rows, 1):
                    out.append(
                        Piece(
                            header + "\n" + row.get_text(" | ", strip=True),
                            section,
                            {"table": i, "row": n},
                        )
                    )
            else:
                out.append(Piece(e.get_text(" ", strip=True), section, {"element": i}))
        if not out:
            out.append(Piece(soup.get_text(" ", strip=True), "Document", {}))
    elif suffix in {".eml", ".mbox"}:
        messages = (
            [BytesParser(policy=policy.default).parsebytes(path.read_bytes())]
            if suffix == ".eml"
            else list(mailbox.mbox(path))
        )
        for i, message in enumerate(messages, 1):
            content = []
            for part in message.walk():
                if (
                    part.get_content_type() == "text/plain"
                    and part.get_content_disposition() != "attachment"
                ):
                    content.append(
                        (part.get_payload(decode=True) or b"").decode(
                            part.get_content_charset() or "utf-8", errors="replace"
                        )
                    )
            out.append(
                Piece(
                    "\n".join(content),
                    str(message.get("Subject", "Email")),
                    {"message": i, "date": str(message.get("Date", ""))},
                )
            )
            if not content:
                warnings.append(
                    f"Message {i}: no plain-text body; HTML-only export needs review"
                )
    elif suffix == ".json":
        value = strip_secrets(json.loads(path.read_text()))
        entries = (
            value.items()
            if isinstance(value, dict)
            else enumerate(value)
            if isinstance(value, list)
            else [("value", value)]
        )
        for k, v in entries:
            out.append(
                Piece(
                    json.dumps({str(k): v}, ensure_ascii=False, indent=2),
                    str(k),
                    {"json_pointer": "/" + str(k)},
                )
            )
    else:
        section, lines = "Document", []
        for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if line.startswith("#"):
                if lines:
                    out.append(Piece("\n".join(lines), section, {"line_end": i - 1}))
                section, lines = line.lstrip("# ").strip(), []
            lines.append(line)
        if lines:
            out.append(
                Piece(
                    "\n".join(lines), section, {"line_end": i if "i" in locals() else 1}
                )
            )
    pieces = []
    for piece in out:
        # Tables repeat their header in each row. Long prose retains section and locator.
        for offset in range(0, len(piece.text), 1800):
            part = piece.text[offset : offset + 2000].strip()
            if part:
                pieces.append(
                    Piece(part, piece.section, piece.locator | {"offset": offset})
                )
    return pieces, warnings


class Corpus:
    def __init__(self, db):
        self.db, self.providers = db, Providers(db)

    def invalidate(self, conn, version):
        if not version:
            return
        conn.execute(
            "UPDATE facts SET active=false,status='stale' WHERE active AND evidence_versions @> %s",
            (js([version]),),
        )
        conn.execute(
            "UPDATE answer_cache SET valid=false WHERE version_ids @> %s",
            (js([version]),),
        )

    def ingest(self, relative: str, candidate="real", kind=None):
        path = self.db.settings.source_path(relative, candidate)
        if path.suffix.lower() not in SUPPORTED or path.stat().st_size > 30_000_000:
            raise ValueError("Unsupported format or file larger than 30 MB")
        hash_ = file_hash(path)
        old = self.db.one(
            "SELECT * FROM documents WHERE candidate=%s AND path=%s",
            (candidate, relative),
        )
        if old and old["current_version"]:
            v = self.db.one(
                "SELECT * FROM versions WHERE id=%s", (old["current_version"],)
            )
            same_model = self.db.one(
                "SELECT 1 FROM chunks WHERE version_id=%s AND embedding_model=%s",
                (v["id"], self.providers.embedding_id),
            )
            if (
                v["hash"] == hash_
                and not old["deleted"]
                and (same_model or v["status"] == "failed")
            ):
                return {"id": old["id"], "version": v["id"], "status": "unchanged"}
        document_id = old["id"] if old else uid()
        version_id = digest([document_id, hash_])
        status, warnings, pieces = "complete", [], []
        try:
            pieces, warnings = extract(path)
            status = "partial" if warnings else "complete"
            if not pieces:
                status, warnings = (
                    "failed",
                    ["No text extracted; OCR or a different export is required"],
                )
        except Exception as exc:
            status, warnings = "failed", [f"Extraction failed: {type(exc).__name__}"]
        embeddings = []
        for start in range(0, len(pieces), 32):
            embeddings.extend(
                self.providers.embed([p.text for p in pieces[start : start + 32]])
            )
        # Same basename in the same package links PDF, HTML and DOCX renderings.
        group = str(Path(relative).with_suffix("")).lower()
        with self.db.tx() as c:
            c.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (candidate + relative,)
            )
            self.invalidate(c, old["current_version"] if old else None)
            c.execute(
                "INSERT INTO documents(id,candidate,path,group_key,kind,current_version) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(candidate,path) DO UPDATE SET current_version=EXCLUDED.current_version, deleted=false",
                (
                    document_id,
                    candidate,
                    relative,
                    group,
                    kind or classification(path),
                    version_id,
                ),
            )
            c.execute(
                "INSERT INTO versions(id,document_id,hash,status,warnings,text_length) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET status=EXCLUDED.status,warnings=EXCLUDED.warnings,extracted_at=now()",
                (
                    version_id,
                    document_id,
                    hash_,
                    status,
                    js(warnings),
                    sum(len(p.text) for p in pieces),
                ),
            )
            c.execute("DELETE FROM chunks WHERE version_id=%s", (version_id,))
            for n, (p, vector) in enumerate(zip(pieces, embeddings)):
                c.execute(
                    "INSERT INTO chunks(version_id,ordinal,section,locator,body,embedding,embedding_model,historical) VALUES(%s,%s,%s,%s,%s,%s::vector,%s,%s)",
                    (
                        version_id,
                        n,
                        p.section,
                        js(p.locator),
                        p.text,
                        str(vector),
                        self.providers.embedding_id,
                        bool(
                            re.search(
                                r"\bhistor(?:y|ical)\b|\bsuperseded\b", p.section, re.I
                            )
                        ),
                    ),
                )
            self.db.audit(
                "document.ingested",
                document_id,
                {"version": version_id, "status": status},
                c,
            )
        return {
            "id": document_id,
            "version": version_id,
            "status": status,
            "warnings": warnings,
            "chunks": len(pieces),
        }

    def sync(
        self,
        candidate="real",
        checkpoint=None,
        cancelled=lambda: False,
        progress=lambda x: None,
    ):
        root = (
            self.db.settings.workspace
            if candidate == "real"
            else self.db.settings.fixture_workspace
        )
        files = sorted(
            p
            for p in root.rglob("*")
            if p.is_file()
            and p.suffix.lower() in SUPPORTED
            and not any(
                x in EXCLUDED or x.startswith(".") for x in p.relative_to(root).parts
            )
            and not SECRET_KEYS.search(p.name)
            and not p.is_symlink()
        )
        counts = {"complete": 0, "partial": 0, "failed": 0, "unchanged": 0}
        seen = []
        for i, path in enumerate(files):
            if cancelled():
                return counts | {"cancelled": True}
            relative = str(path.relative_to(root))
            seen.append(relative)
            result = self.ingest(relative, candidate)
            counts[result["status"]] += 1
            progress(
                {
                    "completed": i + 1,
                    "total": len(files),
                    "counts": counts,
                    "last_path": relative,
                }
            )
        if candidate == "real":
            generated = self.db.settings.state / "prepared-workspace"
            for path in sorted(generated.rglob("*")):
                if (
                    not path.is_file()
                    or path.is_symlink()
                    or path.suffix.lower() not in SUPPORTED
                ):
                    continue
                if cancelled():
                    return counts | {"cancelled": True}
                relative = "_AgentGenerated/" + str(path.relative_to(generated))
                seen.append(relative)
                result = self.ingest(relative, candidate, kind="generated_draft")
                counts[result["status"]] += 1
        with self.db.tx() as c:
            missing = c.execute(
                "SELECT * FROM documents WHERE candidate=%s AND NOT deleted AND NOT (path=ANY(%s))",
                (candidate, seen),
            ).fetchall()
            for doc in missing:
                self.invalidate(c, doc["current_version"])
                c.execute("UPDATE documents SET deleted=true WHERE id=%s", (doc["id"],))
                self.db.audit("document.deleted", doc["id"], {}, c)
        return counts | {"deleted": len(missing), "total": len(seen)}

    def retrieve(
        self,
        query: str,
        candidate="real",
        kinds=None,
        limit=5,
        budget=18000,
        full_document=False,
        rerank=False,
    ):
        self.db.metric("retrieval_calls")
        lexical_only = self.providers.config.retrieval_mode == "lexical"
        vector = None if lexical_only else self.providers.embed([query])[0]
        kinds = kinds or [
            "confirmation_record",
            "employer_source",
            "imported_claim",
            "submission_evidence",
            "tracker",
        ]
        common = """FROM chunks c JOIN versions v ON v.id=c.version_id JOIN documents d ON d.id=v.document_id
          WHERE d.candidate=%s AND NOT d.deleted AND d.current_version=v.id AND d.kind=ANY(%s) AND NOT c.historical"""
        fields = "SELECT c.id,c.version_id,c.ordinal,c.section,c.locator,c.body,d.id AS document_id,d.path,d.kind,d.group_key,v.status,v.warnings "
        lex = self.db.all(
            fields
            + common
            + " AND c.lex @@ websearch_to_tsquery('english',%s) ORDER BY ts_rank_cd(c.lex,websearch_to_tsquery('english',%s)) DESC LIMIT 60",
            (candidate, kinds, query, query),
        )
        semantic = (
            []
            if lexical_only
            else self.db.all(
                fields
                + common
                + " AND c.embedding_model=%s ORDER BY c.embedding <=> %s::vector LIMIT 60",
                (candidate, kinds, self.providers.embedding_id, str(vector)),
            )
        )
        by_id, scores = {}, {}
        for rows in (lex, semantic):
            for rank, row in enumerate(rows):
                by_id[row["id"]] = row
                scores[row["id"]] = scores.get(row["id"], 0) + 1 / (60 + rank + 1)
        ranked = sorted(by_id.values(), key=lambda r: scores[r["id"]], reverse=True)
        if rerank:
            terms = set(re.findall(r"\w+", query.lower()))
            ranked.sort(
                key=lambda r: (
                    len(terms & set(re.findall(r"\w+", r["body"].lower()))),
                    scores[r["id"]],
                ),
                reverse=True,
            )
        selected, groups, spent = [], set(), 0
        for hit in ranked:
            if hit["group_key"] in groups or len(selected) >= limit:
                continue
            all_chunks = self.db.all(
                "SELECT ordinal,section,locator,body FROM chunks WHERE version_id=%s AND NOT historical ORDER BY ordinal",
                (hit["version_id"],),
            )
            size = sum(len(c["body"]) for c in all_chunks)
            # Expand small documents, including every later page. Large docs include parent and neighbors.
            if size <= min(10000, budget - spent) or (
                full_document and size <= budget - spent
            ):
                context = all_chunks
                expansion = "full_document"
            else:
                context = [
                    c
                    for c in all_chunks
                    if abs(c["ordinal"] - hit["ordinal"]) <= 1
                    or c["section"] == hit["section"]
                ]
                expansion = "section_and_neighbors"
            if spent + sum(len(c["body"]) for c in context) > budget:
                context = [c for c in all_chunks if c["ordinal"] == hit["ordinal"]]
                expansion = "matched_chunk_budget_limit"
            if spent + sum(len(c["body"]) for c in context) > budget:
                continue
            spent += sum(len(c["body"]) for c in context)
            groups.add(hit["group_key"])
            selected.append(
                {k: v for k, v in hit.items() if k != "body"}
                | {
                    "score": scores[hit["id"]],
                    "context": context,
                    "expansion": expansion,
                    "trusted_as_instruction": False,
                }
            )
        if not selected:
            self.db.metric("retrieval_failures")
        return {
            "evidence": selected,
            "characters": spent,
            "embedding_provider": None if lexical_only else self.providers.embedding_id,
            "retrieval_mode": "lexical" if lexical_only else "hybrid",
            "warning": "Retrieved claims need confirmation; generated prose is not independent evidence.",
        }
