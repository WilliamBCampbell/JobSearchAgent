import React from "react";
import { answerText } from "./answerText";
import { legacyGreenhouseBlockers } from "./ApplicationJourney";
type Row = Record<string, any>;

export function AnswerScope({ question, value, onChange }: {
  question: Row;
  value: string;
  onChange: (scope: string) => void;
}) {
  const reusable = question.allowed_scopes?.includes("global") === true;
  if (!reusable) {
    return <div className="answer-scope" role="note" aria-label="Answer scope">
      <strong>This application only</strong>
      <p>{question.scope_reason || "This answer needs confirmation for each application and will not be reused automatically."}</p>
      <p>Enter your answer above, then select Confirm answer.</p>
    </div>;
  }
  return <fieldset className="answer-scope">
    <legend>Use this answer for</legend>
    <label className="checkbox">
      <input type="radio" name={"answer-scope-" + question.id} value="application"
        checked={value === "application"} onChange={() => onChange("application")} />
      This application only
    </label>
    <label className="checkbox">
      <input type="radio" name={"answer-scope-" + question.id} value="global"
        checked={value === "global"}
        aria-describedby={"scope-help-" + question.id}
        onChange={() => onChange("global")} />
      Global
    </label>
    <p id={"scope-help-" + question.id}>Global answers can be reused for the same question on other applications.</p>
  </fieldset>;
}

export function EmployerFormPlan({
  application,
  onCorrect,
}: {
  application: Row;
  onCorrect?: (key: string) => void;
}) {
  const plan = application.payload.form_plan;
  if (!plan) return null;
  const spec = plan.spec;
  const answers = application.payload.answers || {};
  const legacyGreenhouse = spec.adapter === "greenhouse" &&
    spec.unsupported.some((reason: string) => legacyGreenhouseBlockers.includes(reason));
  return (
    <section className="panel" aria-label="Employer application plan">
      <h2>Employer application answers</h2>
      <p>
        Approval covers these answers, selected files, and employer notices.
        {spec.adapter === "greenhouse"
          ? " The agent can fill a visible Greenhouse browser for local review. Files stay local until an approved sending run."
          : " The agent prepares the plan locally before entering candidate information into the employer portal."}
        {" "}Check the application status for any sending attempt.
      </p>
      <p>
        <a href={spec.url} target="_blank" rel="noreferrer">
          Official application form
        </a>
      </p>
      <table>
        <thead>
          <tr>
            <th>Employer question</th>
            <th>Prepared answer</th>
          </tr>
        </thead>
        <tbody>
          {spec.fields
            .filter((f: Row) => f.type !== "file")
            .map((f: Row) => (
              <tr key={f.key}>
                <th>
                  {f.label}
                  {f.required ? " (required)" : ""}
                </th>
                <td>
                  {answers[f.key]
                    ? answerText(answers[f.key].value)
                    : f.required
                      ? application.questions?.some(
                          (q: Row) =>
                            q.key === f.key &&
                            q.reason?.code === "personal_context_needed",
                        )
                        ? "Your personal perspective is needed"
                        : "Your answer is needed"
                      : "Left blank"}
                  {answers[f.key] && (
                    <details>
                      <summary>Answer source</summary>
                      <pre>
                        {JSON.stringify(answers[f.key].provenance, null, 2)}
                      </pre>
                    </details>
                  )}
                  {onCorrect && !application.attempts?.length &&
                    ["scored", "selected", "review", "missing_answers", "prepared", "manual_handoff"].includes(
                      application.state,
                    ) && (
                      <button
                        className="secondary"
                        onClick={() => onCorrect(f.key)}
                      >
                        Change answer
                      </button>
                    )}
                </td>
              </tr>
            ))}
        </tbody>
      </table>
      {legacyGreenhouse && <p>Greenhouse browser filling is now available. Use Open browser and fill application to refresh this saved form plan and its notices.</p>}
      {!legacyGreenhouse && spec.notices.length > 0 && (
        <>
          <h3>Employer notices</h3>
          {spec.notices.map((notice: string) => (
            <p key={notice}>{notice}</p>
          ))}
        </>
      )}
      {!legacyGreenhouse && spec.unsupported.length > 0 && (
        <>
          <h3>Manual completion needed</h3>
          <ul>
            {spec.unsupported.map((reason: string) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}


export function EmployerAnswerInput({
  field,
  value,
  onChange,
}: {
  field: Row;
  value: any;
  onChange: (v: any) => void;
}) {
  if (field.purpose === "self_identification") {
    const blank = "Leave this question blank";
    const raw = value?.choices ?? value;
    const selected: string[] = Array.isArray(raw) ? raw : raw && raw !== blank ? [raw] : [];
    const multi = field.type === "multiselect";
    const free = selected.some(o => field.free_form_options?.includes(o));
    const change = (next: string[]) => {
      if (next.some(o => field.free_form_options?.includes(o))) onChange({ choices: next, text: value?.text || "" });
      else onChange(multi ? next : next[0] || blank);
    };
    return <div className="personal-answer">
      <p>These choices are saved locally and excluded from model prompts.</p>
      {multi ? <fieldset><legend>{field.label}</legend>{field.options.map((o: string) => <label className="checkbox" key={o}>
        <input type="checkbox" checked={selected.includes(o)} onChange={e => change(e.target.checked ? [...selected, o] : selected.filter(v => v !== o))} />{o}
      </label>)}</fieldset> : <select aria-label={field.label} required value={selected[0] || (value === blank ? blank : "")} onChange={e => change(e.target.value === blank ? [] : [e.target.value])}>
        <option value="">Choose an answer</option>
        {!field.required && <option>{blank}</option>}
        {field.options.map((o: string) => <option key={o}>{o}</option>)}
      </select>}
      {multi && !field.required && <button type="button" className="secondary" onClick={() => onChange([])}>{blank}</button>}
      {multi && Array.isArray(value) && !value.length && <p>This question will be left blank.</p>}
      {free && <label>Your self-description<input required maxLength={2000} aria-label="Your self-description" value={value?.text || ""} onChange={e => onChange({choices: selected, text: e.target.value})} /></label>}
    </div>;
  }
  if (field.purpose === "demographic_consent") {
    return <select required aria-label={field.label} value={Array.isArray(value) ? value.length ? "yes" : "no" : ""} onChange={e => onChange(e.target.value === "yes" ? ["I consent"] : [])}>
      <option value="">Choose for this application</option><option value="yes">I consent</option><option value="no">I do not consent</option>
    </select>;
  }
  if (field.type === "checkbox") {
    const selected: string[] = Array.isArray(value) ? value : [];
    return (
      <select
        multiple
        required={field.required}
        aria-label={field.label}
        value={selected}
        onChange={(e) =>
          onChange(Array.from(e.target.selectedOptions, (o) => o.value))
        }
      >
        {field.options.map((o: string) => (
          <option key={o}>{o}</option>
        ))}
      </select>
    );
  }
  if (field.options?.length)
    return (
      <select
        required={field.required}
        aria-label={field.label}
        value={value || ""}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">Choose for this application</option>
        {field.options.map((o: string) => (
          <option key={o}>{o}</option>
        ))}
      </select>
    );
  if (field.type === "textarea")
    return (
      <textarea
        required={field.required}
        aria-label={field.label}
        value={value || ""}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  return (
    <input
      required={field.required}
      aria-label={field.label}
      type={
        ["email", "number", "date", "url", "tel"].includes(field.type)
          ? field.type
          : "text"
      }
      value={value || ""}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}
