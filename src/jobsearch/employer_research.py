"""Public research snapshots used as employer evidence, never candidate facts."""

from pathlib import Path

from bs4 import BeautifulSoup

from .config import atomic_write, digest, file_hash
from .openings import OfficialOpenings
from .workflow import Workflow


class EmployerResearch:
    def __init__(self, db):
        self.db = db

    def capture(self, aid, urls):
        urls = list(dict.fromkeys(urls))
        if len(urls) > 4:
            raise ValueError("Add at most four research links at a time")
        flow = Workflow(self.db)
        app = flow.get(aid)
        flow.editable(app)
        if app["paused"] or self.db.one(
            "SELECT 1 FROM attempts WHERE application_id=%s", (aid,)
        ):
            raise ValueError("Research can only change before a sending attempt")
        records = []
        for url in urls:
            final_url, body = OfficialOpenings(self.db).fetch(url)
            soup = BeautifulSoup(body, "html.parser")
            if not soup.find("html") or not soup.get_text(strip=True):
                raise ValueError("Employer research must be a readable web page")
            path = (
                self.db.settings.state / "employer-research" / (digest(body) + ".html")
            )
            atomic_write(path, body)
            records.append(
                {
                    "url": final_url,
                    "requested_url": url,
                    "path": str(path),
                    "hash": digest(body),
                    "title": soup.title.get_text(" ", strip=True)
                    if soup.title
                    else final_url,
                }
            )
        replaced = {r["requested_url"] for r in records}
        previous = [
            r
            for r in app["payload"].get("employer_research", [])
            if r["requested_url"] not in replaced
        ]
        flow.update_payload(aid, {"employer_research": (previous + records)[-6:]})
        return records

    def sources(self, app):
        sources = []
        records = app["payload"].get("employer_research", [])
        page_budget = min(8000, 12000 // max(1, len(records)))
        root = (self.db.settings.state / "employer-research").resolve()
        for record in records:
            path = Path(record["path"]).resolve()
            if (
                not path.is_relative_to(root)
                or not path.is_file()
                or file_hash(path) != record["hash"]
            ):
                raise ValueError("Saved employer research changed or is missing")
            soup = BeautifulSoup(path.read_bytes(), "html.parser")
            for node in soup(["script", "style", "nav", "footer", "header"]):
                node.decompose()
            main = soup.find("main") or soup.find("article") or soup
            sources.append(
                {
                    "id": "research:" + digest([record["url"], record["hash"]]),
                    "kind": "employer_research",
                    "url": record["url"],
                    "title": record["title"],
                    "text": main.get_text(" ", strip=True)[:page_budget],
                }
            )
        return sources
