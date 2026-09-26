"use client";

import { useState, useEffect, useCallback } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import type { Run } from "@/lib/types";
import StatusBadge from "@/components/StatusBadge";

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

export default function RunsPage() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [loading, setLoading] = useState(true);

  const fetchRuns = useCallback(async () => {
    try {
      const data = await api.listRuns(50);
      setRuns(data);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchRuns();
    const interval = setInterval(fetchRuns, 5000);
    return () => clearInterval(interval);
  }, [fetchRuns]);

  return (
    <div className="min-h-screen grid-bg px-8 py-8">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-white">All Runs</h1>
          <p className="text-sm text-slate-500 mt-0.5">
            {runs.length} total — auto-refreshes every 5s
          </p>
        </div>
        <Link
          href="/submit"
          className="btn-glow px-4 py-2 rounded-lg text-sm font-semibold text-white flex items-center gap-2"
        >
          <svg width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
            <path d="M12 5v14M5 12h14" />
          </svg>
          New Task
        </Link>
      </div>

      <div className="glass overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center h-48 text-slate-500 gap-3">
            <div className="w-5 h-5 border-2 border-violet-600 border-t-transparent rounded-full animate-spin" />
            Loading runs…
          </div>
        ) : runs.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-48 gap-3 text-slate-500">
            <svg width="40" height="40" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24" className="text-slate-700">
              <path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2M9 5a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2M9 5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2" />
            </svg>
            <p className="text-sm">No runs yet</p>
            <Link href="/submit" className="text-sm text-violet-400 hover:underline">
              Submit your first task →
            </Link>
          </div>
        ) : (
          <table className="w-full text-left">
            <thead>
              <tr className="border-b border-[rgba(139,92,246,0.12)]">
                {["Run ID", "Repository", "Issue", "Status", "Files", "Language", "Started", ""].map(
                  (h) => (
                    <th
                      key={h}
                      className="px-4 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wider"
                    >
                      {h}
                    </th>
                  )
                )}
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => {
                const trace = run.trace ?? [];
                const inspect = trace.find((t) => t.type === "inspection_complete");
                const lang = inspect?.data?.primary_language as string | undefined;
                const files = inspect?.data?.total_files as number | undefined;

                return (
                  <tr
                    key={run.run_id}
                    className="border-b border-[rgba(139,92,246,0.08)] hover:bg-[rgba(139,92,246,0.04)] transition-colors"
                  >
                    <td className="py-3 px-4">
                      <code className="text-xs text-violet-400 font-mono">
                        {run.run_id.slice(0, 8)}
                      </code>
                    </td>
                    <td className="py-3 px-4">
                      <p className="text-sm text-slate-200">{repoName(run.repo_url)}</p>
                    </td>
                    <td className="py-3 px-4 text-sm text-slate-400">
                      {run.issue_number ? `#${run.issue_number}` : "—"}
                    </td>
                    <td className="py-3 px-4">
                      <StatusBadge status={run.status} />
                    </td>
                    <td className="py-3 px-4 text-xs text-slate-500">
                      {files != null ? files : "—"}
                    </td>
                    <td className="py-3 px-4 text-xs text-slate-500 capitalize">
                      {lang ?? "—"}
                    </td>
                    <td className="py-3 px-4 text-xs text-slate-500">
                      {timeAgo(run.created_at)}
                    </td>
                    <td className="py-3 px-4">
                      <Link
                        href={`/runs/${run.run_id}`}
                        className="text-xs px-3 py-1.5 rounded-md border border-[rgba(139,92,246,0.3)] text-violet-400 hover:bg-[rgba(139,92,246,0.1)] transition-colors"
                      >
                        View →
                      </Link>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
