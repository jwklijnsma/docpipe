import type { Job } from "./types";

export function ago(ts: number | null | undefined, now = Date.now() / 1000): string {
  if (!ts) return "never";
  const s = Math.max(0, now - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export function dur(s: number | null | undefined): string {
  if (s == null) return "";
  s = Math.round(s);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

export function size(n: number | null | undefined): string {
  if (n == null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

export function plural(n: number, word: string, many = word + "s"): string {
  return `${n} ${n === 1 ? word : many}`;
}

export function shortPath(p: string): string {
  const parts = p.split("/").filter(Boolean);
  return parts.length > 3 ? "…/" + parts.slice(-3).join("/") : p;
}

export function jobTitle(j: Pick<Job, "dry_run">): string {
  return j.dry_run ? "Dry run" : "Generate docs";
}

export function exitText(j: Job): string {
  switch (j.status) {
    case "queued":
      return j.position ? `waiting · #${j.position} in the queue` : "waiting";
    case "running":
      return j.stage || "starting";
    case "cancelled":
      return j.started ? "cancelled" : "cancelled before it started";
    case "failed":
      return j.result?.error ? `failed: ${j.result.error}` : `failed (exit ${j.exit_code})`;
    case "done": {
      const r = j.result;
      if (!r) return "completed";
      const pages = plural(r.pages, "page");
      return r.issues ? `${pages} · ${plural(r.issues, "QA warning")}` : `${pages} · QA passed`;
    }
  }
}

export function progressPct(j: Job): number | null {
  if (j.status === "queued") return 0;
  if (j.status === "running") return null; // indeterminate: the model decides how long
  return 100;
}

export function logClass(text: string): string {
  if (text.startsWith("$ ")) return "l-cmd";
  if (/\[(ERROR|CRITICAL)\]|^error|✗|❌/.test(text)) return "l-bad";
  if (/\[WARNING\]|^warning|⚠/.test(text)) return "l-warn";
  if (/\[SUCCESS\]|✓|✅/.test(text)) return "l-ok";
  return "";
}

/** Read and write per-browser conveniences; storage can be missing or throw. */
export function stored<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem("ansreview:" + key);
    return v === null ? fallback : (JSON.parse(v) as T);
  } catch {
    return fallback;
  }
}
export function store(key: string, value: unknown): void {
  try {
    localStorage.setItem("ansreview:" + key, JSON.stringify(value));
  } catch {
    /* private mode etc. */
  }
}
