import { useEffect, useState } from "react";
import { WorkflowSection } from "./ApplicationSections";

type Row = Record<string, any>;

export function SubmissionHandoff({ application, review, api, onSaved }: {
  application: Row; review: Row | null;
  api: (path: string, body?: unknown) => Promise<any>; onSaved: () => void;
}) {
  const [packet, setPacket] = useState<Row | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [created, setCreated] = useState(false);
  const stale = !!packet && (packet.stale || packet.application_revision !== application.revision || packet.application_state !== application.state || !review || packet.reviewed_hash !== review.hash);
  useEffect(() => {
    let current = true;
    setPacket(null); setMessage(""); setCreated(false);
    api("/applications/" + application.id + "/submission-handoff")
      .then(value => { if (current) setPacket(value); })
      .catch(error => { if (current) setMessage(error.message); });
    return () => { current = false; };
  }, [application.id]);
  return <WorkflowSection section="submission-handoff" className="panel" id="submission-handoff" summary="Codex submission handoff" forceOpen={created && !application.receipts?.length}>
    <p className="eyebrow">SUBMIT THROUGH CODEX CLI</p>
    <h2>Take this application into Codex</h2>
    <p>The app inspects the application, extracts its fields, and prepares answers from your profile. The prompt includes those answers and full document paths. Codex handles form entry and submission. Creating a prompt sends nothing.</p>
    <button disabled={busy || !review || application.paused} onClick={async () => {
      setBusy(true); setMessage("");
      try {
        const result = await api("/applications/" + application.id + "/submission-handoff", { reviewed_hash: review!.hash });
        setPacket(result); setCreated(true); onSaved();
        setMessage("The prompt and document copies are saved. Paste the prompt into Codex CLI to continue.");
      } catch (error) { setMessage((error as Error).message); }
      finally { setBusy(false); }
    }}>{busy ? "Creating packet…" : packet ? "Create updated submission prompt" : "Create submission prompt"}</button>
    {message && <p role="status">{message}</p>}
    {packet && <>
      <p>{packet.mode === "submit" ? "This packet can be used to complete this application." : packet.mode === "reconcile" ? "An earlier outcome is uncertain. This prompt asks Codex to reconcile it before another submission." : "This packet is for reference. Its prompt prohibits another submission."}</p>
      <p>Packet folder: <code>{packet.directory}</code></p>
      <p>Prompt file: <code>{packet.prompt_path}</code></p>
      {stale && <p role="status">This application changed. Create an updated prompt before copying.</p>}
      <button className="secondary" disabled={stale || application.paused} onClick={async () => {
        try { await navigator.clipboard.writeText(packet.prompt); setMessage("Prompt copied. Paste it into Codex CLI."); }
        catch { setMessage("Select and copy the prompt below, or open the saved prompt file."); }
      }}>Copy submission prompt</button>
      <label>Codex prompt<textarea readOnly rows={12} value={packet.prompt} /></label>
      <p>After Codex finishes, record the employer confirmation in this application. A generated prompt or an attempted send does not establish receipt.</p>
    </>}
  </WorkflowSection>;
}
