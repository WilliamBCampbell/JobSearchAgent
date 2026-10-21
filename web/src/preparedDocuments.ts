type Artifact = { id: string; kind: string; modified_at?: number | null };

export function latestPreparedDocuments<T extends Artifact>(
  artifacts: T[],
  packageIds: Set<string>,
): T[] {
  const newest = new Map<string, T>();
  const sorted = [...artifacts].sort((a, b) =>
    (b.modified_at ?? -Infinity) - (a.modified_at ?? -Infinity) ||
    Number(packageIds.has(b.id)) - Number(packageIds.has(a.id)) ||
    a.id.localeCompare(b.id),
  );
  for (const artifact of sorted) {
    if (!newest.has(artifact.kind)) newest.set(artifact.kind, artifact);
  }
  return [...newest.values()].sort((a, b) => a.kind.localeCompare(b.kind));
}
