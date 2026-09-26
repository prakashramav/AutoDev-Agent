"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";

export default function SubmitForm({ onSubmitted }: { onSubmitted?: () => void }) {
  const router = useRouter();
  const [repoUrl, setRepoUrl] = useState("");
  const [issueNumber, setIssueNumber] = useState("");
  const [issueText, setIssueText] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    if (!repoUrl.trim()) {
      setError("Repository URL is required");
      return;
    }
    if (!issueNumber && !issueText.trim()) {
      setError("Provide an issue number or description");
      return;
    }

    setLoading(true);
    try {
      const run = await api.submitTask({
        repo_url: repoUrl.trim(),
        issue_number: issueNumber ? parseInt(issueNumber) : undefined,
        issue_text: issueText.trim() || undefined,
      });
      onSubmitted?.();
      router.push(`/runs/${run.run_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to submit task");
    } finally {
      setLoading(false);
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="glass p-6 fade-in-up h-fit"
      id="submit-task-form"
    >
      <div className="flex items-center gap-2 mb-5">
        <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-violet-600 to-blue-600 flex items-center justify-center">
          <svg width="14" height="14" fill="none" stroke="white" strokeWidth="2.5" viewBox="0 0 24 24">
            <path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z" />
          </svg>
        </div>
        <h2 className="text-sm font-semibold text-white">Submit a GitHub Issue</h2>
      </div>

      <div className="space-y-4">
        {/* Repo URL */}
        <div>
          <label className="text-xs font-medium text-slate-400 block mb-1.5">
            GitHub Repository URL <span className="text-red-400">*</span>
          </label>
          <input
            id="repo-url-input"
            type="url"
            value={repoUrl}
            onChange={(e) => setRepoUrl(e.target.value)}
            placeholder="https://github.com/owner/repo"
            className="input-field w-full px-3 py-2.5 text-sm"
            required
          />
        </div>

        {/* Issue number */}
        <div>
          <label className="text-xs font-medium text-slate-400 block mb-1.5">
            Issue Number
          </label>
          <input
            id="issue-number-input"
            type="number"
            value={issueNumber}
            onChange={(e) => setIssueNumber(e.target.value)}
            placeholder="#143"
            min={1}
            className="input-field w-full px-3 py-2.5 text-sm"
          />
        </div>

        {/* Divider */}
        <div className="flex items-center gap-3 my-1">
          <div className="flex-1 h-px bg-[rgba(139,92,246,0.12)]" />
          <span className="text-[10px] text-slate-600 uppercase tracking-widest">or</span>
          <div className="flex-1 h-px bg-[rgba(139,92,246,0.12)]" />
        </div>

        {/* Issue text */}
        <div>
          <label className="text-xs font-medium text-slate-400 block mb-1.5">
            Issue Description
          </label>
          <textarea
            id="issue-text-input"
            value={issueText}
            onChange={(e) => setIssueText(e.target.value)}
            placeholder="Login API returns 500 when user doesn't provide an email…"
            rows={3}
            className="input-field w-full px-3 py-2.5 text-sm resize-none"
          />
        </div>

        {/* Error */}
        {error && (
          <p className="text-xs text-red-400 bg-red-950/30 border border-red-500/20 rounded-md px-3 py-2">
            {error}
          </p>
        )}

        {/* Submit button */}
        <button
          id="run-agent-btn"
          type="submit"
          disabled={loading}
          className="btn-glow w-full py-3 rounded-lg text-sm font-bold text-white disabled:opacity-60 disabled:cursor-not-allowed flex items-center justify-center gap-2"
        >
          {loading ? (
            <>
              <div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
              Submitting…
            </>
          ) : (
            <>
              <svg width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                <path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z" />
              </svg>
              Run Agent
            </>
          )}
        </button>
      </div>
    </form>
  );
}
