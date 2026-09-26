import type { RunStatus } from "@/lib/types";

const STATUS_CONFIG: Record<
  RunStatus,
  { label: string; className: string; dot: string }
> = {
  pending:               { label: "Pending",        className: "badge-pending",       dot: "bg-yellow-400" },
  cloning:               { label: "Cloning",         className: "badge-cloning",        dot: "bg-yellow-400" },
  inspecting:            { label: "Inspecting",      className: "badge-inspecting",     dot: "bg-yellow-400" },
  planning:              { label: "Planning",        className: "badge-planning",       dot: "bg-blue-400" },
  modifying:             { label: "Modifying",       className: "badge-modifying",      dot: "bg-blue-400" },
  testing:               { label: "Testing",         className: "badge-testing",        dot: "bg-blue-400" },
  reviewing:             { label: "Reviewing",       className: "badge-reviewing",      dot: "bg-blue-400" },
  creating_pr:           { label: "Creating PR",     className: "badge-creating_pr",    dot: "bg-purple-400" },
  awaiting_confirmation: { label: "Awaiting Conf.",  className: "badge-awaiting_confirmation", dot: "bg-purple-400" },
  done:                  { label: "Done",            className: "badge-done",           dot: "bg-emerald-400" },
  failed:                { label: "Failed",          className: "badge-failed",         dot: "bg-red-400" },
};

export default function StatusBadge({ status }: { status: RunStatus }) {
  const cfg = STATUS_CONFIG[status] ?? {
    label: status,
    className: "badge-unknown",
    dot: "bg-slate-400",
  };

  const isActive = ["cloning", "inspecting", "planning", "modifying", "testing", "reviewing", "creating_pr"].includes(status);

  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-semibold tracking-wide ${cfg.className}`}
    >
      <span
        className={`w-1.5 h-1.5 rounded-full ${cfg.dot} ${isActive ? "animate-pulse" : ""}`}
      />
      {cfg.label}
    </span>
  );
}
