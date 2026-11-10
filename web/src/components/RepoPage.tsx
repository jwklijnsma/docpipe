import { useCallback, useEffect, useMemo, useState } from "react";
import { api, enc } from "../api";
import { Markdown, splitFrontMatter } from "../markdown";
import { go, useApp } from "../store";
import { isActive, LANG_NAMES, SOURCE_TYPES, TYPE_LABEL, type Docs, type Job, type Page, type RepoDetail } from "../types";
import { ago, dur, exitText, jobTitle, plural, shortPath, size, store, stored } from "../util";
import { Downloads, JobLog, Progress } from "./JobsPanel";

type Opts = { primary_lang?: string; langs?: string[]; fast?: boolean; force?: boolean; type?: string; subdir?: string; model?: string };
const LANGS = ["nl", "en", "de", "fr", "fy"];

function optsFor(id: string, primary: string, langs: string[]): Opts {
  return { primary_lang: primary, langs, ...stored<Opts>("opts:" + id, {}) };
}

function Options({ id, model, primary, langs }: { id: string; model: string; primary: string; langs: string[] }) {
  const [o, setO] = useState<Opts>(() => optsFor(id, primary, langs));
  const [open, setOpen] = useState(() => stored("opts-open", false));
  useEffect(() => setO(optsFor(id, primary, langs)), [id, primary, langs]);
  const set = (patch: Partial<Opts>) => {
    const n = { ...o, ...patch };
    setO(n);
    store("opts:" + id, n);
  };
  const p = o.primary_lang || "en";
  const targets = (o.langs || []).filter((l) => l !== p);
  const toggle = (l: string) => set({ langs: targets.includes(l) ? targets.filter((x) => x !== l) : [...targets, l] });
  const summary = [`${LANG_NAMES[p] ?? p}${targets.length ? " → " + targets.join(", ") : ""}`, o.fast && "fast", o.force && "rebuild all", o.model, o.subdir && "subdir " + o.subdir, o.type && o.type !== "auto" && o.type].filter(Boolean);
  return (
    <details className="card options" open={open} onToggle={(e) => { const v = (e.target as HTMLDetailsElement).open; setOpen(v); store("opts-open", v); }}>
      <summary>Options <span className="hint">· {summary.join(" · ")}</span></summary>
      <div className="opt-grid">
        <fieldset>
          <legend>Language</legend>
          <div className="field">
            <label htmlFor="o-primary">Write in</label>
            <select id="o-primary" value={p} onChange={(e) => set({ primary_lang: e.target.value })}>
              {LANGS.map((l) => <option key={l} value={l}>{LANG_NAMES[l]}</option>)}
            </select>
          </div>
          <div className="field">
            <label>Also translate to</label>
            <div className="chips">
              {LANGS.filter((l) => l !== p).map((l) => (
                <label key={l} className="check"><input type="checkbox" checked={targets.includes(l)} onChange={() => toggle(l)} />{LANG_NAMES[l]}</label>
              ))}
            </div>
          </div>
        </fieldset>
        <fieldset>
          <legend>Source</legend>
          <div className="field">
            <label htmlFor="o-type">Type</label>
            <select id="o-type" value={o.type || "auto"} onChange={(e) => set({ type: e.target.value })}>
              <option value="auto">detect automatically</option>
              {SOURCE_TYPES.map((t) => <option key={t} value={t}>{TYPE_LABEL[t]}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="o-subdir">Only this folder (monorepo)</label>
            <input id="o-subdir" type="text" placeholder="e.g. charts/owncast" value={o.subdir || ""} onChange={(e) => set({ subdir: e.target.value })} />
          </div>
        </fieldset>
        <fieldset>
          <legend>Run</legend>
          <div className="field">
            <label htmlFor="o-model">Model override</label>
            <input id="o-model" type="text" placeholder={model} value={o.model || ""} onChange={(e) => set({ model: e.target.value })} />
          </div>
          <label className="check"><input type="checkbox" checked={!!o.fast} onChange={(e) => set({ fast: e.target.checked })} />Fast: key files only, one QA round</label>
          <label className="check"><input type="checkbox" checked={!!o.force} onChange={(e) => set({ force: e.target.checked })} />Rebuild everything (ignore the cache)</label>
        </fieldset>
      </div>
    </details>
  );
}

function RepoJob({ job }: { job: Job }) {
  const { cancelJob, openJob } = useApp();
  const [showLog, setShowLog] = useState(false);
  const active = isActive(job);
  useEffect(() => setShowLog(job.status === "failed"), [job.id, job.status]);
  const label = { queued: "Waiting", running: "Writing", done: "Finished", failed: "Failed", cancelled: "Cancelled" }[job.status];
  return (
    <div className={"card job-card " + job.status}>
      <div className="card-head">
        <div className="job-status">
          <span className={"dot " + (job.status === "running" ? "run" : job.status === "queued" ? "wait" : job.status === "done" ? "ok" : "bad")} />
          <strong>{jobTitle(job)} · {label}</strong>
          <span className="muted">{job.started ? dur(job.seconds) : ""}{!active && job.ended ? ` · ${ago(job.ended)}` : ""}</span>
        </div>
        <div className="actions">
          <button className="btn small" onClick={() => setShowLog(!showLog)}>{showLog ? "Hide log" : "Log"}</button>
          {active && <button className="btn small danger" onClick={() => cancelJob(job.id)}>{job.status === "queued" ? "Remove from queue" : "Cancel"}</button>}
          <button className="btn small" onClick={() => openJob(job.id)}>All jobs</button>
        </div>
      </div>
      <div className="card-body">
        {active && <Progress job={job} />}
        <div className="progress-meta"><span className="stage">{exitText(job)}</span>{job.status === "running" && <span>runs in the background · you can leave this page</span>}</div>
        <Downloads job={job} />
        {showLog && <JobLog job={job} />}
      </div>
    </div>
  );
}

/** Page list + rendered page, per language. */
function DocsViewer({ repoId, docs, onGenerate }: { repoId: string; docs: Docs | null; onGenerate: () => void }) {
  const langs = useMemo(() => Object.keys(docs?.languages || {}), [docs]);
  // Open in the language the docs were written in (the first page of the last run).
  const written = docs?.last_run?.outputs?.[0]?.match(/docs\/([^/]+)\//)?.[1] ?? "";
  const [lang, setLang] = useState<string>(() => stored(`lang:${repoId}`, "") || written);
  const current = langs.includes(lang) ? lang : langs[0];
  const pages: Page[] = (current && docs?.languages[current]) || [];
  const [path, setPath] = useState<string | null>(null);
  const shown = pages.find((p) => p.path === path) ?? pages.find((p) => !p.index) ?? pages[0];
  const [text, setText] = useState<string | null>(null);
  const [raw, setRaw] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!shown) return;
    let stop = false;
    setText(null);
    setError("");
    api<{ content: string }>(`repos/${enc(repoId)}/page?path=${enc(shown.path)}`)
      .then((d) => !stop && setText(d.content), (e) => !stop && setError(e.message));
    return () => { stop = true; };
  }, [repoId, shown?.path, shown?.time]);

  if (!docs || !langs.length) {
    return (
      <div className="empty">
        <h3>No docs yet</h3>
        <p>Generate them with the button above. It runs in the background; you can close this page meanwhile.</p>
        <button className="btn primary" onClick={onGenerate}>Generate docs</button>
      </div>
    );
  }
  const pick = (l: string) => { setLang(l); store(`lang:${repoId}`, l); setPath(null); };
  // Links between pages: resolve relative .md links against the current page.
  const resolve = (href: string) => {
    if (!shown || /^[a-z]+:/i.test(href) || href.startsWith("#")) return null;
    const clean = href.split("#")[0];
    if (!clean.endsWith(".md")) return null;
    const parts = shown.path.split("/").slice(0, -1);
    for (const seg of clean.split("/")) {
      if (seg === "..") parts.pop();
      else if (seg && seg !== ".") parts.push(seg);
    }
    const target = parts.join("/");
    return pages.some((p) => p.path === target) ? target : null;
  };
  const fm: Record<string, string> = text ? splitFrontMatter(text)[0] : {};
  const depth = (p: Page) => p.path.split("/").length - 2;
  return (
    <div className="docs">
      <nav className="docs-nav" aria-label="Pages">
        {langs.length > 1 && (
          <div className="seg lang-seg" role="group" aria-label="Language">
            {langs.map((l) => <button key={l} aria-pressed={l === current} onClick={() => pick(l)}>{l.toUpperCase()}</button>)}
          </div>
        )}
        <ul>
          {pages.map((p) => (
            <li key={p.path}>
              <button aria-current={p.path === shown?.path} style={{ paddingLeft: 10 + depth(p) * 14 }} onClick={() => setPath(p.path)} title={p.path}>
                {p.index ? "Overview" : p.title}
              </button>
            </li>
          ))}
        </ul>
      </nav>
      <article className="docs-page">
        {shown && (
          <div className="docs-toolbar">
            <span className="mono muted small">{shown.path}</span>
            {fm.revision && <span className="muted small">rev {fm.revision}</span>}
            <span className="muted small">{ago(shown.time)}</span>
            <div className="actions">
              <div className="seg" role="group" aria-label="View">
                <button aria-pressed={!raw} onClick={() => setRaw(false)}>Page</button>
                <button aria-pressed={raw} onClick={() => setRaw(true)}>Markdown</button>
              </div>
              <a className="btn small" href={`/api/repos/${enc(repoId)}/page?path=${enc(shown.path)}&download=1`} download>↓ .md</a>
            </div>
          </div>
        )}
        {error ? <p className="errors">{error}</p>
          : text === null ? <p className="muted">Loading…</p>
            : raw ? <pre className="md-code raw"><code>{text}</code></pre>
              : <div className="md prose"><Markdown src={text} resolve={resolve} onNavigate={(p) => setPath(p)} /></div>}
      </article>
    </div>
  );
}

function RunTab({ detail }: { detail: RepoDetail }) {
  const r = detail.last_run;
  if (!r) return <div className="empty"><p>No run yet.</p></div>;
  return (
    <div>
      <div className="card">
        <div className="card-head"><h3>Last run</h3></div>
        <div className="card-body facts-grid">
          <div><span className="muted small">Result</span><strong className={r.status === "ok" ? "ok-text" : "bad-text"}>{r.status ?? "?"}</strong></div>
          <div><span className="muted small">Type</span><strong>{TYPE_LABEL[r.type ?? ""] ?? r.type}</strong></div>
          <div><span className="muted small">Model</span><strong className="mono">{r.model}</strong></div>
          <div><span className="muted small">Duration</span><strong>{dur(r.seconds)}</strong></div>
          <div><span className="muted small">When</span><strong>{ago(r.time)}</strong></div>
        </div>
      </div>
      {r.error && <div className="callout warn">{r.error}</div>}
      <div className="card">
        <div className="card-head"><h3>QA warnings</h3><span className="muted small">{r.issues.length ? "things the reviewer agent could not fix; worth a human look" : ""}</span></div>
        <div className="card-body">
          {r.issues.length ? <ul className="notes">{r.issues.map((x, i) => <li key={i}>{x}</li>)}</ul> : <p className="muted tight">None: every page passed QA.</p>}
        </div>
      </div>
      <div className="card">
        <div className="card-head"><h3>Pages written</h3></div>
        <div className="card-body"><ul className="notes">{r.outputs.map((o) => <li key={o} className="mono small">{o.replace(/^site\/docs\//, "")}</li>)}</ul></div>
      </div>
    </div>
  );
}

function DownloadsTab({ detail, docs }: { detail: RepoDetail; docs: Docs | null }) {
  const has = !!docs && Object.keys(docs.languages).length > 0;
  const base = `/api/repos/${enc(detail.id)}`;
  return (
    <div className="card">
      <ul className="dl-list">
        <li>
          <div><strong>All docs (ZIP)</strong><div className="muted small">{has ? `${plural(detail.pages, "page")} in ${detail.languages.join(", ")}, with mkdocs.yml: unzip and run "mkdocs serve", or copy docs/ into your docs repo` : "generate docs first"}</div></div>
          {has ? <a className="btn small primary" href={`${base}/docs.zip`} download>↓ Download</a> : <span className="muted small">—</span>}
        </li>
        {detail.upload && (
          <li>
            <div><strong>Uploaded project (ZIP)</strong><div className="muted small">{detail.upload.files} files · {size(detail.upload.bytes)}</div></div>
            <a className="btn small" href={`${base}/download`} download>↓ Download</a>
          </li>
        )}
      </ul>
    </div>
  );
}

export function RepoPage({ repoId, tab }: { repoId: string; tab: string }) {
  const { status, jobs, repoVersion, settingsVersion, startJob, handleError, refreshRepos, toast } = useApp();
  const [detail, setDetail] = useState<RepoDetail | null>(null);
  const [docs, setDocs] = useState<Docs | null>(null);
  const [error, setError] = useState("");
  const version = repoVersion[repoId] || 0;
  const primary = status?.primary_language || "en";
  const defaultLangs = useMemo(() => status?.languages || [], [status]);

  const load = useCallback(async () => {
    try {
      const [d, x] = await Promise.all([api<RepoDetail>(`repos/${enc(repoId)}`), api<Docs>(`repos/${enc(repoId)}/docs`)]);
      setDetail(d);
      setDocs(x);
      setError("");
    } catch (err) {
      if ((err as { status?: number }).status === 404) setError("This source is gone (deleted, or the server was restarted with other --root folders).");
      else handleError(err);
    }
  }, [repoId, handleError]);
  useEffect(() => { setDetail(null); setDocs(null); }, [repoId]);
  useEffect(() => { load(); }, [load, version, settingsVersion]);

  if (error) return <div className="empty"><h3>Could not open this source</h3><p>{error}</p></div>;
  if (!detail) return <div className="empty"><p>Loading…</p></div>;

  const repoJobs = jobs.filter((j) => j.repo === repoId);
  const latest = repoJobs[0];
  const busy = repoJobs.some(isActive);
  const up = detail.upload;
  const generate = (dry = false) => {
    const o = optsFor(repoId, primary, defaultLangs);
    const p = o.primary_lang || primary;
    startJob(repoId, { ...o, primary_lang: p, langs: (o.langs || []).filter((l) => l !== p), dry_run: dry || undefined });
  };
  const clear = async () => {
    if (!confirm(`Remove the generated docs of ${detail.name} (and its cache) from this server?`)) return;
    try { await api(`repos/${enc(repoId)}/clear`, { method: "POST" }); toast({ text: "Docs removed." }); load(); refreshRepos(); } catch (err) { handleError(err); }
  };
  const del = async () => {
    if (!confirm(`Delete the uploaded ${detail.name} and its docs from this server?`)) return;
    try { await api(`repos/${enc(repoId)}/delete`, { method: "POST" }); toast({ text: `${detail.name} deleted.` }); await refreshRepos(); location.hash = ""; } catch (err) { handleError(err); }
  };
  const hasDocs = detail.pages > 0;
  const tabs: [string, string, number | null][] = [["docs", "Docs", detail.pages || null], ["run", "Run & QA", detail.issues || null], ["downloads", "Downloads", null]];

  return (
    <div>
      <div className="repo-head">
        <div className="repo-title">
          <h1>{detail.name} <span className={"type-badge big t-" + detail.type}>{TYPE_LABEL[detail.type] ?? detail.type}</span></h1>
          <div className="repo-sub">
            {up ? (
              <>
                <span className="zip-badge">ZIP</span>
                <span title={up.filename}>{up.filename}</span>
                <span>{up.files} files</span>
                <span>uploaded {ago(up.uploaded)}</span>
              </>
            ) : (
              <>
                {detail.branch && <span>⎇ <code>{detail.branch}</code></span>}
                {detail.last_commit && <span className="mono">{detail.last_commit}</span>}
                <span className="path" title={detail.path}>{shortPath(detail.path)}</span>
              </>
            )}
            <span>model <code>{detail.model}</code></span>
            {hasDocs && <span>{plural(detail.pages, "page")} · {detail.languages.join(", ")} · {ago(detail.docs_time)}</span>}
          </div>
        </div>
        <div className="actions">
          <button className="btn primary" onClick={() => generate(false)}>{hasDocs ? "Regenerate docs" : "Generate docs"}</button>
          <button className="btn" onClick={() => generate(true)} title="Scan, extract and select only: shows what each page will use, no model calls">Dry run</button>
          {hasDocs && <a className="btn" href={`/api/repos/${enc(repoId)}/docs.zip`} download>↓ Docs ZIP</a>}
          {hasDocs && <button className="btn" disabled={busy} onClick={clear}>Clear</button>}
          {up && <button className="btn danger" disabled={busy} onClick={del}>Delete</button>}
        </div>
      </div>
      <Options id={repoId} model={detail.model} primary={primary} langs={defaultLangs} />
      {latest && <RepoJob job={latest} />}
      <div className="tabs" role="tablist">
        {tabs.map(([key, l, n]) => (
          <button key={key} role="tab" aria-selected={tab === key} onClick={() => go(repoId, key)}>
            {l}{n !== null && <span className="count">{n}</span>}
          </button>
        ))}
      </div>
      {tab === "run" ? <RunTab detail={detail} />
        : tab === "downloads" ? <DownloadsTab detail={detail} docs={docs} />
          : <DocsViewer key={repoId + ":" + (detail.docs_time ?? 0)} repoId={repoId} docs={docs} onGenerate={() => generate(false)} />}
    </div>
  );
}
