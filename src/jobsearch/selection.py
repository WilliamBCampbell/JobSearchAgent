"""Explain the existing role-selection policy to both the API and guided UI."""

from .config import digest


def selection_review(app):
    score = app.get("score") or {}
    mode = "primary" if score.get("primary") else "conditional"
    blockers = []
    if app.get("imported"):
        blockers.append(
            "This is an imported tracker record, not a new application workflow."
        )
    if app.get("paused"):
        blockers.append("Resume this application before selecting it.")
    if app["state"] not in ("scored", "deferred"):
        blockers.append(
            "Complete the fit assessment before selecting this role."
            if app["state"] in ("discovered", "verified")
            else "This application has already moved beyond role selection."
        )
    if not score.get("scores") or score.get("band") not in (
        "primary",
        "conditional",
        "skip",
    ):
        blockers.append("A completed fit assessment is required.")
    if score.get("band") == "skip":
        blockers.append(
            "This assessment does not permit selection. Review the disqualifying requirements or score before proceeding."
        )
    reasons = list(dict.fromkeys(score.get("reasons", [])))
    if score.get("total", 100) < 80:
        reasons.append(
            f"Overall fit is {score['total']:g}/100; primary recommendations require at least 80."
        )
    if not score.get("primary") and not reasons:
        reasons.append(
            "The saved assessment is conditional and needs your explicit decision."
        )
    return {
        "mode": "blocked" if blockers else mode,
        "blockers": blockers,
        "reasons": reasons,
        "uncertainties": [
            r
            for r in score.get("requirements", [])
            if r.get("material", True)
            and r.get("assessment") in ("unconfirmed", "gap", "partial", "hard_stop")
            and not (r.get("central") and r.get("classification") == "required")
        ],
        "central_count": sum(
            r.get("central", False)
            and r.get("classification") == "required"
            and r.get("assessment") in ("unconfirmed", "gap", "partial", "hard_stop")
            for r in score.get("requirements", [])
        ),
        "review_hash": digest(
            {
                k: app.get(k)
                for k in (
                    "id",
                    "revision",
                    "state",
                    "score",
                    "official",
                    "paused",
                    "imported",
                )
            }
        ),
    }
