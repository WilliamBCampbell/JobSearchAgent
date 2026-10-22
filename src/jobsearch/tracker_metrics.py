"""Read the authoritative spreadsheet separately from in-app workflow counts."""

from collections import Counter
from datetime import datetime, timezone
from .tracker import TRACKER_NAME, read_ods


def dashboard(db, candidate):
    root = (
        db.settings.workspace if candidate == "real" else db.settings.fixture_workspace
    )
    path = root / TRACKER_NAME
    tracker = read_ods(path) if path.exists() else None
    records = tracker["records"] if tracker else []
    statuses = Counter(row["status"] or "Unspecified" for row in records)
    months = Counter()
    undated = 0
    for row in records:
        if not row["date_applied"]:
            continue
        parsed = None
        for fmt in ("%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y"):
            try:
                parsed = datetime.strptime(row["date_applied"].strip(), fmt)
                break
            except ValueError:
                pass
        if parsed:
            months[parsed.strftime("%Y-%m")] += 1
        else:
            undated += 1
    apps = db.all(
        "SELECT id,employer,role,requisition,state,revision,score,updated_at FROM applications WHERE candidate=%s AND NOT imported ORDER BY updated_at DESC",
        (candidate,),
    )
    return {
        "candidate": candidate,
        "tracker_path": str(path),
        "tracker": tracker,
        "read_at": datetime.now(timezone.utc).isoformat(),
        "modified_at": datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc
        ).isoformat()
        if tracker
        else None,
        "writes_enabled": candidate == "fixture" or db.settings.live_tracker_writes,
        "status_counts": dict(sorted(statuses.items())),
        "monthly_applications": dict(sorted(months.items())),
        "unparsed_dates": undated,
        "missing_dates": sum(not row["date_applied"] for row in records),
        "evidence_counts": {
            "Current facts": db.one(
                "SELECT count(*) AS n FROM facts WHERE candidate=%s AND active",
                (candidate,),
            )["n"],
            "Documents": db.one(
                "SELECT count(*) AS n FROM documents WHERE candidate=%s AND NOT deleted",
                (candidate,),
            )["n"],
            "App workflows": len(apps),
            "Workflows with receipts": db.one(
                "SELECT count(DISTINCT r.application_id) AS n FROM receipts r JOIN applications a ON a.id=r.application_id WHERE a.candidate=%s AND NOT a.imported AND NOT COALESCE((r.evidence->>'invalidated')::boolean,false)",
                (candidate,),
            )["n"],
        },
        "applications": apps,
        "workflow_counts": dict(sorted(Counter(a["state"] for a in apps).items())),
        "measurements": db.all("SELECT * FROM metrics ORDER BY name"),
        "conflicts": db.all(
            "SELECT c.* FROM conflicts c JOIN applications a ON a.id=c.subject WHERE a.candidate=%s AND NOT c.resolved ORDER BY c.id",
            (candidate,),
        ),
    }
