"use client";

import { useState, useEffect, useCallback } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { api } from "@/lib/api";
import type { Run, TraceEntry } from "@/lib/types";
import StatusBadge from "@/components/StatusBadge";

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
  const [run, setRun] = useState<Run | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const isActive = run
    ? !["done", "failed"].includes(run.status)
    : false;

  const fetchRun = useCallback(async () => {
    try {
      const data = await api.getRun(id);
      setRun(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load run");
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    fetchRun();
    if (isActive) {
      const interval = setInterval(fetchRun, 3000);
      return () => clearInterval(interval);
    }
  }, [fetchRun, isActive]);

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
                <InfoRow label="PR">
                  <a
                    href={run.pr_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-violet-400 hover:underline"
                  >
                    {run.pr_url}
                  </a>
                </InfoRow>
              )}
              {run.error_message && (
                <div className="py-3">
                  <p className="text-xs font-semibold text-red-400 uppercase tracking-wider mb-2">
                    Error
                  </p>
                  <pre className="text-xs text-red-300 bg-red-950/20 border border-red-500/20 rounded-md p-3 whitespace-pre-wrap break-words">
                    {run.error_message}
                  </pre>
                </div>
              )}
            </div>
          </div>

          {/* Test results */}
          {run.test_results && (
            <div className="glass overflow-hidden">
              <div className="px-4 py-3 border-b border-[rgba(139,92,246,0.12)]">
                <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
                  Test Results
                </h2>
              </div>
              <div className="p-4">
                <pre className="text-xs text-slate-300 whitespace-pre-wrap break-words">
                  {JSON.stringify(run.test_results, null, 2)}
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
    </div>
  );
}
