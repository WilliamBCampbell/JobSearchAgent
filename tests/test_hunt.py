from datetime import datetime, timezone
import pytest
from jobsearch.config import atomic_write, digest
from jobsearch.db import js
from jobsearch.fixtures import seed, assessment
from jobsearch.hunt import Hunt, HuntRequest
from jobsearch.jobspy_client import JobSpyClient
from jobsearch.llm.service import InferenceService
from jobsearch.queue import Worker
from jobsearch.workflow import Workflow, ApprovalInput
from jobsearch.openings import OfficialOpenings


async def test_mcp_search_request_is_idempotent_across_clients(db):
    data = {"request": {"request_key": "resume-same-hunt", "mode": "fixture"}}
    first = await JobSpyClient(db).call("start_search", data)
    second = await JobSpyClient(db).call("start_search", data)
    assert first["job_id"] == second["job_id"]
    assert db.one("SELECT count(*) n FROM jobs")["n"] == 1


async def test_autonomous_loop_mcp_to_ranked_packages_survives_restart(db, monkeypatch):
    seed(db)
    original = db.settings.workspace / "original.txt"
    original.write_text("unchanged source")

    async def verification(self, lead):
        content = b"<html><h1>Software Engineer</h1><p>Build C# APIs. Required delivery and independent work. Remote in Ohio.</p></html>"
        path = db.settings.state / "official-captures" / "fixture.html"
        atomic_write(path, content)
        return {
            "open": True,
            "status": "verified",
            "requisition": lead["discovery"]["requisition"],
            "application_url": "https://employer.example/apply",
            "url": "https://employer.example/job",
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "capture_path": str(path),
            "source_hash": digest(content),
        }

    monkeypatch.setattr(Hunt, "fixture_verify", verification)
    fixture = InferenceService.fixture

    def output(self, request, packet):
        if request.task in ("requirement_extraction", "fit_assessment"):
            data = assessment(db, track=packet["application"]["track"]).model_dump()
            for requirement in data["requirements"]:
                requirement["source"] = packet["sources"][0]["id"]
            if request.task == "requirement_extraction":
                return {
                    "requirements": [
                        {
                            k: r[k]
                            for k in (
                                "text",
                                "source",
                                "category",
                                "classification",
                                "central",
                            )
                        }
                        for r in data["requirements"]
                    ],
                    "missing_questions": [],
                }
            data.pop("exceptional_low_pay_reason")
            data["compensation"] = {
                "base": {"min": 100000, "max": 140000},
                "bonus": None,
                "commission": None,
                "equity": None,
                "ote": None,
                "classification": "base",
            }
            return data | {"missing_questions": []}
        return fixture(self, request, packet)

    monkeypatch.setattr(InferenceService, "fixture", output)
    hunt = Hunt(db).start(HuntRequest(candidate="fixture", primary_limit=2))
    for _ in range(90):
        # A fresh worker on each step exercises resumption from database state.
        worker = Worker(db)
        db.execute("UPDATE jobs SET available_at=now() WHERE status='queued'")
        await worker.run_once()
        await worker.browser.close()
        current = Hunt(db).get(hunt["id"])
        if current["status"] == "complete":
            break
        failed = db.all("SELECT kind,error FROM jobs WHERE status='failed'")
        assert not failed, failed
    assert current["status"] == "complete"
    ready = [x for x in current["leads"] if x["status"] == "ready"]
    assert len(ready) == 2
    assert all(x["score"]["primary"] and x["state"] == "prepared" for x in ready)
    assert all(x["payload"]["package"]["files"] for x in ready)
    assert not db.all("SELECT * FROM approvals")
    assert not db.all("SELECT * FROM attempts")
    assert db.one("SELECT 1 FROM audit WHERE event='mcp.tool_completed'")
    assert original.read_text() == "unchanged source"
    assert list(db.settings.workspace.iterdir()) == [original]


def test_real_hunt_requires_live_sources_and_enabled_codex(db):
    with pytest.raises(ValueError, match="online"):
        Hunt(db).start(HuntRequest())
    db.set_setting("live_search", True)
    with pytest.raises(ValueError, match="Codex"):
        Hunt(db).start(HuntRequest())


async def test_pause_resume_cancel_do_not_lose_checkpoint(db):
    hunt = Hunt(db).start(HuntRequest(candidate="fixture"))
    service = Hunt(db)
    service.save(hunt["id"], {"stage": "saved", "search_id": "pending-id"})
    service.control(hunt["id"], "pause")
    worker = Worker(db)
    await worker.run_once()
    assert service.get(hunt["id"])["checkpoint"]["stage"] == "saved"
    service.control(hunt["id"], "resume")
    assert service.get(hunt["id"])["status"] == "running"
    service.control(hunt["id"], "cancel")
    assert service.get(hunt["id"])["status"] == "cancelled"
    await worker.browser.close()


def test_real_proposals_are_separate_from_imported_status_and_submission(db):
    flow = Workflow(db)
    proposal = flow.create(
        "Employer", "Engineer", "NEW-1", candidate="real", discovery_key="lead-1"
    )
    same = flow.create(
        "Employer", "Engineer", "NEW-1", candidate="real", discovery_key="lead-1"
    )
    assert proposal["id"] == same["id"]
    with pytest.raises(ValueError, match="disabled"):
        flow.authorize(
            proposal["id"], ApprovalInput(approve=True, reviewed_hash="a" * 64)
        )
    db.execute("UPDATE applications SET imported=true WHERE id=%s", (proposal["id"],))
    with pytest.raises(ValueError, match="read-only"):
        flow.transition(proposal["id"], "verified")


def test_official_greenhouse_checks_identity_and_current_job(db, monkeypatch):
    service = OfficialOpenings(db)

    def get(url):
        if "/jobs/" not in url:
            return {"name": "Example Company"}
        return {
            "id": 123,
            "title": "Software Engineer",
            "requisition_id": "R-9",
            "absolute_url": "https://job-boards.greenhouse.io/example/jobs/123",
            "content": "<p>Build C# APIs and PostgreSQL services. Work independently to deliver production software, review changes and support customer workflows.</p>",
            "questions": [{"label": "Salary target", "required": True}],
        }

    monkeypatch.setattr(service, "get_json", get)
    monkeypatch.setattr("jobsearch.openings.public_url", lambda u: None)
    lead = {"company": "Example Company", "title": "Software Engineer"}
    result = service.ats(lead, "https://job-boards.greenhouse.io/example/jobs/123")
    assert result["open"] and result["requisition"] == "R-9"
    assert result["details"]["questions"][0]["required"]
    with pytest.raises(ValueError, match="employer"):
        service.ats(
            lead | {"company": "Other Company"},
            "https://job-boards.greenhouse.io/example/jobs/123",
        )


def test_career_import_uses_confirmed_sections_and_excludes_role_commitments(db):
    from jobsearch.corpus import Corpus
    from jobsearch.intake import Intake

    path = db.settings.workspace / "job_search_workflow.md"
    path.write_text("""# Workflow
### Confirmed technical and customer experience
- C# and PostgreSQL production software.

### AI delivery and BAM answers confirmed October 8, 2026
Contributed to an agent framework; was not its primary developer.

For BAM, confirmed a salary and a schedule for that application.

### Historical draft
Invented unrelated experience must not become confirmed.
""")
    result = Corpus(db).ingest(path.name, "real")
    intake = Intake(db)
    assert intake.import_confirmed_career("real", path, result) == 2
    intake.import_confirmed_career("real", path, result)
    facts = db.all("SELECT * FROM facts")
    assert len(facts) == 2
    assert all(
        f["status"] == "confirmed" and f["evidence_versions"] == [result["version"]]
        for f in facts
    )
    assert all("BAM" not in f["value"] and "Invented" not in f["value"] for f in facts)


def test_private_generated_documents_survive_sync_without_becoming_facts(db):
    from jobsearch.corpus import Corpus

    path = db.settings.state / "prepared-workspace" / "Example" / "resume.md"
    atomic_write(
        path, "# Generated resume\nUnsupported invented tenure remains a draft."
    )
    corpus = Corpus(db)
    corpus.ingest("_AgentGenerated/Example/resume.md", "real", kind="generated_draft")
    corpus.sync("real")
    doc = db.one("SELECT * FROM documents")
    assert not doc["deleted"] and doc["kind"] == "generated_draft"
    assert not corpus.retrieve("invented tenure", "real")["evidence"]
    assert list(db.settings.workspace.iterdir()) == []


def test_model_schema_constrains_rubric_and_evidence_identifiers(db):
    packet = {
        "facts": [{"id": "confirmed-fact"}],
        "sources": [{"id": "official:capture"}],
        "rubric": {"technical": 35, "delivery": 25},
    }
    schema = InferenceService(db).output_schema("fit_assessment", packet)
    props = schema["$defs"]["AssessedRequirement"]["properties"]
    assert props["category"]["enum"] == ["technical", "delivery"]
    assert props["source"]["enum"] == ["official:capture"]
    assert props["fact_ids"]["items"]["enum"] == ["confirmed-fact"]


async def test_invalid_model_output_gets_one_repair_then_stays_visible(db, monkeypatch):
    from jobsearch.db import uid

    seed(db)
    service = Hunt(db)
    hunt = service.start(HuntRequest(candidate="fixture"))
    service.save(
        hunt["id"], {"search_id": "finished", "discovery_loaded": True, "discovered": 1}
    )
    app = Workflow(db).create("Employer Demo", "Engineer", "R-1")
    child = uid()
    db.execute(
        "INSERT INTO jobs(id,kind,status,input,error) VALUES(%s,'llm','failed',%s,'Invalid reference')",
        (child, js({"task": "fit_assessment", "application_id": app["id"]})),
    )
    lid = uid()
    db.execute(
        "INSERT INTO hunt_leads(id,hunt_id,ordinal,discovery,status,application_id,child_job) VALUES(%s,%s,0,'{}','fit_assessment',%s,%s)",
        (lid, hunt["id"], app["id"], child),
    )
    calls = []

    def start(request):
        calls.append(request)
        jid = uid()
        db.execute(
            "INSERT INTO jobs(id,kind,status,input,error) VALUES(%s,'llm','failed',%s,'Still invalid')",
            (jid, js(request.model_dump())),
        )
        return {"job_id": jid}

    monkeypatch.setattr(service.inference, "start", start)
    await service.tick({"input": {"hunt_id": hunt["id"]}})
    assert len(calls) == 1 and "Invalid reference" in calls[0].question
    await service.tick({"input": {"hunt_id": hunt["id"]}})
    lead = db.one("SELECT * FROM hunt_leads WHERE id=%s", (lid,))
    assert lead["status"] == "evaluation_failed" and lead["reason"] == "Still invalid"
    assert len(calls) == 1


def test_real_package_stays_private_and_has_readable_contact_and_history(db):
    from jobsearch.memory import Memory, FactInput
    from jobsearch.packages import Packages
    from pathlib import Path
    from pypdf import PdfReader

    for key, value, kind in [
        ("first_name", "Sample", "candidate"),
        ("last_name", "Candidate", "candidate"),
        ("email", "sample@example.invalid", "candidate"),
        ("phone", "+15555550123", "candidate"),
        (
            "employment_example",
            {
                "title": "Software Engineer",
                "employer": "Example",
                "start": "2020",
                "end": "2025",
            },
            "employment",
        ),
        ("skill_csharp", "Built C# APIs and PostgreSQL services.", "skill"),
    ]:
        Memory(db).put(
            FactInput(
                candidate="real",
                kind=kind,
                key=key,
                value=value,
                status="confirmed",
                provenance={"type": "user_confirmation"},
            )
        )
    app = Workflow(db).create("Employer", "Engineer", "R-1", candidate="real")
    result = Packages(db).prepare(app, include_cover=False)
    pdf = next(f for f in result["files"] if f["kind"] == "resume.pdf")
    text = "\n".join(p.extract_text() for p in PdfReader(pdf["path"]).pages)
    assert "Sample Candidate" in text and "sample@example.invalid" in text
    assert "Software Engineer" in text and "Example | 2020 to 2025" in text
    assert "PROFESSIONAL EXPERIENCE" in text and "PROFESSIONAL SUMMARY" in text
    assert Path(pdf["path"]).is_relative_to(db.settings.state / "prepared-workspace")
    assert list(db.settings.workspace.iterdir()) == []
    assert (
        db.one(
            "SELECT count(*) n FROM documents WHERE candidate='real' AND kind='generated_draft'"
        )["n"]
        == 3
    )
