import type { Run, SubmitTaskPayload } from "./types";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  /** Submit a new task */
  submitTask: (payload: SubmitTaskPayload): Promise<Run> =>
    request<Run>("/tasks", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  /** Get a single run by ID */
  getRun: (runId: string): Promise<Run> =>
    request<Run>(`/tasks/${runId}`),

  /** List recent runs */
  listRuns: (limit = 20): Promise<Run[]> =>
    request<Run[]>(`/tasks?limit=${limit}`),

  /** Health check */
  health: (): Promise<{ status: string }> =>
    request<{ status: string }>("/health"),

  /** Restart / retry a run */
  restartRun: (runId: string): Promise<Run> =>
    request<Run>(`/tasks/${runId}/restart`, {
      method: "POST",
    }),

  /** Delete a run */
  deleteRun: (runId: string): Promise<{ status: string }> =>
    request<{ status: string }>(`/tasks/${runId}`, {
      method: "DELETE",
    }),

  /** Delete all failed runs */
  cleanupFailedRuns: (): Promise<{ deleted_count: number }> =>
    request<{ deleted_count: number }>("/tasks/failed/cleanup", {
      method: "DELETE",
    }),
};
