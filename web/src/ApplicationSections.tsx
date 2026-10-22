import { createContext, useContext, useLayoutEffect, useRef, type ReactNode } from "react";

type Row = Record<string, any>;
type Section = "selection" | "fit" | "documents" | "answers" | "question" | "manual-review" | "final-review" | "submission-handoff" | "receipt" | "outcome" | "activity" | "controls";
const StageContext = createContext<{ phase: string; current: (section: Section) => boolean } | null>(null);

export function ApplicationSections({ application: a, journey: p, children }: { application: Row; journey: Row; children: ReactNode }) {
  const working = p.status === "working";
  const terminal = ["closed", "skipped", "rejected", "deferred"].includes(a.state);
  const job = a.jobs?.find((j: Row) => ["queued", "running"].includes(j.status));
  // Polling, notices, draft typing, and job checkpoints do not change this key.
  const phase = JSON.stringify([a.id, a.state, a.payload.package?.version, p.step, p.status, p.target, p.action, job?.id]);
  const current = (section: Section): boolean => {
    if (terminal) return section === "fit";
    switch (section) {
      case "selection":
      case "fit": return !working && p.step === 0;
      case "documents": return !working && (p.step === 1 || p.target === "prepared-documents" || p.target === "package-review");
      case "answers":
      case "question": return !working && p.step === 2;
      case "manual-review": return !working && p.step === 3 && a.state === "manual_handoff";
      case "final-review": return !working && p.step === 3 && a.state !== "manual_handoff";
      case "submission-handoff": return !working && (p.step === 3 || p.target === "submission-handoff");
      case "receipt": return p.step === 5;
      case "outcome": return p.target === "submission-outcome";
      case "activity": return working || p.target === "work-progress" || p.step === 4 || (p.status === "blocked" && a.jobs?.[0]?.status === "failed");
      case "controls": return p.target === "workflow-controls";
    }
  };
  return <StageContext.Provider value={{ phase, current }}>{children}</StageContext.Provider>;
}

export function WorkflowSection({ section, id, summary, className, children, forceOpen = false }: {
  section: Section; id?: string; summary: ReactNode; className?: string; children: ReactNode; forceOpen?: boolean;
}) {
  const stage = useContext(StageContext);
  const ref = useRef<HTMLDetailsElement>(null);
  const phase = stage?.phase ?? "";
  const automaticOpen = forceOpen || (stage?.current(section) ?? false);
  useLayoutEffect(() => {
    if (ref.current) ref.current.open = automaticOpen;
  }, [phase, automaticOpen]);
  // Native details preserve manual toggles and navigation between stage changes.
  return <details ref={ref} id={id} className={className} data-workflow-section={section}>
    <summary>{summary}</summary>
    {children}
  </details>;
}
