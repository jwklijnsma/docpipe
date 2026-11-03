"""
Stage-tagged logging in the Hostkey style:

    [2026-04-13 11:25:17] 🚀 [INFO]    (owncast) Processing: Owncast [en]
    [2026-04-13 11:25:17] 💾 [GIT]     (owncast) Cloning https://...
    [2026-04-13 11:25:18] 🤖 [LLM]     (owncast) Sending model request (attempt 1/4) ...

The (source) tag keeps lines readable when several sources run in parallel.
`--log-format json` emits one JSON object per line for Loki/EFK/OpenShift logging.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

STYLES = {
    "INFO":    ("🚀", "34"),
    "GIT":     ("💾", "35"),
    "AGENT":   ("🧩", "35"),
    "LLM":     ("🤖", "36"),
    "QA":      ("🔍", "33"),
    "TRANS":   ("🌐", "36"),
    "FIX":     ("📝", "33"),
    "CACHE":   ("♻️ ", "32"),
    "PERF":    ("⏱️ ", "34"),
    "SUCCESS": ("✅", "32"),
    "WARN":    ("⚠️ ", "33"),
    "ERROR":   ("❌", "31"),
}


def _stage_of(record: logging.LogRecord) -> str:
    stage = getattr(record, "stage", None)
    if stage:
        return stage
    if record.levelno >= logging.ERROR:
        return "ERROR"
    if record.levelno >= logging.WARNING:
        return "WARN"
    return "INFO"


class PrettyFormatter(logging.Formatter):
    def __init__(self, color: bool):
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        stage = _stage_of(record)
        icon, code = STYLES.get(stage, STYLES["INFO"])
        ts = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        tag = f"[{stage}]".ljust(9)
        src = getattr(record, "src", None)
        src_txt = f"({src}) " if src else ""
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        if self.color:
            return (f"\033[2m[{ts}]\033[0m {icon} \033[1;{code}m{tag}\033[0m "
                    f"\033[2m{src_txt}\033[0m{msg}")
        return f"[{ts}] {icon} {tag} {src_txt}{msg}"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "stage": _stage_of(record).lower(),
            "source": getattr(record, "src", None),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def setup_logging(fmt: str = "pretty", color: bool | None = None,
                  log_file: str | None = None, verbose: bool = False) -> None:
    if color is None:
        color = (not os.environ.get("NO_COLOR")) and (
            bool(os.environ.get("FORCE_COLOR")) or sys.stdout.isatty()
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # urllib3 is chatty at DEBUG
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(JsonFormatter() if fmt == "json" else PrettyFormatter(color))
    root.addHandler(console)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(PrettyFormatter(color=False))
        root.addHandler(fh)


class StageLogger:
    """Logger with one method per stage, optionally bound to a source name."""

    def __init__(self, name: str = "docpipe", src: str | None = None):
        self._log = logging.getLogger(name)
        self.src = src

    def bind(self, src: str) -> "StageLogger":
        return StageLogger(self._log.name, src)

    def _emit(self, stage: str, level: int, msg: str, *args, exc_info=None) -> None:
        self._log.log(level, msg, *args, exc_info=exc_info,
                      extra={"stage": stage, "src": self.src})

    def info(self, m, *a):    self._emit("INFO", logging.INFO, m, *a)
    def git(self, m, *a):     self._emit("GIT", logging.INFO, m, *a)
    def agent(self, m, *a):   self._emit("AGENT", logging.INFO, m, *a)
    def llm(self, m, *a):     self._emit("LLM", logging.INFO, m, *a)
    def qa(self, m, *a):      self._emit("QA", logging.INFO, m, *a)
    def trans(self, m, *a):   self._emit("TRANS", logging.INFO, m, *a)
    def fix(self, m, *a):     self._emit("FIX", logging.INFO, m, *a)
    def cache(self, m, *a):   self._emit("CACHE", logging.INFO, m, *a)
    def perf(self, m, *a):    self._emit("PERF", logging.INFO, m, *a)
    def success(self, m, *a): self._emit("SUCCESS", logging.INFO, m, *a)
    def debug(self, m, *a):   self._emit("INFO", logging.DEBUG, m, *a)
    def warning(self, m, *a): self._emit("WARN", logging.WARNING, m, *a)
    def error(self, m, *a):   self._emit("ERROR", logging.ERROR, m, *a)
    def exception(self, m, *a): self._emit("ERROR", logging.ERROR, m, *a, exc_info=True)
