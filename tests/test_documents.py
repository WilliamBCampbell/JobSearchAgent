"""Document quality, provenance, and the established HTML conversion workflow."""

import copy
import json
from pathlib import Path

import pytest
from pypdf import PdfReader
from docx import Document

from jobsearch.config import file_hash
from jobsearch.documents.content import fixture_document, validate_document
from jobsearch.documents.rendering import document_html
from jobsearch.llm.contracts import InferenceRequest
from jobsearch.llm.service import InferenceService
from jobsearch.memory import Memory, FactInput
from jobsearch.packages import Packages
from .test_llm import application


def packet_and_draft(db):
    app = application(db)
    packet = InferenceService(db).context(
        InferenceRequest(task="package_draft", application_id=app["id"])
    )
    return app, packet, fixture_document(packet)


def test_document_requires_complete_history_and_supported_claims(db):
    _, packet, draft = packet_and_draft(db)
    assert validate_document(draft, packet)
    invalid = copy.deepcopy(draft)
    invalid["experience"] = []
    with pytest.raises(ValueError, match="employment record"):
        validate_document(invalid, packet)
    invalid = copy.deepcopy(draft)
    invalid["summary"]["text"] = "Increased revenue by 500 percent."
    with pytest.raises(ValueError, match="Unsupported number"):
        validate_document(invalid, packet)
    invalid["summary"]["text"] = "Candidate confirmed these skills on October 8, 2026."
    with pytest.raises(ValueError, match="internal notes"):
        validate_document(invalid, packet)
    invalid = copy.deepcopy(draft)
    invalid["skills"][0]["text"] = draft["summary"]["text"]
    with pytest.raises(ValueError, match="repeats"):
        validate_document(invalid, packet)
    invalid = copy.deepcopy(draft)
    invalid["summary"]["fact_ids"] = ["unknown"]
    with pytest.raises(ValueError, match="out-of-scope"):
        validate_document(invalid, packet)


def test_document_rendering_uses_confirmed_dates_and_escapes_text(db):
    app, packet, draft = packet_and_draft(db)
    profile = {"name": "Alex <Example>", "contacts": "alex@example.invalid"}
    draft["summary"]["text"] = "<script>outside instructions</script>"
    result = document_html("resume", draft, packet["facts"], profile, app)
    assert "01/2020 to 08/2026" in result
    assert "<script>" not in result and "&lt;script&gt;" in result
    assert "Professional Experience" in result and "<ul>" in result


def test_document_evidence_excludes_private_answers_and_detects_conflicts(db):
    app, packet, _ = packet_and_draft(db)
    assert all(f["key"] not in ("email", "professional_name") for f in packet["facts"])
    Memory(db).put(
        FactInput(
            key="salary",
            value="secret other-role answer",
            kind="answer",
            scope="other-role",
            status="confirmed",
            provenance={"type": "user_confirmation"},
        )
    )
    request = InferenceRequest(task="package_draft", application_id=app["id"])
    assert "secret other-role" not in json.dumps(InferenceService(db).context(request))
    Memory(db).put(
        FactInput(
            key="api_delivery",
            value="Contradictory claim",
            kind="employment",
            status="confirmed",
            provenance={"type": "user_confirmation"},
        )
    )
    with pytest.raises(ValueError, match="Conflicting career"):
        InferenceService(db).context(request)


def test_package_renders_from_html_and_reuses_immutable_versions(db, writing_skill):
    app, _, _ = packet_and_draft(db)
    result = Packages(db).prepare(app)
    assert result["renderer"] == "html-letter-v4"
    assert len(result["files"]) == 6 and len(result["previews"]) == 2
    for kind in ("resume", "cover_letter"):
        pdf = next(f for f in result["files"] if f["kind"] == kind + ".pdf")
        reader = PdfReader(pdf["path"])
        assert len(reader.pages) == 1
        assert (reader.pages[0].mediabox.width, reader.pages[0].mediabox.height) == (
            612,
            792,
        )
        doc = Document(
            next(f["path"] for f in result["files"] if f["kind"] == kind + ".docx")
        )
        assert round(doc.sections[0].page_width.inches, 1) == 8.5
        assert round(doc.sections[0].page_height.inches) == 11
        assert result["checks"][kind]["text_matches_html"]
        assert result["checks"][kind]["visual_review"] == "pending"
        if kind == "cover_letter":
            from bs4 import BeautifulSoup

            source = Path(
                next(
                    f["path"]
                    for f in result["files"]
                    if f["kind"] == "cover_letter.html"
                )
            )
            texts = [
                BeautifulSoup(source.read_text(), "html.parser").get_text(" "),
                " ".join(paragraph.text for paragraph in doc.paragraphs),
                reader.pages[0].extract_text(),
            ]
            for text in texts:
                assert "requisition" not in text.lower()
                assert app["requisition"] not in text
                assert app["employer"] in text and app["role"] in text
    before = {f["path"]: file_hash(Path(f["path"])) for f in result["files"]}
    again = Packages(db).prepare(app)
    assert again["version"] == result["version"]
    assert before == {p: file_hash(Path(p)) for p in before}
    assert len(db.all("SELECT * FROM llm_runs")) == 2
    assert not db.all("SELECT * FROM attempts")
    generated = db.all("SELECT * FROM documents WHERE kind='generated_draft'")
    assert len(generated) >= 6
    for artifact in db.all("SELECT claims FROM artifacts"):
        assert set(artifact["claims"]) <= set(result["claim_fact_ids"])
    pdf_path = Path(
        next(f["path"] for f in result["files"] if f["kind"] == "resume.pdf")
    )
    pdf_path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="immutable"):
        Packages(db).prepare(app)
    assert pdf_path.read_bytes() == b"tampered"
    # A new review must have its own immutable review record, even for identical text.
    writing_skill.write_text(writing_skill.read_text() + "\nCheck repetition again.\n")
    refreshed = Packages(db).prepare(app)
    assert refreshed["version"] != result["version"]
    assert pdf_path.read_bytes() == b"tampered"
    assert len(db.all("SELECT * FROM llm_runs")) == 4


def test_failed_content_review_never_publishes_attachments(db, monkeypatch):
    app, _, _ = packet_and_draft(db)
    original = InferenceService.fixture

    def review(self, request, packet):
        if request.task == "package_review":
            return {
                "passed": False,
                "issues": ["Framework ownership exceeds the cited evidence."],
            }
        return original(self, request, packet)

    monkeypatch.setattr(InferenceService, "fixture", review)
    with pytest.raises(ValueError, match="Framework ownership"):
        Packages(db).prepare(app)
    assert not db.all("SELECT * FROM artifacts")
    assert len(db.all("SELECT * FROM llm_runs WHERE task='package_draft'")) == 2


def test_cancelled_package_does_not_publish(db):
    app, _, _ = packet_and_draft(db)
    with pytest.raises(ValueError, match="cancelled"):
        Packages(db).prepare(app, cancelled=lambda: True)
    assert not db.all("SELECT * FROM artifacts")


def test_confirmed_training_import_retains_provenance_and_is_not_a_degree(db):
    from jobsearch.corpus import Corpus
    from jobsearch.intake import Intake

    path = db.settings.fixture_workspace / "Confirmed_Application_Answers.json"
    data = {
        "confirmed_date": "2026-10-09",
        "education": {
            "training": "Example Coding School",
            "training_location": "Columbus, Ohio",
            "training_completed_year": 2018,
        },
    }
    path.write_text(json.dumps(data))
    result = Corpus(db).ingest(path.name, "fixture")
    assert Intake(db).import_confirmed_education("fixture", path, result, data) == 1
    fact = db.one("SELECT * FROM facts WHERE key='education_example_coding_school'")
    assert fact["provenance"]["json_pointer"] == "/education"
    assert fact["evidence_versions"] == [result["version"]]
    assert fact["value"] == "Example Coding School | Columbus, Ohio | Completed 2018"
    assert (
        Intake(db).import_confirmed_education(
            "fixture", path, result, {"education": data["education"]}
        )
        == 0
    )
    _, packet, draft = packet_and_draft(db)
    draft["education"] = []
    with pytest.raises(ValueError, match="omitted confirmed education"):
        validate_document(draft, packet)


@pytest.mark.parametrize(
    "leak",
    [
        "Requisition R-12345",
        "REQ #12345",
        "Job ID: 12345",
        "Posting ID 12345",
        "Application ID 12345",
        "source_refs: posting",
        "aabbcc112233",
        "61d87d7a-c9de-4c31-928a-fbbfe6c06018",
    ],
)
def test_cover_letter_rejects_tracking_metadata_in_draft_and_rendering(db, leak):
    app, packet, draft = packet_and_draft(db)
    app = app | {
        "requisition": "aabbcc112233",
        "id": "61d87d7a-c9de-4c31-928a-fbbfe6c06018",
    }
    packet["application"].update({k: app[k] for k in ("id", "requisition")})
    draft["cover_letter"][0]["text"] += " " + leak
    with pytest.raises(ValueError, match="tracking metadata"):
        validate_document(draft, packet)
    with pytest.raises(ValueError, match="tracking metadata"):
        document_html(
            "cover_letter",
            draft,
            packet["facts"],
            {"name": "Alex Example", "contacts": "alex@example.invalid"},
            app,
        )


def test_template_metadata_cannot_bypass_publication_checks(db, monkeypatch):
    app, _, _ = packet_and_draft(db)
    original = document_html

    def regressed_template(kind, document, facts, profile, application):
        result = original(kind, document, facts, profile, application)
        if kind == "cover_letter":
            result = result.replace("</body>", "<p>Requisition R-12345</p></body>")
        return result

    monkeypatch.setattr("jobsearch.packages.document_html", regressed_template)
    with pytest.raises(ValueError, match="tracking metadata"):
        Packages(db).prepare(app)
    assert not db.all("SELECT * FROM artifacts")
