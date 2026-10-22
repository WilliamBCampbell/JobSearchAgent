import React, { useEffect, useState } from "react";

export const profileId = new URLSearchParams(location.search).get("profile") || "default";

export function profileUrl(path: string): string {
  if (profileId === "default") return path;
  const url = new URL(path, location.origin);
  url.searchParams.set("profile", profileId);
  return url.pathname + url.search + url.hash;
}

type Profile = { id: string; name: string; workspace: string };
type Api = (path: string, body?: unknown, method?: string) => Promise<any>;

function switchProfile(id: string) {
  // Reload clears every draft and in-flight view; other tabs keep their profile.
  location.assign("/?profile=" + encodeURIComponent(id));
}

export function UserProfiles({ api, busy }: { api: Api; busy: boolean }) {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let current = true;
    setLoading(true);
    setFailed(false);
    api("/profiles")
      .then(result => { if (current) setProfiles(result); })
      .catch(() => { if (current) setFailed(true); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [api, retry]);
  return <div className="user-profiles">
    <label>User profile
      <select aria-label="User profile" value={profileId} disabled={busy || loading || failed || !profiles.length}
        onChange={e => switchProfile(e.target.value)}>
        {!profiles.length && <option value={profileId}>{loading ? "Loading profiles…" : "Profiles unavailable"}</option>}
        {profiles.map(profile => <option key={profile.id} value={profile.id}>{profile.name}</option>)}
      </select>
    </label>
    {failed && <div className="profile-load-error" role="alert">
      <p>Couldn’t load profiles.</p>
      <button type="button" onClick={() => setRetry(value => value + 1)}>Retry profiles</button>
    </div>}
  </div>;
}

export function CreateUserProfile({ api, busy }: { api: Api; busy: boolean }) {
  const [name, setName] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  return <section className="panel profile-settings">
    <h2>User profiles</h2>
    <p>Create a separate job search for yourself or a household member. Each profile has its own documents, answers, applications, and settings.</p>
    <details>
      <summary>Create user profile</summary>
      <form onSubmit={async e => {
        e.preventDefault();
        setCreating(true);
        setError("");
        try {
          const profile = await api("/profiles", { name, workspace });
          switchProfile(profile.id);
        } catch (e) {
          setError((e as Error).message);
          setCreating(false);
        }
      }}>
        <label>Profile name<input value={name} onChange={e => setName(e.target.value)} required maxLength={100} /></label>
        <label>Source document folder<input value={workspace} onChange={e => setWorkspace(e.target.value)} required placeholder="/path/to/source-documents" /></label>
        <p className="muted">Choose an existing folder separate from other profiles. After creating the profile, open Documents to import or index its files.</p>
        <button disabled={busy || creating || !name.trim() || !workspace.trim()}>{creating ? "Creating profile…" : "Create profile"}</button>
      </form>
    </details>
    {error && <p className="error" role="alert">{error}</p>}
  </section>;
}
