export const applicationColumns = [
  { key: "employer", label: "Employer / role" },
  { key: "requisition", label: "Requisition" },
  { key: "state", label: "State" },
  { key: "fit", label: "Fit" },
] as const;

export type ApplicationColumn = (typeof applicationColumns)[number]["key"];
export type ApplicationSort = {
  column: ApplicationColumn;
  direction: "ascending" | "descending";
};

export const defaultApplicationSort: ApplicationSort = {
  column: "state",
  direction: "ascending",
};

type Application = {
  employer: string;
  role: string;
  requisition?: string | null;
  state: string;
  paused?: boolean;
  score?: { total?: number | null } | null;
};

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

const activeStates = new Set([
  "discovered", "verified", "scored", "selected", "preparing", "prepared",
  "filling", "missing_answers", "review", "authorized", "manual_handoff",
]);
const submittedStates = new Set(["submitted", "follow_up"]);
const inactiveStates = new Set(["deferred", "closed", "rejected", "skipped"]);

function statusGroup(row: Application): number {
  if (submittedStates.has(row.state)) return 2;
  if (row.paused || inactiveStates.has(row.state)) return 3;
  if (activeStates.has(row.state)) return 0;
  // Attempts, verification, and unresolved outcomes need reconciliation.
  return 1;
}

function highestScoreFirst(a: Application, b: Application): number {
  const left = a.score?.total, right = b.score?.total;
  if (left == null || right == null) return Number(left == null) - Number(right == null);
  return right - left;
}

export function sortApplications<T extends Application>(rows: T[], sort: ApplicationSort | null): T[] {
  sort = sort ?? defaultApplicationSort;
  const value = (row: T) => sort.column === "fit" ? row.score?.total : row[sort.column];
  return [...rows].sort((a, b) => {
    if (sort.column === "state") {
      const group = statusGroup(a) - statusGroup(b);
      return (sort.direction === "ascending" ? group : -group) || highestScoreFirst(a, b);
    }
    const left = value(a), right = value(b);
    // Keep unscored applications and empty cells last in either direction.
    const missingLeft = left == null || left === "";
    const missingRight = right == null || right === "";
    if (missingLeft || missingRight) return Number(missingLeft) - Number(missingRight);
    const comparison = sort.column === "fit"
      ? Number(left) - Number(right)
      : collator.compare(String(left), String(right)) ||
        (sort.column === "employer" ? collator.compare(a.role, b.role) : 0);
    return sort.direction === "ascending" ? comparison : -comparison;
  });
}
