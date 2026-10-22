"""Approval-bound Greenhouse upload and application request contracts."""

from __future__ import annotations

import json
import asyncio
import re
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import available_timezones

import httpx

from .config import digest
from .db import js
from .workflow import Workflow


def empty(value):
    if isinstance(value, dict):
        return all(empty(v) for v in value.values())
    if isinstance(value, list):
        return all(empty(v) for v in value)
    return value is None or value == ""


def blank_employments(value):
    """Accept the native form's unfilled row, never unreviewed work history."""
    if value is None:
        return True
    if not isinstance(value, list):
        return False
    for row in value:
        if not isinstance(row, dict) or set(row) - {
            "company_name",
            "title",
            "start_date",
            "end_date",
            "current",
        }:
            return False
        if row.get("current", False) is not False:
            return False
        if any(row.get(key) not in (None, "") for key in ("company_name", "title")):
            return False
        for key in ("start_date", "end_date"):
            date = row.get(key)
            if date is not None and (
                not isinstance(date, dict)
                or set(date) - {"month", "year"}
                or any(v not in (None, "") for v in date.values())
            ):
                return False
    return True


def validate_application(body, spec, payload, options, uploads, errors=None):
    """Reject extra answers, consents, references, files, and changed option IDs."""

    def reject(code="review_mismatch"):
        if errors is not None:
            errors.append(code)
        return False

    if len(body) > 1_000_000:
        return reject()
    try:
        outer = json.loads(body)
        if not isinstance(outer, dict) or set(outer) - {
            "job_application",
            "authenticity_token",
            "fingerprint",
            "g-recaptcha-enterprise-token",
            "security_code",
            "captcha_retried",
            "request_token",
        }:
            return reject("unrecognized_request_fields")
        application = dict(outer["job_application"])
        demographics = application.pop("demographic_answers", [])
        consent = application.pop("data_compliance", {})
        if not isinstance(demographics, list) or not isinstance(consent, dict):
            return reject()
        expected_demographics = {}
        custom = application.pop("answers_attributes", {})
        if not isinstance(custom, dict):
            return reject()
        for f in spec.fields:
            if f.type == "file":
                a = next(
                    (a for a in payload["attachments"] if a["kind"] == f.name + ".pdf"),
                    None,
                )
                if a:
                    uploaded = uploads.get(f.name)
                    if not uploaded or not uploaded.get("confirmed"):
                        return reject()
                    if application.pop(f.name + "_url", None) != uploaded["file_url"]:
                        return reject()
                    if application.pop(f.name + "_url_filename", None) != a["name"]:
                        return reject()
                continue
            value = payload["answers"].get(f.key, {}).get("value", "")
            if f.binding != "standard":
                from .self_identification import choices

                selected = choices(f, value)
                if f.binding == "data_compliance":
                    if consent.pop(f.name, False) is not bool(selected):
                        return reject("unreviewed_consent")
                elif f.binding == "eeoc":
                    expected = str(f.option_ids[selected[0]]) if selected else ""
                    if application.pop(f.name, "") != expected:
                        return reject("self_identification_mismatch")
                elif f.binding == "demographic" and selected:
                    expected_demographics[str(f.name)] = {
                        str(f.option_ids[label]): value["text"]
                        if isinstance(value, dict) and label in f.free_form_options
                        else None
                        for label in selected
                    }
                continue
            if f.name.startswith("question_"):
                qid = f.name.removeprefix("question_").removesuffix("[]")
                answer = dict(custom.pop(qid, {}))
                if str(answer.pop("question_id", qid)) != qid:
                    return reject()
                priority = answer.pop("priority", 0)
                if not isinstance(priority, int):
                    return reject()
                if f.type in ("text", "textarea", "email", "url", "tel"):
                    if answer.pop("text_value", "") != value or answer:
                        return reject()
                elif f.type in ("select", "multiselect", "checkbox"):
                    from .self_identification import choices

                    if f.type == "checkbox":
                        from .forms import valid_value

                        if value == "" and not f.required:
                            value = []
                        if not valid_value(f, value):
                            return reject()
                        chosen = value
                    else:
                        chosen = choices(f, value)
                    if not chosen:
                        if not empty(answer):
                            return reject()
                    else:
                        if f.type in ("multiselect", "checkbox"):
                            selected = answer.pop(
                                "answer_selected_options_attributes", {}
                            )
                            expected = [options.get(f.name, {}).get(v) for v in value]
                            if (
                                answer
                                or None in expected
                                or not isinstance(selected, dict)
                            ):
                                return reject()
                            actual = []
                            for row in selected.values():
                                if not isinstance(row, dict) or set(row) != {
                                    "question_option_id"
                                }:
                                    return reject()
                                actual.append(str(row["question_option_id"]))
                            if sorted(actual) != sorted(str(v) for v in expected):
                                return reject()
                            continue
                        option = options.get(f.name, {}).get(value)
                        if option is None:
                            return reject()
                        if str(option) in ("0", "1"):
                            if (
                                str(answer.pop("boolean_value", None)) != str(option)
                                or answer
                            ):
                                return reject()
                        else:
                            selected = answer.pop(
                                "answer_selected_options_attributes", {}
                            )
                            if (
                                answer
                                or not isinstance(selected, dict)
                                or len(selected) != 1
                            ):
                                return reject()
                            row = next(iter(selected.values()))
                            if (
                                not isinstance(row, dict)
                                or set(row) != {"question_option_id"}
                                or str(row["question_option_id"]) != str(option)
                            ):
                                return reject()
                else:
                    return reject()
            else:
                actual = application.pop(f.name, "")
                if f.name == "phone":
                    actual = re.sub(r"\D", "", str(actual))
                    value = re.sub(r"\D", "", str(value))
                if actual != value:
                    return reject()
        if custom or consent:
            return reject()
        actual_demographics = {}
        for row in demographics:
            if not isinstance(row, dict) or set(row) != {
                "question_id",
                "answer_options",
            }:
                return reject()
            qid = str(row["question_id"])
            if qid in actual_demographics or not isinstance(
                row["answer_options"], list
            ):
                return reject()
            selected = {}
            for option in row["answer_options"]:
                if (
                    not isinstance(option, dict)
                    or set(option) - {"answer_option_id", "text"}
                    or "answer_option_id" not in option
                ):
                    return reject()
                oid = str(option["answer_option_id"])
                if oid in selected:
                    return reject()
                selected[oid] = option.get("text")
            actual_demographics[qid] = selected
        if actual_demographics != expected_demographics:
            return reject("self_identification_mismatch")
        # The native form emits a blank work row with current=false even when
        # employment controls are hidden. False is not globally an empty value:
        # extra consent fields and populated history must still fail validation.
        if not blank_employments(application.pop("employments", None)):
            return reject("unreviewed_employment")
        # Optional education rows are emitted empty by the native form.
        for name in (
            "demographic_answers",
            "data_compliance",
            "attachments",
            "educations",
            "mapped_url_token",
            "appcast_click_id",
        ):
            if not empty(application.pop(name, None)):
                return reject()
        if application.pop("from_job_board_renderer", True) is not True:
            return reject()
        timezone = application.pop("time_zone", None)
        if timezone is not None and timezone not in available_timezones():
            return reject()
        return not application or reject("unrecognized_application_fields")
    except (ValueError, TypeError, KeyError, AttributeError):
        return reject()


def approved_upload(db, body, content_type, payload, metadata):
    """Restore omitted CDP file bytes and validate all storage fields."""
    if len(body) > 25_000_000 or not content_type.startswith("multipart/form-data;"):
        raise ValueError("Unexpected Greenhouse upload encoding")
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: "
        + content_type.encode()
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + body
    )
    values, files = {}, []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        data = part.get_payload(decode=True) or b""
        if part.get_filename():
            files.append((name, part.get_filename(), data))
        elif name in values:
            raise ValueError("Repeated upload field")
        else:
            values[name] = data.decode("utf-8")
    if len(files) != 1 or files[0][0] != "file":
        raise ValueError("Unexpected upload file controls")
    key = values.get("key", "")
    matches = []
    for f, record in metadata.items():
        if f == "url" or not isinstance(record, dict):
            continue
        pattern = (
            re.escape(record["key"])
            .replace(r"\{timestamp\}", r"\d{10,16}")
            .replace(r"\{unique_id\}", r"[a-z0-9]{1,20}")
        )
        if re.fullmatch(pattern, key):
            matches.append((f, record))
    if len(matches) != 1:
        raise ValueError("The upload destination does not match a reviewed file")
    field, record = matches[0]
    artifacts = [a for a in payload["attachments"] if a["kind"] == field + ".pdf"]
    if len(artifacts) != 1 or artifacts[0]["name"] != files[0][1]:
        raise ValueError("An unapproved document was attached")
    expected = {
        **record["fields"],
        "key": key,
        "utf8": "✓",
        "authenticity_token": "1234",
        "Content-Type": "application/octet-stream",
    }
    if values != expected:
        raise ValueError("The upload policy changed")
    a = artifacts[0]
    row = db.one(
        "SELECT path FROM artifacts WHERE id=%s AND application_id=%s",
        (a["id"], payload["application_id"]),
    )
    if not row:
        raise ValueError("The document does not belong to this application")
    content = Path(row["path"]).read_bytes()
    if digest(content) != a["hash"] or (
        files[0][2] and digest(files[0][2]) != a["hash"]
    ):
        raise ValueError("Attachment bytes differ from the approved version")
    request = httpx.Request(
        "POST",
        metadata["url"],
        files=[
            *((k, (None, v)) for k, v in expected.items()),
            ("file", (a["name"], content, "application/pdf")),
        ],
    )
    return (
        field,
        metadata["url"].rstrip("/") + "/" + key,
        request.read(),
        request.headers["content-type"],
    )


class GreenhouseTransport:
    def __init__(self, db, app, spec, review, options):
        self.db, self.app, self.spec, self.review, self.options = (
            db,
            app,
            spec,
            review,
            options,
        )
        self.flow = Workflow(db)
        self.fixture = app["candidate"] == "fixture"
        target = urlparse(spec.url)
        self.submit_url = (
            spec.url if self.fixture else "https://boards.greenhouse.io" + target.path
        )
        self.receipt_url = spec.url + "/confirmation"
        self.metadata = {}
        self.uploads = {}
        self.posted = False
        self.post_status = None
        self.receipt_status = None
        self.attempt = None
        self.stage = "opening_form"
        self.error = None
        self.verification_pending = False
        self.verification_allowed = False
        self.verification_count = 0
        self.browser_diagnostics = {}
        self.request_token = None  # Native token stays in this browser process only.
        self.response_ready = asyncio.Event()
        self.response_ready.set()

    @property
    def upload_url(self):
        if not self.metadata.get("url"):
            return None
        storage = urlparse(self.metadata["url"])
        return storage._replace(path=storage.path or "/").geturl()

    def progress(self, stage, **detail):
        self.stage = stage
        if self.browser_diagnostics:
            detail["browser_diagnostics"] = dict(self.browser_diagnostics)
        if self.attempt:
            self.db.execute(
                "UPDATE attempts SET result=result || %s WHERE id=%s",
                (js({"stage": stage, **detail}), self.attempt),
            )
        self.db.execute(
            """UPDATE jobs SET checkpoint=checkpoint || %s
            WHERE kind IN ('submit','continue_verification') AND input->>'application_id'=%s AND status='running'""",
            (js({"stage": stage, **detail}), self.app["id"]),
        )

    def browser_console(self, message):
        # Retain a category only. Console text can contain answers or tokens.
        text = message.text.lower()
        if "recaptcha" in text and "content security policy" in text:
            if not self.browser_diagnostics.get("recaptcha_csp_blocked"):
                self.browser_diagnostics["recaptcha_csp_blocked"] = True
                self.progress(self.stage)

    def check(self):
        app = self.flow.get(self.app["id"])
        if app["paused"] or self.db.one(
            "SELECT 1 FROM jobs WHERE input->>'application_id'=%s AND status='running' AND cancel_requested",
            (app["id"],),
        ):
            raise ValueError("Application sending is paused or cancelled")
        current = self.flow.review(app["id"])
        if current["questions"] or current["hash"] != self.review["hash"]:
            raise ValueError("The application changed after approval")
        if not self.db.one(
            "SELECT 1 FROM approvals WHERE application_id=%s AND payload_hash=%s AND NOT invalidated",
            (app["id"], current["hash"]),
        ):
            raise ValueError("Matching approval is missing")
        if self.attempt:
            row = self.db.one(
                "SELECT * FROM attempts WHERE id=%s AND application_id=%s",
                (self.attempt, app["id"]),
            )
            if (
                not row
                or row["payload_hash"] != current["hash"]
                or app["state"]
                not in (
                    "submission_attempt",
                    "receipt_verification",
                    "awaiting_challenge",
                )
            ):
                raise ValueError("The submission checkpoint is no longer active")

    async def handle(self, route, gate):
        """Return True for handled requests. Unknown traffic stays blocked."""
        request = route.request
        u = urlparse(request.url)
        presign = (
            not self.fixture
            and u.hostname == "boards.greenhouse.io"
            and u.path == "/uncacheable_attributes/presigned_fields"
        ) or (
            self.fixture
            and request.url
            == self.db.settings.origin
            + "/fixture/greenhouse-presign?application_id="
            + self.app["id"]
        )
        if presign and request.method == "GET" and gate["phase"] == "inspect":
            self.check()
            headers = self.db.settings.local_headers() if self.fixture else {}
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(request.url, headers=headers)
                response.raise_for_status()
            metadata = response.json()
            storage = urlparse(metadata["url"])
            if not self.fixture and (
                storage.scheme != "https"
                or storage.hostname != "grnhse-prod-jben-us-east-1.s3.amazonaws.com"
                or storage.path not in ("", "/")
                or storage.query
                or storage.username
                or storage.port not in (None, 443)
            ):
                raise ValueError("The Greenhouse storage destination needs review")
            if (
                self.fixture
                and metadata["url"]
                != self.db.settings.origin + "/fixture/greenhouse-upload"
            ):
                raise ValueError("Unexpected fixture upload destination")
            # Keep metadata unchanged: the native widget appends "/<key>" to
            # construct the file URL. Canonicalize only transport comparisons.
            self.metadata = metadata
            await route.fulfill(json=metadata)
            return True
        if request.url == self.upload_url and request.method in (
            "POST",
            "OPTIONS",
        ):
            self.check()
            if not self.attempt or gate["phase"] != "fill":
                await route.abort("blockedbyclient")
                return True
            headers = dict(request.headers)
            headers.pop("x-jsa-token", None)
            headers.pop("x-jsa-profile", None)
            if self.fixture:
                headers.update(self.db.settings.local_headers())
            if request.method == "POST":
                field, url, body, kind = approved_upload(
                    self.db,
                    request.post_data_buffer or b"",
                    request.headers.get("content-type", ""),
                    self.review["payload"],
                    self.metadata,
                )
                if field in self.uploads or any(
                    not record["confirmed"] for record in self.uploads.values()
                ):
                    await route.abort("blockedbyclient")
                    return True
                self.uploads[field] = {"file_url": url, "confirmed": False}
                self.progress("uploading_file")
                headers["content-type"] = kind
                headers.pop("content-length", None)
                await route.continue_(headers=headers, post_data=body)
            else:
                await route.continue_(headers=headers)
            return True
        if request.url == self.submit_url and request.method in ("POST", "OPTIONS"):
            self.check()
            continuation = (
                self.posted
                and self.verification_pending
                and self.verification_allowed
                and self.verification_count < 3
            )
            if (
                not self.attempt
                or gate["phase"] != "send"
                or (self.posted and not continuation)
            ):
                await route.abort("blockedbyclient")
                return True
            headers = dict(request.headers)
            headers.pop("x-jsa-token", None)
            headers.pop("x-jsa-profile", None)
            if self.fixture:
                headers.update(self.db.settings.local_headers())
            if request.method == "POST":
                # A verification POST continues the same native application.
                # Never persist its code or token, or permit a general resend.
                try:
                    envelope = json.loads(request.post_data_buffer or b"")
                    token = envelope.get("request_token")
                    code = envelope.get("security_code")
                    metadata_ok = (
                        token is None or isinstance(token, str) and len(token) <= 8192
                    ) and (
                        isinstance(code, str)
                        and bool(re.fullmatch(r"[A-Za-z0-9]{8}", code))
                        and token == self.request_token
                        if continuation
                        else not code
                    )
                except (ValueError, AttributeError):
                    metadata_ok = False
                if not metadata_ok:
                    self.error = (
                        "The verification request did not match this browser session."
                    )
                    await route.abort("blockedbyclient")
                    return True
                errors = []
                if not validate_application(
                    request.post_data_buffer or b"",
                    self.spec,
                    self.review["payload"],
                    self.options,
                    self.uploads,
                    errors,
                ):
                    code = errors[0]
                    self.error = (
                        "The browser blocked the final application request because "
                        + (
                            "it included employment history outside the reviewed answers."
                            if code == "unreviewed_employment"
                            else "the employer request did not match the reviewed form, answers, or files."
                        )
                        + " No final application request was sent."
                    )
                    if continuation:
                        self.error = "The verification request changed approved answers or files and was blocked."
                    self.progress("submission_blocked", reason_code=code)
                    self.db.audit(
                        "greenhouse.request_blocked",
                        self.app["id"],
                        {"reason_code": code},
                    )
                    await route.abort("blockedbyclient")
                    return True
                self.posted = True
                if continuation:
                    self.verification_allowed = False
                    self.verification_pending = False
                    self.verification_count += 1
                else:
                    self.request_token = token
                self.post_status = None
                self.response_ready.clear()
                # Persist before releasing the POST. A crash after this point
                # must never be interpreted as proof that sending did not occur.
                self.progress(
                    "verifying_email" if continuation else "sending_application",
                    send_started=True,
                    verification_count=self.verification_count,
                )
            await route.continue_(headers=headers)
            return True
        if request.url == self.receipt_url and request.method == "GET" and self.posted:
            headers = self.db.settings.local_headers() if self.fixture else {}
            await route.continue_(headers=headers)
            return True
        # Approved sessions may use the employer's verification provider. Never
        # solve, suppress, or synthesize a challenge/token; visible challenges pause.
        if (
            not self.fixture
            and u.scheme == "https"
            and (
                (
                    u.hostname in ("www.recaptcha.net", "www.google.com")
                    and u.path.startswith("/recaptcha/")
                )
                or (
                    u.hostname == "www.gstatic.com" and u.path.startswith("/recaptcha/")
                )
            )
        ):
            self.check()
            await route.continue_()
            return True
        return False

    async def response(self, response):
        if response.url == self.upload_url and response.request.method == "POST":
            # Uploads are serial; only the request whose field is pending can finish.
            for field, record in self.uploads.items():
                if not record["confirmed"]:
                    record["confirmed"] = 200 <= response.status < 300
                    self.progress(
                        "file_uploaded" if record["confirmed"] else "upload_rejected",
                        upload_status=response.status,
                    )
                    if record["confirmed"]:
                        artifact = next(
                            a
                            for a in self.review["payload"]["attachments"]
                            if a["kind"] == field + ".pdf"
                        )
                        self.db.execute(
                            "UPDATE artifacts SET attached=true WHERE id=%s AND application_id=%s",
                            (artifact["id"], self.app["id"]),
                        )
                        self.db.audit(
                            "greenhouse.file_uploaded",
                            self.app["id"],
                            {"artifact_id": artifact["id"], "hash": artifact["hash"]},
                        )
        if response.url == self.submit_url and response.request.method == "POST":
            self.post_status = response.status
            if response.status == 428:
                try:
                    body = await response.json()
                    kind = body.get("code")
                    safe_code = (
                        kind
                        if isinstance(kind, str)
                        and kind
                        in {
                            "captcha-failed",
                            "invalid-security-code",
                            "expired-security-code",
                            "security-code-error",
                        }
                        else "unrecognized"
                    )
                    email_challenge = (
                        kind == "captcha-failed"
                        and isinstance(body.get("security_code_recipient"), str)
                        and bool(body["security_code_recipient"])
                    ) or kind == "invalid-security-code"
                    self.verification_pending = email_challenge
                    self.progress(
                        "awaiting_email_verification"
                        if email_challenge
                        else "verification_blocked",
                        challenge_type="email" if email_challenge else "employer",
                        verification_status=(
                            "incorrect_code"
                            if kind == "invalid-security-code"
                            else "code_required"
                            if email_challenge
                            else "needs_attention"
                        ),
                        post_status=428,
                        response_format="json",
                        response_code=safe_code,
                    )
                except Exception:
                    self.progress(
                        "verification_blocked",
                        post_status=428,
                        response_format="unreadable",
                        response_code="unavailable",
                    )
            self.response_ready.set()
        if response.url == self.receipt_url and response.request.method == "GET":
            self.receipt_status = response.status
