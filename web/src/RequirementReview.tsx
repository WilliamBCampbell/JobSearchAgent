import React, { useEffect, useState } from "react";

type Row = Record<string, any>;

export function fitValidationMessages(application: Row): string[] {
  const covered = new Set<string>();
  for (const row of application.fit_review?.requirements ?? []) {
    covered.add(row.text);
    covered.add(`Unresolved central requirement: ${row.text}`);
    covered.add(`Confirmed qualification gap: ${row.text}`);
  }
  for (const label of application.fit_review?.contact_fields ?? []) covered.add(`Unresolved central requirement: ${label}`);
  return (application.score?.reasons ?? []).filter((reason: string) => !covered.has(reason));
}

export function RequirementReview({ application, api, run, jobs = [] }: {
  application: Row;
  api: (path: string, body?: unknown) => Promise<any>;
  run: (task: () => Promise<any>, message?: string) => Promise<any>;
  jobs?: Row[];
}) {
  const [current, setCurrent] = useState(application);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [showResolved, setShowResolved] = useState(false);
  useEffect(() => setCurrent(application), [application.id, application.revision, application.fit_review?.score_hash]);
  const review = current.fit_review;
  if (!review || !current.score?.scores) return null;
  const unresolved = review.requirements.filter((row: Row) => !row.decision);
  const resolved = review.requirements.filter((row: Row) => row.decision);
  const visible = showResolved ? [...unresolved, ...resolved] : unresolved;
  const pending = jobs.some(j => j.kind === "rescore" && j.input.application_id === current.id && ["queued", "running"].includes(j.status));
  const editable = review.editable && !saving && !pending;
  const decide = async (row: Row, decision: string) => {
    setSaving(true);
    try {
      await run(async () => {
        const result = await api(`/applications/${current.id}/requirements/decision`, {
          requirement_key: row.key, score_hash: review.score_hash,
          decision, note: notes[row.key] ?? row.decision?.note ?? "",
        });
        setCurrent(result);
        return result;
      }, "Decision saved for this application. You can rescore the job using the updated evidence.");
    } finally { setSaving(false); }
  };
  return (
    <section id="central-requirements" className="requirement-review" aria-label="Central requirement decisions">
      <h3>Central requirements</h3>
      {review.needs_contact_cleanup && <p>This saved assessment includes an answer field as a qualification. Rescore to remove that old check and reassess the remaining fit concerns.</p>}
      <p>Confirm means you meet the requirement. Deny means you do not. Add a short note if useful. Decisions apply only to this application.</p>
      {!unresolved.some((r: Row) => r.central && r.classification === "required") && <p>No unresolved central requirements.</p>}
      {unresolved.some((r: Row) => !r.central || r.classification !== "required") && <p>Other unconfirmed qualifications below also affect fit. Confirm only experience you have, or deny it to record the gap.</p>}
      {resolved.length > 0 && (
        <button className="secondary" aria-expanded={showResolved}
          onClick={() => setShowResolved(!showResolved)}>
          {showResolved ? "Hide" : "Show"} resolved requirements ({resolved.length})
        </button>
      )}
      {visible.map((row: Row) => (
        <article className="panel" key={row.key} aria-label={row.text}>
          <h4>{row.text}</h4>
          <small>{row.central && row.classification === "required" ? "Central required qualification" : "Other qualification affecting fit"}</small>
          <p>{row.reason}</p>
          <small>Source: {row.source.startsWith("official:") ? "Official employer posting" : row.source.startsWith("form:") ? "Employer application form" : row.source} · Deduction: {row.deduction}</small>
          <details><summary>Source reference</summary><code>{row.source}</code></details>
          {row.decision && <p role="status">{row.decision.decision === "confirm" ? "Confirmed: you meet this requirement." : "Denied: this qualification gap remains."}</p>}
          <label>
            Quick note (optional)
            <textarea aria-label={`Note for ${row.text}`} maxLength={2000}
              disabled={!editable} value={notes[row.key] ?? row.decision?.note ?? ""}
              onChange={e => setNotes({...notes, [row.key]: e.target.value})} />
          </label>
          <div className="actions">
            <button disabled={!editable} onClick={() => decide(row, "confirm")}>Confirm</button>
            <button className="secondary" disabled={!editable} onClick={() => decide(row, "deny")}>Deny</button>
          </div>
        </article>
      ))}
      {!review.editable && <p>Decisions can be changed before submission. Resume paused work first. Imported records remain read only.</p>}
      <div className="actions">
        <button disabled={!editable || !["scored", "selected", "prepared", "missing_answers", "review", "verified", "manual_handoff"].includes(current.state)}
          onClick={() => run(() => api(`/applications/${current.id}/rescore`, {score_hash: review.score_hash}), "Rescoring queued with your saved decisions and notes.")}>
          {pending ? "Rescoring job…" : "Rescore job"}
        </button>
      </div>
      <p>Rescoring uses the configured evaluation provider. Confirmations clear the matching gaps immediately; rescoring reassesses the rest of the fit. Official opening, location, salary, and score thresholds still apply.</p>
    </section>
  );
}
