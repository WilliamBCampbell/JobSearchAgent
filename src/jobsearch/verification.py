from datetime import datetime, timezone
import ipaddress
import socket
from urllib.parse import urlparse, urljoin
import re
import httpx
from bs4 import BeautifulSoup
from .config import atomic_write, digest


def public_url(url):
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname or u.username or u.password:
        raise ValueError("Official sources require HTTPS without URL credentials")
    addresses = {
        r[4][0] for r in socket.getaddrinfo(u.hostname, 443, type=socket.SOCK_STREAM)
    }
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ValueError("Official source must resolve to a public address")
    return u


class Verification:
    def __init__(self, db):
        self.db = db

    def verify(self, app, url, official_host):
        if app["candidate"] == "fixture":
            if not url.startswith(self.db.settings.origin + "/fixture/posting/"):
                raise ValueError(
                    "Fixture verification requires the local fixture employer"
                )
        else:
            u = public_url(url)
            if u.hostname != official_host or official_host not in self.db.setting(
                "official_hosts", []
            ):
                raise ValueError(
                    "Confirm the employer careers host in settings before verification"
                )
        with httpx.Client(
            timeout=20, follow_redirects=False, trust_env=False
        ) as client:
            response = client.get(url)
        if response.is_redirect:
            return {
                "open": False,
                "status": "redirect_requires_review",
                "redirect": urljoin(url, response.headers.get("location", "")),
            }
        response.raise_for_status()
        if len(response.content) > 5_000_000:
            raise ValueError("Official page exceeds the capture limit")
        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
        closed = bool(
            re.search(
                r"no longer accepting|position (?:has been )?filled|job (?:is )?closed|no longer available",
                text,
                re.I,
            )
        )
        links = [
            urljoin(url, a.get("href", ""))
            for a in soup.find_all("a")
            if re.search(r"apply", a.get_text(), re.I)
        ]
        # A public page without an identifiable requisition and route stays unresolved.
        links = [x for x in links if urlparse(x).hostname == urlparse(url).hostname]
        is_open = (
            response.status_code == 200
            and app["requisition"] in text
            and bool(links)
            and not closed
        )
        capture = (
            self.db.settings.state
            / "official-captures"
            / f"{digest([app['id'], response.text])}.html"
        )
        atomic_write(capture, response.content)
        result = {
            "url": url,
            "application_url": links[0] if links else None,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "open": is_open,
            "status": "closed" if closed else "verified" if is_open else "needs_review",
            "requisition": app["requisition"],
            "source_hash": digest(response.content),
            "capture_path": str(capture),
            "fixture": app["candidate"] == "fixture",
        }
        self.db.audit(
            "opening.verified",
            app["id"],
            {"status": result["status"], "source_hash": result["source_hash"]},
        )
        return result
