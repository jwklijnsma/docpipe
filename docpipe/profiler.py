"""
Per-source timing + LLM accounting, with tuning hints derived from the
numbers Ollama itself reports (eval speed, model load time, num_ctx).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass
class LLMCall:
    stage: str
    in_tok: int
    out_tok: int
    seconds: float
    load_s: float
    gen_s: float
    num_ctx: int
    truncated: bool


class Profiler:
    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.stages: dict[str, float] = defaultdict(float)
        self.calls: list[LLMCall] = []
        self._current = "other"
        self._lock = threading.Lock()

    @contextmanager
    def stage(self, name: str):
        prev, self._current = self._current, name
        t = time.monotonic()
        try:
            yield
        finally:
            with self._lock:
                self.stages[name] += time.monotonic() - t
            self._current = prev

    def record(self, **kw) -> None:
        kw.setdefault("stage", self._current)
        with self._lock:
            self.calls.append(LLMCall(**kw))

    @property
    def wall(self) -> float:
        return time.monotonic() - self.t0

    # ----- reporting ---------------------------------------------------------

    def summary(self, title: str) -> str:
        wall = self.wall
        llm_s = sum(c.seconds for c in self.calls)
        out_tok = sum(c.out_tok for c in self.calls)
        in_tok = sum(c.in_tok for c in self.calls)
        gen_s = sum(c.gen_s for c in self.calls)
        lines = [
            f"Performance summary — {title}",
            f"  wall {wall:.1f}s | LLM {llm_s:.1f}s ({_pct(llm_s, wall)}) | "
            f"{len(self.calls)} calls | in {in_tok}t | out {out_tok}t | "
            f"{(out_tok / gen_s) if gen_s else 0:.1f} tok/s",
            f"  {'stage':<16}{'time':>8}{'share':>8}",
        ]
        for name, dt in sorted(self.stages.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {name:<16}{dt:>7.1f}s{_pct(dt, wall):>8}")
        hints = self.hints()
        if hints:
            lines.append("  Hints:")
            lines += [f"   • {h}" for h in hints]
        return "\n".join(lines)

    def hints(self) -> list[str]:
        out: list[str] = []
        if not self.calls:
            return out
        load = sum(c.load_s for c in self.calls)
        reloads = sum(1 for c in self.calls if c.load_s > 1.0)
        if reloads:
            out.append(
                f"Model was (re)loaded {reloads}x, costing {load:.0f}s. Keep keep_alive high, "
                "use the same model for all roles, and use fewer num_ctx_buckets "
                "(every num_ctx change forces a reload)."
            )
        gen_s = sum(c.gen_s for c in self.calls)
        out_tok = sum(c.out_tok for c in self.calls)
        tps = out_tok / gen_s if gen_s else 0
        if 0 < tps < 15:
            out.append(
                f"Generation is slow ({tps:.1f} tok/s). Run `docpipe check`: if the model is "
                "not 100% in VRAM it is partly running on CPU — use a smaller quant/model "
                "or a lower num_ctx bucket."
            )
        if any(c.truncated for c in self.calls):
            out.append("Some outputs hit max_output_tokens and were cut off — raise it slightly.")
        big = [c for c in self.calls if c.num_ctx > min(c2.num_ctx for c2 in self.calls)]
        if big:
            out.append(
                f"{len(big)} call(s) needed a larger num_ctx bucket (up to "
                f"{max(c.num_ctx for c in big)}). Lower input_token_budget to stay in the "
                "smallest bucket."
            )
        wall = self.wall
        qa_like = sum(v for k, v in self.stages.items() if k.startswith(("qa", "fix")))
        if wall and qa_like / wall > 0.4:
            out.append(
                f"QA/fix loops took {_pct(qa_like, wall)} of the time. Use --fast or "
                "set pipeline.semantic_qa: false for routine runs."
            )
        return out


def _pct(part: float, whole: float) -> str:
    return f"{(part / whole * 100) if whole else 0:.0f}%"
