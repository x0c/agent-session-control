"""Canonical repository check: lint, types, full tests, packaging, clean install.

Run with the repository-local environment, e.g.::

    uv run --python 3.12 --group dev scripts/check.py

This is the complete-repository gate. Every step runs in the foreground with
complete logs. Nothing here suppresses warnings and there are no
ownership-conditioned exemptions: pytest escalates all warnings to failures,
and any Pyrefly ERROR or WARN anywhere in the repository fails the gate.
The pytest step always discovers the complete ``tests/`` directory, not a
per-worker subset.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(label: str, *argv: str, cwd: str = ROOT, env: dict | None = None) -> None:
    print(f":: {label}: {' '.join(argv)}", flush=True)
    proc = subprocess.run(argv, cwd=cwd, env=env or os.environ)
    if proc.returncode != 0:
        raise SystemExit(f"check failed at {label} (exit {proc.returncode}): {' '.join(argv)}")


def capture(label: str, *argv: str, cwd: str = ROOT) -> str:
    print(f":: {label}: {' '.join(argv)}", flush=True)
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=os.environ)
    print(proc.stdout, flush=True)
    print(proc.stderr, flush=True)
    if proc.returncode != 0:
        raise SystemExit(f"check failed at {label} (exit {proc.returncode}): {' '.join(argv)}")
    return proc.stdout + proc.stderr


def main() -> None:
    python = sys.executable
    print(f"check interpreter: {python}", flush=True)
    major, minor = sys.version_info[:2]
    if not (major == 3 and 10 <= minor <= 14):
        raise SystemExit(
            f"check requires Python 3.10-3.14 (got {major}.{minor}); "
            "refusing to hide an environment mismatch as a pass."
        )
    run("ruff-check", python, "-m", "ruff", "check", "src", "tests", "scripts", "examples")
    run("ruff-format", python, "-m", "ruff", "format", "--check", "src", "tests", "scripts", "examples")
    # Canonical complete-repo gate: EVERY warning/error fails, regardless of
    # which worker owns the file. There is no partial-pass regime and no
    # ownership-conditioned exemption.
    diagnostics = capture("pyrefly", python, "-m", "pyrefly", "check", "--min-severity", "warn")
    actionable = [line for line in diagnostics.splitlines() if line.strip().startswith(("ERROR", "WARN"))]
    if actionable:
        raise SystemExit("check failed at pyrefly: actionable diagnostics:\n" + "\n".join(actionable))
    run("pytest", python, "-m", "pytest", "-W", "error", "-q", "tests")
    run(
        "extras-present",
        python,
        "-c",
        "import sesskit, acp, jsonschema;"
        "print('history extra: sesskit ok;', 'acp extra: acp ok;', 'schema validation: jsonschema ok')",
    )
    dist = os.path.join(ROOT, "dist")
    shutil.rmtree(dist, ignore_errors=True)
    run("build", python, "-m", "build", "--outdir", dist)
    wheels = sorted(f for f in os.listdir(dist) if f.endswith(".whl"))
    sdists = sorted(f for f in os.listdir(dist) if f.endswith(".tar.gz"))
    if not wheels or not sdists:
        raise SystemExit(f"expected wheel and sdist in {dist}: {sorted(os.listdir(dist))}")
    print(f"artifacts: {wheels + sdists}", flush=True)
    wheel = os.path.join(dist, wheels[0])
    with tempfile.TemporaryDirectory(prefix="asc-clean-install-") as tmp:
        venv_python = _make_venv(tmp)
        _pip_install(venv_python, wheel)
        probe = (
            "import agent_session_control as asc;"
            "from agent_session_control import runtime, store, correlation;"
            "plan = runtime.build_create_plan('codex', '/repo');"
            "assert plan.argv[:2] == ('codex', '--no-daemon');"
            "print('clean install ok:', asc.__version__)"
        )
        run("clean-import", venv_python, "-c", probe, cwd=tmp)
        cli_probe = capture("cli-version", venv_python, "-m", "agent_session_control", "version", cwd=tmp)
        compact = cli_probe.lower().replace(" ", "")
        if '"ok":true' not in compact:
            raise SystemExit("check failed at cli-version: no ok envelope in output above")
    print("check: all steps passed", flush=True)


def _make_venv(tmp: str) -> str:
    path = os.path.join(tmp, "venv")
    uv = shutil.which("uv")
    if uv is not None:
        run("venv", uv, "venv", "--python", "3.12", "--seed", path)
    else:
        # Plain stdlib venv (ensurepip provides pip by default); there is no
        # --seed option here, so it must not be passed.
        run("venv", sys.executable, "-m", "venv", path)
    if os.name == "nt":
        return os.path.join(path, "Scripts", "python.exe")
    return os.path.join(path, "bin", "python")


def _pip_install(venv_python: str, wheel: str) -> None:
    uv = shutil.which("uv")
    if uv is not None:
        run("clean-install", uv, "pip", "install", "--python", venv_python, wheel)
        return
    run("clean-install", venv_python, "-m", "pip", "install", wheel)


if __name__ == "__main__":
    main()
