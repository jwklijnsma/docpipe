"""
Ollama client.

Improvements over v1:
  * /api/chat with system/user messages and JSON-schema structured output
  * num_ctx picked from fixed buckets -> no model reloads between calls
  * num_predict cap -> no runaway 32k-token generations
  * thread-safe least-busy endpoint picking with per-endpoint concurrency slots
  * 404/400 fail fast with an actionable message; network/5xx errors retry
  * reports real eval speed and model-load time from Ollama's own counters
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from dataclasses import dataclass

import requests

from docpipe.config import LLMConfig
from docpipe.log import StageLogger
from docpipe.profiler import Profiler

CHARS_PER_TOKEN = 3.5   # conservative for code/YAML; errs toward a bigger bucket


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


class ModelMissing(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


@dataclass
class _Endpoint:
    url: str
    slots: threading.BoundedSemaphore
    busy: int = 0


class LLM:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.endpoints = [
            _Endpoint(u.rstrip("/"), threading.BoundedSemaphore(max(1, cfg.parallel_per_endpoint)))
            for u in cfg.endpoints
        ]
        self.session = requests.Session()
        self.session.verify = cfg.ca_bundle or True
        self._lock = threading.Lock()

    # ----- sizing ------------------------------------------------------------

    def pick_num_ctx(self, input_tokens: int, max_output: int) -> int:
        need = int((input_tokens + max_output) * 1.1)
        buckets = sorted(self.cfg.num_ctx_buckets)
        for b in buckets:
            if need <= b:
                return b
        return buckets[-1]

    # ----- endpoint slots ----------------------------------------------------

    def _acquire(self) -> _Endpoint:
        with self._lock:
            ep = min(self.endpoints, key=lambda e: (e.busy, random.random()))
            ep.busy += 1
        ep.slots.acquire()
        return ep

    def _release(self, ep: _Endpoint) -> None:
        ep.slots.release()
        with self._lock:
            ep.busy -= 1

    # ----- main call ---------------------------------------------------------

    def chat(self, system: str, user: str, *, role: str = "writer",
             temperature: float = 0.2, schema: dict | None = None,
             max_output: int | None = None, log: StageLogger | None = None,
             prof: Profiler | None = None) -> str:
        log = log or StageLogger()
        model = self.cfg.model_for(role)
        max_out = max_output or self.cfg.max_output_tokens
        in_est = estimate_tokens(system) + estimate_tokens(user)
        num_ctx = self.pick_num_ctx(in_est, max_out)
        if in_est + max_out > num_ctx:
            log.warning("Input (~%dt) + output (%dt) exceeds largest num_ctx %d — "
                        "server will truncate. Lower input_token_budget.",
                        in_est, max_out, num_ctx)

        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            "keep_alive": self.cfg.keep_alive,
            "options": {"temperature": temperature, "num_ctx": num_ctx, "num_predict": max_out},
        }
        if schema:
            payload["format"] = schema

        last_err: Exception | None = None
        for attempt in range(1, self.cfg.max_attempts + 1):
            ep = self._acquire()
            data = None
            t0 = time.monotonic()
            try:
                log.llm("Sending model request (attempt %d/%d) with %d tokens context",
                        attempt, self.cfg.max_attempts, num_ctx)
                r = self.session.post(f"{ep.url}/api/chat", json=payload,
                                      timeout=self.cfg.request_timeout)
                if r.status_code == 404:
                    raise ModelMissing(
                        f"{ep.url} has no model {model!r}. On the server run "
                        f"`ollama pull {model}` (check names with `docpipe check`)."
                    )
                if r.status_code == 400:
                    raise LLMError(f"{ep.url} rejected the request: {r.text[:300]}")
                r.raise_for_status()
                data = r.json()
            except (ModelMissing, LLMError):
                raise
            except (requests.RequestException, ValueError) as exc:
                last_err = exc
            finally:
                self._release(ep)

            if data is None:
                delay = self.cfg.base_delay * 2 ** (attempt - 1)
                log.warning("LLM call failed on %s (attempt %d/%d): %s — retrying in %.0fs",
                            ep.url, attempt, self.cfg.max_attempts, last_err, delay)
                time.sleep(delay)
                continue

            elapsed = time.monotonic() - t0
            text = (data.get("message") or {}).get("content", "").strip()
            in_tok = int(data.get("prompt_eval_count") or in_est)
            out_tok = int(data.get("eval_count") or 0)
            load_s = (data.get("load_duration") or 0) / 1e9
            gen_s = (data.get("eval_duration") or 0) / 1e9
            truncated = data.get("done_reason") == "length"
            tps = out_tok / gen_s if gen_s else (out_tok / elapsed if elapsed else 0)
            log.info("Context: input=%dt + output=%dt (num_ctx %d) in %.1fs, %.1f tok/s%s",
                     in_tok, out_tok, num_ctx, elapsed, tps,
                     f", model load {load_s:.1f}s" if load_s > 1 else "")
            if truncated:
                log.warning("Output hit max_output_tokens (%d) and was cut off", max_out)
            if prof:
                prof.record(in_tok=in_tok, out_tok=out_tok, seconds=elapsed, load_s=load_s,
                            gen_s=gen_s, num_ctx=num_ctx, truncated=truncated)
            return text

        raise LLMError(f"all {self.cfg.max_attempts} attempts failed: {last_err}")

    def chat_json(self, system: str, user: str, schema: dict, **kw) -> dict:
        raw = self.chat(system, user, schema=schema, temperature=0.0, **kw)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
        return {}

    # ----- diagnostics -------------------------------------------------------

    def check(self, benchmark: bool = True, log: StageLogger | None = None) -> list[str]:
        """Reachability, installed models, VRAM residency, optional speed test."""
        log = log or StageLogger()
        problems: list[str] = []
        for ep in self.endpoints:
            try:
                version = self.session.get(f"{ep.url}/api/version", timeout=15).json().get("version")
                tags = self.session.get(f"{ep.url}/api/tags", timeout=15)
                tags.raise_for_status()
            except (requests.RequestException, ValueError) as exc:
                problems.append(f"{ep.url}: unreachable ({exc})")
                log.error("%s unreachable: %s", ep.url, exc)
                continue

            installed = {m["name"] for m in tags.json().get("models", [])}
            log.success("%s reachable (Ollama %s), %d models installed", ep.url, version, len(installed))
            for model in self.cfg.all_models():
                if model in installed or f"{model}:latest" in installed:
                    log.success("  model %s present", model)
                else:
                    problems.append(f"{ep.url}: missing model {model!r} — run `ollama pull {model}`")
                    log.error("  model %s MISSING — run `ollama pull %s` on the server", model, model)

            if benchmark and not any(model in p for p in problems for model in [self.cfg.model]):
                self._benchmark(ep, log)
                self._report_residency(ep, log, problems)
        return problems

    def _benchmark(self, ep: _Endpoint, log: StageLogger) -> None:
        payload = {
            "model": self.cfg.model, "stream": False, "keep_alive": self.cfg.keep_alive,
            "messages": [{"role": "user", "content": "Write five sentences about lighthouses."}],
            "options": {"num_ctx": min(self.cfg.num_ctx_buckets), "num_predict": 200, "temperature": 0},
        }
        try:
            d = self.session.post(f"{ep.url}/api/chat", json=payload,
                                  timeout=self.cfg.request_timeout).json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("  benchmark failed: %s", exc)
            return
        gen_s = (d.get("eval_duration") or 0) / 1e9
        pe_s = (d.get("prompt_eval_duration") or 0) / 1e9
        load_s = (d.get("load_duration") or 0) / 1e9
        tps = (d.get("eval_count") or 0) / gen_s if gen_s else 0
        log.perf("  benchmark %s: %.1f tok/s generation, prompt %.2fs, model load %.1fs",
                 self.cfg.model, tps, pe_s, load_s)
        if tps and tps < 15:
            log.warning("  generation under 15 tok/s — expect slow docs (see residency below)")

    def _report_residency(self, ep: _Endpoint, log: StageLogger, problems: list[str]) -> None:
        try:
            running = self.session.get(f"{ep.url}/api/ps", timeout=15).json().get("models", [])
        except (requests.RequestException, ValueError):
            return
        for m in running:
            size, vram = m.get("size") or 0, m.get("size_vram") or 0
            share = vram / size * 100 if size else 0
            log.perf("  loaded: %s — %.1f GB, %.0f%% in GPU memory", m.get("name"), size / 1e9, share)
            if size and share < 99:
                msg = (f"{ep.url}: {m.get('name')} only {share:.0f}% in VRAM — part runs on CPU "
                       "(very slow). Use a smaller quant, a smaller num_ctx bucket, or more GPUs.")
                problems.append(msg)
                log.warning("  %s", msg)
