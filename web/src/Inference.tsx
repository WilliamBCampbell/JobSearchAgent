import React, { useEffect, useState } from "react";
import { RequirementReview } from "./RequirementReview";

type Row = Record<string, any>;
type API = (path: string, body?: unknown, method?: string) => Promise<any>;
type Run = (task: () => Promise<any>, message?: string) => Promise<any>;
export const tasks: Record<string, string> = {
  query_expansion: "Find better search terms",
  requirement_extraction: "Extract posting requirements",
  evidence_selection: "Select career evidence",
  fit_assessment: "Evaluate job fit",
  draft: "Draft application text",
  package_draft: "Write resume and cover letter",
  package_review: "Review document claims and writing",
  form_answers: "Prepare employer answers from evidence",
  form_answer_review: "Review employer answer evidence",
  conflict_review: "Review conflicting evidence",
};

export function ModelSettings({
  config,
  api,
  run,
}: {
  config: Row;
  api: API;
  run: Run;
}) {
  const [draft, setDraft] = useState(config);
  const [catalog, setCatalog] = useState<Row | null>(null);
  return (
    <section className="panel">
      <h2>Codex evaluations</h2>
      <p>
        Selected facts, source passages, and job requirements go to OpenAI
        through your saved Codex login. The document index stays local. Account
        usage limits apply. One evaluation runs at a time.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          run(
            () => api("/settings/llm", draft, "PUT"),
            "Evaluation settings saved.",
          );
        }}
      >
        <label>
          Evaluation provider
          <select
            aria-label="Evaluation provider"
            value={draft.backend}
            onChange={(e) =>
              setDraft({ ...draft, backend: e.target.value, enabled: false })
            }
          >
            <option value="fixture">Offline fixtures</option>
            <option value="codex_exec">Codex</option>
          </select>
        </label>
        {draft.backend === "codex_exec" && (
          <>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={draft.enabled}
                onChange={(e) =>
                  setDraft({ ...draft, enabled: e.target.checked })
                }
              />
              Enable sending selected evidence to OpenAI for these evaluations
            </label>
            <label>
              Time limit per evaluation in seconds
              <input
                type="number"
                min={10}
                max={900}
                value={draft.timeout_seconds}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    timeout_seconds: Number(e.target.value),
                  })
                }
              />
            </label>
            <p>
              These are starting choices. Compare results before increasing
              effort or changing models. Unavailable models produce a visible
              error; the worker never silently substitutes one.
            </p>
            <button
              type="button"
              onClick={() =>
                run(async () => setCatalog(await api("/llm/catalog")))
              }
            >
              Check installed models
            </button>
            {catalog && (
              <p>
                {catalog.version || catalog.error}. Catalog listings do not
                guarantee account access.
              </p>
            )}
            <datalist id="codex-models">
              {catalog?.models?.map((m: Row) => (
                <option key={m.model} value={m.model} />
              ))}
            </datalist>
            <table>
              <thead>
                <tr>
                  <th>Task</th>
                  <th>Model</th>
                  <th>Reasoning effort</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(draft.routes).map(([task, value]) => {
                  const route = value as Row;
                  return (
                    <tr key={task}>
                      <th>{tasks[task]}</th>
                      <td>
                        <input
                          aria-label={`${tasks[task]} model`}
                          list="codex-models"
                          value={route.model}
                          onChange={(e) =>
                            setDraft({
                              ...draft,
                              routes: {
                                ...draft.routes,
                                [task]: { ...route, model: e.target.value },
                              },
                            })
                          }
                        />
                      </td>
                      <td>
                        <select
                          aria-label={`${tasks[task]} effort`}
                          value={route.effort}
                          onChange={(e) =>
                            setDraft({
                              ...draft,
                              routes: {
                                ...draft.routes,
                                [task]: { ...route, effort: e.target.value },
                              },
                            })
                          }
                        >
                          {["low", "medium", "high", "xhigh"].map((v) => (
                            <option key={v}>{v}</option>
                          ))}
                        </select>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </>
        )}
        <button>Save evaluation settings</button>
      </form>
    </section>
  );
}

export function EvaluationDesk({
  candidate,
  applications,
  jobs,
  api,
  run,
}: {
  candidate: string;
  applications: Row[];
  jobs: Row[];
  api: API;
  run: Run;
}) {
  const [task, setTask] = useState("query_expansion");
  const [applicationId, setApplicationId] = useState("");
  const [question, setQuestion] = useState("");
  const [sources, setSources] = useState("");
  const [runs, setRuns] = useState<Row[]>([]);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<Row | null>(null);
  const [evidence, setEvidence] = useState<any>(null);
  useEffect(() => {
    const refresh = async () => {
      setRuns(await api("/llm/runs?candidate=" + candidate));
      if (selected) setDetail(await api("/llm/runs/" + selected));
    };
    refresh().catch(() => {});
    const timer = setInterval(() => refresh().catch(() => {}), 1500);
    return () => clearInterval(timer);
  }, [candidate, selected]);
  const output = detail?.output;
  const factLabel = (id: string) =>
    detail?.input?.facts
      ?.find((f: Row) => f.id === id)
      ?.key.replaceAll("_", " ") || id;
  const sourceLabel = (id: string) => {
    const source = detail?.input?.sources?.find((s: Row) => s.id === id);
    return source?.url || source?.path || id;
  };

  const currentJob = detail ? jobs.find((j) => j.id === detail.job_id) : null;
  return (
    <>
      <section className="panel">
        <h2>Request an evaluation</h2>
        <p>
          Results are proposals for review. An evaluation cannot confirm a
          career fact, authorize a submission, or update an imported application
          status.
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            run(
              () =>
                api("/llm/jobs", {
                  task,
                  candidate,
                  application_id: applicationId || null,
                  question,
                  source_versions: sources.split(/\s*,\s*/).filter(Boolean),
                }),
              "Evaluation queued. Progress will appear below.",
            );
          }}
        >
          <label>
            Task
            <select
              aria-label="Task"
              value={task}
              onChange={(e) => setTask(e.target.value)}
            >
              {Object.entries(tasks).filter(([key]) => !key.startsWith("package_") && !key.startsWith("form_")).map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Application
            <select
              aria-label="Application"
              value={applicationId}
              onChange={(e) => setApplicationId(e.target.value)}
              required={[
                "fit_assessment",
                "draft",
                "evidence_selection",
              ].includes(task)}
            >
              <option value="">General research</option>
              {applications.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.employer} / {a.role} / {a.requisition}
                </option>
              ))}
            </select>
          </label>
          <label>
            Question or drafting request
            <textarea
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              maxLength={4000}
              placeholder="For example: Find evidence of customer onboarding and API integration work."
            />
          </label>
          <details>
            <summary>Choose specific sources</summary>
            <label>
              Document version IDs, separated by commas
              <input
                value={sources}
                onChange={(e) => setSources(e.target.value)}
              />
            </label>
            <p>
              Use current version IDs from Documents. Otherwise, evaluations use
              the verified posting and relevant career evidence.
            </p>
          </details>
          <button>Queue evaluation</button>
        </form>
      </section>
      <section className="panel">
        <h2>Evaluation history</h2>
        {!runs.length && <p>No evaluations have run for this workspace.</p>}
        {runs.map((r) => (
          <div className="job" key={r.id}>
            <button
              className="text-button"
              onClick={() => {
                setSelected(r.id);
                setDetail(null);
              }}
            >
              {tasks[r.task]}
            </button>
            <span>
              {r.model} / {r.effort}
            </span>
            <span>{r.status}</span>
            <small>{new Date(r.created_at).toLocaleString()}</small>
          </div>
        ))}
      </section>
      {detail && (
        <section className="panel" aria-label="Evaluation details">
          <h2>{tasks[detail.task]}</h2>
          <p>
            {detail.model} / {detail.effort} · {detail.status}
          </p>
          {currentJob && ["running", "queued"].includes(currentJob.status) && (
            <button
              onClick={() =>
                run(() => api("/jobs/" + currentJob.id + "/cancel", {}))
              }
            >
              Cancel evaluation
            </button>
          )}
          {currentJob &&
            ["failed", "cancelled"].includes(currentJob.status) && (
              <button
                onClick={() =>
                  run(
                    () => api("/jobs/" + currentJob.id + "/resume", {}),
                    "Evaluation queued with current evidence.",
                  )
                }
              >
                Retry evaluation
              </button>
            )}
          {detail.error && <p role="alert">{detail.error}</p>}
          <ol>
            {detail.events.map((e: Row) => (
              <li key={e.seq}>
                {new Date(e.at).toLocaleTimeString()} ·{" "}
                {e.event.replaceAll("_", " ")}
              </li>
            ))}
          </ol>
          {output?.queries && (
            <ul>
              {output.queries.map((q: string) => (
                <li key={q}>
                  <button
                    className="text-button"
                    onClick={() =>
                      run(async () =>
                        setEvidence(
                          await api("/retrieve", {
                            candidate,
                            query: q,
                            full_document: true,
                          }),
                        ),
                      )
                    }
                  >
                    {q}
                  </button>
                </li>
              ))}
            </ul>
          )}
          {output?.paragraphs?.map((p: Row, i: number) => (
            <div key={i}>
              <p>{p.text}</p>
              <small>
                Facts: {p.fact_ids.map(factLabel).join(", ") || "none"} ·
                Sources: {p.source_refs.map(sourceLabel).join(", ") || "none"}
              </small>
            </div>
          ))}
          {output?.fact_ids && (
            <ul>
              {output.fact_ids.map((id: string) => (
                <li key={id}>{factLabel(id)}</li>
              ))}
            </ul>
          )}
          {output?.summary && <p>{output.summary}</p>}
          {output?.score_preview && (
            <p>
              <strong>{output.score_preview.total}/100</strong> ·{" "}
              {output.score_preview.band}. Review every requirement before using
              this assessment.
            </p>
          )}
          {output?.requirements && (
            <table>
              <thead>
                <tr>
                  <th>Requirement</th>
                  <th>Assessment</th>
                  <th>Evidence and deduction</th>
                </tr>
              </thead>
              <tbody>
                {output.requirements.map((r: Row, i: number) => (
                  <tr key={i}>
                    <td>
                      {r.text}
                      <small>
                        {r.category} · {r.classification}
                        {r.central ? " · central" : ""}
                      </small>
                    </td>
                    <td>
                      {r.assessment || "Extracted"}
                      <small>{r.reason}</small>
                    </td>
                    <td>
                      {r.fact_ids?.map(factLabel).join(", ")}
                      <small>{sourceLabel(r.source)}</small>
                      {r.deduction !== undefined && (
                        <span>Deduction: {r.deduction}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {output?.missing_questions?.length > 0 && (
            <ul>
              {output.missing_questions.map((q: string) => (
                <li key={q}>{q}</li>
              ))}
            </ul>
          )}
          {output?.score_preview &&
            applications.some(
              (a) =>
                a.id === detail.application_id &&
                !a.imported &&
                ["verified", "scored", "review", "filling"].includes(a.state),
            ) && (
              <button
                onClick={() =>
                  run(
                    () => api("/llm/runs/" + detail.id + "/apply-score", {}),
                    "Reviewed assessment applied. Eligibility gates remain enforced.",
                  )
                }
              >
                Use reviewed assessment
              </button>
            )}
          {detail.task === "fit_assessment" && applications.filter(a => a.id === detail.application_id && a.score?.scores).map(a => (
            <div key={a.id}>
              <h3>Current application fit: {a.score.total}/100 · {a.score.band}</h3>
              <p>The evaluation above is a saved result. Decisions below update the current application assessment.</p>
              <RequirementReview application={a} api={api} run={run} jobs={jobs} />
            </div>
          ))}
          <details>
            <summary>Evidence packet and structured result</summary>
            <pre>
              {JSON.stringify(
                { input: detail.input, output, usage: detail.usage },
                null,
                2,
              )}
            </pre>
          </details>
        </section>
      )}
      {evidence && (
        <section className="panel">
          <h2>Retrieved evidence</h2>
          <pre>{JSON.stringify(evidence, null, 2)}</pre>
        </section>
      )}
    </>
  );
}
