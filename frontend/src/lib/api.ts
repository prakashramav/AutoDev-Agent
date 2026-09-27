import type { Run, SubmitTaskPayload } from "./types";

const BASE = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/+$/, "");

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const url = `${BASE}${path.startsWith("/") ? path : `/${path}`}`;
  const res = await fetch(url, {
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
  deleteRun: async (runId: string): Promise<{ status: string }> => {
    try {
      return await request<{ status: string }>(`/tasks/${runId}`, {
        method: "DELETE",
      });
    } catch {
      return await request<{ status: string }>(`/tasks/${runId}/delete`, {
        method: "POST",
      });
    }
  },

  /** Delete all failed runs */
  cleanupFailedRuns: async (): Promise<{ deleted_count: number }> => {
    try {
      return await request<{ deleted_count: number }>("/tasks/failed/cleanup", {
        method: "DELETE",
      });
    } catch {
      return await request<{ deleted_count: number }>("/tasks/failed/cleanup", {
        method: "POST",
      });
    }
  },
};
