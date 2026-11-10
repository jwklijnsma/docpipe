import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, ApiError, AuthError, enc } from "./api";
import { isActive, type Artifact, type Job, type Repo, type Status } from "./types";
import { dur, jobTitle } from "./util";

export interface Toast {
  id: number;
  text: string;
  link?: { label: string; href?: string; download?: boolean; onClick?: () => void };
  kind?: "ok" | "bad";
}

interface Ctx {
  authed: boolean | null;
  setAuthed: (v: boolean) => void;
  status: Status | null;
  repos: Repo[];
  jobs: Job[];
  /** Bumped per repo whenever a job on it finishes, so open pages reload their data. */
  repoVersion: Record<string, number>;
  toasts: Toast[];
  toast: (t: Omit<Toast, "id">, ms?: number) => void;
  dismiss: (id: number) => void;
  refreshRepos: () => Promise<void>;
  refreshJobs: () => Promise<void>;
  refreshStatus: () => Promise<void>;
  /** Set the model for every new scan and fix ("" = back to the configured default). */
  setModel: (model: string) => Promise<boolean>;
  /** Bumped when server settings change, so open pages reload. */
  settingsVersion: number;
  startJob: (repoId: string, body: Record<string, unknown>, note?: string) => Promise<Job | null>;
  cancelJob: (id: string) => Promise<void>;
  jobsOpen: boolean;
  setJobsOpen: (v: boolean) => void;
  /** Open the jobs panel with this job's log expanded. */
  openJob: (id: string) => void;
  focusJob: string | null;
  handleError: (err: unknown) => void;
}

const AppContext = createContext<Ctx | null>(null);

export function useApp(): Ctx {
  const c = useContext(AppContext);
  if (!c) throw new Error("useApp outside AppProvider");
  return c;
}

let toastSeq = 0;

export function primaryDownload(j: Job): Artifact | undefined {
  return j.downloads.find((d) => d.primary) ?? j.downloads[0];
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [repos, setRepos] = useState<Repo[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [repoVersion, setRepoVersion] = useState<Record<string, number>>({});
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [jobsOpen, setJobsOpen] = useState(false);
  const [settingsVersion, setSettingsVersion] = useState(0);
  const [focusJob, setFocusJob] = useState<string | null>(null);
  const openJob = useCallback((id: string) => {
    setFocusJob(id);
    setJobsOpen(true);
  }, []);
  const prevJobs = useRef<Map<string, Job["status"]>>(new Map());

  const dismiss = useCallback((id: number) => setToasts((t) => t.filter((x) => x.id !== id)), []);
  const toast = useCallback(
    (t: Omit<Toast, "id">, ms = 6000) => {
      const id = ++toastSeq;
      setToasts((list) => [...list.slice(-3), { ...t, id }]);
      window.setTimeout(() => dismiss(id), ms);
    },
    [dismiss],
  );

  const handleError = useCallback(
    (err: unknown) => {
      if (err instanceof AuthError) setAuthed(false);
      else toast({ text: err instanceof Error ? err.message : String(err), kind: "bad" });
    },
    [toast],
  );

  const refreshRepos = useCallback(async () => {
    try {
      const d = await api<{ repos: Repo[] }>("repos");
      setRepos(d.repos);
      setAuthed(true);
    } catch (err) {
      if (err instanceof AuthError) setAuthed(false);
      else handleError(err);
    }
  }, [handleError]);

  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await api<Status>("status"));
    } catch (err) {
      if (err instanceof AuthError) setAuthed(false);
    }
  }, []);

  const refreshJobs = useCallback(async () => {
    let list: Job[];
    try {
      list = (await api<{ jobs: Job[] }>("jobs")).jobs;
    } catch (err) {
      if (err instanceof AuthError) setAuthed(false);
      return;
    }
    // Announce jobs that finished since the last poll (they may have run while you were elsewhere).
    const finished: Job[] = [];
    for (const j of list) {
      const before = prevJobs.current.get(j.id);
      if (before && (before === "queued" || before === "running") && !isActive(j)) finished.push(j);
    }
    prevJobs.current = new Map(list.map((j) => [j.id, j.status]));
    setJobs(list);
    if (finished.length) {
      setRepoVersion((v) => {
        const n = { ...v };
        for (const j of finished) n[j.repo] = (n[j.repo] || 0) + 1;
        return n;
      });
      refreshRepos();
      for (const j of finished) {
        const what = `${jobTitle(j)} of ${j.repo_name}`;
        if (j.status === "done") {
          const pages = j.result ? `${j.result.pages} page${j.result.pages === 1 ? "" : "s"}` : "docs";
          const qa = j.result && j.result.issues ? `, ${j.result.issues} QA warning${j.result.issues === 1 ? "" : "s"}` : "";
          toast(
            { text: `${what} finished in ${dur(j.seconds)}: ${pages}${qa}.`, kind: "ok", link: { label: "Read", href: `#/r/${enc(j.repo)}/docs` } },
            12000,
          );
        } else if (j.status === "failed") {
          toast({ text: `${what} failed (exit ${j.exit_code}).`, kind: "bad", link: { label: "Show log", onClick: () => openJob(j.id) } }, 12000);
        }
      }
    }
  }, [refreshRepos, toast, openJob]);

  const setModel = useCallback(
    async (model: string) => {
      try {
        const r = await api<{ model: string; model_source: string }>("settings", { method: "POST", body: { model } });
        await refreshStatus();
        setSettingsVersion((v) => v + 1);
        toast({ text: r.model_source === "ui" ? `New docs are written with ${r.model}.` : `Back to the default model, ${r.model}.`, kind: "ok" });
        return true;
      } catch (err) {
        handleError(err);
        return false;
      }
    },
    [refreshStatus, toast, handleError],
  );

  const startJob = useCallback(
    async (repoId: string, body: Record<string, unknown>, note?: string) => {
      try {
        const j = await api<Job>(`repos/${enc(repoId)}/generate`, { method: "POST", body });
        await refreshJobs();
        // The server's own view: running already, or its place in the queue.
        const now = await api<Job>(`jobs/${j.id}`).catch(() => j);
        // One short line; the job card and the jobs panel show the rest.
        const where = now.position ? `queued (#${now.position} in the queue)` : "started";
        toast({ text: `${note ? note + " " : ""}${jobTitle(j)} ${where}. It keeps running if you leave.` }, 4000);
        return j;
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) toast({ text: err.message });
        else handleError(err);
        return null;
      }
    },
    [refreshJobs, toast, handleError],
  );

  const cancelJob = useCallback(
    async (id: string) => {
      try {
        await api(`jobs/${id}/cancel`, { method: "POST" });
        await refreshJobs();
      } catch (err) {
        handleError(err);
      }
    },
    [refreshJobs, handleError],
  );

  // Boot + polling. Jobs poll fast while anything is queued or running.
  useEffect(() => {
    refreshRepos();
  }, [refreshRepos]);
  useEffect(() => {
    if (!authed) return;
    refreshStatus();
    refreshJobs();
    const t = window.setInterval(refreshStatus, 30000);
    return () => window.clearInterval(t);
  }, [authed, refreshStatus, refreshJobs]);
  const anyActive = jobs.some(isActive);
  useEffect(() => {
    if (!authed) return;
    const t = window.setInterval(refreshJobs, anyActive ? 1200 : 6000);
    return () => window.clearInterval(t);
  }, [authed, anyActive, refreshJobs]);

  // The tab title shows background work, so it is visible from other tabs too.
  useEffect(() => {
    const running = jobs.filter(isActive).length;
    document.title = running ? `(${running}) docpipe` : "docpipe";
  }, [jobs]);

  const value = useMemo<Ctx>(
    () => ({ authed, setAuthed, status, repos, jobs, repoVersion, toasts, toast, dismiss, refreshRepos, refreshJobs, refreshStatus, setModel, settingsVersion, startJob, cancelJob, jobsOpen, setJobsOpen, openJob, focusJob, handleError }),
    [authed, status, repos, jobs, repoVersion, toasts, toast, dismiss, refreshRepos, refreshJobs, refreshStatus, setModel, settingsVersion, startJob, cancelJob, jobsOpen, openJob, focusJob, handleError],
  );
  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

/** `#/r/<repo>/<tab>` routing. */
export function useRoute(): { repoId: string | null; tab: string } {
  const parse = () => {
    const r = location.hash.match(/^#\/r\/([^/]+)(?:\/(\w+))?/);
    return { repoId: r ? decodeURIComponent(r[1]) : null, tab: (r && r[2]) || "docs" };
  };
  const [route, setRoute] = useState(parse);
  useEffect(() => {
    const on = () => setRoute(parse());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

export const go = (repoId: string, tab = "docs") => {
  location.hash = `#/r/${enc(repoId)}/${tab}`;
};
