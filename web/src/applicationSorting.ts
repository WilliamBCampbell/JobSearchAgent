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

type Application = {
  employer: string;
  role: string;
  requisition?: string | null;
  state: string;
  score?: { total?: number | null };
};

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

export function sortApplications<T extends Application>(rows: T[], sort: ApplicationSort | null): T[] {
  if (!sort) return rows;
  const value = (row: T) => sort.column === "fit" ? row.score?.total : row[sort.column];
  return [...rows].sort((a, b) => {
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
