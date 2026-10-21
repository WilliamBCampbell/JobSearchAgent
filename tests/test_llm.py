import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import pytest
from fastapi.testclient import TestClient
from jobsearch.api import create_app
from jobsearch.config import atomic_write, digest
from jobsearch.db import js
from jobsearch.fixtures import seed
from jobsearch.workflow import Workflow
from jobsearch.memory import FactInput, Memory
from jobsearch.queue import Worker
from jobsearch.providers import Providers, ProviderConfig
from jobsearch.corpus import Corpus
from jobsearch.llm.contracts import InferenceRequest, Queries
from jobsearch.llm.routing import LLMConfig, Route
from jobsearch.llm.service import InferenceService
from jobsearch.llm.runner import execute, InferenceFailure, InferenceStopped


def application(db):
    seed(db)
    app = Workflow(db).create("Evaluation Demo", "API Engineer", "EVAL-1")
    body = b"<h1>API Engineer EVAL-1</h1><p>C# APIs required. Ohio eligible. Base salary $100000 to $140000.</p>"
    path = db.settings.state / "official-captures" / "posting.html"
    atomic_write(path, body)
    from datetime import datetime, timezone

    official = {
        "open": True,
        "application_url": "https://employer.example/apply/EVAL-1",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "source_hash": digest(body),
        "capture_path": str(path),
    }
    Workflow(db).set_official(app["id"], official)
    return Workflow(db).get(app["id"])


def test_routing_is_independent_of_embeddings(db):
    service = InferenceService(db)
    cfg = LLMConfig(backend="codex_exec", enabled=True)
    service.configure(cfg)
    assert service.config.route("query_expansion").model == "gpt-6-luna"
    assert service.config.route("fit_assessment").model == "gpt-6.1-sol"
    assert service.config.route("conflict_review").model == "gpt-6-astra"
    assert Providers(db).config.provider == "fixture"
    assert not Providers(db).config.enabled


def test_no_enabled_destination_no_process(db, monkeypatch):
    service = InferenceService(db)
    service.configure(LLMConfig(backend="codex_exec"))
    monkeypatch.setattr(
        "jobsearch.llm.service.execute", lambda *a: pytest.fail("Must not call Codex")
    )
    with pytest.raises(ValueError, match="disabled"):
        service.run(InferenceRequest(task="query_expansion", question="APIs"))


def test_scoped_evidence_correction_invalidates_llm_cache(db):
    app = application(db)
    memory = Memory(db)
    own = memory.put(
        FactInput(
            key="api_project",
            value="Built customer APIs",
            status="confirmed",
            scope=app["id"],
            provenance={"type": "user_confirmation"},
        )
    )
    memory.put(
        FactInput(
            key="salary",
            value="150000",
            kind="answer",
            status="confirmed",
            scope="another-role",
            provenance={"type": "user_confirmation"},
        )
    )
    memory.put(
        FactInput(
            key="api_project",
            value="Other company secret",
            status="confirmed",
            scope="another-role",
            provenance={"type": "user_confirmation"},
        )
    )
    service = InferenceService(db)
    req = InferenceRequest(task="evidence_selection", application_id=app["id"])
    first = service.run(req)
    assert service.run(req)["cached"]
    context = service.context(req)
    assert "Other company secret" not in json.dumps(
        context
    ) and "150000" not in json.dumps(context)
    memory.put(
        FactInput(
            key="api_project",
            value="Contributed to customer APIs",
            status="confirmed",
            scope=app["id"],
            provenance={"type": "user_confirmation"},
            supersedes=own["id"],
        )
    )
    corrected = service.run(req)
    assert not corrected["cached"] and corrected["run_id"] != first["run_id"]
    assert own["id"] not in corrected["output"]["fact_ids"]


def test_model_cannot_confirm_facts_or_waive_gap(db):
    app = application(db)
    service = InferenceService(db)
    req = InferenceRequest(task="fit_assessment", application_id=app["id"])
    packet = service.context(req)
    out = service.fixture(req, packet)
    for row in out["requirements"]:
        row["deduction"] = 0
    out["ohio"] = "confirmed"
    out["compensation"]["classification"] = "base"
    parsed = service.validate(req.task, out, packet)
    assert parsed["score_preview"]["total"] == 100
    assert not parsed["score_preview"]["primary"]
    before = db.one("SELECT count(*) n FROM facts")["n"]
    with pytest.raises(InferenceFailure, match="unconfirmed"):
        service.validate(
            "evidence_selection",
            {"fact_ids": ["invented"], "missing_questions": []},
            packet,
        )
    assert db.one("SELECT count(*) n FROM facts")["n"] == before
    assert not db.all("SELECT * FROM approvals")


def test_unknown_source_and_stale_assessment_blocked(db):
    app = application(db)
    service = InferenceService(db)
    req = InferenceRequest(task="fit_assessment", application_id=app["id"])
    packet = service.context(req)
    out = service.fixture(req, packet)
    out["requirements"][0]["source"] = "invented"
    with pytest.raises(InferenceFailure, match="unknown requirement"):
        service.validate(req.task, out, packet)
    result = service.run(req)
    Workflow(db).update_payload(app["id"], {"consents": {"new_commitment": True}})
    with pytest.raises(ValueError, match="changed"):
        service.apply_score(result["run_id"])


def test_provider_output_is_rejected_if_evidence_changes_mid_run(db, monkeypatch):
    app = application(db)
    service = InferenceService(db)
    service.configure(LLMConfig(backend="codex_exec", enabled=True))

    def changed(*args):
        Workflow(db).update_payload(app["id"], {"consents": {"changed": True}})
        return {"fact_ids": [], "missing_questions": []}, {}

    monkeypatch.setattr("jobsearch.llm.service.execute", changed)
    with pytest.raises(InferenceFailure, match="changed"):
        service.run(
            InferenceRequest(task="evidence_selection", application_id=app["id"])
        )
    assert db.one("SELECT status FROM llm_runs")["status"] == "failed"


def fake_factory(tmp_path, script):
    fake = tmp_path / "fake.py"
    fake.write_text(script)
    return lambda route, schema, output: [sys.executable, str(fake), str(output)]


def test_real_subprocess_stream_validation_and_no_reasoning_log(tmp_path):
    factory = fake_factory(
        tmp_path,
        """import sys,json
from pathlib import Path
sys.stdin.read()
print(json.dumps({'type':'thread.started','thread_id':'synthetic'}),flush=True)
print(json.dumps({'type':'item.completed','item':{'type':'reasoning','text':'DO NOT STORE ME'}}),flush=True)
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':12,'output_tokens':4}}),flush=True)
Path(sys.argv[1]).write_text(json.dumps({'queries':['API integration']}))
""",
    )
    events = []
    result, usage = execute(
        "fixture",
        Queries.model_json_schema(),
        Route(model="gpt-6-luna"),
        tmp_path / "run",
        5,
        lambda k, d: events.append((k, d)),
        lambda: False,
        factory,
    )
    assert result["queries"] == ["API integration"] and usage["input_tokens"] == 12
    assert "DO NOT STORE ME" not in json.dumps(events)
    assert not (tmp_path / "run" / "output.json").exists()


@pytest.mark.parametrize("mode", ["cancel", "timeout", "malformed", "tool", "error"])
def test_subprocess_failures_are_bounded(tmp_path, mode):
    script = "import sys,time,json\nsys.stdin.read()\n"
    if mode in ("cancel", "timeout"):
        script += "time.sleep(30)\n"
    elif mode == "malformed":
        script += 'print("invalid",flush=True)\n'
    elif mode == "tool":
        script += "print(json.dumps({'type':'item.started','item':{'type':'command_execution','command':'private-secret'}}),flush=True)\ntime.sleep(30)\n"
    else:
        script += "print(json.dumps({'type':'error','message':'private-secret'}),flush=True)\n"
    start = time.monotonic()
    with pytest.raises((InferenceFailure, InferenceStopped)) as error:
        execute(
            "fixture",
            Queries.model_json_schema(),
            Route(model="gpt-6-luna"),
            tmp_path / "run",
            0.4,
            lambda k, d: None,
            lambda: mode == "cancel" and time.monotonic() - start > 0.1,
            fake_factory(tmp_path, script),
        )
    assert time.monotonic() - start < 4
    assert "private-secret" not in str(error.value)


async def test_api_queues_monitored_jobs_and_task_dedup(db):
    app = application(db)
    with TestClient(create_app(db.settings)) as client:
        h = {"X-JSA-Token": db.settings.token()}
        request = {"task": "fit_assessment", "application_id": app["id"]}
        assert client.post("/api/llm/jobs", json=request).status_code == 401
        a = client.post("/api/llm/jobs", json=request, headers=h).json()
        again = client.post("/api/llm/jobs", json=request, headers=h).json()
        b = client.post(
            "/api/llm/jobs",
            json={"task": "evidence_selection", "application_id": app["id"]},
            headers=h,
        ).json()
        assert a["job_id"] == again["job_id"] and a["job_id"] != b["job_id"]
        worker = Worker(db)
        await worker.run_once()
        row = client.get("/api/jobs/" + a["job_id"], headers=h).json()
        assert row["status"] == "complete"
        rid = row["result"]["run_id"]
        detail = client.get("/api/llm/runs/" + rid, headers=h).json()
        assert detail["events"][-1]["event"] == "validated"
        assert "directory" not in detail
        assert Workflow(db).get(app["id"])["state"] == "verified"
        r = client.post("/api/llm/runs/" + rid + "/apply-score", json={}, headers=h)
        assert r.status_code == 200 and r.json()["state"] == "scored"
        await worker.browser.close()


async def test_restart_marks_old_inference_and_resumes_read_only_job(db):
    service = InferenceService(db)
    request = InferenceRequest(task="query_expansion", question="APIs")
    queued = service.start(request)
    result = service.run(request, queued["job_id"])
    db.execute(
        "UPDATE llm_runs SET status='running',output=NULL WHERE id=%s",
        (result["run_id"],),
    )
    db.execute(
        "UPDATE jobs SET status='running',attempts=1,worker='dead',lease_until=now()-interval '1 minute' WHERE id=%s",
        (queued["job_id"],),
    )
    worker = Worker(db)
    await worker.run_once()
    assert (
        db.one("SELECT status FROM llm_runs WHERE id=%s", (result["run_id"],))["status"]
        == "interrupted"
    )
    assert (
        db.one("SELECT status FROM jobs WHERE id=%s", (queued["job_id"],))["status"]
        == "complete"
    )
    assert not db.all("SELECT * FROM attempts")
    await worker.browser.close()


def test_lexical_retrieval_does_not_call_embeddings(db, monkeypatch):
    path = db.settings.workspace / "evidence.md"
    path.write_text("## Delivery\nBuilt PostgreSQL APIs for customer onboarding.")
    corpus = Corpus(db)
    corpus.ingest(path.name)
    Providers(db).configure(ProviderConfig(retrieval_mode="lexical"))
    monkeypatch.setattr(
        "jobsearch.providers.Providers.embed",
        lambda *a: pytest.fail("Lexical search must not embed"),
    )
    result = corpus.retrieve("PostgreSQL")
    assert result["evidence"] and result["embedding_provider"] is None


def test_timeout_includes_blocked_prompt_delivery(tmp_path):
    factory = fake_factory(tmp_path, "import time\ntime.sleep(30)\n")
    started = time.monotonic()
    with pytest.raises(InferenceFailure, match="timed out"):
        execute(
            "x" * 200000,
            Queries.model_json_schema(),
            Route(model="gpt-6-luna"),
            tmp_path / "run",
            0.4,
            lambda k, d: None,
            lambda: False,
            factory,
        )
    assert time.monotonic() - started < 4


def test_worker_death_kills_codex_process(tmp_path):
    marker = tmp_path / "pid"
    script = tmp_path / "parent.py"
    script.write_text("""import sys
from pathlib import Path
from jobsearch.llm.runner import execute
from jobsearch.llm.contracts import Queries
from jobsearch.llm.routing import Route
root=Path(sys.argv[1])
def event(k,d):
    if k=='process_started': (root/'pid').write_text(str(d['pid']))
execute('test',Queries.model_json_schema(),Route(model='gpt-6-luna'),root/'run',40,event,lambda:False,lambda *a:[sys.executable,'-c','import time;time.sleep(30)'])
""")
    parent = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.03)
        assert marker.exists()
        child = int(marker.read_text())
        time.sleep(0.15)
        parent.kill()
        parent.wait(timeout=3)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            stat = Path(f"/proc/{child}/stat")
            if not stat.exists() or stat.read_text().split()[2] == "Z":
                break
            time.sleep(0.03)
        else:
            os.kill(child, signal.SIGKILL)
            pytest.fail("Codex survived the worker process")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=3)


def test_existing_form_gap_cannot_be_removed_by_model(db):
    app = application(db)
    from jobsearch.fixtures import assessment

    data = assessment(db).model_dump()
    data["requirements"].append(
        {
            "text": "Two years of production model deployment",
            "source": "https://employer.example/form",
            "category": "technical",
            "classification": "required",
            "central": True,
            "material": True,
            "assessment": "unconfirmed",
            "fact_ids": [],
            "deduction": 0,
            "reason": "The application form introduced this requirement.",
        }
    )
    db.execute("UPDATE applications SET score=%s WHERE id=%s", (js(data), app["id"]))
    service = InferenceService(db)
    request = InferenceRequest(task="fit_assessment", application_id=app["id"])
    packet = service.context(request)
    assert any(s["id"].startswith("form:") for s in packet["sources"])
    out = service.fixture(request, packet)
    # Offline rescoring now retains central requirements. Construct the invalid
    # model output explicitly so this test still checks omission protection.
    service.validate(request.task, out, packet)
    out["requirements"] = [
        r
        for r in out["requirements"]
        if r["text"] != "Two years of production model deployment"
    ]
    with pytest.raises(InferenceFailure, match="central requirement"):
        service.validate(request.task, out, packet)


def test_contact_and_commitment_fields_do_not_go_to_model(db):
    app = application(db)
    Memory(db).put(
        FactInput(
            key="salary",
            value="180000",
            scope=app["id"],
            status="confirmed",
            provenance={"type": "user_confirmation"},
        )
    )
    request = InferenceRequest(task="evidence_selection", application_id=app["id"])
    packet = InferenceService(db).context(request)
    keys = {f["key"] for f in packet["facts"]}
    assert not keys & {"salary", "professional_name", "email", "phone"}
    assert packet["facts"]


def test_new_application_question_is_not_deduplicated_with_different_input(db):
    app = application(db)
    service = InferenceService(db)
    one = service.start(
        InferenceRequest(
            task="draft", application_id=app["id"], question="API delivery"
        )
    )
    two = service.start(
        InferenceRequest(
            task="draft", application_id=app["id"], question="Team collaboration"
        )
    )
    assert one["job_id"] != two["job_id"]


async def test_model_jobs_wait_for_capacity_without_spending_retry_attempts(db):
    service = InferenceService(db)
    first = service.start(InferenceRequest(task="query_expansion", question="APIs"))
    second = service.start(
        InferenceRequest(task="query_expansion", question="Onboarding")
    )
    worker = Worker(db)
    assert worker.claim()["id"] == first["job_id"]
    assert worker.claim() is None
    assert (
        db.one("SELECT attempts FROM jobs WHERE id=%s", (second["job_id"],))["attempts"]
        == 0
    )
    db.execute("UPDATE jobs SET status='complete' WHERE id=%s", (first["job_id"],))
    assert worker.claim()["id"] == second["job_id"]
    await worker.browser.close()


def test_failed_validation_still_records_provider_usage(db, monkeypatch):
    service = InferenceService(db)
    service.configure(LLMConfig(backend="codex_exec", enabled=True))

    def invalid(prompt, schema, route, directory, timeout, event, cancelled):
        event("turn_completed", {"usage": {"input_tokens": 10, "output_tokens": 5}})
        return {"wrong_key": "bad response"}, {"input_tokens": 10, "output_tokens": 5}

    monkeypatch.setattr("jobsearch.llm.service.execute", invalid)
    with pytest.raises(InferenceFailure):
        service.run(InferenceRequest(task="query_expansion", question="APIs"))
    assert db.one("SELECT usage FROM llm_runs")["usage"]["input_tokens"] == 10
