export class AuthError extends Error {}

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

type Opts = { method?: "GET" | "POST"; body?: unknown };

/** Calls the docpipe API. POSTs carry the X-Docpipe header the server requires. */
export async function api<T = unknown>(path: string, opts: Opts = {}): Promise<T> {
  const init: RequestInit = { credentials: "same-origin", headers: {} };
  if (opts.method === "POST") {
    init.method = "POST";
    init.headers = { "X-Docpipe": "1", "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.body ?? {});
  }
  const res = await fetch("/api/" + path, init);
  let data: any = null;
  try {
    data = await res.json();
  } catch {
    /* empty body */
  }
  if (res.status === 401) throw new AuthError("unauthorised");
  if (!res.ok) throw new ApiError((data && data.error) || res.statusText, res.status);
  return data as T;
}

export const enc = encodeURIComponent;

/** Upload a ZIP with progress (fetch cannot report upload progress, XHR can). */
export function uploadZip(
  file: File,
  onProgress: (pct: number) => void,
): Promise<{ id: string; name: string; filename: string; files: number; skipped: number; type: string }> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/uploads");
    xhr.withCredentials = true;
    xhr.setRequestHeader("X-Docpipe", "1");
    xhr.setRequestHeader("Content-Type", "application/zip");
    xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(Math.round((100 * e.loaded) / e.total));
    xhr.onerror = () => reject(new ApiError("the connection was closed (file too large?)", 0));
    xhr.onload = () => {
      let data: any = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        /* not JSON */
      }
      if (xhr.status === 401) reject(new AuthError("unauthorised"));
      else if (xhr.status !== 201) reject(new ApiError((data && data.error) || xhr.statusText, xhr.status));
      else resolve(data);
    };
    xhr.send(file);
  });
}
