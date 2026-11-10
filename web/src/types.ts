export interface UploadInfo {
  filename: string;
  uploaded: number;
  files: number;
  bytes: number;
  skipped: number;
}

export type SourceType = "helm" | "k8s" | "argocd" | "ansible" | "git" | "local";
export const SOURCE_TYPES: SourceType[] = ["helm", "k8s", "argocd", "ansible", "git", "local"];
export const TYPE_LABEL: Record<string, string> = {
  helm: "Helm", k8s: "Kubernetes", argocd: "Argo CD", ansible: "Ansible", git: "Git", local: "Project",
};

export interface Repo {
  id: string;
  name: string;
  path: string;
  type: SourceType;
  upload: UploadInfo | null;
  docs_time: number | null;
  pages: number;
  languages: string[];
  status: string | null;
  issues: number;
}

export interface LastRun {
  time: number;
  model: string | null;
  status: string | null;
  type: string | null;
  seconds: number | null;
  issues: string[];
  error: string | null;
  outputs: string[];
}

export interface RepoDetail extends Repo {
  branch: string | null;
  last_commit: string | null;
  model: string;
  last_run: LastRun | null;
}

export interface Page {
  path: string;      // "nl/apps/owncast.md"
  title: string;
  index: boolean;
  size: number;
  time: number;
}

export interface Docs {
  languages: Record<string, Page[]>;
  last_run: LastRun | null;
  mkdocs: boolean;
}

export interface Artifact {
  name: string;
  label: string;
  url: string;
  primary?: boolean;
}

export type JobStatus = "queued" | "running" | "done" | "failed" | "cancelled";

export interface Job {
  id: string;
  repo: string;
  repo_name: string;
  action: "generate";
  options: Record<string, unknown>;
  status: JobStatus;
  dry_run: boolean;
  exit_code: number | null;
  created: number;
  started: number | null;
  ended: number | null;
  seconds: number;
  stage: string;
  downloads: Artifact[];
  result: { status: string | null; issues: number; pages: number; error: string | null } | null;
  position: number | null;
  lines?: string[];
  next?: number;
}

export interface Status {
  version: string;
  endpoints: string[];
  model: string;
  model_source: "ui" | "config";
  default_model: string;
  ollama: string | null;
  ollama_error?: string;
  model_available: boolean | null;
  models: string[];
  roots: string[];
  auth: boolean;
  primary_language: string;
  languages: string[];
  uploads: { enabled: boolean; max_mb?: number; keep_days?: number };
}

export const isActive = (j: Job) => j.status === "queued" || j.status === "running";

export const LANG_NAMES: Record<string, string> = {
  nl: "Nederlands", en: "English", de: "Deutsch", fr: "Français", fy: "Frysk", es: "Español",
};
