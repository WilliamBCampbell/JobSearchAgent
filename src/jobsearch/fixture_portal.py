"""Sanitized hosted-ATS fixtures. Never route a real candidate through these endpoints."""

import html
import json

from fastapi import Request
from fastapi.responses import HTMLResponse, Response, RedirectResponse

from .forms import FormSpec
from .portal_browser import matches_multipart
from .workflow import Workflow


def register_portal_fixtures(api, db):
    flow = Workflow(db)

    def candidate(aid):
        app = flow.get(aid)
        if app["candidate"] != "fixture":
            raise ValueError("Synthetic employer fixtures only")
        return app

    @api.get("/fixture/lever/{aid}/apply", response_class=HTMLResponse)
    def form(aid: str):
        app = candidate(aid)
        challenge = (
            "<p data-challenge>Verification needed. Complete this in your normal browser.</p>"
            if app["payload"].get("scenario") == "lever_challenge"
            else ""
        )
        changed = (
            '<label>Required certification<input name="certification" required></label>'
            if app["payload"].get("scenario") == "lever_changed"
            else ""
        )
        card = html.escape(
            json.dumps(
                {"fields": [{"text": "Annual base salary target", "required": True}]}
            ),
            quote=True,
        )
        return HTMLResponse(
            f'''<!doctype html><meta charset="utf-8"><title>Example employer application</title>
        <h1>{html.escape(app["employer"])}</h1><p>Synthetic employer. No real application is sent.</p>{challenge}
        <form id="application-form" enctype="multipart/form-data" method="POST">
        <label for="resume-upload-input">Resume/CV</label><input name="resume" id="resume-upload-input" type="file">
        <label for="full-name">Full name</label><input id="full-name" name="name" required>
        <label for="email">Email</label><input id="email" name="email" type="email" required>
        <input name="cards[salary][baseTemplate]" type="hidden" value="{card}">
        <textarea name="cards[salary][field0]" required></textarea>{changed}
        <label for="consent">Optional SMS consent</label><input id="consent" type="checkbox" name="smsConsent" value="Yes">
        <p class="application-privacy-policy">By applying, you agree to this employer processing this application.</p>
        <button id="btn-submit" type="button">Submit application</button></form>
        <script src="/fixture/lever.js"></script>''',
            headers={"Referrer-Policy": "same-origin"},
        )

    @api.get("/fixture/lever.js")
    def script():
        return Response(
            """const f=document.querySelector('#application-form');
        document.querySelector('[name=name]').addEventListener('input',()=>fetch('/fixture/should-never-receive-personal-data?name='+encodeURIComponent(f.elements.name.value)).catch(()=>{}));
        document.querySelector('[name=resume]').addEventListener('change',()=>fetch('/parseResume',{method:'POST',body:new FormData(f)}).catch(()=>{}));
        document.querySelector('#btn-submit').addEventListener('click',()=>f.requestSubmit());""",
            media_type="application/javascript",
        )

    @api.post("/fixture/lever/{aid}/apply")
    async def receive(aid: str, request: Request):
        app = candidate(aid)
        attempt = db.one("SELECT * FROM attempts WHERE application_id=%s", (aid,))
        review = flow.review(aid)
        if (
            not attempt
            or attempt["payload_hash"] != review["hash"]
            or app["state"] not in ("submission_attempt", "receipt_verification")
        ):
            raise ValueError("A matching durable attempt is required")
        spec = FormSpec.model_validate(app["payload"]["form_plan"]["spec"])
        if not matches_multipart(
            await request.body(),
            request.headers.get("content-type", ""),
            spec,
            review["payload"],
        ):
            raise ValueError("Employer payload differs from approval")
        db.audit("fixture.portal_received", aid)
        if app["payload"].get("scenario") == "lever_no_receipt":
            return HTMLResponse(
                "<h1>Processing</h1><p>No confirmation is available.</p>"
            )
        return RedirectResponse(f"/fixture/lever/{aid}/thanks", status_code=303)

    @api.get("/fixture/lever/{aid}/thanks", response_class=HTMLResponse)
    def receipt(aid: str):
        candidate(aid)
        if not db.one(
            "SELECT 1 FROM audit WHERE event='fixture.portal_received' AND subject=%s",
            (aid,),
        ):
            raise ValueError("No application was received")
        return '<!doctype html><meta charset="utf-8"><h1>Application submitted!</h1><p>The synthetic employer received this application.</p>'
