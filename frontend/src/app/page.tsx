"use client";

import { useState, useEffect, useCallback } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import type { Run } from "@/lib/types";
import StatusBadge from "@/components/StatusBadge";
import SubmitForm from "@/components/SubmitForm";

// ── Stat card ───────────────────────────────────────────────────────────────

function StatCard({
  label,
  value,
  sub,
  color,
}: {
  label: string;
  value: number | string;
  sub?: string;
  color: string;
}) {
  return (
    <div className="glass p-5 fade-in-up">
      <p className="text-xs text-slate-500 uppercase tracking-widest font-semibold mb-1">
        {label}
      </p>
      <p className={`text-3xl font-bold ${color}`}>{value}</p>
      {sub && <p className="text-xs text-slate-500 mt-1">{sub}</p>}
    </div>
  );
}

// ── Relative time ────────────────────────────────────────────────────────────

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function repoName(url: string): string {
  try {
    const parts = new URL(url).pathname.split("/").filter(Boolean);
    return parts.slice(-2).join("/");
  } catch {
    return url;
  }
}

// ── Run row ──────────────────────────────────────────────────────────────────

function RunRow({
  run,
  onRestart,
  onDelete,
  isRestarting,
  isDeleting,
}: {
  run: Run;
  onRestart?: (runId: string) => void;
  onDelete?: (runId: string) => void;
  isRestarting?: boolean;
  isDeleting?: boolean;
}) {
  return (
    <tr className="border-b border-[rgba(139,92,246,0.08)] hover:bg-[rgba(139,92,246,0.04)] transition-colors">
      <td className="py-3 px-4">
        <code className="text-xs text-violet-400 font-mono">
          {run.run_id.slice(0, 8)}
        </code>
      </td>
      <td className="py-3 px-4">
        <p className="text-sm text-slate-200 font-medium">{repoName(run.repo_url)}</p>
        <p className="text-xs text-slate-500 truncate max-w-[200px]">{run.repo_url}</p>
      </td>
      <td className="py-3 px-4 text-sm text-slate-400">
        {run.issue_number ? `#${run.issue_number}` : "—"}
      </td>
      <td className="py-3 px-4">
        <StatusBadge status={run.status} />
      </td>
      <td className="py-3 px-4 text-xs text-slate-500">{timeAgo(run.created_at)}</td>
      <td className="py-3 px-4">
        <div className="flex items-center gap-2">
          {run.status === "failed" && onRestart && (
            <button
              onClick={() => onRestart(run.run_id)}
              disabled={isRestarting}
              title="Restart this failed run"
              className="text-xs px-2.5 py-1.5 rounded-md bg-violet-600/20 hover:bg-violet-600/40 border border-violet-500/30 text-violet-300 flex items-center gap-1 transition-all disabled:opacity-50"
            >
              <svg
                className={`w-3 h-3 ${isRestarting ? "animate-spin" : ""}`}
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                viewBox="0 0 24 24"
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
              {isRestarting ? "…" : "Restart"}
            </button>
          )}
          <Link
            href={`/runs/${run.run_id}`}
            className="text-xs px-3 py-1.5 rounded-md border border-[rgba(139,92,246,0.3)] text-violet-400 hover:bg-[rgba(139,92,246,0.1)] transition-colors"
          >
            View →
          </Link>
          {onDelete && (
            <button
              onClick={() => onDelete(run.run_id)}
              disabled={isDeleting}
              title="Delete this run"
              className="p-1.5 rounded-md text-slate-500 hover:text-red-400 hover:bg-red-950/30 border border-transparent hover:border-red-500/30 transition-all disabled:opacity-50"
            >
              <svg
                className={`w-3.5 h-3.5 ${isDeleting ? "animate-spin" : ""}`}
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                viewBox="0 0 24 24"
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
              </svg>
            </button>
          )}
        </div>
      </td>
    </tr>
  );
}

// ── Main page ────────────────────────────────────────────────────────────────

export default function DashboardPage() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [apiOnline, setApiOnline] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(true);
  const [restartingId, setRestartingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    try {
      await api.health();
      setApiOnline(true);
      const data = await api.listRuns(10);
      setRuns(data);
    } catch {
      setApiOnline(false);
    } finally {
      setLoading(false);
    }
  }, []);

  const handleRestart = async (runId: string) => {
    if (restartingId) return;
    setRestartingId(runId);
    try {
      const updated = await api.restartRun(runId);
      setRuns((prev) =>
        prev.map((r) => (r.run_id === runId ? updated : r))
      );
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to restart run");
    } finally {
      setRestartingId(null);
    }
  };

  const handleDelete = async (runId: string) => {
    if (deletingId) return;
    if (!confirm(`Delete run ${runId.slice(0, 8)}?`)) return;
    setDeletingId(runId);
    try {
      await api.deleteRun(runId);
      setRuns((prev) => prev.filter((r) => r.run_id !== runId));
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to delete run");
    } finally {
      setDeletingId(null);
    }
  };

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 5000);
    return () => clearInterval(interval);
  }, [fetchData]);

  const totalRuns = runs.length;
  const activeRuns = runs.filter(
    (r) => !["done", "failed"].includes(r.status)
  ).length;
  const doneRuns = runs.filter((r) => r.status === "done").length;
  const failedRuns = runs.filter((r) => r.status === "failed").length;

  return (
    <div className="min-h-screen grid-bg">
      {/* Header */}
      <div className="px-8 pt-8 pb-0">
        <div className="flex items-center justify-between mb-2">
          <div>
            <h1 className="text-2xl font-bold text-white">Dashboard</h1>
            <p className="text-sm text-slate-500 mt-0.5">
              AI-powered autonomous software engineering agent
            </p>
          </div>
          <div className="flex items-center gap-2 text-sm">
            <div
              className={`w-2 h-2 rounded-full ${
                apiOnline === null
                  ? "bg-slate-500"
                  : apiOnline
                  ? "bg-emerald-400 animate-pulse"
                  : "bg-red-500"
              }`}
            />
            <span className="text-slate-400">
              {apiOnline === null
                ? "Connecting…"
                : apiOnline
                ? "API Online"
                : "API Offline — start docker-compose up"}
            </span>
          </div>
        </div>
      </div>

      {/* API offline banner */}
      {apiOnline === false && (
        <div className="mx-8 mt-4 px-4 py-3 rounded-lg bg-red-950/40 border border-red-500/30 text-red-300 text-sm flex items-center gap-3">
          <svg width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <circle cx="12" cy="12" r="10" /><path d="M12 8v4M12 16h.01" />
          </svg>
          <span>
            Backend not reachable at <code className="font-mono text-red-200">localhost:8000</code>.
            Run <code className="font-mono text-red-200">docker-compose up --build</code> to start it.
          </span>
        </div>
      )}

      {/* Stat cards */}
      <div className="grid grid-cols-4 gap-4 px-8 mt-6">
        <StatCard label="Total Runs" value={totalRuns} color="text-white" />
        <StatCard label="Active" value={activeRuns} color="text-yellow-400" sub="in progress" />
        <StatCard label="Completed" value={doneRuns} color="text-emerald-400" sub="PRs ready" />
        <StatCard label="Failed" value={failedRuns} color="text-red-400" sub="need attention" />
      </div>

      <div className="px-8 mt-8 grid grid-cols-5 gap-6">
        {/* Submit form */}
        <div className="col-span-2">
          <SubmitForm onSubmitted={fetchData} />
        </div>

        {/* Recent runs table */}
        <div className="col-span-3 glass overflow-hidden fade-in-up">
          <div className="px-5 py-4 border-b border-[rgba(139,92,246,0.12)] flex items-center justify-between">
            <h2 className="text-sm font-semibold text-white">Recent Runs</h2>
            <Link
              href="/runs"
              className="text-xs text-violet-400 hover:text-violet-300 transition-colors"
            >
              View all →
            </Link>
          </div>
          {loading ? (
            <div className="flex items-center justify-center h-40 text-slate-500 text-sm">
              <div className="w-5 h-5 border-2 border-violet-600 border-t-transparent rounded-full animate-spin mr-3" />
              Loading…
            </div>
          ) : runs.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-40 text-slate-500 text-sm gap-2">
              <svg width="36" height="36" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24" className="text-slate-700">
                <path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2M9 5a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2M9 5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2" />
              </svg>
              No runs yet — submit your first task!
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left">
                <thead>
                  <tr className="border-b border-[rgba(139,92,246,0.12)]">
                    {["Run ID", "Repository", "Issue", "Status", "Started", ""].map(
                      (h) => (
                        <th
                          key={h}
                          className="px-4 py-2.5 text-xs font-semibold text-slate-500 uppercase tracking-wider"
                        >
                          {h}
                        </th>
                      )
                    )}
                  </tr>
                </thead>
                <tbody>
                  {runs.map((r) => (
                    <RunRow
                      key={r.run_id}
                      run={r}
                      onRestart={handleRestart}
                      onDelete={handleDelete}
                      isRestarting={restartingId === r.run_id}
                      isDeleting={deletingId === r.run_id}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
