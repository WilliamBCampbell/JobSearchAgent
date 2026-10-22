import { answerText } from "./answerText";
import { SelectionReview } from "./SelectionReview";
import { TrackerPage, TrackerSync } from "./Tracker";
import { EmployerFormPlan, EmployerAnswerInput, AnswerScope } from "./ApplicationForm";
import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import { EvaluationDesk, ModelSettings } from "./Inference";
import { SearchAgent } from "./Hunt";
import { InterventionInbox } from "./Interventions";
import { RequirementReview, fitValidationMessages } from "./RequirementReview";
import { applicationColumns, defaultApplicationSort, sortApplications, type ApplicationSort, type ApplicationColumn } from "./applicationSorting";
import { latestPreparedDocuments } from "./preparedDocuments";
import { ApplicationSections, WorkflowSection } from "./ApplicationSections";
import { applicationJourney, ApplicationJourney, ManualPackageReview, hasGreenhouseBrowser } from "./ApplicationJourney";
import { PackageReview } from "./PackageReview";
import { CreateUserProfile, UserProfiles, profileUrl } from "./UserProfiles";
import { EmployerApplicationLink } from "./EmployerApplicationLink";
import { SubmissionHandoff } from "./SubmissionHandoff";

type Row = Record<string, any>;
let unlockToken = "";
async function api(
  path: string,
  body?: unknown,
  method?: string,
): Promise<any> {
  const r = await fetch(profileUrl("/api" + path), {
    method: method || (body === undefined ? "GET" : "POST"),
    headers: {
      "Content-Type": "application/json",
      ...(unlockToken ? { "X-JSA-Token": unlockToken } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    let data;
    try {
      data = await r.json();
    } catch {
      data = { detail: r.statusText };
    }
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail),
    );
  }
  return r.json();
}
const pretty = (x: unknown) => JSON.stringify(x, null, 2);
const title = (s: string) =>
  s === "skipped" ? "Discarded" : s.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
function Badge({ value }: { value: string }) {
  return <span className={"badge " + value}>{title(value)}</span>;
}
function Json({ data, label = "Evidence" }: { data: any; label?: string }) {
  return (
    <details>
      <summary>{label}</summary>
      <pre>{pretty(data)}</pre>
    </details>
  );
}
function App() {
  const [applicationSort, setApplicationSort] = useState<ApplicationSort>(defaultApplicationSort);
  const sortQueue = (column: ApplicationColumn) => setApplicationSort(previous => ({
    column,
    direction: previous?.column === column
      ? (previous.direction === "ascending" ? "descending" : "ascending")
      : (column === "fit" ? "descending" : "ascending"),
  }));
  const [ready, setReady] = useState(false),
    [token, setToken] = useState(""),
    [page, setPage] = useState("overview"),
    [candidate, setCandidate] = useState("real");
  const [overview, setOverview] = useState<Row>({
      applications: [],
      jobs: [],
      metrics: [],
      conflicts: [],
    }),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false);
  const [facts, setFacts] = useState<Row[]>([]),
    [docs, setDocs] = useState<Row[]>([]),
    [appId, setAppId] = useState(""),
    [application, setApplication] = useState<Row | null>(null),
    [review, setReview] = useState<Row | null>(null),
    [reviewError, setReviewError] = useState<string | null>(null),
    [editingQuestion, setEditingQuestion] = useState<string | null>(null);
  const [searchId, setSearchId] = useState(""),
    [results, setResults] = useState<Row | null>(null),
    [track, setTrack] = useState("software"),
    [query, setQuery] = useState(""),
    [evidence, setEvidence] = useState<Row | null>(null);
  const [settings, setSettings] = useState<Row | null>(null),
    [audit, setAudit] = useState<Row[]>([]),
    [correction, setCorrection] = useState<Row | null>(null),
    [corrected, setCorrected] = useState("");
  const [receiptNote, setReceiptNote] = useState(""),
    [answerDrafts, setAnswerDrafts] = useState<Record<string, any>>({}),
    [contextDrafts, setContextDrafts] = useState<Record<string, string>>({}),
    [researchDrafts, setResearchDrafts] = useState<Record<string, string>>({}),
    [scopeDrafts, setScopeDrafts] = useState<Record<string, string>>({}),
    [focusQuestionId, setFocusQuestionId] = useState<string | null>(null),
    [manualReceiptOpen, setManualReceiptOpen] = useState(false);
  const [notSubmittedRun, setNotSubmittedRun] = useState("");
  useEffect(() => setManualReceiptOpen(false), [appId]);
  const navigateSection = (id: string) => {
    const node = document.getElementById(id);
    if (!node) return;
    let parent: HTMLElement | null = node;
    while (parent) {
      if (parent instanceof HTMLDetailsElement) parent.open = true;
      parent = parent.parentElement;
    }
    node.scrollIntoView({ block: "start" });
  };
  useEffect(() => {
    if (!focusQuestionId || page !== "application") return;
    const panel = document.getElementById("question-" + focusQuestionId);
    if (!panel) return;
    navigateSection("question-" + focusQuestionId);
    const input = panel.querySelector<HTMLElement>(
      "form input:not([type=radio]), form textarea, form select",
    );
    const target = input || panel;
    target.scrollIntoView({ block: "center" });
    target.focus({ preventScroll: true });
    setFocusQuestionId(null);
  }, [focusQuestionId, application, page]);
  const run = async (task: () => Promise<any>, message = "") => {
    setBusy(true);
    setError("");
    try {
      const r = await task();
      if (message) setNotice(message);
      await refresh();
      return r;
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };
  const refreshTicket = useRef(0);
  const refresh = async () => {
    const ticket = ++refreshTicket.current;
    const [v, a, result] = await Promise.all([
      api("/overview?candidate=" + candidate),
      appId ? api("/applications/" + appId) : Promise.resolve(null),
      searchId ? api("/search/" + searchId + "/results") : Promise.resolve(null),
    ]);
    if (ticket !== refreshTicket.current) return;
    setOverview(v);
    if (a) {
      setApplication(a);
      setReview(a.review ?? null);
      setReviewError(a.review_error ?? null);
    }
    if (result) setResults(result);
  };
  useEffect(() => {
    const h = new URLSearchParams(location.hash.slice(1));
    const t = h.get("token");
    if (t) {
      unlockToken = t;
      history.replaceState(null, "", location.pathname + location.search);
      api("/session", {})
        .then(() => {
          unlockToken = "";
          setReady(true);
        })
        .catch((e) => setError(e.message));
    } else
      api("/overview")
        .then(() => setReady(true))
        .catch(() => {});
  }, []);
  useEffect(() => {
    if (!ready) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try { await refresh(); } catch (e) { if (!stopped) setError((e as Error).message); }
      if (!stopped) timer = setTimeout(poll, 2000);
    };
    void poll();
    return () => { stopped = true; clearTimeout(timer); refreshTicket.current++; };
  }, [ready, candidate, appId, searchId]);
  useEffect(() => {
    if (!ready) return;
    if (page === "facts") api("/facts?candidate=" + candidate).then(setFacts);
    if (page === "workspace")
      api("/documents?candidate=" + candidate).then(setDocs);
    if (page === "settings") api("/settings").then(setSettings);
    if (page === "audit") api("/audit").then(setAudit);
  }, [ready, page, candidate, notice]);
  const openApplication = (id: string) => {
    setAppId(id);
    setReview(null);
    setReviewError(null);
    setNotice("");
    setPage("application");
  };
  const action = (name: string, body: Row = {}) =>
    run(() => api("/applications/" + appId + "/" + name, body));
  const answerScope = (q: Row) => !q.allowed_scopes?.includes("global") ? "application" : scopeDrafts[q.id] || (
    q.allowed_scopes?.includes("global") && application?.payload.answers?.[q.key]?.scope === "global"
      ? "global" : "application"
  );
  const selectResult = (row: Row) =>
    run(async () => {
      const a = await api("/applications", {
        employer: row.company,
        role: row.title,
        requisition: row.requisition,
        track,
        source: row.sources,
      });
      setAppId(a.id);
      setPage("application");
      await api("/applications/" + a.id + "/verify", {});
      await api("/applications/" + a.id + "/fixture-score", {});
      return a;
    });
  const currentPackageIds = new Set<string>([
    ...(application?.payload.package?.files || []),
    ...(application?.payload.package?.previews || []),
  ].map((f: Row) => f.id));
  const currentArtifacts: Row[] = latestPreparedDocuments(
    application?.package_review?.version
      ? (application?.artifacts || []).filter((f: Row) => currentPackageIds.has(f.id))
      : application?.artifacts || [], currentPackageIds);
  const currentArtifactIds = new Set(currentArtifacts.map((f: Row) => f.id));
  const previousArtifacts = (application?.artifacts || []).filter((f: Row) =>
    !currentArtifactIds.has(f.id) && !f.kind.endsWith(".preview.png"));
  const documentPreviews = currentArtifacts.filter((f: Row) => f.kind.endsWith(".preview.png"));
  const activeJob = overview.jobs.find((j: Row) =>
    ["queued", "running"].includes(j.status),
  );
  const next: Record<string, string> = {
    discovered: "Verify the official opening.",
    verified: "Evaluate requirements against confirmed evidence.",
    scored: "Inspect the score and select this role.",
    selected: "Prepare the resume and cover letter.",
    preparing: "The worker is rendering your documents.",
    prepared:
      "Preview the package, choose attachments, and open the application.",
    filling:
      "The agent is inspecting the form and preparing confirmed answers.",
    missing_answers:
      "Manual answer handoff: the agent cannot reliably answer the questions below. Confirm them to resume preparation. Sending still requires your approval.",
    awaiting_challenge:
      "Complete verification in the visible browser, then resume. Codes stay out of records.",
    review:
      "Inspect the employer questions, prepared answers, and selected documents.",
    authorized: "The approved application is queued for its final action.",
    submission_attempt:
      "A submission attempt is recorded. Waiting for employer confirmation.",
    receipt_verification: "Checking the employer receipt.",
    submitted:
      "Receipt captured. Review the tracker update or record a follow-up.",
    unresolved:
      "No receipt is confirmed. Reconcile the outcome before any retry.",
    deferred:
      "This application is deferred. Its decision and evidence remain available.",
    manual_handoff:
      "Use the saved answers in your normal browser. Confirm the outcome here.",
  };
  if (!ready)
    return (
      <main className="unlock">
        <div className="brandmark">J</div>
        <p className="eyebrow">LOCAL WORKSPACE</p>
        <h1>
          Your search.
          <br />
          Your evidence.
        </h1>
        <p>
          Unlock the workspace with the local access token. Personal files stay
          on this computer unless you enable a model destination.
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            unlockToken = token;
            run(async () => {
              await api("/session", {});
              unlockToken = "";
              setReady(true);
            });
          }}
        >
          <label>
            Access token
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              autoComplete="off"
              required
            />
          </label>
          <button>Open workspace</button>
        </form>
        <p className="muted">
          Run <code>jobsearch open</code> to open an authenticated local
          session.
        </p>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
      </main>
    );
  return (
    <div className="shell">
      <aside>
        <div className="wordmark">
          <span className="brandmark">J</span>
          <div>
            Job Search<small>User profiles</small>
          </div>
        </div>
        <UserProfiles api={api} busy={busy} />
        <nav>
          {[
            ["overview", "Overview"],
            ["workspace", "Documents"],
            ["facts", "Profile & answers"],
            ["search", "Find roles"],
            ["applications", "Applications"],
            ["metrics", "Metrics & tracker"],
            ["notifications", `Needs attention (${overview.notifications?.applications || 0})`],
            ["evaluations", "Evaluations"],
            ["audit", "Activity"],
            ["settings", "Privacy & settings"],
          ].map(([key, label]) => (
            <button
              className={page === key ? "selected" : ""}
              key={key}
              onClick={() => setPage(key)}
            >
              {label}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <span className="dot" /> Local services
          <br />
          <small>
            {candidate === "real" ? "Your workspace" : "Fixture demonstration"}
          </small>
        </div>
      </aside>
      <main>
        <header>
          <div>
            <p className="eyebrow">EVIDENCE BEFORE ACTION</p>
            <h1>
              {
                (
                  {
                    overview: "Overview",
                    workspace: "Documents & sources",
                    facts: "Profile & confirmed answers",
                    search: "Find your next role",
                    evaluations: "Evidence & evaluations",
                    applications: "Application queue",
                    metrics: "Metrics & tracker",
                    notifications: "Needs your attention",
                    application: application?.employer || "Application",
                    audit: "Recorded activity",
                    settings: "Privacy & settings",
                  } as Row
                )[page]
              }
            </h1>
          </div>
        </header>
        <div className="privacy-strip">
          <span className="dot" />
          {!overview.provider
            ? "Loading workspace privacy settings."
            : overview.llm?.backend === "codex_exec" && overview.llm?.enabled
              ? `Selected evaluation evidence goes to OpenAI through Codex. ${overview.provider.provider === "fixture" ? "Document search stays local." : "Embedding destination: " + overview.provider.destination}`
              : overview.provider.provider === "fixture"
                ? "Offline fixture models. No document data leaves this computer."
                : `Model destination: ${overview.provider?.destination}. ${overview.provider?.enabled ? "Enabled by you." : "Disabled."}`}
          <span>
            {candidate === "real"
              ? (overview.real_tracker_writes_enabled ? "Career source documents stay unchanged. Tracker updates require your review." : "Career source documents stay unchanged. Tracker writes are disabled.")
              : "Synthetic candidate and employers. No real submissions."}
          </span>
        </div>
        {overview.notifications?.pending > 0 && page !== "notifications" && (
          <div className="attention-banner" role="status">
            <span>{overview.notifications.applications} applications need your attention · {overview.notifications.unread} unread notices</span>
            <button className="secondary" onClick={() => setPage("notifications")}>View notifications</button>
          </div>
        )}
        {page === "notifications" && (
          <InterventionInbox
            inbox={overview.notifications || {items: []}}
            busy={busy}
            openApplication={openApplication}
            markRead={(id) => run(() => api(`/notifications/${id}/read?candidate=${candidate}`, {}))}
            discard={(id) => run(() => api(`/applications/${id}/control`, {action: "discard", reason: "Discarded by the user from Needs attention"}), "Role discarded. Its history is retained.")}
          />
        )}
        {error && (
          <div className="error" role="alert">
            {error}
            <button onClick={() => setError("")}>Dismiss</button>
          </div>
        )}
        {notice && (
          <div className="notice" role="status">
            {notice}
            <button onClick={() => setNotice("")}>Dismiss</button>
          </div>
        )}
        {page === "overview" && (
          <>
            <section className="welcome">
              <div>
                <p className="eyebrow">CURRENT STEP</p>
                <h2>
                  {candidate === "real"
                    ? "Connect your existing evidence"
                    : "Walk through a fixture application"}
                </h2>
                <p>
                  {candidate === "real"
                    ? "Import the tracker and confirmation records, then inspect sources and corrections. The spreadsheet remains the authority for imported statuses."
                    : "Start with confirmed facts, compare roles, review a tailored package, and approve a local test submission."}
                </p>
                <div className="actions">
                  <button
                    disabled={busy}
                    onClick={() =>
                      run(
                        () =>
                          candidate === "real"
                            ? api("/workspace/import?candidate=real", {})
                            : api("/fixtures/seed", {}),
                        candidate === "real"
                          ? "Workspace import queued."
                          : "Fixture facts and documents are ready.",
                      )
                    }
                  >
                    {candidate === "real"
                      ? "Import existing workspace"
                      : "Load demonstration"}
                  </button>
                  <button
                    className="secondary"
                    onClick={() =>
                      setPage(candidate === "real" ? "workspace" : "search")
                    }
                  >
                    {candidate === "real"
                      ? "Inspect documents"
                      : "Find fixture roles"}
                  </button>
                </div>
              </div>
              <div className="step-list">
                {[
                  "Connect evidence",
                  "Check fit",
                  "Prepare & review",
                  "Approve & verify receipt",
                ].map((s, i) => (
                  <div key={s}>
                    <span>{i + 1}</span>
                    {s}
                  </div>
                ))}
              </div>
            </section>
            <section className="panel">
              <div className="section-title">
                <h2>Next actions</h2>
                {activeJob && <Badge value={activeJob.status} />}
              </div>
              {overview.applications
                .filter(
                  (a: Row) =>
                    !a.imported &&
                    !["submitted", "skipped", "closed"].includes(a.state),
                )
                .slice(0, 5)
                .map((a: Row) => (
                  <button
                    className="queue-row"
                    key={a.id}
                    onClick={() => openApplication(a.id)}
                  >
                    <div>
                      <strong>{a.employer}</strong>
                      <span>{a.role}</span>
                    </div>
                    <Badge value={a.state} />
                  </button>
                ))}
              {!overview.applications.some((a: Row) => !a.imported) && (
                <p className="muted">
                  Load the demonstration and search to create a new application.
                </p>
              )}
            </section>
            <button className="secondary" onClick={() => setPage("metrics")}>View metrics and tracker history</button>
            <JobList jobs={overview.jobs} run={run} />
          </>
        )}
        {page === "metrics" && <TrackerPage api={api} candidate={candidate} openApplication={openApplication} refreshOverview={() => { void refresh(); }} />}
        {page === "workspace" && (
          <>
            <section className="panel">
              <h2>Connected workspace</h2>
              <p>
                <code>{overview.workspace}</code>
              </p>
              <p>
                Import preserves originals and records file hashes, versions,
                extraction warnings, and source locations. PDF, DOCX, ODS, HTML,
                Markdown, text, JSON, EML, and MBOX are supported.
              </p>
              <div className="actions">
                <button
                  disabled={busy}
                  onClick={() =>
                    run(
                      () => api("/workspace/import?candidate=" + candidate, {}),
                      "Tracker and confirmed-record import queued.",
                    )
                  }
                >
                  Import tracker & confirmations
                </button>
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() =>
                    run(
                      () => api("/workspace/ingest?candidate=" + candidate, {}),
                      "Document ingestion queued.",
                    )
                  }
                >
                  Ingest all supported documents
                </button>
                <button
                  className="secondary"
                  onClick={() =>
                    run(async () =>
                      setDocs(await api("/documents?candidate=" + candidate)),
                    )
                  }
                >
                  Refresh sources
                </button>
              </div>
            </section>
            <JobList
              jobs={overview.jobs.filter((j: Row) =>
                ["ingest", "import"].includes(j.kind),
              )}
              run={run}
            />
            <section className="panel">
              <h2>Search evidence</h2>
              <form
                className="inline-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  run(async () =>
                    setEvidence(
                      await api("/retrieve", {
                        query,
                        candidate,
                        full_document: true,
                      }),
                    ),
                  );
                }}
              >
                <input
                  aria-label="Evidence question"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Find an API project or a later-page detail"
                  required
                />
                <button>Find sources</button>
              </form>
              {evidence && (
                <Json
                  data={evidence}
                  label="Retrieved passages and references"
                />
              )}
            </section>
            <section className="panel">
              <h2>Document versions</h2>
              <table>
                <thead>
                  <tr>
                    <th>Source</th>
                    <th>Kind</th>
                    <th>Extraction</th>
                  </tr>
                </thead>
                <tbody>
                  {docs.map((d) => (
                    <tr key={d.id}>
                      <td>
                        {d.path}
                        <small>{d.current_version?.slice(0, 12)}</small>
                      </td>
                      <td>{title(d.kind)}</td>
                      <td>
                        <Badge value={d.status || "pending"} />
                        {d.warnings?.length > 0 && (
                          <Json data={d.warnings} label="Extraction notes" />
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          </>
        )}
        {page === "facts" && (
          <>
            <section className="panel">
              <h2>Ask from memory first</h2>
              <form
                className="inline-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  run(async () =>
                    setEvidence(
                      await api("/answers/resolve", {
                        key: query,
                        candidate,
                        scope: "global",
                      }),
                    ),
                  );
                }}
              >
                <input
                  aria-label="Memory key"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="professional_name or postgresql_years"
                  required
                />
                <button>Check answer</button>
              </form>
              {evidence && (
                <Json data={evidence} label="Answer source and evidence" />
              )}
            </section>
            {correction && (
              <section className="panel correction">
                <h2>Correct {title(correction.key)}</h2>
                <p>
                  Scope: {correction.scope}. The previous fact remains in the
                  audit history. Dependent cached answers will be invalidated.
                </p>
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    run(async () => {
                      let value: any = corrected;
                      try {
                        value = JSON.parse(corrected);
                      } catch {}
                      await api("/facts", {
                        candidate,
                        kind: correction.kind,
                        key: correction.key,
                        value,
                        status: "confirmed",
                        scope: correction.scope,
                        provenance: {
                          type: "user_confirmation",
                          note: "Corrected through profile review",
                        },
                        supersedes: correction.id,
                      });
                      setCorrection(null);
                      setFacts(await api("/facts?candidate=" + candidate));
                    }, "Correction saved; dependent answers invalidated.");
                  }}
                >
                  <label>
                    Correct value
                    <textarea
                      value={corrected}
                      onChange={(e) => setCorrected(e.target.value)}
                      required
                    />
                  </label>
                  <button>Confirm correction</button>
                  <button
                    className="secondary"
                    type="button"
                    onClick={() => setCorrection(null)}
                  >
                    Cancel
                  </button>
                </form>
              </section>
            )}
            <section className="panel">
              <h2>Facts and their sources</h2>
              <p>
                Role-specific pay, travel, schedules, and consent stay with that
                application. Generated prose cannot establish a confirmed fact.
              </p>
              {facts.map((f) => (
                <article
                  className={"fact " + (!f.active ? "inactive" : "")}
                  key={f.id}
                >
                  <div className="section-title">
                    <strong>{title(f.key)}</strong>
                    <Badge value={f.status} />
                  </div>
                  <p>
                    {typeof f.value === "string" ? f.value : pretty(f.value)}
                  </p>
                  <small>
                    {f.scope === "global"
                      ? "Candidate-wide fact"
                      : `For ${overview.applications.find((a: Row) => a.id === f.scope)?.employer || "one application"} only`}
                  </small>
                  <Json
                    data={{
                      provenance: f.provenance,
                      evidence_versions: f.evidence_versions,
                      effective_from: f.effective_from,
                      effective_to: f.effective_to,
                      verified_at: f.verified_at,
                      supersedes: f.supersedes,
                    }}
                    label="Why this fact is available"
                  />
                  {f.active && (
                    <button
                      className="text-button"
                      onClick={() => {
                        setCorrection(f);
                        setCorrected(
                          typeof f.value === "string"
                            ? f.value
                            : pretty(f.value),
                        );
                      }}
                    >
                      Correct this fact
                    </button>
                  )}
                </article>
              ))}
            </section>
          </>
        )}
        {page === "search" && (
          <>
            <SearchAgent
              api={api}
              run={run}
              openApplication={openApplication}
              chooseReal={() => setCandidate("real")}
            />
            <details>
              <summary>Sample search for testing</summary>
              <section className="panel">
                <h2>Search across sources</h2>
                <p>
                  At most five primary recommendations per batch. Official
                  verification and requirement checks determine which roles
                  qualify.
                </p>
                <div className="inline-form">
                  <label>
                    Search track
                    <select
                      aria-label="Search track"
                      value={track}
                      onChange={(e) => setTrack(e.target.value)}
                    >
                      <option value="software">Software engineering</option>
                      <option value="applied_ai">Applied AI engineering</option>
                      <option value="solutions">Solutions engineering</option>
                    </select>
                  </label>
                  <button
                    disabled={busy}
                    onClick={() =>
                      run(async () => {
                        const j = await api("/search", {
                          track,
                          profile:
                            track === "solutions"
                              ? "solutions"
                              : track === "applied_ai"
                                ? "ai"
                                : "software_dev",
                          mode: "fixture",
                        });
                        setCandidate("fixture");
                        setAppId("");
                        setApplication(null);
                        setReview(null);
                        setResults(null);
                        setSearchId(j.job_id);
                      }, "Sample search queued in the fixture workspace.")
                    }
                  >
                    Search fixture sources
                  </button>
                  <button
                    className="secondary"
                    onClick={() =>
                      run(async () => setEvidence(await api("/search/health")))
                    }
                  >
                    Check source health
                  </button>
                </div>
                {candidate === "real" && (
                  <p>
                    This test uses sample listings in the fixture workspace. Use
                    Find my best matches above to search real openings.
                  </p>
                )}
                {evidence && (
                  <Json data={evidence} label="Source capabilities" />
                )}
              </section>
              <JobList
                jobs={overview.jobs.filter((j: Row) => j.kind === "search")}
                run={run}
              />
              {results && (
                <>
                  <section className="panel">
                    <h2>{results.total} discovery groups</h2>
                    <p>
                      Scraped city labels and remote flags do not establish
                      employer policy. LinkedIn's ignored remote filter and
                      partial rate-limit failure remain in the manifest.
                    </p>
                    <Json
                      data={results.manifest}
                      label="Queries, effective filters, warnings, and raw artifact paths"
                    />
                  </section>
                  <div className="match-grid">
                    {results.results.map((r: Row, i: number) => (
                      <article className="panel match" key={i}>
                        <p className="eyebrow">
                          {r.sources.map((s: Row) => s.source).join(" + ")}
                        </p>
                        <h2>{r.title}</h2>
                        <h3>{r.company}</h3>
                        <small>Requisition {r.requisition}</small>
                        <p>
                          {r.description ||
                            "Description missing. Verify the official posting."}
                        </p>
                        {r.duplicate_review ? (
                          <>
                            <Badge value="duplicate_review" />
                            <Json
                              data={r.prior_matches}
                              label="Prior applications"
                            />
                          </>
                        ) : (
                          <button
                            disabled={busy}
                            onClick={() => selectResult(r)}
                          >
                            Verify & inspect fit
                          </button>
                        )}
                      </article>
                    ))}
                  </div>
                </>
              )}
            </details>
          </>
        )}
        {page === "applications" && (
          <section className="panel">
            <h2>{overview.applications.length} applications</h2>
            {applicationSort.column === "state" && <p className="muted">
              {applicationSort.direction === "ascending" ? "Active, unsubmitted applications first" : "Inactive applications first"}, then highest fit score. Unscored applications appear last in each group.
            </p>}
            <table aria-label="Application queue">
              <thead>
                <tr>
                  {applicationColumns.map(({ key, label }) => (
                    <th key={key} scope="col" aria-sort={applicationSort?.column === key ? applicationSort.direction : "none"}>
                      <button className="sort-header" onClick={() => sortQueue(key)}>
                        {label} <span aria-hidden="true">{applicationSort?.column === key ? (applicationSort.direction === "ascending" ? "↑" : "↓") : "↕"}</span>
                      </button>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {sortApplications(overview.applications, applicationSort).map((a: Row) => (
                  <tr
                    key={a.id}
                    onClick={() => openApplication(a.id)}
                    className="clickable"
                  >
                    <td>
                      <strong>{a.employer}</strong>
                      <small>{a.role}</small>
                    </td>
                    <td>{a.requisition}</td>
                    <td>
                      <Badge value={a.state} />
                      {a.imported && <small>Imported record</small>}
                    </td>
                    <td>{a.score?.total ?? "Not scored"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        )}
        {page === "application" && application && (
          <ApplicationSections application={application} journey={applicationJourney(application, review, overview.real_submission_enabled, manualReceiptOpen)}>
            <section className="panel application-heading">
              <div>
                <p className="eyebrow">Requisition {application.requisition}</p>
                <h2>{application.role}</h2>
                <EmployerApplicationLink application={application} />
                {application.imported && <p>
                  {application.payload.form_plan &&
                  application.state === "awaiting_challenge"
                    ? "Employer verification needs your normal browser. Open the saved handoff below. No verification codes are recorded."
                    : next[application.state] ||
                      "Inspect the recorded status and evidence."}
                </p>}
              </div>
              <Badge
                value={application.paused ? "paused" : application.progress?.status === "blocked" ? "needs_attention" : application.progress?.status === "working" ? "working" : application.state}
              />
            </section>
            {!application.imported && (application.payload.package || application.payload.attachments?.length > 0) &&
              <SubmissionHandoff application={application} review={review} api={api} onSaved={() => { void refresh(); }} />}
            {!application.imported && <ApplicationJourney
              application={application} review={review} realSending={overview.real_submission_enabled}
              busy={busy} onAction={action} onNavigate={navigateSection}
              receiptOpen={manualReceiptOpen} onReceipt={() => {
                setManualReceiptOpen(true);
                setTimeout(() => navigateSection("receipt-section"), 0);
              }}
            />}
            {!application.imported && application.state === "scored" && application.selection_review && <WorkflowSection section="selection" summary="Role selection decision">
              <SelectionReview application={application} busy={busy} onSelect={(body) => action("select", body)} onNavigate={navigateSection} />
            </WorkflowSection>}
            {!application.imported && (
              <WorkflowSection section="controls" id="workflow-controls" summary="Other actions and handoff tools">
              <div className="actions controls">
                <button
                  className="secondary"
                  onClick={() =>
                    action("control", {
                      action: application.paused ? "resume" : "pause",
                    })
                  }
                >
                  {application.paused ? "Resume work" : "Pause work"}
                </button>
                {![
                  "submitted",
                  "skipped", "closed", "rejected", "follow_up",
                  "unresolved",
                  "submission_attempt",
                  "receipt_verification",
                ].includes(application.state) && (
                  <button
                    className="text-button"
                    disabled={busy || !application.can_discard}
                    title={application.discard_blocker || "Remove this role from active work; keep its history"}
                    onClick={() =>
                      action("control", {
                        action: "discard",
                        reason: "Discarded by the user in the guided workflow",
                      })
                    }
                  >
                    Discard
                  </button>
                )}
                {application.submission_mode !== "external" && (application.attempts?.length > 0 || ["unresolved", "receipt_verification"].includes(application.state)) && <button
                  className="text-button"
                  onClick={() =>
                    run(() => api("/applications/" + appId + "/reconcile", {}))
                  }
                >
                  Check receipt
                </button>}
                <button
                  className="text-button"
                  onClick={() =>
                    run(async () => {
                      await api("/applications/" + appId + "/handoff", {});
                      setNotice(
                        "Normal-browser handoff saved. Use the download link below.",
                      );
                    })
                  }
                >
                  Save normal-browser handoff
                </button>
                {application.handoff_ready && (
                  <a
                    href={profileUrl("/api/handoffs/" + appId)}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Open saved handoff
                  </a>
                )}
              </div>
              </WorkflowSection>
            )}
            {application.imported ? (
              <section className="panel">
                <h2>Imported record</h2>
                <p>
                  The ODS status is authoritative. Development actions cannot
                  change this application.
                </p>
                <Json
                  data={application.payload}
                  label="Tracker record and provenance"
                />
              </section>
            ) : (
              <>
                {application.state === "manual_handoff" && !application.attempts?.length && !hasGreenhouseBrowser(application) && <ManualPackageReview
                  application={application} review={review} error={reviewError} busy={busy} onNavigate={navigateSection}
                  onReview={async (hash) => {
                    const result = await run(() => api("/applications/" + appId + "/handoff-review", { confirm: true, reviewed_hash: hash }), "Package review saved. Continue on the employer’s site.");
                    if (result) navigateSection("application-journey");
                  }}
                />}
                {application.state === "verified" && application.candidate === "fixture" && (
                  <button
                    disabled={busy}
                    onClick={() => action("fixture-score")}
                  >
                    Use fixture score
                  </button>
                )}
                {application.state === "verified" && !application.progress && (
                  <button
                    id="evaluation-controls"
                    disabled={busy}
                    onClick={() =>
                      run(async () => {
                        await api("/llm/jobs", {
                          task: "fit_assessment",
                          candidate,
                          application_id: application.id,
                        });
                        setPage("evaluations");
                      }, "Fit evaluation queued.")
                    }
                  >
                    Evaluate fit with evidence
                  </button>
                )}
                {application.score?.scores && (
                  <WorkflowSection section="fit" id="role-fit" summary="Role fit and supporting evidence">
                  <section className="panel">
                    <div className="section-title">
                      <div>
                        <h2>
                          Fit assessment{" "}
                          <span className="score">
                            {application.score.total}/100
                          </span>
                        </h2>
                        <p>Scores are judgments, not hiring probabilities.</p>
                        {application.score.preference_bonus > 0 && <p>Nearby employer: +{application.score.preference_bonus} ranking points for a confirmed driving distance within 100 miles. Qualification gates still apply.</p>}
                      </div>
                      <Badge value={application.score.band} />
                    </div>
                    {application.score.requirements?.some((r: Row) => r.application_question) && <details><summary>Application questions to prepare</summary><p>These requests need answers, not qualification confirmations. The agent will use your career evidence and ask for missing details during answer preparation.</p><ul>{application.score.requirements.filter((r: Row) => r.application_question).map((r: Row) => <li key={r.text}>{r.text}</li>)}</ul></details>}
                    <div className="rubric">
                      {Object.entries(application.score.scores).map(
                        ([k, v]) => (
                          <div key={k}>
                            <span>{title(k)}</span>
                            <meter
                              min="0"
                              max={application.score.weights[k]}
                              value={Number(v)}
                            />
                            <strong>
                              {String(v)}/{application.score.weights[k]}
                            </strong>
                          </div>
                        ),
                      )}
                    </div>
                    {(application.state === "scored" ? [] : fitValidationMessages(application)).map((r: string) => (
                      <p className="gap" key={r}>
                        {r}
                      </p>
                    ))}
                    <RequirementReview key={application.id} application={application} api={api} run={run} jobs={application.jobs || []} />
                    <Json
                      data={application.score.requirements}
                      label="Requirement matrix, evidence, and deductions"
                    />
                    <Json
                      data={{
                        compensation: application.score.compensation,
                        ohio: application.score.ohio,
                        official: application.official,
                      }}
                      label="Compensation, Ohio eligibility, and official source"
                    />
                  </section>
                  </WorkflowSection>
                )}
                {["prepared", "review"].includes(application.state) && !application.payload.package?.version && (
                  <button
                    className="secondary"
                    disabled={busy}
                    onClick={() => action("prepare")}
                  >
                    Regenerate from current facts
                  </button>
                )}
                {application.package_review?.version && <WorkflowSection section="documents" summary={application.package_review?.reviewed ? "Reviewed documents and revision options" : "Document review"}>
                <PackageReview key={application.id} application={application} busy={busy}
                  onPreview={() => navigateSection("prepared-documents")}
                  onRevise={(version, notes) => run(() => api(`/applications/${appId}/package-revisions`, {version, notes}), "Your suggestions were saved. The agent is preparing a new document version.")}
                  onAccept={async (version) => {
                    const result = await run(() => api(`/applications/${appId}/package-review`, {version}), "Document review saved. Continue with the employer answers and final application review.");
                    if (result) navigateSection("application-journey");
                    return result;
                  }}
                />
                </WorkflowSection>}
                {application.artifacts?.length > 0 && (
                  <WorkflowSection section="documents" id="prepared-documents" summary="Document previews and attachment selection">
                  <section className="panel">
                    <h2>Prepared documents</h2>
                    <p>
                      {application.package_review?.version ? "Showing files from the current package version." : "Showing the latest file of each type."} Choose the documents to attach. A prepared cover letter is
                      not automatically included.
                    </p>
                    <div className="artifact-list">
                      {currentArtifacts.filter((f: Row) => !f.kind.endsWith(".preview.png")).map((f: Row) => (
                        <div key={f.id}>
                          <a
                            href={profileUrl("/api/artifacts/" + f.id)}
                            target="_blank"
                            rel="noreferrer"
                          >
                            {title(f.kind)}
                          </a>
                          {f.kind.endsWith(".pdf") && (
                            <label className="checkbox">
                              <input
                                type="checkbox"
                                checked={
                                  application.payload.attachments?.includes(
                                    f.id,
                                  ) || false
                                }
                                disabled={[
                                  "authorized",
                                  "submission_attempt",
                                  "receipt_verification",
                                  "submitted",
                                  "unresolved",
                                ].includes(application.state)}
                                onChange={(e) => {
                                  const ids =
                                    application.payload.attachments || [];
                                  run(() =>
                                    api(
                                      "/applications/" + appId + "/payload",
                                      {
                                        attachments: e.target.checked
                                          ? [...ids, f.id]
                                          : ids.filter(
                                              (id: string) => id !== f.id,
                                            ),
                                      },
                                      "PATCH",
                                    ),
                                  );
                                }}
                              />
                              Attach
                            </label>
                          )}
                          <small>
                            {f.hash.slice(0, 14)} ·{" "}
                            {f.immutable
                              ? "Submitted version locked"
                              : "Prepared version"}
                          </small>
                        </div>
                      ))}
                    </div>
                    {documentPreviews.map((f: Row) => (
                      <WorkflowSection key={f.id} section="documents" summary={f.kind.startsWith("resume") ? "Resume PDF preview" : "Cover letter PDF preview"}>
                        <img className="document-preview" alt={f.kind.startsWith("resume") ? "Rendered resume page" : "Rendered cover letter page"}
                          src={profileUrl("/api/artifacts/" + f.id + "?preview=true")} />
                      </WorkflowSection>
                    ))}
                    {documentPreviews.length === 0 && currentArtifacts.find(
                      (a: Row) => a.kind === "resume.html",
                    ) && (
                      <iframe
                        title="Resume preview"
                        sandbox=""
                        src={profileUrl(
                          "/api/artifacts/" +
                          currentArtifacts.find(
                            (a: Row) => a.kind === "resume.html",
                          )!.id +
                          "?preview=true"
                        )}
                      />
                    )}
                    {previousArtifacts.length > 0 && (
                      <details>
                        <summary>Previous document versions</summary>
                        <ul>{previousArtifacts.map((f: Row) => (
                          <li key={f.id}><a href={profileUrl("/api/artifacts/" + f.id)} target="_blank" rel="noreferrer">{title(f.kind)} · {f.hash.slice(0, 10)}</a>{application.payload.attachments?.includes(f.id) && " · Selected attachment"}</li>
                        ))}</ul>
                      </details>
                    )}
                  </section>
                  </WorkflowSection>
                )}
                {application.payload.form_plan && (
                  <WorkflowSection section="answers" id="prepared-answers" summary="Prepared answers and sources">
                  <EmployerFormPlan
                    application={application}
                    onCorrect={async (key) => {
                        const result = await run(() => api(
                          "/applications/" + appId + "/review-answer",
                          { key },
                        ));
                        if (!result) return;
                        setAnswerDrafts(previous => previous[result.question_id] !== undefined
                          ? previous
                          : { ...previous, [result.question_id]: result.current_value ?? "" });
                        setEditingQuestion(result.question_id);
                        setFocusQuestionId(result.question_id);
                    }}
                  />
                  </WorkflowSection>
                )}
                {application.questions
                  ?.filter(
                    (q: Row) =>
                      !q.answer_fact ||
                      q.needs_review ||
                      q.id === editingQuestion,
                  )
                  .map((q: Row) => (
                    <WorkflowSection section="question" key={q.id} summary={q.prompt} forceOpen={q.id === editingQuestion}>
                    <section className="panel question" id={"question-" + q.id} tabIndex={-1}>
                      <p className="eyebrow">
                        {q.id === editingQuestion || q.needs_review
                          ? "EDIT ANSWER"
                          : q.reason?.code === "personal_context_needed"
                          ? "YOUR PERSONAL PERSPECTIVE IS NEEDED"
                          : "YOUR ANSWER IS NEEDED"}
                      </p>
                      <h2>{q.prompt}</h2>
                      {q.reason?.message && <p>{q.reason.message}</p>}
                      {q.can_suggest && <div className="answer-suggestions">
                        <p>Suggest an answer using this profile’s confirmed records, the posting, and saved employer research.</p>
                        <details>
                          <summary>Add employer research links</summary>
                          <label>Public employer pages, one URL per line
                            <textarea aria-label="Employer research URLs" rows={3}
                              placeholder="https://company.example/products"
                              value={researchDrafts[q.id] || ""}
                              onChange={e => setResearchDrafts(previous => ({...previous, [q.id]: e.target.value}))} />
                          </label>
                          <p>Up to four pages. Their text becomes evidence for this application’s suggestions.</p>
                        </details>
                        <button type="button" className="secondary"
                          disabled={busy || application.jobs?.some((j: Row) => j.kind === "draft_form_answer" && j.input?.question_id === q.id && ["queued", "running"].includes(j.status))}
                          onClick={() => run(() => api(`/questions/${q.id}/draft-answer`, {
                            source_urls: (researchDrafts[q.id] || "").split("\n").map(s => s.trim()).filter(Boolean),
                          }), "Preparing a suggested answer for your review.")}>
                          {q.reason?.suggestion ? "Refresh suggestion" : "Suggest an answer"}
                        </button>
                      </div>}
                      {q.reason?.suggestion && <div className="answer-suggestion">
                        <h3>AI suggestion</h3>
                        <p>Review the details and wording before confirming your answer.</p>
                        <blockquote className="narrative-draft">{answerText(q.reason.suggestion.value)}</blockquote>
                        <button type="button" className="secondary" onClick={() => setAnswerDrafts(previous => ({...previous, [q.id]: q.reason.suggestion.value}))}>Use suggestion in my answer</button>
                      </div>}
                      {q.reason?.suggestion && q.reason?.employer_sources?.length > 0 && <details>
                        <summary>Employer sources for this suggestion</summary>
                        <ul>{q.reason.employer_sources.map((s: Row) => <li key={s.id}>
                          {s.url ? <a href={s.url} target="_blank" rel="noopener noreferrer">{s.title}</a> : s.title}
                        </li>)}</ul>
                      </details>}
                      {q.reason?.context?.length > 0 && (
                        <details>
                          <summary>
                            Career evidence available to the agent
                          </summary>
                          <ul>
                            {q.reason.context.map((item: Row) => (
                              <li key={item.fact_id}>
                                {item.text}
                                <small>{item.source}</small>
                              </li>
                            ))}
                          </ul>
                        </details>
                      )}
                      {q.reason?.sources_checked?.length > 0 && (
                        <details>
                          <summary>Workspace sources checked</summary>
                          <ul>
                            {q.reason.sources_checked.map((item: Row) => (
                              <li key={item.version_id}>
                                {item.path} · {item.locators.join(", ")}
                                {item.warnings?.length > 0 && (
                                  <small>{item.warnings.join("; ")}</small>
                                )}
                              </li>
                            ))}
                          </ul>
                        </details>
                      )}
                      {q.reason?.fact_ids?.length > 0 && (
                        <Json
                          data={q.reason.fact_ids}
                          label="Evidence needing review"
                        />
                      )}
                      <p>
                        Your confirmation replaces conflicting answers only
                        within the scope shown below.
                      </p>
                      <form
                        onSubmit={(e) => {
                          e.preventDefault();
                          run(async () => {
                            await api("/questions/" + q.id + "/answer", {
                              value:
                                answerDrafts[q.id] ??
                                q.reason?.suggestion?.value,
                              scope: answerScope(q),
                              ...(q.answer_guidance?.choice_mismatch ? {context: contextDrafts[q.id] ?? q.answer_context ?? ""} : {}),
                            });
                            setAnswerDrafts(previous => {
                              const next = { ...previous };
                              delete next[q.id];
                              return next;
                            });
                            setEditingQuestion(null);
                            if (
                              application.payload.form_plan &&
                              application.state === "review"
                            )
                              await api("/applications/" + appId + "/fill", {});
                          }, "Answer saved.");
                        }}
                      >
                        {q.answer_guidance?.choice_mismatch && <div className="answer-input">
                          <label>Examples and interview-demo details
                            <textarea aria-label="Examples and interview-demo details" rows={8} maxLength={12000}
                              placeholder="Project or tool; your contribution; what you can demonstrate; any limits on sharing."
                              value={contextDrafts[q.id] ?? q.answer_context ?? ""}
                              onChange={e => setContextDrafts(previous => ({...previous, [q.id]: e.target.value}))} />
                          </label>
                          <p>These notes stay with this application. They are not sent through the employer’s dropdown. The configured evaluation provider can use them when preparing answers for this application.</p>
                          <button type="button" className="secondary" disabled={busy || application.jobs?.some((j: Row) => j.kind === "draft_answer_context" && ["queued", "running"].includes(j.status))}
                            onClick={() => run(() => api(`/questions/${q.id}/draft-context`, {}), "Drafting examples from your confirmed career evidence.")}>Draft examples from my records</button>
                          {q.reason?.context_message && <p>{q.reason.context_message}</p>}
                          {q.reason?.context_suggestion && <><blockquote className="narrative-draft">{q.reason.context_suggestion.value}</blockquote>
                            <button type="button" className="secondary" onClick={() => setContextDrafts(previous => ({...previous, [q.id]: String(q.reason.context_suggestion.value)}))}>Use this draft in my notes</button></>}
                        </div>}
                        <div className="answer-input">
                          <p>{q.answer_guidance?.choice_mismatch ? "Choice to send to the employer" : "Your answer"}</p>
                          {application.payload.form_schema?.find(
                            (f: Row) => f.key === q.key && f.name,
                          ) ? (
                            <EmployerAnswerInput
                              field={{...application.payload.form_schema.find(
                                (f: Row) => f.key === q.key,
                              ), ...(q.answer_guidance?.narrative && !q.answer_guidance?.choice_mismatch && !q.answer_guidance?.single_line ? {type: "textarea"} : {})}}
                              value={
                                answerDrafts[q.id] ??
                                q.reason?.suggestion?.value
                              }
                              onChange={(value) =>
                                setAnswerDrafts({
                                  ...answerDrafts,
                                  [q.id]: value,
                                })
                              }
                            />
                          ) : q.key === "travel" ? (
                            <select
                              aria-label="Your answer"
                              required
                              value={answerDrafts[q.id] || ""}
                              onChange={(e) =>
                                setAnswerDrafts({
                                  ...answerDrafts,
                                  [q.id]: e.target.value,
                                })
                              }
                            >
                              <option value="">Choose for this role</option>
                              <option>Up to 10%</option>
                              <option>No travel</option>
                            </select>
                          ) : (
                            <input
                              aria-label="Your answer"
                              required
                              value={answerDrafts[q.id] || ""}
                              onChange={(e) =>
                                setAnswerDrafts({
                                  ...answerDrafts,
                                  [q.id]: e.target.value,
                                })
                              }
                            />
                          )}
                        </div>
                        <AnswerScope question={q} value={answerScope(q)}
                          onChange={(value) => setScopeDrafts(previous => ({ ...previous, [q.id]: value }))} />
                        <button disabled={busy}>Confirm answer</button>
                      </form>
                    </section>
                    </WorkflowSection>
                  ))}
                {review && ["review", "authorized", "submission_attempt", "receipt_verification", "awaiting_challenge", "unresolved", "submitted", "follow_up"].includes(application.state) && (
                  <WorkflowSection section="final-review" summary="Final application review">
                  <section className="panel final-review" id="final-review">
                    <p className="eyebrow">FINAL REVIEW</p>
                    <h2>
                      {application.candidate === "fixture"
                        ? "Approve this fixture application"
                        : "Review this application"}
                    </h2>
                    <p>
                      <strong>{review.payload.employer}</strong> ·{" "}
                      {review.payload.role} · {review.payload.requisition}
                    </p>
                    <table>
                      <tbody>
                        {Object.entries(review.payload.answers).map(
                          ([k, v]) => (
                            <tr key={k}>
                              <th>
                                {application.payload.form_schema?.find(
                                  (f: Row) => f.key === k,
                                )?.label || title(k)}
                              </th>
                              <td>
                                {answerText((v as Row).value)}
                                <Json
                                  data={(v as Row).provenance}
                                  label="Answer source"
                                />
                              </td>
                            </tr>
                          ),
                        )}
                      </tbody>
                    </table>
                    <h3>
                      {application.payload.form_plan
                        ? "Files selected for sending"
                        : "Files actually attached"}
                    </h3>
                    {application.payload.form_plan?.omitted_attachments?.map((f: Row) => (
                      <p key={f.kind}>{f.name} remains prepared but will not be attached. {f.reason}</p>
                    ))}
                    {review.payload.attachments.map((f: Row) => (
                      <p key={f.id}>
                        {f.name}
                        <small>SHA-256: {f.hash}</small>
                      </p>
                    ))}
                    <h3>Material consents</h3>
                    <p>
                      {Object.keys(review.payload.consents).length
                        ? pretty(review.payload.consents)
                        : "No additional consents recorded."}
                    </p>
                    <p>Review these answers and document versions, then create a Codex submission prompt above. The app will not upload or submit them.</p>
                  </section>
                  </WorkflowSection>
                )}
              </>
            )}
            {application.receipts?.length > 0 && (
              <WorkflowSection section="receipt" summary="Employer confirmation"><section className="panel receipt" id="receipt-section">
                <p className="eyebrow">RECEIPT CONFIRMED</p>
                <h2>Application received</h2>
                {application.receipts.map((r: Row) => (
                  <div key={r.id}>
                    <p>{r.evidence.text}</p>
                    <small>{r.created_at}</small>
                    <Json data={r.evidence} label="Receipt evidence" />
                  </div>
                ))}
                {!application.imported && <TrackerSync id={appId} api={api} onSaved={() => { void refresh(); }} />}
              </section></WorkflowSection>
            )}
            {application.submission_recovery?.can_confirm_not_submitted && (
              <WorkflowSection section="outcome" summary="Resolve submission outcome"><section className="panel" id="submission-outcome">
                <h2>Not submitted</h2>
                <p>Greenhouse stopped at verification. Use this option only if you did not finish verification or submit this application in another browser. A verification-code email is not an application receipt.</p>
                <label className="checkbox">
                  <input type="checkbox"
                    checked={notSubmittedRun === application.submission_recovery.run_id}
                    onChange={(e) => setNotSubmittedRun(e.target.checked ? application.submission_recovery.run_id : "")} />
                  I did not complete verification or submit this application elsewhere.
                </label>
                <button disabled={busy || notSubmittedRun !== application.submission_recovery.run_id}
                  onClick={() => {
                    const recovery = application.submission_recovery;
                    action("confirm-not-submitted", {
                      confirm: true, attempt_id: recovery.attempt_id,
                      run_id: recovery.run_id, reviewed_hash: recovery.reviewed_hash,
                    });
                    setNotSubmittedRun("");
                  }}>
                  Confirm not submitted
                </button>
                <p>This records the outcome. Create an updated Codex handoff afterward.</p>
              </section></WorkflowSection>
            )}
            {((application.state === "unresolved" && !application.submission_recovery?.can_retry) || (application.state === "manual_handoff" && (manualReceiptOpen || application.attempts?.length > 0))) && (
              <WorkflowSection section="receipt" summary="Employer confirmation"><section className="panel" id="receipt-section">
                <h2>Record employer confirmation</h2>
                <p>
                  Use this only when the employer confirmed receipt of your application. Record that confirmation page or email. A verification-code email does not count. If the outcome is unknown, leave it unresolved.
                </p>
                <label>
                  Receipt confirmed by you
                  <textarea
                    value={receiptNote}
                    onChange={(e) => setReceiptNote(e.target.value)}
                    placeholder="Describe the employer receipt and where you saw it"
                  />
                </label>
                <button
                  disabled={!receiptNote || busy}
                  onClick={() =>
                    action("confirm-receipt", {
                      confirm: true,
                      note: receiptNote,
                    })
                  }
                >
                  I confirm employer receipt
                </button>
              </section></WorkflowSection>
            )}
            <WorkflowSection section="activity" id="work-progress" summary="Agent activity and submission records">
            <Json
              data={{
                approvals: application.approvals,
                attempts: application.attempts,
              }}
              label="Approval and attempt records"
            />
            <JobList
              jobs={application.jobs || []}
              run={run}
            />
            </WorkflowSection>
          </ApplicationSections>
        )}
        {page === "audit" && (
          <>
            <section className="panel">
              <h2>Operational evidence</h2>
              <p>
                Recorded actions, source outcomes, and state changes. Private
                model reasoning and verification codes are not logged.
              </p>
              <table>
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Event</th>
                    <th>Evidence</th>
                  </tr>
                </thead>
                <tbody>
                  {audit.map((a) => (
                    <tr key={a.seq}>
                      <td>{new Date(a.at).toLocaleString()}</td>
                      <td>{a.event}</td>
                      <td>
                        <Json data={{ subject: a.subject, ...a.detail }} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          </>
        )}
        {page === "evaluations" && (
          <>
            <EvaluationDesk
              key={candidate}
              candidate={candidate}
              applications={overview.applications}
              jobs={overview.jobs}
              api={api}
              run={run}
            />
            <JobList
              jobs={overview.jobs.filter((j: Row) => j.kind === "llm")}
              run={run}
            />
          </>
        )}
        {page === "settings" && settings && (
          <>
            <CreateUserProfile api={api} busy={busy} />
            <section className="panel">
              <h2>Data mode</h2>
              <p>Use your profile’s records for your job search. Demonstration mode shows synthetic records for testing.</p>
              <label className="switch">
                Data mode
                <select
                  aria-label="Data mode"
                  value={candidate}
                  onChange={(e) => {
                    setCandidate(e.target.value);
                    setAppId("");
                    setApplication(null);
                    setPage("overview");
                  }}
                >
                  <option value="fixture">Fixture demonstration</option>
                  <option value="real">Current profile records</option>
                </select>
              </label>
            </section>
            <section className="panel">
              <h2>Document search</h2>
              <p>
                Document retrieval is separate from Codex evaluations. Lexical
                search uses the local text index. The fixture vector option is a
                test surrogate. Optional Ollama embeddings require enabling
                their destination and re-ingesting documents.
              </p>
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  run(
                    () => api("/settings/provider", settings.provider, "PUT"),
                    "Provider settings saved.",
                  );
                }}
              >
                <label>
                  Search method
                  <select
                    value={settings.provider.retrieval_mode || "hybrid"}
                    onChange={(e) =>
                      setSettings({
                        ...settings,
                        provider: {
                          ...settings.provider,
                          retrieval_mode: e.target.value,
                        },
                      })
                    }
                  >
                    <option value="lexical">Local text search</option>
                    <option value="hybrid">Text and vector search</option>
                  </select>
                </label>
                <label>
                  Embedding provider
                  <select
                    aria-label="Provider"
                    value={settings.provider.provider}
                    onChange={(e) =>
                      setSettings({
                        ...settings,
                        provider: {
                          ...settings.provider,
                          provider: e.target.value,
                          enabled: false,
                        },
                      })
                    }
                  >
                    <option value="fixture">Offline fixtures</option>
                    <option value="ollama">Ollama server</option>
                  </select>
                </label>
                {settings.provider.provider !== "fixture" && (
                  <>
                    <label>
                      Data destination URL
                      <input
                        type="url"
                        value={settings.provider.destination}
                        onChange={(e) =>
                          setSettings({
                            ...settings,
                            provider: {
                              ...settings.provider,
                              destination: e.target.value,
                            },
                          })
                        }
                        placeholder="http://127.0.0.1:11434"
                        required
                      />
                    </label>
                    <label>
                      Embedding model
                      <input
                        value={settings.provider.embedding_model}
                        onChange={(e) =>
                          setSettings({
                            ...settings,
                            provider: {
                              ...settings.provider,
                              embedding_model: e.target.value,
                            },
                          })
                        }
                        required
                      />
                    </label>
                    <label className="checkbox">
                      <input
                        type="checkbox"
                        checked={settings.provider.enabled}
                        onChange={(e) =>
                          setSettings({
                            ...settings,
                            provider: {
                              ...settings.provider,
                              enabled: e.target.checked,
                            },
                          })
                        }
                      />
                      Enable sending selected document text and evidence to{" "}
                      {settings.provider.destination || "the destination above"}
                    </label>
                  </>
                )}
                <button>Save provider settings</button>
              </form>
            </section>
            {settings.llm && (
              <ModelSettings config={settings.llm} api={api} run={run} />
            )}
            <section className="panel">
              <h2>Local storage and controls</h2>
              <p>
                Personal state: <code>{settings.state}</code>
              </p>
              <p>
                Source workspace: <code>{settings.workspace}</code>
              </p>
              <p>
                Applications are submitted through Codex CLI using an exported prompt and prepared files.{" "}
                {settings.real_tracker_writes_enabled ? "Reviewed tracker writes are enabled." : "Tracker writes are disabled."}{" "}
                Live JobSpy searches are{" "}
                {settings.live_search ? "enabled" : "disabled"}.
              </p>
              <button className="secondary" onClick={() => setPage("metrics")}>Manage tracker updates</button>
              <button
                className="secondary"
                onClick={() =>
                  run(
                    () => api("/reports/manual?candidate=" + candidate, {}),
                    "Manual follow-up report prepared if needed.",
                  )
                }
              >
                Create manual follow-up report
              </button>
            </section>
          </>
        )}
      </main>
    </div>
  );
}
function JobList({
  jobs,
  run,
}: {
  jobs: Row[];
  run: (task: () => Promise<any>, message?: string) => Promise<any>;
}) {
  return jobs.length ? (
    <section className="panel">
      <h2>Work progress</h2>
      {jobs.slice(0, 8).map((j) => (
        <div className="job" key={j.id}>
          <div>
            <strong>{j.kind === "llm" ? "Evaluation" : title(j.kind)}</strong>
            <small>
              {j.status === "complete" && j.checkpoint.stage === "failed"
                ? "Task finished; an earlier evaluation failed"
                : j.checkpoint.model
                ? `${j.checkpoint.model} · ${j.checkpoint.stage}`
                : j.checkpoint.completed
                  ? `${j.checkpoint.completed} of ${j.checkpoint.total} documents`
                  : j.checkpoint.queries_completed
                    ? `${j.checkpoint.queries_completed} of ${j.checkpoint.queries_total} queries`
                    : j.id.slice(0, 8)}
            </small>
          </div>
          <Badge value={j.status} />
          {["queued", "running"].includes(j.status) && (
            <button
              className="text-button"
              onClick={() => run(() => api("/jobs/" + j.id + "/cancel", {}))}
            >
              Cancel
            </button>
          )}
          {["failed", "cancelled"].includes(j.status) &&
            j.kind !== "submit" && (
              <button
                className="text-button"
                onClick={() => run(() => api("/jobs/" + j.id + "/resume", {}))}
              >
                Resume
              </button>
            )}
          {j.error && j.status !== "complete" && <span className="error-inline">{j.error}</span>}
          <Json
            data={{
              checkpoint: j.checkpoint,
              result: j.result,
              error: j.error,
            }}
            label="Details"
          />
        </div>
      ))}
    </section>
  ) : null;
}
createRoot(document.getElementById("root")!).render(<App />);
