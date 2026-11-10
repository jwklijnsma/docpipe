import { useCallback, useEffect, useRef, useState } from "react";
import { AuthError, uploadZip } from "./api";
import { Login, Toasts, TopBar } from "./components/Chrome";
import { JobsPanel } from "./components/JobsPanel";
import { RepoPage } from "./components/RepoPage";
import { Sidebar } from "./components/Sidebar";
import { AppProvider, go, useApp, useRoute } from "./store";
import { size } from "./util";

function Welcome() {
  const { repos, status } = useApp();
  const n = repos.length;
  return (
    <div className="empty">
      <h3>{n ? "Pick a repository" : "No repositories yet"}</h3>
      <p>
        {n ? `${n} ${n === 1 ? "source" : "sources"} found (Helm charts, manifests, Argo CD, Ansible, projects). Choose one on the left to read its docs or generate them.` : "Restart docpipe serve with --root pointing at a repo or a folder that contains them."}
        {status?.uploads.enabled ? " Or upload a ZIP: its docs are generated in the background straight away." : ""}
      </p>
    </div>
  );
}

function useUploader() {
  const { status, toast, refreshRepos, startJob, setAuthed, handleError } = useApp();
  const [progress, setProgress] = useState<number | null>(null);
  const onFile = useCallback(
    async (file: File) => {
      if (!status?.uploads.enabled) return toast({ text: "Uploads are switched off on this server.", kind: "bad" });
      if (!/\.zip$/i.test(file.name)) return toast({ text: "Choose a .zip file of a chart, repo or project.", kind: "bad" });
      const max = (status.uploads.max_mb || 0) * 1048576;
      if (max && file.size > max) return toast({ text: `${file.name} is ${size(file.size)}; the limit is ${status.uploads.max_mb} MB.`, kind: "bad" });
      setProgress(0);
      try {
        const up = await uploadZip(file, setProgress);
        await refreshRepos();
        go(up.id, "docs");
        // Uploading is asking for docs: queue them right away, in the server's languages.
        startJob(up.id, { primary_lang: status.primary_language, langs: status.languages }, `${up.filename}: ${up.files} files unpacked${up.skipped ? ` (${up.skipped} skipped)` : ""}.`);
      } catch (err) {
        if (err instanceof AuthError) setAuthed(false);
        else handleError(new Error("Upload refused: " + (err as Error).message));
      } finally {
        setProgress(null);
      }
    },
    [status, toast, refreshRepos, startJob, setAuthed, handleError],
  );
  return { busy: progress !== null, progress, onFile };
}

function DropZone({ onFile }: { onFile: (f: File) => void }) {
  const { status } = useApp();
  const [show, setShow] = useState(false);
  const depth = useRef(0);
  useEffect(() => {
    if (!status?.uploads.enabled) return;
    const hasFiles = (e: DragEvent) => [...(e.dataTransfer?.types || [])].includes("Files");
    const enter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth.current++;
      setShow(true);
    };
    const over = (e: DragEvent) => hasFiles(e) && e.preventDefault();
    const leave = () => {
      if (--depth.current <= 0) {
        depth.current = 0;
        setShow(false);
      }
    };
    const drop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth.current = 0;
      setShow(false);
      const f = e.dataTransfer?.files?.[0];
      if (f) onFile(f);
    };
    window.addEventListener("dragenter", enter);
    window.addEventListener("dragover", over);
    window.addEventListener("dragleave", leave);
    window.addEventListener("drop", drop);
    return () => {
      window.removeEventListener("dragenter", enter);
      window.removeEventListener("dragover", over);
      window.removeEventListener("dragleave", leave);
      window.removeEventListener("drop", drop);
    };
  }, [status, onFile]);
  return show ? <div className="dropzone"><div>Drop the ZIP to generate its docs</div></div> : null;
}

function Shell() {
  const { authed, repos } = useApp();
  const { repoId, tab } = useRoute();
  const upload = useUploader();
  useEffect(() => {
    if (authed && !repoId && repos.length === 1 && !location.hash) go(repos[0].id);
  }, [authed, repoId, repos]);

  if (authed === null) return null;
  if (!authed) {
    return (
      <>
        <header className="topbar"><div className="brand"><img src="/favicon.svg" alt="" /> docpipe</div></header>
        <Login />
      </>
    );
  }
  return (
    <>
      <TopBar />
      <div className="layout">
        <Sidebar repoId={repoId} tab={tab} upload={upload} />
        <main>{repoId ? <RepoPage key={repoId} repoId={repoId} tab={tab} /> : <Welcome />}</main>
      </div>
      <JobsPanel />
      <DropZone onFile={upload.onFile} />
      <Toasts />
    </>
  );
}

export default function App() {
  return (
    <AppProvider>
      <Shell />
    </AppProvider>
  );
}
