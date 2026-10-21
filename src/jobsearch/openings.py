"""Read-only official-opening adapters. Discovery text never establishes an opening."""

from datetime import datetime, timezone
import html
import json
import re
from urllib.parse import urlparse, urljoin, quote, parse_qs
import httpx
from bs4 import BeautifulSoup
from .config import atomic_write, digest
from .verification import public_url
from .tracker import normalize

ATS = {
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.eu.lever.co",
    "jobs.ashbyhq.com",
}


def company_key(value):
    return re.sub(
        r"\b(incorporated|inc|llc|ltd|corporation|corp|company|the)\b",
        "",
        normalize(value),
    ).replace(" ", "")


def same_company(left, right):
    a, b = company_key(left), company_key(right)
    return bool(a and b and a == b)


class OfficialOpenings:
    def __init__(self, db):
        self.db = db
        self.responses = []

    def fetch(self, url):
        # Every redirect is checked before issuing a request. No cookies, credentials,
        # POSTs, browser challenges, or retries on rate limiting.
        with httpx.Client(
            timeout=18, trust_env=False, follow_redirects=False
        ) as client:
            for _ in range(5):
                public_url(url)
                with client.stream(
                    "GET",
                    url,
                    headers={
                        "User-Agent": "JobSearchAgent/0.1 (public job verification)"
                    },
                ) as r:
                    if r.is_redirect:
                        url = urljoin(url, r.headers.get("location", ""))
                        continue
                    r.raise_for_status()
                    body = bytearray()
                    for block in r.iter_bytes():
                        body.extend(block)
                        if len(body) > 5_000_000:
                            raise ValueError("Official response exceeds capture limit")
                    raw = bytes(body)
                    path = (
                        self.db.settings.state / "official-raw" / (digest(raw) + ".bin")
                    )
                    atomic_write(path, raw)
                    self.responses.append(
                        {
                            "url": url,
                            "hash": digest(raw),
                            "path": str(path),
                            "fetched_at": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                    return url, raw
        raise ValueError("Official page redirected too many times")

    def get_json(self, url):
        _, body = self.fetch(url)
        return json.loads(body)

    def capture(
        self,
        lead,
        url,
        apply_url,
        requisition,
        title,
        description,
        source,
        details=None,
    ):
        public_url(apply_url)
        if not description or len(description) < 100:
            raise ValueError("Official description is missing or incomplete")
        # Do not attach a different vacancy to a discovery result.
        wanted = set(normalize(lead["title"]).split())
        actual = set(normalize(title).split())
        if len(wanted & actual) / max(1, len(wanted)) < 0.5:
            raise ValueError("Official title does not match the discovery")
        content = (
            "<!doctype html><meta charset='utf-8'><h1>"
            + html.escape(title)
            + "</h1><p>Employer: "
            + html.escape(lead["company"])
            + "</p><p>Requisition: "
            + html.escape(str(requisition))
            + "</p>"
            + description
            + "<pre>"
            + html.escape(json.dumps(details or {}, default=str))
            + "</pre>"
        ).encode()
        path = (
            self.db.settings.state / "official-captures" / (digest(content) + ".html")
        )
        atomic_write(path, content)
        return {
            "open": True,
            "status": "verified",
            "url": url,
            "application_url": apply_url,
            "requisition": str(requisition),
            "title": title,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "source_hash": digest(content),
            "capture_path": str(path),
            "verification_source": source,
            "raw_sources": self.responses.copy(),
            "details": details or {},
            "fixture": False,
        }

    def ats(self, lead, url):
        u = urlparse(url)
        parts = [p for p in u.path.split("/") if p]
        if (
            u.hostname in ("boards.greenhouse.io", "job-boards.greenhouse.io")
            and len(parts) >= 3
            and parts[1] == "jobs"
            and parts[2].isdigit()
        ):
            board, job = parts[0], parts[2]
            base = f"https://boards-api.greenhouse.io/v1/boards/{quote(board, safe='')}"
            identity = self.get_json(base)
            if not same_company(identity.get("name", ""), lead["company"]):
                raise ValueError("Official board employer does not match the discovery")
            data = self.get_json(
                base + f"/jobs/{job}?questions=true&pay_transparency=true"
            )
            if str(data.get("id")) != job:
                raise ValueError("Official requisition mismatch")
            return self.capture(
                lead,
                url,
                data["absolute_url"],
                data.get("requisition_id") or job,
                data["title"],
                html.unescape(data["content"]),
                base + f"/jobs/{job}",
                {
                    k: data.get(k)
                    for k in (
                        "location",
                        "questions",
                        "location_questions",
                        "compliance",
                        "pay_input_ranges",
                        "metadata",
                    )
                },
            )
        if u.hostname in ("jobs.lever.co", "jobs.eu.lever.co") and len(parts) >= 2:
            board, job = parts[:2]
            if not same_company(board, lead["company"]):
                raise ValueError("Official board employer does not match the discovery")
            api_host = (
                "api.eu.lever.co"
                if u.hostname == "jobs.eu.lever.co"
                else "api.lever.co"
            )
            endpoint = f"https://{api_host}/v0/postings/{quote(board, safe='')}/{quote(job, safe='')}?mode=json"
            data = self.get_json(endpoint)
            desc = (
                data.get("description", "")
                + "".join(
                    "<h2>" + html.escape(x["text"]) + "</h2>" + x["content"]
                    for x in data.get("lists", [])
                )
                + data.get("additional", "")
            )
            return self.capture(
                lead,
                data["hostedUrl"],
                data["applyUrl"],
                data["id"],
                data["text"],
                desc,
                endpoint,
                {
                    k: data.get(k)
                    for k in ("categories", "salaryRange", "workplaceType")
                },
            )
        if u.hostname == "jobs.ashbyhq.com" and len(parts) >= 2:
            board, job = parts[:2]
            if not same_company(board, lead["company"]):
                raise ValueError("Official board employer does not match the discovery")
            endpoint = f"https://api.ashbyhq.com/posting-api/job-board/{quote(board, safe='')}?includeCompensation=true"
            data = self.get_json(endpoint)
            match = next(
                (
                    x
                    for x in data.get("jobs", [])
                    if job in urlparse(x.get("jobUrl", "")).path.split("/")
                    and x.get("isListed", True)
                ),
                None,
            )
            if not match:
                raise ValueError("Opening is absent from the current official board")
            return self.capture(
                lead,
                match["jobUrl"],
                match["applyUrl"],
                job,
                match["title"],
                match["descriptionHtml"],
                endpoint,
                {
                    k: match.get(k)
                    for k in (
                        "location",
                        "secondaryLocations",
                        "address",
                        "workplaceType",
                        "compensation",
                    )
                },
            )
        return None

    def verify(self, lead):
        candidates = [lead.get("job_url_direct"), lead.get("job_url")]
        candidates += re.findall(
            r'https://(?:job-boards\.greenhouse\.io|boards\.greenhouse\.io|jobs\.lever\.co|jobs\.ashbyhq\.com)/[^\s<>"\)]+',
            lead.get("description") or "",
        )
        errors = []
        visited = set()
        for url in candidates[:5]:
            if not url or url in visited:
                continue
            visited.add(url)
            try:
                result = self.ats(lead, url)
                if result:
                    return result
                public_url(url)
                # Aggregator pages can only lead us to a supported official ATS.
                final, body = self.fetch(url)
                if final != url:
                    result = self.ats(lead, final)
                    if result:
                        return result
                soup = BeautifulSoup(body, "html.parser")
                # Embedded Greenhouse boards often redirect short links to an
                # employer careers page carrying gh_jid rather than an ATS URL.
                query = parse_qs(urlparse(final).query)
                greenhouse_id = (query.get("gh_jid") or [None])[0]
                if greenhouse_id:
                    embedded = re.findall(
                        r"(?:boards\.greenhouse\.io/embed/job_board\?for=|boards-api\.greenhouse\.io/v1/boards/|job-boards\.greenhouse\.io/)([a-zA-Z0-9_-]+)",
                        body.decode(errors="replace"),
                    )
                    for board in list(dict.fromkeys(embedded))[:2]:
                        result = self.ats(
                            lead,
                            f"https://job-boards.greenhouse.io/{board}/jobs/{greenhouse_id}",
                        )
                        if result:
                            return result
                for a in soup.select("a[href]"):
                    link = urljoin(final, a["href"])
                    if urlparse(link).hostname in ATS and link not in visited:
                        visited.add(link)
                        result = self.ats(lead, link)
                        if result:
                            return result
                        if len(visited) > 8:
                            break
                # Employer-owned JSON-LD is accepted only with a company-domain
                # match and an actual application route on that same domain.
                company_url = lead.get("company_url_direct") or ""
                company_host = urlparse(company_url).hostname
                final_host = urlparse(final).hostname
                supported_host = final_host.endswith(
                    ".breezy.hr"
                ) or final_host.endswith(".isolvedhire.com")
                if not supported_host and (
                    not company_host
                    or not (
                        final_host == company_host
                        or final_host.endswith("." + company_host.removeprefix("www."))
                    )
                ):
                    continue
                for tag in soup.select('script[type="application/ld+json"]'):
                    try:
                        payload = json.loads(tag.get_text())
                    except (ValueError, TypeError):
                        continue
                    entries = (
                        payload
                        if isinstance(payload, list)
                        else payload.get("@graph", [payload])
                    )
                    for entry in entries:
                        if entry.get("@type") != "JobPosting" or not same_company(
                            entry.get("hiringOrganization", {}).get("name", ""),
                            lead["company"],
                        ):
                            continue
                        expires = entry.get("validThrough")
                        if expires and datetime.fromisoformat(
                            expires.replace("Z", "+00:00")
                        ).replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
                            raise ValueError("Official posting has expired")
                        links = [
                            urljoin(final, a["href"])
                            for a in soup.select("a[href]")
                            if re.search(r"apply", a.get_text(), re.I)
                        ]
                        links = [
                            x
                            for x in links
                            if urlparse(x).hostname == urlparse(final).hostname
                        ]
                        ident = entry.get("identifier", {})
                        req = ident.get("value") if isinstance(ident, dict) else ident
                        if not req and final_host.endswith(".breezy.hr"):
                            match = re.search(r"/p/([a-f0-9]+)-", urlparse(final).path)
                            req = match.group(1) if match else None
                        if not links and soup.select("form[action]"):
                            links = [final]
                        if req and links:
                            return self.capture(
                                lead,
                                final,
                                links[0],
                                req,
                                entry["title"],
                                entry["description"],
                                final,
                                {
                                    "jobLocationType": entry.get("jobLocationType"),
                                    "applicantLocationRequirements": entry.get(
                                        "applicantLocationRequirements"
                                    ),
                                    "baseSalary": entry.get("baseSalary"),
                                },
                            )
            except (httpx.HTTPError, ValueError, KeyError, OSError) as exc:
                errors.append(
                    str(exc)[:180]
                    if isinstance(exc, ValueError)
                    else type(exc).__name__
                )
        return {
            "open": False,
            "status": "unverified",
            "reason": "No current official opening could be established",
            "errors": errors,
        }
