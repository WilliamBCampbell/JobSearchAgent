import React, { useEffect, useState } from "react";
type Row = Record<string, any>;

export function SearchAgent({
  api,
  run,
  openApplication,
  chooseReal,
}: {
  api: (path: string, body?: unknown) => Promise<any>;
  run: (action: () => Promise<any>, notice?: string) => any;
  openApplication: (id: string) => void;
  chooseReal: () => void;
}) {
  const [track, setTrack] = useState("software");
  const [hunt, setHunt] = useState<Row | null>(null);
  const [history, setHistory] = useState<Row[]>([]);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);
  useEffect(() => {
    let alive = true;
    api("/hunts?candidate=real")
      .then(async (rows) => {
        if (!alive) return;
        setHistory(rows);
        if (rows[0]) {
          const h = await api("/hunts/" + rows[0].id);
          if (alive) setHunt(h);
        }
      })
      .catch((e) => alive && setError(e.message));
    return () => {
      alive = false;
    };
  }, []);
  useEffect(() => {
    if (!hunt || ["complete", "cancelled"].includes(hunt.status)) return;
    let alive = true;
    const timer = setInterval(
      () =>
        api("/hunts/" + hunt.id)
          .then((h) => alive && setHunt(h))
          .catch((e) => alive && setError(e.message)),
      3000,
    );
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [hunt?.id, hunt?.status]);
  const ready = hunt?.leads.filter((l: Row) => l.status === "ready") || [];
  const stage = hunt?.checkpoint?.stage;
  const done = hunt && ["complete", "cancelled"].includes(hunt.status);
  return (
    <>
      <section className="panel">
        <h2>Find and prepare the best matches</h2>
        <p>
          The agent searches Indeed and LinkedIn, checks employer postings and
          prior applications, evaluates the requirements, and prepares up to
          five qualifying roles for review.
        </p>
        <div className="inline-form">
          <label>
            Search track
            <select
              aria-label="Agent search track"
              value={track}
              onChange={(e) => setTrack(e.target.value)}
            >
              <option value="software">Software engineering</option>
              <option value="applied_ai">Applied AI engineering</option>
              <option value="solutions">Solutions engineering</option>
            </select>
          </label>
          <button
            disabled={starting || !!(hunt && !done)}
            onClick={() =>
              run(async () => {
                setStarting(true);
                try {
                  const h = await api("/hunts", { candidate: "real", track });
                  chooseReal();
                  setHunt(h);
                  setError("");
                } finally {
                  setStarting(false);
                }
              }, "The agent is searching online. You can leave this screen while it works.")
            }
          >
            Find my best matches
          </button>
        </div>
        <p>
          Each run checks up to 60 discoveries and evaluates up to 12 verified
          openings. Codex receives selected career evidence and the posting.
          Your original documents and tracker stay unchanged.
        </p>
        <p>
          The agent inspects supported employer forms and prepares answers for
          review. Missing facts and unsupported portals appear as follow-up
          items. Sending to real employers is disabled in this deployment.
        </p>
        {error && <p role="alert">{error}</p>}
      </section>
      {hunt && (
        <section className="panel" aria-label="Search agent progress">
          <h2>
            {hunt.status === "paused"
              ? "Search paused"
              : stage || "Starting search"}
          </h2>
          <p>
            {hunt.checkpoint?.discovered ?? 0} discoveries ·{" "}
            {hunt.leads.filter((l: Row) => l.application_id).length} verified
            openings · {ready.length} prepared recommendations
          </p>
          {!done && (
            <div className="actions">
              <button
                className="secondary"
                onClick={() =>
                  run(async () =>
                    setHunt(
                      await api(
                        `/hunts/${hunt.id}/${hunt.status === "paused" ? "resume" : "pause"}`,
                        {},
                      ),
                    ),
                  )
                }
              >
                {hunt.status === "paused"
                  ? "Resume search"
                  : "Pause after current task"}
              </button>
              <button
                className="secondary"
                onClick={() =>
                  run(async () =>
                    setHunt(await api(`/hunts/${hunt.id}/cancel`, {})),
                  )
                }
              >
                Cancel search
              </button>
            </div>
          )}
          {hunt.jobs?.some(
            (j: Row) => j.kind === "hunt" && j.status === "failed",
          ) && (
            <p role="alert">
              The coordinator stopped:{" "}
              {
                hunt.jobs.find(
                  (j: Row) => j.kind === "hunt" && j.status === "failed",
                ).error
              }
              . Resume retries from its saved checkpoint.
            </p>
          )}
          {hunt.jobs?.some(
            (j: Row) => j.kind === "hunt" && j.status === "failed",
          ) && (
            <button
              onClick={() =>
                run(async () =>
                  setHunt(await api(`/hunts/${hunt.id}/resume`, {})),
                )
              }
            >
              Retry from checkpoint
            </button>
          )}
          {done && ready.length === 0 && (
            <p>
              No applications are ready for review from this run. The evidence
              and exclusions below explain the result; the agent does not fill
              the shortlist with weaker matches.
            </p>
          )}
          {hunt.checkpoint?.manifest && (
            <details>
              <summary>Source results and warnings</summary>
              <pre>{JSON.stringify(hunt.checkpoint.manifest, null, 2)}</pre>
            </details>
          )}
        </section>
      )}
      <div className="match-grid">
        {ready.map((lead: Row) => (
          <article className="panel match" key={lead.id}>
            <p className="eyebrow">
              Prepared for review · {lead.score.total}/100
            </p>
            <h2>{lead.role}</h2>
            <h3>{lead.employer}</h3>
            <p>Requisition {lead.requisition}</p>
            <p>
              {lead.score.ohio === "confirmed"
                ? "Ohio eligibility confirmed"
                : "U.S. remote; Ohio eligibility needs confirmation"}
            </p>
            <p>
              Base salary:{" "}
              {lead.score.compensation?.base
                ? JSON.stringify(lead.score.compensation.base)
                : "Not established"}
            </p>
            <details>
              <summary>Deductions and requirements</summary>
              <pre>
                {JSON.stringify(
                  {
                    deductions: lead.score.deductions,
                    requirements: lead.score.requirements,
                  },
                  null,
                  2,
                )}
              </pre>
            </details>
            <button
              onClick={() => {
                chooseReal();
                openApplication(lead.application_id);
              }}
            >
              Review application package
            </button>
            {lead.payload?.package?.files
              ?.filter((f: Row) => f.kind === "resume.pdf")
              .map((f: Row) => (
                <a
                  key={f.id}
                  href={"/api/artifacts/" + f.id + "?preview=true"}
                  target="_blank"
                  rel="noreferrer"
                >
                  Preview resume PDF
                </a>
              ))}
            <a
              href={lead.official.application_url}
              target="_blank"
              rel="noreferrer"
            >
              Official opening
            </a>
          </article>
        ))}
      </div>
      {hunt && (
        <section className="panel">
          <details>
            <summary>
              All discoveries and decisions ({hunt.leads.length})
            </summary>
            <table>
              <thead>
                <tr>
                  <th>Employer / role</th>
                  <th>Search outcome / current fit</th>
                  <th>Search decision</th>
                </tr>
              </thead>
              <tbody>
                {hunt.leads.map((l: Row) => (
                  <tr key={l.id}>
                    <td>
                      {l.discovery.company}
                      <small>{l.discovery.title}</small>
                    </td>
                    <td>
                      {l.status.replaceAll("_", " ")}
                      {l.score?.total !== undefined && (
                        <small>Current fit: {l.score.total}/100 · {l.score.band}</small>
                      )}
                    </td>
                    <td>
                      {l.reason || ""}
                      {l.application_id && l.score?.scores && (
                        <div className="actions">
                          <button className="secondary" onClick={() => {
                            chooseReal();
                            openApplication(l.application_id);
                          }}>Review fit</button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </section>
      )}
      {history.length > 1 && (
        <section className="panel">
          <label>
            Previous search
            <select
              value={hunt?.id || ""}
              onChange={(e) =>
                run(async () => setHunt(await api("/hunts/" + e.target.value)))
              }
            >
              {history.map((h) => (
                <option key={h.id} value={h.id}>
                  {new Date(h.created_at).toLocaleString()} · {h.request.track}{" "}
                  · {h.status}
                </option>
              ))}
            </select>
          </label>
        </section>
      )}
    </>
  );
}
