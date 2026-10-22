type Row = Record<string, any>;

function webUrl(value: unknown): string | null {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

export function EmployerApplicationLink({ application }: { application: Row }) {
  const formUrl = webUrl(application.official?.application_url)
    || webUrl(application.payload?.form_plan?.spec?.url);
  const postingUrl = webUrl(application.official?.url);
  return <div className="employer-application-link">
    {formUrl ? <a className="primary-link" href={formUrl} target="_blank" rel="noopener noreferrer">
      Open employer application
    </a> : postingUrl ? <a className="primary-link" href={postingUrl} target="_blank" rel="noopener noreferrer">
      Open employer posting
    </a> : <p className="muted">No employer application link was saved for this record.</p>}
  </div>;
}
