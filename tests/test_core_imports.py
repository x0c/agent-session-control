"""Core import isolation: the public surface loads without optional extras."""

import json
import os
import subprocess
import sys

ISOLATION_PROBE = """
import json
import sys

class Blocker:
    BLOCKED = ("sesskit", "agent_client_protocol", "agent_session_control.acp",
               "agent_session_control.cli", "agent_session_control.history",
               "agent_session_control.controller", "agent_session_control.terminal",
               "agent_session_control.supervisor", "textual", "cryptography")

    def find_module(self, name, path=None):
        if name in self.BLOCKED or any(name.startswith(b + ".") for b in self.BLOCKED):
            return self
        return None

    def load_module(self, name):
        raise ImportError(f"blocked optional dependency: {name}")

sys.meta_path.insert(0, Blocker())

import agent_session_control as asc
from agent_session_control import errors, models, runtime, store, correlation

plan = runtime.build_create_plan("claude", "/repo")
assert plan.argv[0] == "claude"
assert runtime.runtime_profile("pi")["id"] == "pi"
assert correlation.own_message(correlation.wire_prompt("run_abc", "m-1", "hi"), "run_abc") == "m-1"
assert models.RunResult(run_id="r", state="unknown").evidence == "unknown"

import tempfile, os
with tempfile.TemporaryDirectory() as tmp:
    s = store.RunStore(root=os.path.join(tmp, "control"))
    rec, created = s.create("op", "k", {"a": 1}, {"execution": "queued"})
    assert created and rec["run_id"].startswith("run_")

for mod in ("sesskit", "agent_client_protocol", "textual"):
    assert mod not in sys.modules, mod
print(json.dumps({"ok": True, "version": asc.__version__}))
"""


def test_core_imports_without_optional_dependencies():
    env = dict(os.environ)
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    env["PYTHONPATH"] = os.path.abspath(src) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run([sys.executable, "-c", ISOLATION_PROBE], capture_output=True, text=True, timeout=120, env=env)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["version"] == "0.1.0"
