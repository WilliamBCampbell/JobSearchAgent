import { useState } from "react";
type Row = Record<string, any>;

export function PackageReview({
  application,
  busy,
  onRevise,
  onAccept,
  onPreview,
}: {
  application: Row;
  busy: boolean;
  onRevise: (version: string, notes: string) => Promise<any>;
  onAccept: (version: string) => Promise<any>;
  onPreview: () => void;
}) {
  const [notes, setNotes] = useState("");
  const review = application.package_review;
  if (
    !review?.version ||
    application.imported ||
    application.attempts?.length ||
    ["submitted", "follow_up", "rejected", "closed", "skipped"].includes(
      application.state,
    )
  )
    return null;
  const history = review.history || [];
  const latest = history[0];
  return (
    <section
      className="panel"
      id="package-review"
      aria-label="Review application documents"
    >
      <h2>Review your application documents</h2>
      <p>
        Read the resume and cover letter below. Suggest changes to wording,
        emphasis, or organization, then review the regenerated version before
        continuing.
      </p>
      <p>
        <strong>
          {review.reviewed
            ? "This document version is reviewed."
            : "This document version needs your review."}
        </strong>{" "}
        Version {review.version}
      </p>
      <button className="secondary" onClick={onPreview}>
        View document previews
      </button>
      {latest && (
        <p role="status">
          {["queued", "running"].includes(latest.status)
            ? "Your changes are being prepared. The previous documents remain available below."
            : latest.status === "complete"
              ? "The revised documents are ready. Check the new previews against your suggestions."
              : "The revision did not finish. The previous documents are preserved. Review the task details or edit your notes and try again."}
        </p>
      )}
      <label htmlFor="package-revision-notes">
        What would you like changed?
      </label>
      <textarea
        id="package-revision-notes"
        rows={5}
        maxLength={2000}
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
        placeholder="For example: shorten the opening paragraph and give more space to my customer integration work."
      />
      <p className="muted">
        These suggestions apply only to this application. New experience or
        qualifications must be confirmed in your profile before the writer can
        use them.
      </p>
      {review.blocker && <p className="gap">{review.blocker}</p>}
      {notes.trim() && review.can_accept && (
        <p className="muted">
          Send these suggestions, or clear them to keep the current documents.
        </p>
      )}
      <div className="actions">
        <button
          disabled={busy || !review.can_revise || !notes.trim()}
          onClick={async () => {
            const result = await onRevise(review.version, notes.trim());
            if (result) setNotes("");
          }}
        >
          Regenerate with my suggestions
        </button>
        <button
          className="secondary"
          disabled={
            busy || !review.can_accept || review.reviewed || !!notes.trim()
          }
          onClick={() => onAccept(review.version)}
        >
          Documents look good
        </button>
      </div>
      <p className="muted">
        Reviewing documents does not approve submission. You will review the
        completed application and selected attachments before sending.
      </p>
      {history.length > 0 && (
        <details>
          <summary>Revision requests</summary>
          {history.map((r: Row) => (
            <div key={r.job_id}>
              <p>
                <strong>{new Date(r.created_at).toLocaleString()}</strong> ·{" "}
                {r.status}
              </p>
              <p style={{ whiteSpace: "pre-wrap" }}>{r.notes}</p>
              {review.can_revise && (
                <button
                  className="text-button"
                  onClick={() => setNotes(r.notes)}
                >
                  Edit these suggestions
                </button>
              )}
              <small>
                From version {r.base_version}
                {r.result_version ? ` to ${r.result_version}` : ""}
              </small>
              {r.error && <p className="gap">{r.error}</p>}
            </div>
          ))}
        </details>
      )}
    </section>
  );
}
