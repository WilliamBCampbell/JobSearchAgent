import { useEffect, useState } from "react";

type Row = Record<string, any>;
export function SelectionReview({
  application: a,
  busy,
  onSelect,
  onNavigate,
}: {
  application: Row;
  busy: boolean;
  onSelect: (body: Row) => void;
  onNavigate: (id: string) => void;
}) {
  const [reason, setReason] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const review = a.selection_review;
  useEffect(() => {
    setReason("");
    setAcknowledged(false);
  }, [a.id, review?.review_hash]);
  if (a.state !== "scored" || !review) return null;
  const conditional = review.mode === "conditional";
  const pending = (a.jobs || []).some((j: Row) =>
    ["running", "queued"].includes(j.status),
  );
  const reasons = review.reasons.filter(
    (r: string) =>
      !r.startsWith("Unresolved central requirement:") &&
      !r.startsWith("Confirmed qualification gap:"),
  );
  return (
    <section
      className="panel selection-review"
      id="role-selection"
      aria-label="Role selection decision"
    >
      <h2>
        {review.mode === "blocked"
          ? "Selection needs attention"
          : conditional
            ? "Decide whether to pursue this conditional role"
            : "This role is ready for selection"}
      </h2>
      <p>
        Assessment complete: <strong>{a.score.total}/100</strong>.{" "}
        {a.score.primary
          ? "It meets the primary recommendation criteria."
          : "It does not meet every primary recommendation criterion."}
      </p>
      {review.blockers.length > 0 && (
        <ul>
          {review.blockers.map((text: string) => (
            <li key={text}>{text}</li>
          ))}
        </ul>
      )}
      {reasons.length > 0 && (
        <>
          <h3>Why it is not a primary recommendation</h3>
          <ul>
            {reasons.map((text: string) => (
              <li key={text}>{text}</li>
            ))}
          </ul>
        </>
      )}
      {review.central_count > 0 && (
        <p>
          {review.central_count} central{" "}
          {review.central_count === 1
            ? "requirement needs"
            : "requirements need"}{" "}
          review.{" "}
          <button
            className="text-button"
            onClick={() => onNavigate("central-requirements")}
          >
            Review central requirements
          </button>
        </p>
      )}
      {review.uncertainties.length > 0 && (
        <>
          <h3>Other gaps and uncertainties</h3>
          <p>
            These are saved assessment findings. Form preparation will check
            your current saved answers before asking for missing information.
          </p>
          <ul className="selection-findings">
            {review.uncertainties.map((r: Row, i: number) => (
              <li key={i}>
                <strong>{r.text}</strong>
                <details>
                  <summary>Why this is listed</summary>
                  <p>{r.reason}</p>
                </details>
                <small>
                  {r.classification === "preferred" ? "Preferred" : "Required"}{" "}
                  · {r.category} ·{" "}
                  {r.assessment === "unconfirmed"
                    ? "Not confirmed in this assessment"
                    : r.assessment}
                </small>
              </li>
            ))}
          </ul>
        </>
      )}
      {a.official?.verified_at && (
        <p className="muted">
          Official opening last checked:{" "}
          {new Date(a.official.verified_at).toLocaleString()}.
        </p>
      )}
      <p>
        Selecting a role moves it to document preparation. It does not confirm
        the gaps, change your score, approve answers, or authorize sending an
        application.
      </p>
      {conditional && (
        <>
          <p>
            Conditional selection allows preparation. Automated sending still
            requires a primary assessment; rescore with corrected evidence
            before selection if you want to resolve that gate. If the role stays
            conditional, use a reviewed browser handoff.
          </p>
          <label>
            Why do you want to pursue this role?
            <textarea
              maxLength={2000}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Explain why this role is worth pursuing despite the listed uncertainties."
            />
          </label>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={acknowledged}
              onChange={(e) => setAcknowledged(e.target.checked)}
            />
            I have reviewed these uncertainties and want to proceed with
            preparation.
          </label>
        </>
      )}
      {pending && (
        <p role="status">
          An agent job is still running for this application. Selection will be
          available when it finishes.
        </p>
      )}
      {conditional && (!reason.trim() || !acknowledged) && (
        <p id="selection-help">
          To continue, add your reason and check the acknowledgment above.
        </p>
      )}
      {review.mode !== "blocked" && (
        <button
          disabled={
            busy ||
            pending ||
            (conditional && (!reason.trim() || !acknowledged))
          }
          aria-describedby={
            conditional && (!reason.trim() || !acknowledged)
              ? "selection-help"
              : undefined
          }
          onClick={() =>
            onSelect({
              reviewed_hash: review.review_hash,
              ...(conditional ? { conditional_reason: reason.trim() } : {}),
            })
          }
        >
          {conditional ? "Select conditional role" : "Select this role"}
        </button>
      )}
      <button className="secondary" onClick={() => onNavigate("role-fit")}>
        Review fit evidence and rescoring
      </button>
      <p className="muted">
        Next: prepare and review the documents, complete the employer answers,
        then approve the final application before sending.
      </p>
    </section>
  );
}
