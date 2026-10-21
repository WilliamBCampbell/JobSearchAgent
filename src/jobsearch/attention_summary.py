"""Evidence summaries and discard eligibility for application review screens."""

DISCARD_STATES = {
    "discovered",
    "verified",
    "scored",
    "selected",
    "preparing",
    "prepared",
    "filling",
    "missing_answers",
    "review",
    "authorized",
    "awaiting_challenge",
    "manual_handoff",
    "deferred",
}


def discard_blocker(app, attempted=False, received=False, running=False):
    if app["imported"]:
        return "Imported tracker records cannot be discarded here."
    if (
        attempted
        or received
        or app["state"]
        in {
            "unresolved",
            "submission_attempt",
            "receipt_verification",
            "submitted",
            "follow_up",
            "rejected",
        }
    ):
        return (
            "Reconcile the submission outcome instead of discarding this application."
        )
    if app["state"] not in DISCARD_STATES:
        return "This role is already closed or discarded."
    if running:
        return "Wait for active work to finish, or cancel it under Work progress, before discarding."
    return None


def concerns(score):
    severity = {"hard_stop": 0, "gap": 1, "unconfirmed": 2, "partial": 3}
    rows = [
        r
        for r in score.get("requirements", [])
        if r.get("material", True) and r.get("assessment") in severity
    ]
    rows.sort(
        key=lambda r: (
            0
            if r["assessment"] == "hard_stop" and r.get("classification") == "required"
            else 1,
            0 if r.get("central") and r.get("classification") == "required" else 1,
            0 if r.get("classification") == "required" else 1,
            severity[r["assessment"]],
            -float(r.get("deduction", 0)),
            r.get("text", ""),
        )
    )
    unique = {}
    for row in rows:
        unique.setdefault(row["text"].strip().casefold(), row)
    return list(unique.values())


def initial_scores(conn, candidate):
    # Legacy model previews are labeled as evaluations, never as applied decisions.
    rows = conn.execute(
        """
        SELECT DISTINCT ON (application_id) * FROM (
            SELECT a.id AS application_id, e.detail->'total' AS total,
                   e.at AS recorded_at, 'first_applied_score' AS source, e.seq::text AS source_id
            FROM audit e JOIN applications a ON a.id=e.subject
            WHERE a.candidate=%s AND e.event='fit.initial_score'
            UNION ALL
            SELECT a.id, r.output->'score_preview'->'total', r.created_at,
                   'first_saved_evaluation', r.id
            FROM llm_runs r JOIN applications a ON a.id=r.application_id
            WHERE a.candidate=%s AND r.task='fit_assessment' AND r.status='complete'
              AND jsonb_typeof(r.output->'score_preview'->'total')='number'
        ) evidence ORDER BY application_id, recorded_at, source_id
    """,
        (candidate, candidate),
    ).fetchall()
    return {r["application_id"]: dict(r) for r in rows}
