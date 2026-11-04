"""In-process fakes for Ollama and the GitLab API (no GPU, no network)."""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ARTICLE = """# Nginx Panel

## Overview
A small web server panel that serves static sites for teams who need simple HTTP hosting.

## Requirements
- Ubuntu 22.04 or newer
- Port `80` reachable

## Installation
1. Start the service:

```bash
sudo systemctl enable --now nginx
```

## Configuration
| Setting | Default | Meaning |
|---|---|---|
| `nginx_port` | `80` | Listen port |

## Usage
Open `http://your-server/` in a browser after installation.

## Troubleshooting
Check `/var/log/nginx/error.log` when the service does not start.
"""

DUTCH = """# Nginx Panel

## Overzicht
Een kleine webserver die statische websites host voor teams die eenvoudige hosting nodig hebben.
Je beheert de server zelf en je hebt er geen groot platform voor nodig. De server draait op de
machine van het team en is bereikbaar via de standaardpoort van het web.

## Vereisten
- Ubuntu 22.04 of nieuwer
- Poort `80` is bereikbaar

## Installatie
1. Start de service:

```bash
sudo systemctl enable --now nginx
```

## Configuratie
| Instelling | Standaardwaarde | Betekenis |
|---|---|---|
| `nginx_port` | `80` | Poort waarop de server luistert |

## Gebruik
Open `http://your-server/` in je browser na de installatie.

## Problemen oplossen
Bekijk `/var/log/nginx/error.log` als de service niet start.
"""


class _Server:
    def __init__(self, handler_cls):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def _handler(outer):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send_json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n)) if n else {}

        def do_GET(self):
            outer.get(self)

        def do_POST(self):
            outer.post(self)

        def do_PUT(self):
            outer.put(self)
    return H


class FakeOllama(_Server):
    def __init__(self, models=("qwen2.5:14b",), mention_tool_first=False, english_section_once=False):
        self.models = list(models)
        self.calls: list[dict] = []
        self.mention_tool_first = mention_tool_first
        self.english_section_once = english_section_once
        super().__init__(_handler(self))

    def get(self, h):
        if h.path == "/api/tags":
            h.send_json(200, {"models": [{"name": m} for m in self.models]})
        elif h.path == "/api/version":
            h.send_json(200, {"version": "0.0-fake"})
        elif h.path == "/api/ps":
            h.send_json(200, {"models": [{"name": self.models[0], "size": 10, "size_vram": 10}]})
        else:
            h.send_json(404, {"error": "not found"})

    def post(self, h):
        payload = h.body()
        self.calls.append(payload)
        if payload.get("model") not in self.models:
            return h.send_json(404, {"error": f"model '{payload.get('model')}' not found"})
        h.send_json(200, {"message": {"role": "assistant", "content": self.reply(payload)},
                          "done": True, "done_reason": "stop", "prompt_eval_count": 500,
                          "eval_count": 250, "eval_duration": 5_000_000_000,
                          "load_duration": 100_000_000, "prompt_eval_duration": 200_000_000})

    def _count(self, needle: str) -> int:
        return sum(needle in c["messages"][0]["content"] for c in self.calls)

    def reply(self, payload: dict) -> str:
        system = payload["messages"][0]["content"]
        user = payload["messages"][-1]["content"]
        props = (payload.get("format") or {}).get("properties", {}) if isinstance(payload.get("format"), dict) else {}
        if "files" in props:
            return json.dumps({"files": re.findall(r"^- (\S+)", user, re.M)[:2]})
        if "passed" in props:
            return json.dumps({"passed": True, "issues": []})
        doc = re.search(r"=== DOCUMENT ===\n(.*)\n=== END ===", user, re.S)
        dutch = "into Dutch" in system or "entire document in Dutch" in system or "in Dutch" in system
        if "translator" in system and doc and "into English" in system:
            out = ARTICLE
            for i, block in enumerate(re.findall(r"```.*?```", ARTICLE, re.S)):
                out = out.replace(block, f"⟦CODE{i}⟧")
            return out
        if "translator" in system and doc:
            if dutch:
                # Reproduce the placeholders the real model must keep.
                out = DUTCH
                for i, block in enumerate(re.findall(r"```.*?```", ARTICLE, re.S)):
                    out = out.replace(block, f"⟦CODE{i}⟧")
                if self.english_section_once and self._count("translator") == 1:
                    out = out.replace(out.split("## Gebruik")[1].split("##")[0],
                                      "\nOpen the route in your browser and log in with the account that you "
                                      "created. When the page does not load, check that the pod is running and "
                                      "that the route points to the right service and port for this release.\n\n")
                return out
            return doc.group(1).replace("## Overview", "## Aperçu")
        if "editor" in system and doc:
            text = doc.group(1).replace("Ansible ", "")
            if "is in English" in user:
                return DUTCH
            return text
        if self.mention_tool_first and self._count("editor") == 0:
            return ARTICLE.replace("A small web server", "Ansible deploys a small web server")
        return DUTCH if dutch else ARTICLE


class FakeGitLab(_Server):
    """Implements the three merge-request endpoints docpipe uses."""

    def __init__(self, token="glpat-test"):
        self.token = token
        self.mrs: list[dict] = []
        self.requests: list[tuple[str, str, dict]] = []
        super().__init__(_handler(self))

    def _auth(self, h) -> bool:
        if h.headers.get("PRIVATE-TOKEN") != self.token:
            h.send_json(401, {"message": "401 Unauthorized"})
            return False
        return True

    def get(self, h):
        if not self._auth(h):
            return
        url = urlparse(h.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.requests.append(("GET", url.path, q))
        if url.path.endswith("/merge_requests"):
            h.send_json(200, [m for m in self.mrs if m["state"] == "opened"
                              and m["source_branch"] == q.get("source_branch")])
        else:
            h.send_json(404, {})

    def post(self, h):
        if not self._auth(h):
            return
        body = h.body()
        self.requests.append(("POST", h.path, body))
        mr = {**body, "iid": len(self.mrs) + 1, "state": "opened",
              "web_url": f"{self.url}/group/docs/-/merge_requests/{len(self.mrs) + 1}"}
        self.mrs.append(mr)
        h.send_json(201, mr)

    def put(self, h):
        if not self._auth(h):
            return
        body = h.body()
        self.requests.append(("PUT", h.path, body))
        iid = int(h.path.rstrip("/").split("/")[-1])
        mr = next(m for m in self.mrs if m["iid"] == iid)
        mr.update(body)
        h.send_json(200, mr)
