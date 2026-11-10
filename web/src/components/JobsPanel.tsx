import { useEffect, useRef, useState } from "react";
import { api, enc } from "../api";
import { primaryDownload, useApp } from "../store";
import { isActive, type Job } from "../types";
import { ago, dur, exitText, jobTitle, logClass, progressPct } from "../util";

/** Live log of one job, fetched incrementally while it is open. */
export function JobLog({ job }: { job: Job }) {
  const [lines, setLines] = useState<string[]>([]);
  const since = useRef(0);
  const box = useRef<HTMLDivElement>(null);
  const active = isActive(job);

  useEffect(() => {
    let stop = false;
    let timer = 0;
    since.current = 0;
    setLines([]);
    const tick = async () => {
      try {
        const d = await api<Job>(`jobs/${job.id}?since=${since.current}`);
        if (stop) return;
        if (d.lines && d.lines.length) setLines((l) => [...l, ...d.lines!].slice(-2000));
        since.current = d.next ?? since.current;
        if (isActive(d)) timer = window.setTimeout(tick, 1000);
      } catch {
        if (!stop) timer = window.setTimeout(tick, 3000);
      }
    };
    tick();
    return () => {
      stop = true;
      window.clearTimeout(timer);
    };
    // Restart when the job leaves the queue or finishes, to pick up the last lines.
  }, [job.id, active]);

  useEffect(() => {
    const el = box.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 60) el.scrollTop = el.scrollHeight;
  }, [lines]);

  return (
    <div className="log" role="log" ref={box}>
      {lines.length ? lines.map((l, i) => <div key={i} className={logClass(l)}>{l}</div>) : <div className="l-cmd">{job.status === "queued" ? "Waiting for the jobs before it…" : "…"}</div>}
    </div>
  );
}

export function Progress({ job }: { job: Job }) {
  const pct = progressPct(job);
  return (
    <div className={"progress" + (pct === null ? " indeterminate" : "") + (job.status === "queued" ? " waiting" : "")}>
      <div style={{ width: `${pct ?? 0}%` }} />
    </div>
  );
}

export function Downloads({ job, compact = false }: { job: Job; compact?: boolean }) {
  if (job.status !== "done" || !job.downloads.length) return null;
  const main = primaryDownload(job);
  return (
    <div className="downloads">
      {job.downloads.map((d) => (
        <a key={d.name} className={"btn small" + (d === main && !compact ? " primary" : "")} href={d.url} download>
          ↓ {d.label}
        </a>
      ))}
    </div>
  );
}

function dotClass(j: Job) {
  return j.status === "running" ? "run" : j.status === "queued" ? "wait" : j.status === "done" ? "ok" : "bad";
}

export function JobRow({ job, open, onToggle }: { job: Job; open: boolean; onToggle: () => void }) {
  const { cancelJob, setJobsOpen } = useApp();
  return (
    <li className={"job-row" + (open ? " open" : "")}>
      <div className="job-row-head">
        <span className={"dot " + dotClass(job)} />
        <div className="job-row-main">
          <div className="job-row-title">
            <strong>{jobTitle(job)}</strong>
            <a href={`#/r/${enc(job.repo)}/docs`} onClick={() => setJobsOpen(false)}>{job.repo_name}</a>
          </div>
          <div className="job-row-meta">
            {exitText(job)}
            {job.started ? ` · ${dur(job.seconds)}` : ` · queued ${ago(job.created)}`}
            {!isActive(job) && job.ended ? ` · ${ago(job.ended)}` : ""}
          </div>
        </div>
        <div className="job-row-actions">
          {isActive(job) && <button className="btn small danger" onClick={() => cancelJob(job.id)}>{job.status === "queued" ? "Remove" : "Cancel"}</button>}
          <button className="btn small" onClick={onToggle} aria-expanded={open}>{open ? "Hide log" : "Log"}</button>
        </div>
      </div>
      {isActive(job) && <Progress job={job} />}
      <Downloads job={job} compact />
      {open && <JobLog job={job} />}
    </li>
  );
}

export function JobsPanel() {
  const { jobs, jobsOpen, setJobsOpen, focusJob } = useApp();
  const [open, setOpen] = useState<string | null>(null);
  useEffect(() => {
    if (focusJob) setOpen(focusJob);
  }, [focusJob]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setJobsOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setJobsOpen]);
  if (!jobsOpen) return null;

  const running = jobs.filter((j) => j.status === "running");
  const queued = jobs.filter((j) => j.status === "queued").sort((a, b) => (a.position ?? 0) - (b.position ?? 0));
  const finished = jobs.filter((j) => !isActive(j));
  const section = (title: string, list: Job[]) =>
    list.length ? (
      <section>
        <h3>{title}</h3>
        <ul className="job-list">
          {list.map((j) => <JobRow key={j.id} job={j} open={open === j.id} onToggle={() => setOpen(open === j.id ? null : j.id)} />)}
        </ul>
      </section>
    ) : null;

  return (
    <>
      <div className="scrim" onClick={() => setJobsOpen(false)} />
      <aside className="jobs-panel" aria-label="Background jobs">
        <header>
          <h2>Background jobs</h2>
          <button className="btn small" onClick={() => setJobsOpen(false)} aria-label="Close">✕</button>
        </header>
        <p className="jobs-hint">Jobs run on the server one after another (they share the GPU) and keep going when you close this page.</p>
        {section("Running", running)}
        {section("Waiting", queued)}
        {section("Finished", finished)}
        {!jobs.length && <div className="empty"><p>No jobs yet. Generate docs for a source and it shows up here.</p></div>}
      </aside>
    </>
  );
}
