import { WorkflowSection } from "./ApplicationSections";
import { profileUrl } from "./UserProfiles";
import { useEffect, useState } from "react";
import { answerText } from "./answerText";

type Row = Record<string, any>;
const steps = ["Check the role", "Prepare & review documents", "Prepare answers", "Review & approve", "Send application", "Confirm receipt"];
export const legacyGreenhouseBlockers = [
  "Greenhouse browser completion needs a portal adapter; use the saved answer plan",
  "Additional demographic or consent questions need review before browser filling",
];
export function hasGreenhouseBrowser(a: Row) {
  const spec = a.payload.form_plan?.spec;
  if (spec?.adapter === "greenhouse" && spec.unsupported?.some((r: string) => !legacyGreenhouseBlockers.includes(r))) return false;
  return spec?.adapter === "greenhouse" || /https:\/\/(?:job-boards|boards)\.greenhouse\.io\/[^/]+\/jobs\/\d+/.test(a.official?.application_url || "");
}
export function manualPackageReviewed(a: Row, review: Row | null) {
  return !!review && !(a.questions || []).some((q: Row) => !q.answer_fact || q.needs_review) && a.payload.handoff_review?.reviewed_hash === review.hash;
}

export function applicationJourney(a: Row, review: Row | null, realSending: boolean, receiptOpen: boolean): Row {
  const received = (a.receipts || []).length > 0;
  const attempted = (a.attempts || []).length > 0;
  const manual = a.state === "manual_handoff";
  const reviewed = manualPackageReviewed(a, review);
  const job = (a.jobs || []).find((j: Row) => ["running", "queued"].includes(j.status));
  // Preparation gates come from the API. Receipt/verification actions below
  // remain separate because an attempted application cannot return to filling.
  let p: Row = a.progress || {
    step: 3, title: "Review your package before opening the employer form",
    current: "Your documents and answers are saved. No submission approval is recorded.",
    instruction: "Review the prepared answers and files, then complete the employer form in your browser.",
    following: "Record the employer confirmation after sending.",
    button: "Review answers and files", target: "manual-package-review", action: "",
    completed: [true, true, true, false, false, false], status: "ready",
  };
  if (manual && reviewed && !a.progress) p = {...p,
    title: "Complete the application on the employer’s site",
    current: "Your package review is saved. The app has not submitted this application.",
    instruction: "Open the employer form, use the saved answers and files, and review its final page before submitting.",
    button: "Open employer application",
  };
  if (["authorized", "submission_attempt", "receipt_verification"].includes(a.state)) p = {...p,
    step: 4, title: "The agent is completing your approved application",
    current: "Submission is limited to the answers and files you approved.",
    instruction: "Wait for the receipt check. Do not send a second copy.",
    following: "The app will show employer confirmation or an unresolved outcome.",
    button: "View work progress", target: "work-progress", action: "",
    completed: [true, true, true, true, false, false],
  };
  if (a.state === "authorized" && !attempted && !job) p = {...p,
    title: "Resume your approved application",
    current: "Approval is saved, but sending has not started.",
    instruction: "Resume the approved sending run. The agent checks that the answers and files still match your approval.",
    button: "Resume approved application", action: "resume-approved",
  };
  if (a.state === "awaiting_challenge") {
    p = {...p, step: attempted ? 4 : 2, title: "Complete employer verification",
      current: "The employer is waiting for your verification. No receipt is confirmed.",
      instruction: "Complete the verification in the employer browser. Do not enter codes into this app.",
      following: "The agent checks for confirmation before any retry.",
      button: attempted ? "View browser handoff" : "Resume after browser verification", target: "workflow-controls", action: attempted ? "" : "continue",
      completed: [true, true, attempted, attempted, false, false],
    };
    const verification = a.attempts?.[0]?.result;
    if (hasGreenhouseBrowser(a) && attempted && verification?.challenge_type === "email") {
      p = {...p, title: "Enter the emailed code in the employer browser",
        current: "Greenhouse is waiting for verification. The original approval still applies to the same answers and files.",
        instruction: "Type the code into the eight boxes in the employer window, then choose Continue verification here. If pasting fails, type one character per box.",
        following: "The agent continues this attempt and checks for a receipt. The code is not saved.",
        button: "Continue verification", action: "continue-verification",
      };
      if (verification.verification_status === "incorrect_code") p.instruction = "Greenhouse did not accept that code. Correct it in the employer browser, then continue here.";
      if (verification.verification_count >= 3) p = {...p, instruction: "Three verification attempts have been made. Check the employer instructions and saved handoff before another submission action.", button: "View browser handoff", action: ""};
      const continuation = a.jobs?.find((j: Row) => j.kind === "continue_verification");
      if (continuation?.status === "failed" && continuation.error?.includes("no longer connected")) p = {...p,
        title: "The verification browser is no longer connected",
        instruction: "No request was retried. Check the employer outcome before another submission action.",
        button: "View browser handoff", action: "",
      };
    }
  }
  if (a.state === "unresolved" || (manual && attempted) || receiptOpen || received || a.state === "submitted") p = {...p,
    step: 5, title: received ? "Employer receipt recorded" : "Check the employer confirmation",
    current: received ? "Confirmation evidence is saved with this application." : "No receipt has been confirmed. A button click is not proof of receipt.",
    instruction: received ? "Keep the receipt for follow-up." : "Check the confirmation page or email and record what you received. Do not retry while the outcome is unknown.",
    following: received ? "The application is available for follow-up." : "Only confirmed receipt moves the application to submitted.",
    button: received ? "View receipt" : "Record employer confirmation", target: "receipt-section", action: "",
    completed: [true, true, true, attempted || received, received, received],
  };
  if (a.state === "unresolved" && !received && !job && !receiptOpen) {
    if (a.submission_recovery?.can_retry) p = {...p, step: 4,
      title: a.submission_recovery.kind === "verification" ? "Verification was not completed" : "The application stopped before sending",
      current: a.submission_recovery.reason,
      instruction: "Retry the approved application when you are ready.",
      following: "The agent sends the same approved answers and files. No new approval is needed.",
      button: "Retry approved application", action: "retry-submission",
    };
    else if (a.submission_recovery?.can_confirm_not_submitted) p = {...p, step: 4,
      title: "Resolve the unfinished verification",
      instruction: "If you never completed verification or submitted elsewhere, record Not submitted below. Only confirm receipt if the employer actually confirmed your application.",
      following: "Recording Not submitted enables a separate retry with the existing approval. It does not send anything.",
      button: "Resolve submission outcome", target: "submission-outcome", action: "",
    };
  }
  if (["deferred", "closed", "skipped", "rejected"].includes(a.state)) p = {...p,
    title: a.state === "skipped" ? "This role was discarded" : "This application is not proceeding",
    current: "The decision and supporting records remain saved.", instruction: "Review the saved decision before taking another action.",
    following: "No submission starts from this screen automatically.", button: "View saved evidence", target: "role-fit", action: "",
  };
  if (a.paused) p = {...p, title: "This application is paused", instruction: "Resume to continue from the saved step.", button: "Resume this application", action: "resume", status: "paused"};
  if (p.step === 3 && !manual && a.candidate === "real" && !realSending) p = {...p,
    following: "Sending to real employers is disabled in this deployment. Enable it in settings before approving, or use a normal-browser handoff.",
  };
  return p;
}

export function ApplicationJourney({application: a, review, realSending, busy, onAction, onNavigate, receiptOpen, onReceipt}: {
  application: Row; review: Row | null; realSending: boolean; busy: boolean;
  onAction: (action: string, body?: Row) => void; onNavigate: (id: string) => void;
  receiptOpen: boolean; onReceipt: () => void;
}) {
  const received = (a.receipts || []).length > 0;
  const attempted = (a.attempts || []).length > 0;
  const manual = a.state === "manual_handoff";
  const reviewed = manualPackageReviewed(a, review);
  const job = (a.jobs || []).find((j: Row) => ["running", "queued"].includes(j.status));
  const p = applicationJourney(a, review, realSending, receiptOpen);
  const done = p.completed;
  return <section className="panel application-journey" id="application-journey" aria-label="Application next step">
    <ol className="journey-steps" aria-label="Application steps">
      {steps.map((label, i) => <li key={label} aria-current={i === p.step && !received ? "step" : undefined} className={i === p.step && !received ? "current" : done[i] ? "complete" : ""}>
        <span>{done[i] && (i !== p.step || received) ? "✓" : i + 1}</span>{label}
        <small>{i === p.step && !received ? "Current step" : done[i] ? (i > p.step ? "Saved" : "Complete") : "Not completed"}</small>
      </li>)}
    </ol>
    <p className="eyebrow">{a.paused ? "PAUSED" : job ? "AGENT WORK IN PROGRESS" : received ? "RECEIPT SAVED" : "YOUR NEXT STEP"}</p>
    <h2>{p.title}</h2>
    <dl className="journey-story"><dt>Current progress</dt><dd>{p.current}</dd><dt>Your next action</dt><dd>{p.instruction}</dd><dt>What follows</dt><dd>{p.following}</dd></dl>
    {manual && !a.progress && !attempted && !received && <p className="journey-limitation">
      This employer form needs manual completion for the following reason:
      {(a.payload.form_plan?.spec.unsupported || []).map((r: string) => <span className="block" key={r}>{r}</span>)}
    </p>}
    {p.button === "Open employer application" ? <a className="primary-link" href={a.official?.application_url} target="_blank" rel="noreferrer">{p.button}</a> :
      <button disabled={busy || (!!job && !!p.action && p.action !== "resume")} onClick={() => p.action ? onAction(p.action === "resume" ? "control" : p.action, p.action === "resume" ? {action: "resume"} : {}) : onNavigate(p.target)}>{p.button}</button>}
    {p.opening_blocked && p.status === "blocked" && !job && !a.paused && <div className="actions">
      {(a.official?.application_url || a.official?.url) && <a className="secondary" href={a.official.application_url || a.official.url} target="_blank" rel="noreferrer">Inspect employer posting</a>}
      {!!a.payload.package && <button className="secondary" onClick={() => onNavigate("prepared-documents")}>View saved documents</button>}
      <button className="text-button" disabled={busy || !a.can_discard} title={a.discard_blocker || "Keep the saved history and remove this role from active work"}
        onClick={() => onAction("control", {action: "discard", reason: "Discarded after reviewing the unavailable or unverified opening"})}>Discard this role</button>
    </div>}
    {manual && !received && <button className="text-button" onClick={onReceipt}>Already submitted? Record the confirmation</button>}
    {manual && reviewed && a.handoff_ready && <p><a href={profileUrl("/api/handoffs/" + a.id)} target="_blank" rel="noreferrer">Open the saved answers beside the employer form</a></p>}
  </section>;
}

export function ManualPackageReview({
  application: a,
  review,
  error,
  busy,
  onReview,
  onNavigate,
}: {
  application: Row;
  review: Row | null;
  error: string | null;
  busy: boolean;
  onReview: (hash: string) => void;
  onNavigate: (id: string) => void;
}) {
  const [checked, setChecked] = useState(false);
  useEffect(() => setChecked(false), [review?.hash]);
  const done = manualPackageReviewed(a, review);
  const pending = (a.questions || []).some(
    (q: Row) => !q.answer_fact || q.needs_review,
  );
  if (!review)
    return (
      <WorkflowSection section="manual-review" summary="Review answers and files">
      <section className="panel" id="manual-package-review">
        <h2>{error ? "Package review needs attention" : "Loading your package review"}</h2>
        <p role="status">{error || "Checking the saved answers and selected file versions. The review will appear here when those checks finish."}</p>
        {error && <><button onClick={() => onNavigate("prepared-answers")}>Check saved answers</button><button className="secondary" onClick={() => onNavigate("prepared-documents")}>Check selected files</button></>}
      </section>
      </WorkflowSection>
    );
  return (
    <WorkflowSection section="manual-review" className="panel manual-package-review" id="manual-package-review"
      summary={done ? "Prepared package reviewed" : "Review answers and files"}>
      <p>
        <strong>
          {a.employer} · {a.role}
        </strong>{" "}
        · Requisition {a.requisition}
      </p>
      <p>
        This reviews the saved package. On the employer’s site, you must still
        review any additional questions, consents, and the final submission
        page.
      </p>
      <h3>Files selected to attach</h3>
      {review.payload.attachments.length ? (
        <ul>
          {review.payload.attachments.map((f: Row) => (
            <li key={f.id}>
              <a
                href={profileUrl("/api/artifacts/" + f.id)}
                target="_blank"
                rel="noreferrer"
              >
                {f.name}
              </a>
            </li>
          ))}
        </ul>
      ) : (
        <p>No files selected.</p>
      )}
      <button
        className="text-button"
        onClick={() => onNavigate("prepared-documents")}
      >
        Preview or change selected files
      </button>
      <h3>Prepared answers</h3>
      <dl className="review-answers">
        {Object.entries(review.payload.answers).map(([key, raw]) => {
          const answer = raw as Row;
          return (
            <div key={key}>
              <dt>
                {a.payload.form_schema?.find((f: Row) => f.key === key)
                  ?.label || key}
              </dt>
              <dd>
                {answerText(answer.value)}
              </dd>
            </div>
          );
        })}
      </dl>
      <button
        className="text-button"
        onClick={() => onNavigate("prepared-answers")}
      >
        Change an answer or inspect its source
      </button>
      <h3>Employer notices to check in your browser</h3>
      {(a.payload.form_plan?.spec.notices || []).map((notice: string) => (
        <p key={notice}>{notice}</p>
      ))}
      <p>
        Do not assume that every employer notice or consent is represented in
        this saved plan.
      </p>
      {pending && (
        <p>
          Confirm the outstanding answers before marking this package reviewed.
        </p>
      )}
      {!done && (
        <>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={checked}
              onChange={(e) => setChecked(e.target.checked)}
            />
            I reviewed these answers and selected files.
          </label>
          <button
            disabled={
              busy ||
              a.paused ||
              pending ||
              !checked ||
              !review.payload.attachments.some(
                (f: Row) => f.kind === "resume.pdf",
              )
            }
            onClick={() => onReview(review.hash)}
          >
            Mark package reviewed and continue
          </button>
        </>
      )}
      {done && (
        <p>
          Your package review is saved. Changing an answer or selected file
          requires a new review.
        </p>
      )}
    </WorkflowSection>
  );
}
