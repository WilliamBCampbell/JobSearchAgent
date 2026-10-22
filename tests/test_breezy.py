"""Synthetic Breezy contracts, local review, and approval-bound browser execution."""

import copy
import json
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jobsearch.breezy import breezy_form
from jobsearch.breezy_browser import BreezyBrowser, validate_candidate
from jobsearch.forms import FormPreparation
from jobsearch.queue import Worker
from jobsearch.workflow import ApprovalInput
from .test_package_revisions import prepared


def portal_data(requisition="TEST-1", employer="Example Employer"):
    return {
        "data": {
            "company": {"name": employer},
            "position": {
                "_id": requisition,
                "state": "published",
                "application_form": {
                    "name": "required",
                    "email_address": "required",
                    "phone_number": "required",
                    "resume": "required",
                    "cover_letter": "hidden",
                },
            },
            "formToken": "synthetic-token-never-part-of-contract",
            "questions": [
                {
                    "_id": 100,
                    "questions": [
                        {
                            "_id": "q1",
                            "text": "Preferred contact method",
                            "type": {"id": "dropdown"},
                            "required": True,
                            "options": [{"text": "Email"}, {"text": "Phone"}],
                        },
                        {
                            "_id": "q2",
                            "text": "I have read the application instructions",
                            "type": {"id": "checkboxes"},
                            "required": True,
                            "options": [{"text": "I understand"}],
                        },
                        {
                            "_id": "q3",
                            "text": "Additional information",
                            "type": {"id": "paragraph"},
                            "required": False,
                        },
                    ],
                }
            ],
        }
    }


def html(data, script=""):
    return (
        '<!doctype html><html><body><script type="application/json" id="portal-data">'
        + json.dumps(data)
        + "</script>"
        + script
        + "</body></html>"
    )


SCRIPT = r"""<input name="cName"><input name="cEmail" type="email"><input name="cPhoneNumber">
<input name="cResume" type="file"><span id="filename"></span>
<select name="section_100_question_0"><option>Email</option><option>Phone</option></select>
<input name="section_100_question_1" type="checkbox"><textarea name="section_100_question_2"></textarea>
<button type="button">Submit Application</button><div id="error"></div>
<script>
const data=JSON.parse(document.querySelector('#portal-data').textContent).data;
let resume={};
document.querySelector('[name=cResume]').onchange=async e=>{
 const file=e.target.files[0], form=new FormData();form.append('file',file,file.name);
 const r=await fetch('/api/portal/example/upload',{method:'POST',body:form});
 const d=await r.json();resume={file_name:file.name,url:d.url,error:false};
 document.querySelector('#filename').textContent=file.name;
};
document.querySelector('button').onclick=async()=>{
 const val=n=>document.querySelector('[name="'+n+'"]').value;
 const questions=structuredClone(data.questions[0].questions);
 questions[0].response=val('section_100_question_0');
 questions[1].responses=[document.querySelector('[name=section_100_question_1]').checked];
 questions[2].response=val('section_100_question_2');
 const body={name:val('cName'),email_address:val('cEmail'),phone_number:val('cPhoneNumber'),resume,questionnaire:questions,sms_consent:{has_consent:false},_portal_language:'en'};
 // Match Breezy's client: network errors have null data, which the callback
 // mistakenly treats as success. An explicit error body keeps the form open.
 let error=null;
 try {
   const r=await fetch('/api/apply/'+data.position._id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
   if(!r.ok)error=await r.json();
 } catch (_) {}
 if(!error)location.assign(location.pathname+'/submitted');else document.querySelector('#error').textContent='Local review only';
};
document.querySelector('[name=cEmail]').addEventListener('input',e=>fetch('/leak?email='+encodeURIComponent(e.target.value)));
</script>"""


@pytest.fixture
def breezy_server():
    state = {
        "page": "",
        "posts": [],
        "gets": [],
        "leaks": 0,
        "receipt": True,
        "receipt_script": False,
        "post_status": 200,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["gets"].append(self.path)
            if self.path == "/receipt-assets/confirmation.js":
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript")
                self.end_headers()
                self.wfile.write(
                    b"document.body.querySelector('#confirmation').textContent='Your application has been submitted successfully. Good luck!';"
                )
                return
            if self.path.startswith("/leak"):
                state["leaks"] += 1
            body = (
                (
                    '<html><body><div id="confirmation"></div><script src="/receipt-assets/confirmation.js"></script></body></html>'
                    if state["receipt_script"]
                    else "Application received. Thank you for applying."
                )
                if self.path.endswith("/submitted") and state["receipt"]
                else state["page"]
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state["posts"].append((self.path, body))
            data = (
                {"url": state["origin"] + "/uploaded/resume.pdf"}
                if self.path.endswith("/upload")
                else {}
            )
            status = 200 if self.path.endswith("/upload") else state["post_status"]
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if status != 204:
                self.wfile.write(json.dumps(data).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state["origin"] = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


def test_breezy_contract_excludes_tokens_and_binds_questions():
    data = portal_data()
    url = "https://example.breezy.hr/p/TEST-1/apply"
    spec = breezy_form(html(data), url, "TEST-1", "Example Employer")
    assert len(spec.fields) == 7 and not spec.unsupported
    assert spec.fields[5].purpose == "consent"
    assert not spec.fields[5].qualification
    assert "synthetic-token" not in json.dumps(spec.model_dump())
    data["data"]["formToken"] = "another-token"
    assert breezy_form(html(data), url, "TEST-1").fingerprint == spec.fingerprint
    data["data"]["questions"][0]["questions"][0]["_id"] = "changed"
    assert breezy_form(html(data), url, "TEST-1").fingerprint != spec.fingerprint
    with pytest.raises(ValueError, match="employer"):
        breezy_form(html(data), url, "TEST-1", "Other Employer")


def configure(db, server, monkeypatch):
    flow, aid = prepared(db, reviewed=True)
    app = flow.get(aid)
    data = portal_data(app["requisition"], app["employer"])
    server["page"] = html(data, SCRIPT)
    url = server["origin"] + "/p/" + app["requisition"] + "/apply"
    spec = breezy_form(server["page"], url, app["requisition"], app["employer"])
    monkeypatch.setattr(FormPreparation, "inspect", lambda self, app: spec)
    # These contact preferences are not career qualifications.
    flow.transition(aid, "filling")
    result = FormPreparation(db).plan_spec(aid, spec)
    assert result["state"] == "missing_answers"
    for question in db.all(
        "SELECT * FROM questions WHERE application_id=%s AND answer_fact IS NULL",
        (aid,),
    ):
        field = next(f for f in spec.fields if f.key == question["key"])
        value = (
            ["I understand"]
            if field.purpose == "consent"
            else ("Email" if field.type == "select" else "Synthetic response")
        )
        flow.answer(question["id"], value)
    # Drive the queued fill directly here; the receipt test exercises the worker.
    db.execute(
        "DELETE FROM jobs WHERE kind='fill' AND input->>'application_id'=%s AND status='queued'",
        (aid,),
    )
    result = FormPreparation(db).plan(aid)
    assert result["state"] == "review", result
    assert result["adapter"] == "breezy"
    assert len(flow.get(aid)["payload"]["attachments"]) == 1
    assert all(
        f["purpose"] != "consent"
        or flow.get(aid)["payload"]["answers"][f["key"]]["provenance"]["type"]
        == "user_confirmation"
        for f in flow.get(aid)["payload"]["form_schema"]
    )
    return flow, aid, spec


@pytest.mark.parametrize("post_status", [200, 204])
async def test_breezy_public_success_page_is_not_a_receipt(
    db, breezy_server, monkeypatch, post_status
):
    flow, aid, spec = configure(db, breezy_server, monkeypatch)
    breezy_server["post_status"] = post_status
    breezy_server["receipt_script"] = True
    browser = BreezyBrowser(db)
    try:
        result = await browser.stage(aid)
        assert result["filled"]
        assert browser.sessions[aid]["gate"]["captured"]["_portal_language"] == "en"
        assert browser.sessions[aid]["page"].url == browser.sessions[aid]["spec"].url
        assert breezy_server["posts"] == [] and breezy_server["leaks"] == 0
        assert not db.all("SELECT * FROM attempts")
        assert flow.get(aid)["payload"]["browser_hash"] == flow.review(aid)["hash"]
        with pytest.raises(ValueError, match="approval"):
            await browser.submit(aid)
        flow.authorize(
            aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
        )
        worker = Worker(db)
        try:
            assert await worker.run_once()
            job = db.one("SELECT * FROM jobs WHERE kind='submit'")
            assert job["status"] == "failed"
            assert "does not verify employer receipt" in job["error"]
            assert flow.get(aid)["state"] == "unresolved"
            assert not db.all("SELECT * FROM receipts")
            assert len(db.all("SELECT * FROM attempts")) == 1
            assert "/receipt-assets/confirmation.js" in breezy_server["gets"]
            assert (
                db.one("SELECT result FROM attempts")["result"]["confirmation_page"][
                    "post_status"
                ]
                == post_status
            )
            assert [path for path, _ in breezy_server["posts"]] == [
                "/api/portal/example/upload",
                "/api/apply/" + spec.requisition,
            ]
            assert breezy_server["leaks"] == 0
            attached = flow.review(aid)["payload"]["attachments"][0]
            path = db.one("SELECT path FROM artifacts WHERE id=%s", (attached["id"],))[
                "path"
            ]
            assert Path(path).read_bytes() in breezy_server["posts"][0][1]
            assert db.one("SELECT result FROM attempts")["result"]["confirmation_page"][
                "screenshot_hash"
            ]
            with pytest.raises(ValueError, match="existing submission attempt"):
                await browser.submit(aid)
        finally:
            await worker.browser.close()
    finally:
        await browser.close()


async def test_breezy_rejects_changed_fields_before_upload(
    db, breezy_server, monkeypatch
):
    flow, aid, spec = configure(db, breezy_server, monkeypatch)
    data = portal_data(spec.requisition, flow.get(aid)["employer"])
    data["data"]["questions"][0]["questions"][0]["text"] = "Changed question"
    breezy_server["page"] = html(data, SCRIPT)
    browser = BreezyBrowser(db)
    try:
        with pytest.raises(ValueError, match="changed the application form"):
            await browser.stage(aid)
        assert breezy_server["posts"] == []
        assert not db.all("SELECT * FROM attempts")
        assert not flow.get(aid)["payload"]["browser_hash"]
    finally:
        await browser.close()


async def test_unreviewed_control_returns_error_without_false_success_redirect(
    db, breezy_server, monkeypatch
):
    flow, aid, spec = configure(db, breezy_server, monkeypatch)
    breezy_server["page"] = breezy_server["page"].replace(
        "const body={", "const body={unreviewed_control:'Synthetic unexpected value',"
    )
    browser = BreezyBrowser(db)
    try:
        with pytest.raises(
            ValueError, match="unreviewed application control: unreviewed_control"
        ):
            await browser.stage(aid)
        session = browser.sessions[aid]
        await session["page"].wait_for_timeout(200)
        assert session["page"].url == spec.url
        assert (
            "Local review stopped"
            in await session["page"].locator("#jsa-browser-status").inner_text()
        )
        assert (
            await session["page"].locator("#error").inner_text() == "Local review only"
        )
        assert breezy_server["posts"] == []
        assert not db.all("SELECT * FROM attempts")
        assert not db.all("SELECT * FROM receipts")
        assert not flow.get(aid)["payload"]["browser_hash"]
    finally:
        await browser.close()


async def test_breezy_checks_native_payload_against_review(
    db, breezy_server, monkeypatch
):
    flow, aid, spec = configure(db, breezy_server, monkeypatch)
    browser = BreezyBrowser(db)
    try:
        await browser.stage(aid)
        session = browser.sessions[aid]
        for change in (
            "email",
            "sms",
            "extra",
            "question",
            "file",
            "nested",
            "language",
            "language_type",
        ):
            payload = copy.deepcopy(session["gate"]["captured"])
            if change == "email":
                payload["email_address"] = "other@example.invalid"
            elif change == "sms":
                payload["sms_consent"]["has_consent"] = True
            elif change == "extra":
                payload["unreviewed"] = "Unexpected content"
            elif change == "question":
                payload["questionnaire"][0]["_id"] = "changed"
            elif change == "nested":
                payload["questionnaire"][0]["unreviewed"] = "Unexpected data"
            elif change == "language":
                payload["_portal_language"] = "Unexpected candidate content"
            elif change == "language_type":
                payload["_portal_language"] = {"unreviewed": "Unexpected data"}
            else:
                payload["resume"]["url"] = "https://unreviewed.invalid/file.pdf"
            with pytest.raises(ValueError):
                validate_candidate(
                    payload, spec, flow.review(aid), session["gate"]["upload_url"]
                )
        assert breezy_server["posts"] == []
    finally:
        await browser.close()


async def test_breezy_missing_receipt_and_restart_do_not_resubmit(
    db, breezy_server, monkeypatch
):
    flow, aid, spec = configure(db, breezy_server, monkeypatch)
    browser = BreezyBrowser(db)
    try:
        await browser.stage(aid)
        flow.authorize(
            aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
        )
        breezy_server["receipt"] = False
        with pytest.raises(ValueError, match="No Breezy receipt"):
            await browser.submit(aid)
        assert flow.get(aid)["state"] == "unresolved"
        assert not db.all("SELECT * FROM receipts")
        assert len(db.all("SELECT * FROM attempts")) == 1
        await browser.close()
        other = BreezyBrowser(db)
        try:
            with pytest.raises(ValueError, match="existing submission attempt"):
                await other.submit(aid)
            assert not other.sessions
        finally:
            await other.close()
        assert len(breezy_server["posts"]) == 2
        # A public success page must never turn an unknown outcome into a
        # receipt, even after a successful application HTTP response.
        breezy_server["receipt"] = True
        breezy_server["receipt_script"] = True
        recovery = BreezyBrowser(db)
        try:
            result = await recovery.reconcile(aid)
            assert result["state"] == "unresolved"
            assert result["receipt"] is False
            assert len(breezy_server["posts"]) == 2
            assert len(db.all("SELECT * FROM attempts")) == 1
            assert not db.all("SELECT * FROM receipts")
        finally:
            await recovery.close()
    finally:
        await browser.close()


async def test_receipt_recheck_requires_recorded_successful_response(
    db, breezy_server, monkeypatch
):
    flow, aid, spec = configure(db, breezy_server, monkeypatch)
    browser = BreezyBrowser(db)
    try:
        await browser.stage(aid)
        flow.authorize(
            aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
        )
        attempt = flow.begin_attempt(aid, mark_attached=False)
        flow.unresolved(aid, "Synthetic stop without an application request")
        with pytest.raises(
            ValueError, match="No successful Breezy application response"
        ):
            await browser.reconcile(aid)
        db.audit(
            "breezy.attempt_finished",
            aid,
            {
                "attempt_id": attempt,
                "post_observed": True,
                "post_status": 500,
            },
        )
        with pytest.raises(
            ValueError, match="No successful Breezy application response"
        ):
            await browser.reconcile(aid)
        assert breezy_server["posts"] == []
        assert not db.all("SELECT * FROM receipts")
        assert flow.get(aid)["state"] == "unresolved"
    finally:
        await browser.close()


def test_breezy_known_profile_labels_keep_their_exact_meaning():
    from jobsearch.profile_answers import mapping
    from jobsearch.forms import FieldSpec

    for label, key, options in [
        ("LinkedIn:\nIf you have no LinkedIn page type N/A", "linkedin", []),
        ("Github:\nIf you have no GitHub page type N/A", "github", []),
        ("What state are you located in?", "state", ["Ohio", "Michigan"]),
    ]:
        field = FieldSpec(
            key="custom",
            name="custom",
            label=label,
            type="select" if options else "text",
            options=options,
        )
        assert mapping(field.model_dump()) == (key, "identity")
        field.label += " and consent to receive promotional messages"
        assert mapping(field.model_dump()) is None


@pytest.mark.parametrize("public_recheck", [True, False])
def test_retract_public_breezy_receipt_preserves_attempt_and_history(
    db, monkeypatch, public_recheck
):
    from jobsearch.db import js, uid
    from jobsearch.workflow import Workflow

    flow = Workflow(db)
    aid = flow.create("Example", "Engineer", "TEST-RECEIPT", candidate="fixture")["id"]
    approval, attempt, receipt = uid(), uid(), uid()
    db.execute(
        "INSERT INTO approvals(id,application_id,payload_hash,actor) VALUES(%s,%s,'test','test')",
        (approval, aid),
    )
    db.execute(
        "INSERT INTO attempts(id,application_id,payload_hash,approval_id,state) VALUES(%s,%s,'test',%s,'confirmed')",
        (attempt, aid, approval),
    )
    evidence = {
        "adapter": "breezy",
        "read_only_recheck": public_recheck,
        "confirmed": True,
    }
    db.execute(
        "INSERT INTO receipts(id,application_id,attempt_id,kind,evidence) VALUES(%s,%s,%s,'employer_confirmation',%s)",
        (receipt, aid, attempt, js(evidence)),
    )
    db.execute("UPDATE applications SET state='submitted' WHERE id=%s", (aid,))
    monkeypatch.setattr("jobsearch.tracker.Tracker.manual_report", lambda *args: None)
    if not public_recheck:
        with pytest.raises(ValueError, match="Only receipts inferred"):
            flow.unresolved(
                aid, "Insufficient evidence", retract_public_breezy_receipts=True
            )
        assert flow.get(aid)["state"] == "submitted"
        assert db.one("SELECT state FROM attempts")["state"] == "confirmed"
        return
    flow.unresolved(aid, "Insufficient evidence", retract_public_breezy_receipts=True)
    assert flow.get(aid)["state"] == "unresolved"
    assert db.one("SELECT state FROM attempts")["state"] == "unresolved"
    assert db.one("SELECT evidence FROM receipts")["evidence"]["invalidated"] is True
    assert db.one("SELECT evidence FROM receipts")["evidence"]["confirmed"] is False
    assert db.one("SELECT 1 FROM audit WHERE event='receipt.retracted'")
    assert db.one("SELECT resolved FROM followups")["resolved"] is False
    with pytest.raises(ValueError, match="public Breezy"):
        flow.record_receipt(
            aid,
            attempt,
            evidence | {"requisition": "TEST-RECEIPT", "text": "Thank you"},
        )


def test_retracted_receipt_is_history_only_in_application_api(db, monkeypatch):
    from fastapi.testclient import TestClient
    from jobsearch.api import create_app
    from jobsearch.db import js, uid
    from jobsearch.tracker_metrics import dashboard
    from jobsearch.workflow import Workflow

    aid = Workflow(db).create("Example", "Engineer", "TEST-HISTORY")["id"]
    db.execute(
        "INSERT INTO receipts(id,application_id,kind,evidence) VALUES(%s,%s,'employer_confirmation',%s)",
        (uid(), aid, js({"invalidated": True, "confirmed": False})),
    )
    db.execute("UPDATE applications SET state='unresolved' WHERE id=%s", (aid,))
    assert dashboard(db, "fixture")["evidence_counts"]["Workflows with receipts"] == 0
    with TestClient(create_app(db.settings)) as client:
        response = client.get(
            "/api/applications/" + aid, headers={"X-JSA-Token": db.settings.token()}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "unresolved"
        assert data["receipts"] == []
        assert len(data["receipt_history"]) == 1
