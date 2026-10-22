import { useState } from "react";

export function CopyButton({ text, label }: { text: string; label: string }) {
  const [status, setStatus] = useState("");
  return <>
    <button type="button" className="secondary" aria-label={label} onClick={async () => {
      try { await navigator.clipboard.writeText(text); setStatus("Copied"); }
      catch { setStatus("Clipboard unavailable. Select the text and copy it manually."); }
    }}>{label.split(": ")[0]}</button>
    {status && <span role="status">{status}</span>}
  </>;
}
