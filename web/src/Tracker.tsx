import React, { useEffect, useState } from "react";

type Row = Record<string, any>;
type API = (path: string, body?: unknown, method?: string) => Promise<any>;
const label = (value: string) =>
  value.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
const columns = [
  "Company",
  "Role",
  "Date applied",
  "Status",
  "Last update",
  "Resume",
  "Cover letter",
  "Notes",
];

export function TrackerSync({
  id,
  api,
  onSaved,
}: {
  id: string;
  api: API;
  onSaved?: () => void;
}) {
  const [plan, setPlan] = useState<Row | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setPlan(null);
    setError("");
    setMessage("");
  }, [id]);
  async function preview() {
    setBusy(true);
    setError("");
    setMessage("");
    setPlan(null);
    try {
      setPlan(await api("/tracker/preview/" + id));
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function save() {
    if (!plan) return;
    setBusy(true);
    setError("");
    try {
      const result = await api("/tracker/update/" + id, {
        expected_hash: plan.expected_hash,
        reviewed_hash: plan.review_hash,
      });
      setMessage(
        result.message ||
          "Tracker saved. The backup, notes, checklist, and handoff are recorded.",
      );
      setPlan(null);
      onSaved?.();
    } catch (e) {
      setError(String(e));
      setPlan(null);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="tracker-sync">
      <button className="secondary" disabled={busy} onClick={preview}>
        {busy ? "Checking tracker…" : "Preview tracker update"}
      </button>
      {error && <p role="alert">{error}</p>}
      {message && <p role="status">{message}</p>}
      {plan && (
        <div>
          <h3>
            {plan.action === "already_recorded"
              ? "Already recorded"
              : plan.action === "insert"
                ? "Add application to tracker"
                : "Update application in tracker"}
          </h3>
          <p>{plan.reason}</p>
          <p className="muted tracker-path">{plan.tracker_path}</p>
          {plan.action !== "already_recorded" && (
            <>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Field</th>
                      <th>Current</th>
                      <th>After saving</th>
                    </tr>
                  </thead>
                  <tbody>
                    {columns.map((name, i) => (
                      <tr key={name}>
                        <th>{name}</th>
                        <td>{plan.before?.[i] || "Not recorded"}</td>
                        <td>{plan.after[i] || "Not recorded"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <h3>Tracker totals after saving</h3>
              <dl className="measurement-list">
                {Object.entries(plan.totals_after).map(([key, value]) => (
                  <div key={key}>
                    <dt>{label(key)}</dt>
                    <dd>
                      {String(plan.totals_before[key])} → {String(value)}
                    </dd>
                  </div>
                ))}
              </dl>
              <p>
                Saving updates this row and the spreadsheet totals. It also
                writes generated records under _Agent and saves backups for
                recovery.
              </p>
              {!plan.writes_enabled && (
                <p>
                  Tracker writes are disabled. Enable them on Metrics &amp;
                  tracker, then load a fresh preview.
                </p>
              )}
              <button disabled={busy || !plan.writes_enabled} onClick={save}>
                Save reviewed tracker update
              </button>
              <button
                className="secondary"
                disabled={busy}
                onClick={() => setPlan(null)}
              >
                Cancel
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function Bars({
  values,
  empty,
}: {
  values: Record<string, number>;
  empty: string;
}) {
  const max = Math.max(1, ...Object.values(values));
  return Object.keys(values).length ? (
    <ul className="metric-bars">
      {Object.entries(values).map(([name, value]) => (
        <li key={name}>
          <div>
            <span>{label(name)}</span>
            <strong>{value}</strong>
          </div>
          <div className="metric-track" aria-hidden="true">
            <span style={{ width: `${(value / max) * 100}%` }} />
          </div>
        </li>
      ))}
    </ul>
  ) : (
    <p className="muted">{empty}</p>
  );
}

export function TrackerPage({
  api,
  candidate,
  openApplication,
  refreshOverview,
}: {
  api: API;
  candidate: string;
  openApplication: (id: string) => void;
  refreshOverview: () => void;
}) {
  const [data, setData] = useState<Row | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [sort, setSort] = useState({ key: "company", descending: false });
  const [selected, setSelected] = useState("");
  async function load() {
    setError("");
    try {
      setData(await api("/tracker/metrics?candidate=" + candidate));
    } catch (e) {
      setError(String(e));
    }
  }
  useEffect(() => {
    setData(null);
    setSelected("");
    setMessage("");
    setStatus("");
    setQuery("");
    void load();
  }, [candidate]);
  async function act(fn: () => Promise<any>, success: string) {
    setBusy(true);
    setError("");
    try {
      await fn();
      setMessage(success);
      await load();
      refreshOverview();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  const records: Row[] = data?.tracker?.records || [];
  const filtered = records.filter(
    (r) =>
      (!status || r.status === status) &&
      [r.company, r.role, r.requisition, r.notes]
        .join(" ")
        .toLowerCase()
        .includes(query.toLowerCase()),
  );
  filtered.sort((a, b) => {
    let comparison: number;
    if (sort.key === "date_applied")
      comparison =
        (Date.parse(a[sort.key]) || 0) - (Date.parse(b[sort.key]) || 0);
    else
      comparison = String(a[sort.key]).localeCompare(
        String(b[sort.key]),
        undefined,
        { numeric: true },
      );
    return sort.descending ? -comparison : comparison;
  });
  const measures = Object.fromEntries(
    (data?.measurements || []).map((m: Row) => [m.name, m.value]),
  ) as Record<string, number>;
  const syncable = (data?.applications || []).filter((a: Row) =>
    [
      "submitted",
      "unresolved",
      "deferred",
      "prepared",
      "closed",
      "skipped",
    ].includes(a.state),
  );
  return (
    <>
      <section className="panel">
        <div className="tracker-heading">
          <div>
            <h2>Spreadsheet history</h2>
            <p>Spreadsheet history and current application progress.</p>
          </div>
          <button className="secondary" disabled={busy} onClick={load}>
            Refresh metrics
          </button>
        </div>
        {error && <p role="alert">{error}</p>}
        {message && <p role="status">{message}</p>}
        {!data && !error && <p>Reading tracker…</p>}
        {data && (
          <>
            <p className="muted tracker-path">Source: {data.tracker_path}</p>
            <p className="muted">
              Read {new Date(data.read_at).toLocaleString()}.{" "}
              {data.modified_at &&
                `File updated ${new Date(data.modified_at).toLocaleString()}.`}
            </p>
            {!data.tracker && (
              <p>
                No tracker found in this workspace. Connect the workspace or
                load the fixture demonstration first.
              </p>
            )}
            {data.tracker && (
              <>
                <div className="stats compact">
                  {Object.entries(data.tracker.totals).map(([key, value]) => (
                    <article key={key}>
                      <strong>{String(value)}</strong>
                      <span>{label(key)}</span>
                    </article>
                  ))}
                </div>
                <p className="muted">
                  Submitted includes active applications and rejections. Offers
                  are included in active. Prepared includes deferred
                  applications that have not been submitted. These totals
                  overlap; they are not a funnel.
                </p>
              </>
            )}
          </>
        )}
      </section>
      {data && (
        <>
          <div className="metrics-grid">
            <section className="panel">
              <h2>Tracker status</h2>
              <Bars
                values={data.status_counts}
                empty="No tracker records yet."
              />
            </section>
            <section className="panel">
              <h2>Applications by month</h2>
              <p className="muted">
                One record per recorded application date, including later
                rejections. {data.missing_dates} records without a date are
                excluded.
              </p>
              <Bars
                values={data.monthly_applications}
                empty="No application dates recorded."
              />
              {data.unparsed_dates > 0 && (
                <p>{data.unparsed_dates} dates could not be read.</p>
              )}
            </section>
          </div>
          <section className="panel tracker-records">
            <h2>Spreadsheet records</h2>
            <div className="tracker-filters">
              <label>
                Search records
                <input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Employer, role, requisition, or note"
                />
              </label>
              <label>
                Status
                <select
                  aria-label="Status"
                  value={status}
                  onChange={(e) => setStatus(e.target.value)}
                >
                  <option value="">All statuses</option>
                  {Object.keys(data.status_counts).map((s) => (
                    <option key={s}>{s}</option>
                  ))}
                </select>
              </label>
            </div>
            <p>
              {filtered.length} of {records.length} records
            </p>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    {[
                      ["company", "Employer"],
                      ["role", "Role / requisition"],
                      ["date_applied", "Applied"],
                      ["status", "Status"],
                    ].map(([key, name]) => (
                      <th
                        key={key}
                        aria-sort={
                          sort.key === key
                            ? sort.descending
                              ? "descending"
                              : "ascending"
                            : "none"
                        }
                      >
                        <button
                          className="secondary"
                          onClick={() =>
                            setSort({
                              key,
                              descending:
                                sort.key === key ? !sort.descending : false,
                            })
                          }
                        >
                          {name}
                          {sort.key === key
                            ? sort.descending
                              ? " ↓"
                              : " ↑"
                            : ""}
                        </button>
                      </th>
                    ))}
                    <th>Record details</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((r, i) => (
                    <tr key={`${r.sheet}-${r.row}-${i}`}>
                      <td>{r.company}</td>
                      <td>{r.role}</td>
                      <td>{r.date_applied || "Not recorded"}</td>
                      <td>{r.status}</td>
                      <td>
                        <details>
                          <summary>Notes and documents</summary>
                          <p className="tracker-note">
                            {r.notes || "No notes recorded."}
                          </p>
                          <p>Resume: {r.resume || "Not recorded"}</p>
                          <p>
                            Cover letter: {r.cover_letter || "Not recorded"}
                          </p>
                          <p>Updated: {r.last_update || "Not recorded"}</p>
                          <small>Sheet: {r.sheet}</small>
                        </details>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!filtered.length && <p>No records match these filters.</p>}
          </section>
          <div className="metrics-grid">
            <section className="panel">
              <h2>Workflows in this app</h2>
              <p className="muted">
                Imported spreadsheet records are excluded. Some workflows also
                appear in the tracker, so these counts should not be added to
                tracker totals.
              </p>
              <div className="stats compact">
                {Object.entries(data.evidence_counts).map(([key, value]) => (
                  <article key={key}>
                    <strong>{String(value)}</strong>
                    <span>{key}</span>
                  </article>
                ))}
              </div>
              <Bars
                values={data.workflow_counts}
                empty="No new workflows yet."
              />
              <div className="tracker-app-links">
                {data.applications.map((a: Row) => (
                  <button
                    className="secondary"
                    key={a.id}
                    onClick={() => openApplication(a.id)}
                  >
                    {a.employer} · {a.requisition} · {label(a.state)}
                  </button>
                ))}
              </div>
            </section>
            <section className="panel">
              <h2>Memory and document search</h2>
              <p className="muted">
                Service totals since counters were initialized, across real and
                fixture workspaces. Zero means no recorded events, not proof of
                correctness.
              </p>
              <dl className="measurement-list">
                {[
                  ["memory_hits", "Confirmed memory answers"],
                  ["cache_hits", "Cached answers"],
                  ["retrieval_calls_avoided", "Document searches avoided"],
                  ["retrieval_calls", "Document searches"],
                  ["retrieval_failures", "Searches without evidence"],
                  ["stale_answers_blocked", "Stale answers blocked"],
                  ["memory_conflicts", "Conflicting facts"],
                ].map(([key, name]) => (
                  <div key={key}>
                    <dt>{name}</dt>
                    <dd>{measures[key] || 0}</dd>
                  </div>
                ))}
                <div>
                  <dt>Average answer lookup</dt>
                  <dd>
                    {measures.answer_requests
                      ? `${Math.round((measures.answer_latency_ms || 0) / measures.answer_requests)} ms`
                      : "Not measured"}
                  </dd>
                </div>
              </dl>
            </section>
          </div>
          <section className="panel">
            <h2>Tracker updates</h2>
            <p>
              {data.writes_enabled
                ? "Writes enabled. Each update still requires a reviewed preview."
                : "Writes disabled. Previews are available without changing the spreadsheet."}{" "}
              Enabling writes does not queue or save an update.
            </p>
            {candidate === "real" && (
              <button
                className="secondary"
                disabled={busy}
                onClick={() =>
                  act(
                    () =>
                      api(
                        "/settings/tracker-writes",
                        { enabled: !data.writes_enabled },
                        "PUT",
                      ),
                    data.writes_enabled
                      ? "Tracker writes disabled."
                      : "Tracker writes enabled. Choose an application and review its update.",
                  )
                }
              >
                {data.writes_enabled
                  ? "Disable tracker writes"
                  : "Enable writes to this tracker"}
              </button>
            )}
            <label>
              Application to sync
              <select
                aria-label="Application to sync"
                value={selected}
                onChange={(e) => setSelected(e.target.value)}
              >
                <option value="">Choose an application</option>
                {syncable.map((a: Row) => (
                  <option key={a.id} value={a.id}>
                    {a.employer} · {a.requisition} · {label(a.state)}
                  </option>
                ))}
              </select>
            </label>
            {selected && (
              <TrackerSync
                key={selected}
                id={selected}
                api={api}
                onSaved={() => {
                  void load();
                  refreshOverview();
                }}
              />
            )}
            <details>
              <summary>Recovery and conflicts</summary>
              <p>
                If a tracker save was interrupted, recovery finishes its
                generated records only when their saved hashes still match.
                External edits are preserved.
              </p>
              <button
                className="secondary"
                disabled={busy || !data.writes_enabled}
                onClick={() =>
                  act(async () => {
                    const results = await api(
                      "/tracker/recover?candidate=" + candidate,
                      {},
                    );
                    const conflicts = results.filter((r: Row) => !r.recovered);
                    if (conflicts.length)
                      throw new Error(
                        conflicts.map((r: Row) => r.reason).join("; "),
                      );
                  }, "Recovery checked. No pending conflicts remain in the journals.")
                }
              >
                Check pending tracker recovery
              </button>
              {data.conflicts.map((c: Row) => (
                <div key={c.id}>
                  <p>{label(c.kind)}</p>
                  <pre>{JSON.stringify(c.detail, null, 2)}</pre>
                  <button
                    disabled={busy}
                    onClick={() =>
                      act(
                        () =>
                          api("/tracker/conflicts/" + c.id + "/resolve", {
                            choice: "ods",
                          }),
                        "Spreadsheet change accepted. Review a new preview before syncing.",
                      )
                    }
                  >
                    Accept spreadsheet version
                  </button>
                </div>
              ))}
            </details>
          </section>
        </>
      )}
    </>
  );
}
