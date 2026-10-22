"""Native Breezy review staging and approval-bound execution."""

import asyncio
import json
import re
import secrets
from pathlib import Path
from email import policy
from email.parser import BytesParser
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from .breezy import breezy_form, definition
from .db import js
from .config import digest
from .forms import FormPreparation
from .workflow import Workflow


CONTACT_BINDINGS = {
    "cName": "name",
    "cEmail": "email_address",
    "cPhoneNumber": "phone_number",
    "cAddress": "address",
    "cSummary": "summary",
    "cCoverLetter": "cover_letter",
}

# Locale codes supported by Breezy's portal. This is display metadata, not an
# answer. Keep the finite allowlist separate from candidate-controlled fields.
PORTAL_LANGUAGES = {
    "af",
    "en",
    "en-gb",
    "es",
    "fi",
    "fr",
    "fr-ca",
    "de",
    "ja",
    "ko",
    "nl",
    "pl",
    "pt",
    "pt-br",
    "ru",
    "it",
    "sv",
    "tr",
    "uk",
    "zh",
}

CONFIRMATION = re.compile(
    r"thank you for (?:your application|applying)|(?:we have |we've )received your application|application (?:received|submitted successfully)|your application has been (?:received|submitted successfully)",
    re.I,
)


def accepted(status):
    return isinstance(status, int) and 200 <= status < 300


def portal_asset(request, target):
    url = urlparse(request.url)
    return (
        request.method == "GET"
        and request.resource_type in ("script", "stylesheet", "font")
        and (
            (
                url.scheme == "https"
                and url.hostname == "assets-cdn.breezy.hr"
                and url.path.startswith("/breezy-portal/")
            )
            or (
                target.hostname in ("127.0.0.1", "localhost")
                and (url.scheme, url.netloc) == (target.scheme, target.netloc)
                and url.path.startswith("/receipt-assets/")
            )
        )
    )


def validate_candidate(body, spec, review, upload_url, metadata=None):
    """Check the actual browser JSON, including unchecked and unexpected controls."""
    allowed = set(CONTACT_BINDINGS.values()) | {
        "resume",
        "questionnaire",
        "location",
        "eeoc",
        "salary",
        "sms_consent",
        "_form_token",
        "_form_timing",
        "_has_interaction",
        "_portal_language",
        "source",
        "education",
        "work_history",
    }
    if not isinstance(body, dict):
        raise ValueError("Breezy added an unreviewed application control")
    unknown = set(body) - allowed
    if unknown:
        names = [
            name
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,50}", name)
            else "unknown field"
            for name in sorted(unknown)
        ]
        raise ValueError(
            "Breezy added an unreviewed application control: " + ", ".join(names)[:120]
        )
    if "_portal_language" in body and (
        not isinstance(body["_portal_language"], str)
        or body["_portal_language"] not in PORTAL_LANGUAGES
    ):
        raise ValueError("Breezy supplied an unsupported portal language")
    for key in ("location", "eeoc", "education", "work_history", "source"):
        if body.get(key):
            raise ValueError("Breezy added unreviewed profile information")
    if body.get("salary", {}).get("salary") not in (None, "") or body.get(
        "sms_consent", {}
    ).get("has_consent"):
        raise ValueError("Breezy added unreviewed salary or SMS consent")
    fields = {f.name: f for f in spec.fields}
    answers = review["payload"]["answers"]
    for name, key in CONTACT_BINDINGS.items():
        expected = (
            answers.get(fields[name].key, {}).get("value", "") if name in fields else ""
        )
        if (body.get(key) or "") != str(expected):
            raise ValueError("Breezy contact information differs from review")
    attachment = review["payload"]["attachments"][0]
    resume = body.get("resume", {})
    if (
        set(resume) - {"file_name", "url", "error", "_id"}
        or resume.get("file_name") != attachment["name"]
        or resume.get("url") != upload_url
        or resume.get("error")
    ):
        raise ValueError("Breezy resume differs from the reviewed upload")
    questions = [f for f in spec.fields if f.name.startswith("section_")]
    if len(body.get("questionnaire", [])) != len(questions):
        raise ValueError("Breezy changed its screening questions")
    for index, (field, question) in enumerate(
        zip(questions, body.get("questionnaire", []))
    ):
        if set(question) - {
            "_id",
            "text",
            "body",
            "type",
            "required",
            "options",
            "questionnaire_id",
            "has_actions",
            "response",
            "responses",
        }:
            raise ValueError("Breezy added unreviewed screening data")
        if metadata is not None:
            source = metadata[index]
            if any(
                question.get(key) != source.get(key)
                for key in ("_id", "questionnaire_id", "type", "required")
            ):
                raise ValueError("Breezy changed a screening control")
            for option, original in zip(
                question.get("options", []), source.get("options", [])
            ):
                if (
                    set(option) - {"text", "actions", "enabledActions"}
                    or (option.get("actions") or {}) != (original.get("actions") or {})
                    or (option.get("enabledActions") or [])
                    != (original.get("enabledActions") or [])
                ):
                    raise ValueError("Breezy added unreviewed choice data")
        label = BeautifulSoup(question.get("text", ""), "html.parser").get_text(
            " ", strip=True
        )
        description = BeautifulSoup(question.get("body") or "", "html.parser").get_text(
            " ", strip=True
        )
        if description:
            label += "\n" + description
        if (
            str(question.get("_id")) != spec.question_ids[field.name]
            or label != field.label
            or [o["text"] for o in question.get("options", [])] != field.options
        ):
            raise ValueError("Breezy changed a screening question or its choices")
        expected = answers.get(field.key, {}).get(
            "value", [] if field.type == "checkbox" else ""
        )
        if field.type == "checkbox":
            responses = question.get("responses") or []
            if any(type(v) is not bool for v in responses) or len(responses) > len(
                field.options
            ):
                raise ValueError("Breezy checkbox response changed")
            actual = [
                option
                for i, option in enumerate(field.options)
                if i < len(responses) and responses[i]
            ]
        else:
            actual = question.get("response") or ""
        if (
            set(actual) != set(expected)
            if field.type == "checkbox"
            else actual != expected
        ):
            raise ValueError("Breezy screening answer differs from review")
    return body


def approved_file(db, request, attachment):
    raw = request.post_data_buffer or b""
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: "
        + request.headers.get("content-type", "").encode()
        + b"\r\n\r\n"
        + raw
    )
    parts = list(message.iter_parts()) if message.is_multipart() else []
    if (
        len(parts) != 1
        or parts[0].get_param("name", header="content-disposition") != "file"
        or parts[0].get_filename() != attachment["name"]
        or (
            parts[0].get_payload(decode=True)
            and digest(parts[0].get_payload(decode=True)) != attachment["hash"]
        )
    ):
        raise ValueError("Breezy upload differs from the approved resume")
    # Chromium omits file bodies from intercepted multipart data. Rebuild the
    # upload using the hash-verified artifact rather than forwarding that body.
    row = db.one("SELECT path FROM artifacts WHERE id=%s", (attachment["id"],))
    data = Path(row["path"]).read_bytes() if row else b""
    if digest(data) != attachment["hash"]:
        raise ValueError("The approved resume changed before upload")
    name = attachment["name"]
    if any(c in name for c in ("\r", "\n", '"', "\\")):
        raise ValueError("The resume filename cannot be encoded safely")
    boundary = "jsa-" + secrets.token_hex(16)
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: application/pdf\r\n\r\n'.encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, "multipart/form-data; boundary=" + boundary


class BreezyBrowser:
    def __init__(self, db):
        self.db = db
        self.flow = Workflow(db)
        self.playwright = None
        self.sessions = {}

    async def close(self):
        for session in self.sessions.values():
            await session["browser"].close()
        self.sessions.clear()
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    def active(self, aid, expected_hash=None, attempt=None):
        app = self.flow.get(aid)
        self.flow.editable(app)
        if app["paused"] or self.db.one(
            "SELECT 1 FROM jobs WHERE input->>'application_id'=%s AND status='running' AND cancel_requested",
            (aid,),
        ):
            raise ValueError("Breezy work is paused or cancelled")
        existing = self.db.one(
            "SELECT id FROM attempts WHERE application_id=%s", (aid,)
        )
        if existing and existing["id"] != attempt:
            raise ValueError(
                "Reconcile the existing submission attempt before retrying"
            )
        review = self.flow.review(aid)
        if review["questions"] or (expected_hash and review["hash"] != expected_hash):
            raise ValueError("Breezy answers or selected files changed")
        return app, review

    async def session(self, app, spec, review):
        from .browser import GUARD

        if not self.playwright:
            self.playwright = await async_playwright().start()
        if app["id"] in self.sessions:
            await self.sessions.pop(app["id"])["browser"].close()
        browser = await self.playwright.chromium.launch(
            executable_path="/usr/bin/chromium",
            headless=self.db.settings.headless,
            args=["--disable-background-networking"],
        )
        context = await browser.new_context(
            service_workers="block",
            accept_downloads=False,
            viewport={"width": 1100, "height": 900},
        )
        await context.add_init_script(GUARD)
        await context.route_web_socket("**/*", lambda socket: socket.close())
        target = urlparse(spec.url)
        root = f"{target.scheme}://{target.netloc}"
        local = target.hostname in ("127.0.0.1", "localhost")
        api_root = root if local else "https://app.breezy.hr"
        company = target.hostname.split(".")[0] if not local else "example"
        upload = api_root + f"/api/portal/{company}/upload"
        apply = api_root + f"/api/apply/{spec.requisition}"
        gate = {
            "phase": "inspect",
            "uploaded": False,
            "posted": False,
            "blocked": 0,
            "attempt": None,
            "upload_url": "https://local-review.invalid/resume.pdf",
            "captured": None,
            "post_status": None,
        }
        attachment = review["payload"]["attachments"][0]
        cors = {
            "Access-Control-Allow-Origin": root,
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Allow-Methods": "POST,GET,OPTIONS",
        }
        session = {"browser": browser, "context": context, "gate": gate, "spec": spec}
        self.sessions[app["id"]] = session

        async def route(route):
            request = route.request
            u = urlparse(request.url)
            headers = {
                k: v
                for k, v in request.headers.items()
                if k.lower()
                not in ("x-jsa-token", "x-jsa-profile", "authorization", "cookie")
            }
            try:
                if request.method == "OPTIONS" and request.url in (upload, apply):
                    await route.fulfill(status=204, headers=cors)
                    return
                if (
                    request.method == "GET"
                    and (u.scheme, u.netloc) == (target.scheme, target.netloc)
                    and u.path == "/resume/parse"
                ):
                    # Never let an employer parser populate unreviewed career claims.
                    await route.fulfill(
                        status=200,
                        body="null",
                        content_type="application/json",
                        headers=cors,
                    )
                    return
                if (
                    request.method == "POST"
                    and request.url == upload
                    and gate["phase"] == "fill"
                ):
                    approved_body, content_type = approved_file(
                        self.db, request, attachment
                    )
                    if gate["attempt"]:
                        self.active(app["id"], review["hash"], gate["attempt"])
                        if gate["uploaded"]:
                            raise ValueError("Breezy attempted a duplicate upload")
                        gate["uploaded"] = True
                        headers["content-type"] = content_type
                        headers.pop("content-length", None)
                        response = await route.fetch(
                            headers=headers, post_data=approved_body, max_redirects=0
                        )
                        if response.status != 200:
                            raise ValueError(
                                "Breezy did not accept the approved resume"
                            )
                        data = await response.json()
                        uploaded = urlparse(data.get("url", ""))
                        if uploaded.scheme != "https" and app["candidate"] != "fixture":
                            raise ValueError(
                                "Breezy returned an unexpected upload address"
                            )
                        gate["upload_url"] = data["url"]
                        self.db.execute(
                            "UPDATE artifacts SET attached=true WHERE id=%s",
                            (attachment["id"],),
                        )
                        await route.fulfill(
                            response=response, headers=dict(response.headers) | cors
                        )
                    else:
                        await route.fulfill(
                            json={"url": gate["upload_url"]}, headers=cors
                        )
                    return
                if request.method == "POST" and request.url == apply:
                    if gate["phase"] not in ("capture", "send"):
                        raise ValueError("Breezy sending is not armed for this request")
                    self.active(app["id"], review["hash"], gate["attempt"])
                    body = validate_candidate(
                        request.post_data_json,
                        spec,
                        review,
                        gate["upload_url"],
                        session["question_metadata"],
                    )
                    if gate["phase"] == "capture":
                        gate["captured"] = body
                        await route.fulfill(
                            status=422,
                            json={
                                "type": "localReview",
                                "message": "Local review only. No application was sent.",
                            },
                            headers=cors,
                        )
                        return
                    if not gate["attempt"] or gate["posted"] or not gate["uploaded"]:
                        raise ValueError("Breezy submission is not authorized")
                    gate["posted"] = True
                    response = await route.fetch(headers=headers, max_redirects=0)
                    gate["post_status"] = response.status
                    await route.fulfill(
                        response=response, headers=dict(response.headers) | cors
                    )
                    return
                same = (u.scheme, u.netloc) == (target.scheme, target.netloc)
                read = (
                    gate["phase"] == "inspect"
                    and request.method == "GET"
                    and (
                        (
                            same
                            and u.path in (target.path, "/data/currencies.json")
                            and not u.query
                        )
                        or portal_asset(request, target)
                    )
                )
                receipt = (
                    gate["posted"]
                    and accepted(gate["post_status"])
                    and request.method == "GET"
                    and same
                    and u.path == target.path + "/submitted"
                    and not u.query
                )
                receipt_asset = (
                    gate["posted"]
                    and accepted(gate["post_status"])
                    and portal_asset(request, target)
                )
                if read or receipt or receipt_asset:
                    await route.continue_(headers=headers)
                    return
            except Exception as exc:
                gate["error"] = (
                    str(exc)
                    if isinstance(exc, ValueError)
                    else "Breezy request validation failed"
                )
            gate["blocked"] += 1
            if request.method == "POST" and request.url == apply:
                # Breezy's native XHR error callback passes undefined for network
                # errors, which its submit handler mistakes for success. Return
                # a truthy error body so a local rejection cannot redirect to
                # /submitted. The employer receives no application request.
                await route.fulfill(
                    status=422,
                    json={
                        "type": "localReviewBlocked",
                        "message": gate.get("error")
                        or "Local review blocked this request",
                    },
                    headers=cors,
                )
                return
            await route.abort()

        await context.route("**/*", route)
        page = await context.new_page()
        session["page"] = page
        await page.goto(spec.url, wait_until="domcontentloaded", timeout=30000)
        await page.locator('[name="cEmail"]').wait_for(timeout=15000)
        page_html = await page.content()
        session["question_metadata"] = [
            question
            for section in definition(page_html).get("questions", [])
            for question in section.get("questions", [])
        ]
        current = breezy_form(page_html, spec.url, spec.requisition, app["employer"])
        if current.fingerprint != spec.fingerprint:
            raise ValueError("Breezy changed the application form; inspect it again")
        await page.evaluate("""() => {
          const banner=document.createElement('div');banner.id='jsa-browser-status';banner.setAttribute('role','status');
          banner.style='position:sticky;top:0;z-index:2147483647;background:#17332f;color:white;padding:14px';
          banner.textContent='Local review. Files are staged locally. Return to Job Search to review before approving submission.';
          document.body.prepend(banner);
        }""")
        return session

    async def fill_values(self, session, review):
        page, spec, gate = session["page"], session["spec"], session["gate"]
        gate["phase"] = "fill"
        for field in spec.fields:
            self.active(session["aid"], review["hash"], gate["attempt"])
            control = page.locator("[name=" + json.dumps(field.name) + "]")
            if field.type == "file":
                path = self.db.one(
                    "SELECT path FROM artifacts WHERE id=%s",
                    (review["payload"]["attachments"][0]["id"],),
                )["path"]
                await control.set_input_files(path)
                try:
                    await page.get_by_text(
                        review["payload"]["attachments"][0]["name"], exact=True
                    ).first.wait_for(timeout=15000)
                except Exception:
                    raise ValueError(
                        gate.get("error")
                        or "Breezy did not finish staging the selected resume"
                    ) from None
                continue
            value = (
                review["payload"]["answers"]
                .get(field.key, {})
                .get("value", [] if field.type == "checkbox" else "")
            )
            if field.type in ("checkbox", "radio"):
                if await control.count() != len(field.options):
                    raise ValueError("Breezy changed its choice controls")
                for index, option in enumerate(field.options):
                    await control.nth(index).set_checked(
                        option in value if field.type == "checkbox" else option == value
                    )
            elif field.type == "select":
                if value:
                    await control.select_option(str(value))
            else:
                await control.fill(str(value))
        if await page.locator('[name="smsConsent"]').count():
            await page.locator('[name="smsConsent"]').set_checked(False)
        # Capture the native payload locally. This validates React's actual values,
        # including checkbox groups, without forwarding an application request.
        gate["phase"] = "capture"
        await page.evaluate("window.__jsaArm(true)")
        try:
            await page.get_by_role(
                "button", name=re.compile(r"^Submit Application$", re.I)
            ).click()
            for _ in range(60):
                if gate["captured"] or gate.get("error"):
                    break
                await asyncio.sleep(0.1)
            if not gate["captured"]:
                raise ValueError(
                    gate.get("error")
                    or "Breezy has an additional required control; review the employer form"
                )
        finally:
            gate["phase"] = "hold"
            await page.evaluate("window.__jsaArm(false)")
        return gate["captured"]

    async def stage(self, aid):
        app, review = self.active(aid)
        spec = FormPreparation(self.db).validate_ready(app)
        if spec.adapter != "breezy" or app["state"] != "review":
            raise ValueError("Prepare Breezy answers before filling")
        self.flow.update_payload(aid, {"browser_hash": None})
        self.flow.transition(aid, "filling")
        try:
            session = await self.session(app, spec, review)
            session["aid"] = aid
            await self.fill_values(session, review)
            self.active(aid, review["hash"])
            path = (
                self.db.settings.state
                / "browser-evidence"
                / (aid + "-breezy-review.png")
            )
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            await session["page"].screenshot(path=str(path), full_page=True)
            self.flow.update_payload(aid, {"browser_hash": review["hash"]})
            self.flow.transition(aid, "review")
            return {
                "state": "review",
                "adapter": "breezy",
                "filled": True,
                "mode": "local_browser_review",
                "screenshot": str(path),
                "message": "The Breezy form is filled for review. No files or application data were sent.",
            }
        except Exception as exc:
            self.flow.update_payload(aid, {"browser_hash": None})
            if self.flow.get(aid)["state"] == "filling":
                self.flow.transition(aid, "manual_handoff")
            if aid in self.sessions:
                try:
                    message = (
                        str(exc)
                        if isinstance(exc, ValueError)
                        else "The form could not be verified"
                    )
                    await (
                        self.sessions[aid]["page"]
                        .locator("#jsa-browser-status")
                        .evaluate(
                            "(element, message) => { element.textContent = 'Local review stopped: ' + message + '. No application was sent. Return to Job Search to review the error.'; }",
                            message,
                        )
                    )
                except Exception:
                    pass
            raise

    async def submit(self, aid):
        app, review = self.active(aid)
        if app["candidate"] != "fixture" and not self.db.settings.live_applications:
            raise ValueError("Real application sending is disabled")
        if app["state"] != "authorized" or not self.db.one(
            "SELECT 1 FROM approvals WHERE application_id=%s AND payload_hash=%s AND NOT invalidated",
            (aid, review["hash"]),
        ):
            raise ValueError("Matching approval is required for Breezy submission")
        spec = FormPreparation(self.db).validate_ready(app)
        if app["candidate"] == "fixture" and urlparse(spec.url).hostname not in (
            "127.0.0.1",
            "localhost",
        ):
            raise ValueError(
                "Fixture submission is restricted to local synthetic employers"
            )
        session = await self.session(app, spec, review)
        session["aid"] = aid
        gate = session["gate"]
        self.active(aid, review["hash"])
        await (
            session["page"]
            .locator("#jsa-browser-status")
            .evaluate(
                "e => e.textContent = 'Approved sending: the agent is uploading the reviewed resume and sending the confirmed answers.'"
            )
        )
        attempt = self.flow.begin_attempt(aid, mark_attached=False)
        gate["attempt"] = attempt
        try:
            await self.fill_values(session, review)
            self.active(aid, review["hash"], attempt)
            gate["phase"] = "send"
            await session["page"].evaluate("window.__jsaArm(true)")
            await (
                session["page"]
                .get_by_role("button", name=re.compile(r"^Submit Application$", re.I))
                .click()
            )
            for _ in range(200):
                if gate.get("error"):
                    raise ValueError(gate["error"])
                if (
                    gate["posted"]
                    and accepted(gate["post_status"])
                    and session["page"].url == spec.url + "/submitted"
                ):
                    text = await session["page"].locator("body").inner_text()
                    if CONFIRMATION.search(text):
                        path = (
                            self.db.settings.state
                            / "browser-evidence"
                            / (aid + "-breezy-receipt.png")
                        )
                        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        await session["page"].screenshot(path=str(path), full_page=True)
                        # This public page also loads without a submission.
                        # Preserve it as a diagnostic, not an employer receipt.
                        self.db.execute(
                            "UPDATE attempts SET result=result || %s WHERE id=%s",
                            (
                                js(
                                    {
                                        "confirmation_page": {
                                            "url": session["page"].url,
                                            "screenshot": str(path),
                                            "screenshot_hash": digest(
                                                path.read_bytes()
                                            ),
                                            "post_status": gate["post_status"],
                                            "confirmed": False,
                                        }
                                    }
                                ),
                                attempt,
                            ),
                        )
                        raise ValueError(
                            "Breezy returned a successful HTTP response, but its public "
                            "thank-you page does not verify employer receipt. "
                            "Confirm with the employer before retrying."
                        )
                await asyncio.sleep(0.1)
            raise ValueError(
                "No Breezy receipt was confirmed; reconcile before retrying"
            )
        except Exception:
            self.flow.unresolved(
                aid,
                "Breezy stopped after the durable attempt checkpoint. Check the employer outcome before retrying.",
            )
            raise
        finally:
            gate["phase"] = "hold"
            try:
                await session["page"].evaluate("window.__jsaArm(false)")
            except Exception:
                pass
            self.db.audit(
                "breezy.attempt_finished",
                aid,
                {
                    "attempt_id": attempt,
                    "post_observed": gate["posted"],
                    "post_status": gate["post_status"],
                    "blocked_requests": gate["blocked"],
                },
            )

    async def reconcile(self, aid):
        """Do not promote a public thank-you page to an employer receipt."""
        app = self.flow.get(aid)
        self.flow.editable(app)
        if app["state"] == "submitted":
            return {"state": "submitted", "already_recorded": True}
        attempt = self.db.one("SELECT * FROM attempts WHERE application_id=%s", (aid,))
        if not attempt or app["state"] not in (
            "unresolved",
            "receipt_verification",
            "manual_handoff",
        ):
            raise ValueError(
                "A recorded Breezy submission attempt is required to check its receipt"
            )
        event = self.db.one(
            "SELECT detail FROM audit WHERE subject=%s AND event='breezy.attempt_finished' AND detail->>'attempt_id'=%s ORDER BY seq DESC LIMIT 1",
            (aid, attempt["id"]),
        )
        if (
            not event
            or event["detail"].get("post_observed") is not True
            or not accepted(event["detail"].get("post_status"))
        ):
            raise ValueError(
                "No successful Breezy application response was recorded; confirm the outcome manually"
            )
        return {
            "state": app["state"],
            "receipt": False,
            "message": (
                "Breezy's public thank-you page cannot verify this application. "
                "Confirm receipt through an employer email or directly with the employer "
                "before recording it as submitted. No application was resent."
            ),
        }
