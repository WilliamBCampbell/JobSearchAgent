import React from "react";

type Row = Record<string, any>;

export function InterventionInbox({
  inbox,
  openApplication,
  markRead,
  discard,
  busy,
}: {
  inbox: Row;
  openApplication: (id: string) => void;
  markRead: (id: string) => void;
  discard: (id: string) => void;
  busy: boolean;
}) {
  return (
    <section aria-label="Application notifications">
      <div className="panel">
        <h2>{inbox.applications || 0} applications need your attention</h2>
        <p>
          Required answers, browser verification, failed tasks, missing
          receipts, and final approvals appear here. Reading a notice keeps its
          blocker in place until you resolve the underlying item. Discard
          removes a role from active work while keeping its history.
        </p>
        <p role="status">
          {inbox.unread || 0} unread notices in this workspace.
        </p>
        {!inbox.items?.length && <p>No applications are waiting on you.</p>}
      </div>
      {inbox.items?.map((item: Row) => (
        <article
          className={"panel intervention " + (item.read_at ? "" : "unread")}
          key={item.id}
        >
          <div className="section-title">
            <p className="eyebrow">
              {item.read_at ? "SEEN · STILL OPEN" : "NEW"}
            </p>
            <small>{new Date(item.updated_at).toLocaleString()}</small>
          </div>
          <h2>
            {item.employer} · {item.role}
          </h2>
          <p className="muted">
            Requisition {item.requisition}
            {item.paused ? " · Paused" : ""}
          </p>
          <div className="attention-fit">
            <p>
              <strong>
                Initial score:{" "}
                {item.initial_score?.total != null
                  ? `${item.initial_score.total}/100`
                  : "Not recorded"}
              </strong>
              {item.current_score != null && (
                <span> · Current score: {item.current_score}/100</span>
              )}
            </p>
            {item.initial_score && (
              <small>
                {item.initial_score.source === "first_saved_evaluation"
                  ? "First saved evaluation"
                  : "First applied assessment"}{" "}
                · {new Date(item.initial_score.recorded_at).toLocaleString()}
              </small>
            )}
            {item.concerns?.length > 0 ? (
              <>
                <h3>Biggest current requirement concerns</h3>
                <ul>
                  {item.concerns.map((r: Row, i: number) => (
                    <li key={i}>
                      <strong>{r.text}</strong>
                      <p className="muted">
                        {r.central && r.classification === "required"
                          ? "Central requirement"
                          : r.classification === "preferred"
                            ? "Preferred qualification"
                            : "Required"}{" "}
                        ·{" "}
                        {r.assessment === "hard_stop"
                          ? "Disqualifying gap"
                          : r.assessment === "gap"
                            ? "Qualification gap"
                            : r.assessment === "partial"
                              ? "Partly supported"
                              : "Unconfirmed"}{" "}
                        · {r.deduction} points deducted
                      </p>
                      <details>
                        <summary>Assessment explanation</summary>
                        <p>{r.reason}</p>
                      </details>
                    </li>
                  ))}
                </ul>
                {item.additional_concerns > 0 && (
                  <p>
                    {item.additional_concerns} more concerns in the full fit
                    assessment.
                  </p>
                )}
              </>
            ) : (
              <p className="muted">
                {item.current_score == null
                  ? "No fit assessment recorded yet."
                  : "No current requirement gaps recorded."}
              </p>
            )}
          </div>
          <h3>{item.detail.title}</h3>
          <p>{item.detail.message}</p>
          {item.detail.questions?.length > 0 && (
            <ul>
              {item.detail.questions.map((q: Row) => (
                <li key={q.id}>
                  <strong>{q.prompt}</strong>
                  <p>{q.reason?.message}</p>
                </li>
              ))}
            </ul>
          )}
          {item.kind !== "answers" &&
            item.detail.followups?.map((f: Row) => (
              <div key={f.id}>
                {f.reason && <p>{f.reason}</p>}
                {f.next_step && <p>Next step: {f.next_step}</p>}
              </div>
            ))}
          {item.imported && (
            <p>
              This imported record is read only. Its tracker status has not
              changed.
            </p>
          )}
          <div className="actions">
            <button onClick={() => openApplication(item.application_id)}>
              {item.kind === "answers"
                ? "Review missing answers"
                : "Open application"}
            </button>
            {!item.imported && (
              <button
                className="secondary"
                disabled={busy || !item.can_discard}
                onClick={() => discard(item.application_id)}
              >
                Discard
              </button>
            )}
            {!item.read_at && (
              <button
                className="secondary"
                disabled={busy}
                onClick={() => markRead(item.id)}
              >
                Mark as read
              </button>
            )}
          </div>
          {!item.imported && item.discard_blocker && (
            <p className="muted">{item.discard_blocker}</p>
          )}
        </article>
      ))}
    </section>
  );
}
