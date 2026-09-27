"use client";

import { useState, useEffect, useCallback } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { api } from "@/lib/api";
import type { Run, TraceEntry } from "@/lib/types";
import StatusBadge from "@/components/StatusBadge";
import ConfirmModal from "@/components/ConfirmModal";

// ── Time helpers ─────────────────────────────────────────────────────────────

function fmt(dateStr: string) {
  return new Date(dateStr).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function duration(start: string, end?: string | null) {
  const ms = new Date(end ?? Date.now()).getTime() - new Date(start).getTime();
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

// ── Trace timeline ────────────────────────────────────────────────────────────

function TraceTimeline({ trace }: { trace: TraceEntry[] }) {
  if (!trace.length)
    return (
      <p className="text-sm text-slate-500 italic px-4 py-6">
        No trace entries yet — agent is starting up…
      </p>
    );

  return (
    <div className="space-y-0">
      {trace.map((entry, i) => (
        <div key={i} className="relative flex gap-4 px-4 py-3 trace-line">
          {/* Dot */}
          <div className="relative z-10 mt-1 flex-shrink-0">
            <div className="w-3.5 h-3.5 rounded-full bg-violet-600 border-2 border-[#09090f] shadow" />
          </div>
          {/* Content */}
          <div className="flex-1 min-w-0 pb-3">
            <div className="flex items-center justify-between gap-2 mb-1">
              <code className="text-xs font-mono text-violet-400">{entry.type}</code>
              <span className="text-[10px] text-slate-600 flex-shrink-0">{fmt(entry.timestamp)}</span>
            </div>
            {entry.data && (
              <pre className="text-xs text-slate-400 bg-[#09090f] rounded-md p-3 overflow-x-auto border border-[rgba(139,92,246,0.08)] max-h-48 whitespace-pre-wrap break-words">
                {JSON.stringify(entry.data, null, 2)}
              </pre>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Info row ─────────────────────────────────────────────────────────────────

function InfoRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-4 py-3 border-b border-[rgba(139,92,246,0.08)]">
      <span className="text-xs font-semibold text-slate-500 uppercase tracking-wider w-28 flex-shrink-0 mt-0.5">
        {label}
      </span>
      <span className="text-sm text-slate-300 break-all">{children}</span>
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function RunDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [run, setRun] = useState<Run | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [restarting, setRestarting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState(false);

  const isActive = run
    ? !["done", "failed"].includes(run.status)
    : false;

  const fetchRun = useCallback(async () => {
    if (deleting) return;
    try {
      const data = await api.getRun(id);
      setRun(data);
    } catch (err) {
      if (!deleting) {
        setError(err instanceof Error ? err.message : "Failed to load run");
      }
    } finally {
      setLoading(false);
    }
  }, [id, deleting]);

  const handleRestart = async () => {
    if (restarting) return;
    setRestarting(true);
    try {
      const updated = await api.restartRun(id);
      setRun(updated);
      setError(null);
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to restart run");
    } finally {
      setRestarting(false);
    }
  };

  const confirmDelete = async () => {
    if (deleting) return;
    setDeleting(true);
    try {
      await api.deleteRun(id);
      window.location.href = "/runs";
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to delete run");
      setDeleting(false);
      setShowDeleteModal(false);
    }
  };

  useEffect(() => {
    if (deleting) return;
    fetchRun();
    if (isActive) {
      const interval = setInterval(fetchRun, 3000);
      return () => clearInterval(interval);
    }
  }, [fetchRun, isActive, deleting]);

  if (loading)
    return (
      <div className="flex items-center justify-center min-h-screen text-slate-500 gap-3">
        <div className="w-6 h-6 border-2 border-violet-600 border-t-transparent rounded-full animate-spin" />
        Loading run…
      </div>
    );

  if (error || !run)
    return (
      <div className="flex flex-col items-center justify-center min-h-screen gap-4">
        <p className="text-red-400">{error ?? "Run not found"}</p>
        <Link href="/" className="text-sm text-violet-400 hover:underline">
          ← Back to dashboard
        </Link>
      </div>
    );

  return (
    <div className="min-h-screen grid-bg px-8 py-8">
      {/* Breadcrumb */}
      <div className="flex items-center gap-2 text-sm text-slate-500 mb-6">
        <Link href="/" className="hover:text-violet-400 transition-colors">
          Dashboard
        </Link>
        <span>/</span>
        <Link href="/runs" className="hover:text-violet-400 transition-colors">
          Runs
        </Link>
        <span>/</span>
        <code className="text-violet-400 font-mono">{id.slice(0, 8)}</code>
      </div>

      {/* Header */}
      <div className="flex items-start justify-between mb-6">
        <div>
          <div className="flex items-center gap-3 mb-1">
            <StatusBadge status={run.status} />
            {run.status === "failed" && (
              <button
                onClick={handleRestart}
                disabled={restarting}
                className="flex items-center gap-1.5 px-3 py-1 text-xs font-semibold text-white bg-gradient-to-r from-violet-600 to-indigo-600 hover:from-violet-500 hover:to-indigo-500 rounded-lg shadow-md hover:shadow-violet-500/25 transition-all disabled:opacity-50"
              >
                <svg
                  className={`w-3.5 h-3.5 ${restarting ? "animate-spin" : ""}`}
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  viewBox="0 0 24 24"
                >
                  <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
                {restarting ? "Restarting…" : "Restart Run"}
              </button>
            )}
            <button
              onClick={() => setShowDeleteModal(true)}
              disabled={deleting}
              className="flex items-center gap-1.5 px-3 py-1 text-xs font-semibold text-red-400 bg-red-950/30 hover:bg-red-900/50 border border-red-500/30 rounded-lg shadow transition-all disabled:opacity-50"
            >
              <svg
                className="w-3.5 h-3.5"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                viewBox="0 0 24 24"
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
              </svg>
              {deleting ? "Deleting…" : "Delete"}
            </button>
            {isActive && (
              <span className="text-xs text-yellow-400 animate-pulse">
                Live — refreshing every 3s
              </span>
            )}
          </div>
          <h1 className="text-xl font-bold text-white mt-2">
            Run{" "}
            <code className="font-mono text-violet-400">{id.slice(0, 8)}</code>
          </h1>
          <p className="text-sm text-slate-500 mt-0.5">{run.repo_url}</p>
        </div>
        <div className="text-right text-sm text-slate-500">
          <p>Started: {fmt(run.created_at)}</p>
          <p>Duration: {duration(run.created_at, run.completed_at)}</p>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-6">
        {/* Left — details */}
        <div className="col-span-1 space-y-4">
          <div className="glass overflow-hidden">
            <div className="px-4 py-3 border-b border-[rgba(139,92,246,0.12)]">
              <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
                Run Details
              </h2>
            </div>
            <div className="px-4">
              <InfoRow label="Run ID">
                <code className="font-mono text-xs text-violet-400">{run.run_id}</code>
              </InfoRow>
              <InfoRow label="Repo">{run.repo_url}</InfoRow>
              <InfoRow label="Issue">
                {run.issue_number ? `#${run.issue_number}` : run.issue_text ?? "—"}
              </InfoRow>
              <InfoRow label="Status">
                <StatusBadge status={run.status} />
              </InfoRow>
              {run.sandbox_container_id && (
                <InfoRow label="Container">
                  <code className="font-mono text-xs text-slate-400">
                    {run.sandbox_container_id.slice(0, 12)}
                  </code>
                </InfoRow>
              )}
              {run.pr_url && (
                <div className="my-2 p-3 rounded-lg bg-emerald-950/30 border border-emerald-500/30 flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
                    <span className="text-xs font-semibold text-emerald-300">Pull Request Created</span>
                  </div>
                  <a
                    href={run.pr_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-xs font-semibold text-white bg-emerald-600 hover:bg-emerald-500 px-2.5 py-1 rounded transition-colors shadow-sm flex items-center gap-1"
                  >
                    View PR ↗
                  </a>
                </div>
              )}
              {run.error_message && (
                <div className="py-3">
                  <div className="flex items-center justify-between mb-2">
                    <p className="text-xs font-semibold text-red-400 uppercase tracking-wider">
                      Error
                    </p>
                    <button
                      onClick={handleRestart}
                      disabled={restarting}
                      className="text-xs font-medium text-violet-400 hover:text-violet-300 flex items-center gap-1 transition-colors disabled:opacity-50"
                    >
                      <svg
                        className={`w-3 h-3 ${restarting ? "animate-spin" : ""}`}
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                        viewBox="0 0 24 24"
                      >
                        <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                      </svg>
                      {restarting ? "Restarting…" : "Retry Run"}
                    </button>
                  </div>
                  <pre className="text-xs text-red-300 bg-red-950/20 border border-red-500/20 rounded-md p-3 whitespace-pre-wrap break-words">
                    {run.error_message}
                  </pre>
                </div>
              )}
            </div>
          </div>

          {/* Relevant Files */}
          {run.relevant_files && run.relevant_files.length > 0 && (
            <div className="glass overflow-hidden">
              <div className="px-4 py-3 border-b border-[rgba(139,92,246,0.12)] flex items-center justify-between">
                <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
                  Relevant Files ({run.relevant_files.length})
                </h2>
              </div>
              <div className="p-3 max-h-60 overflow-y-auto space-y-1">
                {run.relevant_files.map((file, idx) => (
                  <div
                    key={idx}
                    className="flex items-center gap-2 text-xs font-mono text-slate-300 py-1 px-2 rounded hover:bg-violet-950/20"
                  >
                    <span className="text-[10px] text-violet-500 font-sans">#{idx + 1}</span>
                    <span className="truncate">{file}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Fix Plan */}
          {run.fix_plan && (
            <div className="glass overflow-hidden">
              <div className="px-4 py-3 border-b border-[rgba(139,92,246,0.12)]">
                <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
                  Fix Plan (Analyzer)
                </h2>
              </div>
              <div className="p-4 max-h-96 overflow-y-auto">
                <pre className="text-xs text-slate-300 font-mono whitespace-pre-wrap break-words leading-relaxed">
                  {run.fix_plan}
                </pre>
              </div>
            </div>
          )}

          {/* Git Diff */}
          {run.diff && (
            <div className="glass overflow-hidden">
              <div className="px-4 py-3 border-b border-[rgba(139,92,246,0.12)] flex items-center justify-between">
                <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
                  Git Diff (Changes Made)
                </h2>
                <span className="text-[10px] text-emerald-400 font-mono">
                  {run.diff.split("\n").filter((l) => l.startsWith("+") && !l.startsWith("+++")).length} additions,{" "}
                  {run.diff.split("\n").filter((l) => l.startsWith("-") && !l.startsWith("---")).length} deletions
                </span>
              </div>
              <div className="p-3 max-h-96 overflow-y-auto bg-[#07070c]">
                <pre className="text-xs font-mono whitespace-pre overflow-x-auto leading-relaxed">
                  {run.diff.split("\n").map((line, idx) => {
                    let color = "text-slate-400";
                    let bg = "";
                    if (line.startsWith("+") && !line.startsWith("+++")) {
                      color = "text-emerald-400";
                      bg = "bg-emerald-950/20";
                    } else if (line.startsWith("-") && !line.startsWith("---")) {
                      color = "text-rose-400";
                      bg = "bg-rose-950/20";
                    } else if (line.startsWith("@@")) {
                      color = "text-cyan-400";
                    }
                    return (
                      <div key={idx} className={`${color} ${bg} px-1.5 rounded-sm`}>
                        {line || " "}
                      </div>
                    );
                  })}
                </pre>
              </div>
            </div>
          )}

          {/* Test results */}
          {run.test_results && (
            <div className="glass overflow-hidden">
              <div className="px-4 py-3 border-b border-[rgba(139,92,246,0.12)] flex items-center justify-between">
                <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
                  Test Results
                </h2>
                {run.test_results.passed !== undefined && (
                  <span
                    className={`text-xs font-semibold px-2 py-0.5 rounded-full ${
                      run.test_results.all_passed || run.test_results.failed === 0
                        ? "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20"
                        : "bg-rose-500/10 text-rose-400 border border-rose-500/20"
                    }`}
                  >
                    {run.test_results.all_passed || run.test_results.failed === 0
                      ? "PASSED"
                      : "FAILED"}
                  </span>
                )}
              </div>
              <div className="p-4 space-y-3">
                <div className="grid grid-cols-4 gap-2 text-center">
                  <div className="bg-slate-900/60 p-2 rounded border border-slate-800">
                    <p className="text-[10px] text-slate-500 uppercase">Passed</p>
                    <p className="text-sm font-semibold text-emerald-400">
                      {String(run.test_results.passed ?? 0)}
                    </p>
                  </div>
                  <div className="bg-slate-900/60 p-2 rounded border border-slate-800">
                    <p className="text-[10px] text-slate-500 uppercase">Failed</p>
                    <p className="text-sm font-semibold text-rose-400">
                      {String(run.test_results.failed ?? 0)}
                    </p>
                  </div>
                  <div className="bg-slate-900/60 p-2 rounded border border-slate-800">
                    <p className="text-[10px] text-slate-500 uppercase">Errors</p>
                    <p className="text-sm font-semibold text-amber-400">
                      {String(run.test_results.errors ?? 0)}
                    </p>
                  </div>
                  <div className="bg-slate-900/60 p-2 rounded border border-slate-800">
                    <p className="text-[10px] text-slate-500 uppercase">Skipped</p>
                    <p className="text-sm font-semibold text-slate-400">
                      {String(run.test_results.skipped ?? 0)}
                    </p>
                  </div>
                </div>
                {Boolean(run.test_results.failure_summary) && (
                  <div>
                    <p className="text-[10px] font-semibold text-rose-400 uppercase tracking-wider mb-1">
                      Failures
                    </p>
                    <pre className="text-xs text-rose-300 bg-rose-950/20 border border-rose-500/20 rounded p-2.5 whitespace-pre-wrap break-words max-h-40 overflow-y-auto">
                      {String(run.test_results.failure_summary)}
                    </pre>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Security & Quality Review */}
          {run.review_notes && (
            <div className="glass overflow-hidden">
              <div className="px-4 py-3 border-b border-[rgba(139,92,246,0.12)] flex items-center justify-between">
                <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
                  Security & Quality Review
                </h2>
                <span className="text-[10px] text-violet-400 font-semibold px-2 py-0.5 rounded bg-violet-950/40 border border-violet-500/20">
                  Review Agent
                </span>
              </div>
              <div className="p-4 max-h-80 overflow-y-auto">
                <pre className="text-xs text-slate-300 font-mono whitespace-pre-wrap break-words leading-relaxed">
                  {run.review_notes}
                </pre>
              </div>
            </div>
          )}
        </div>

        {/* Right — trace timeline */}
        <div className="col-span-2 glass overflow-hidden">
          <div className="px-4 py-3 border-b border-[rgba(139,92,246,0.12)] flex items-center justify-between">
            <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
              Agent Trace
            </h2>
            <span className="text-xs text-slate-600">
              {run.trace?.length ?? 0} event{run.trace?.length !== 1 ? "s" : ""}
            </span>
          </div>
          <div className="overflow-y-auto max-h-[70vh]">
            <TraceTimeline trace={run.trace ?? []} />
          </div>
        </div>
      </div>

      <ConfirmModal
        isOpen={showDeleteModal}
        title="Delete Run"
        message={`Are you sure you want to delete run ${id.slice(0, 8)}? This will permanently remove its execution history, diffs, and sandbox container.`}
        confirmText={deleting ? "Deleting…" : "Delete Run"}
        confirmVariant="danger"
        isLoading={deleting}
        onConfirm={confirmDelete}
        onCancel={() => {
          if (!deleting) setShowDeleteModal(false);
        }}
      />
    </div>
  );
}
