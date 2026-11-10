"""Run the test suite's FakeOllama as a standalone server and print its URL.

Used by the Docker smoke test in CI and handy for trying the web UI without a GPU:
    python3 tests/serve_fake_ollama.py [model ...]
"""
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.fakes import FakeOllama  # noqa: E402

server = FakeOllama(models=tuple(sys.argv[1:]) or ("qwen2.5:14b",))
print(server.url, flush=True)
while True:
    time.sleep(3600)
