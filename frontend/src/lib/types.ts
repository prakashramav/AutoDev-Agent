// Types shared between frontend components and API responses

export type RunStatus =
  | "pending"
  | "cloning"
  | "inspecting"
  | "planning"
  | "modifying"
  | "testing"
  | "reviewing"
  | "creating_pr"
  | "done"
  | "failed"
  | "awaiting_confirmation";

export interface TraceEntry {
  timestamp: string;
  type: string;
  data?: Record<string, unknown>;
}

export interface Run {
  run_id: string;
  status: RunStatus;
  repo_url: string;
  issue_number: number | null;
  issue_text: string | null;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
  sandbox_container_id: string | null;
  file_tree: string | null;
  relevant_files: string[] | null;
  fix_plan: string | null;
  diff: string | null;
  test_results: Record<string, unknown> | null;
  review_notes: string | null;
  pr_url: string | null;
  error_message: string | null;
  trace: TraceEntry[] | null;
}

export interface SubmitTaskPayload {
  repo_url: string;
  issue_number?: number;
  issue_text?: string;
}
