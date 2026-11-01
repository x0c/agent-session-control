"""Canonical repository check: lint, types, full tests, packaging, clean install.

Run with the repository-local environment, e.g.::

    uv sync --group dev --all-extras
    uv run --python 3.12 --group dev scripts/check.py
    uv run --python 3.12 --group dev scripts/check.py --ci

This is the complete-repository gate. Every step runs in the foreground with
complete logs. Nothing here suppresses warnings and there are no
ownership-conditioned exemptions: pytest escalates all warnings to failures,
and any Pyrefly ERROR or WARN anywhere in the repository fails the gate.
The pytest step always discovers the complete ``tests/`` directory, not a
per-worker subset.

Modes: default runs the FULL suite (including ``live``-marked tests, which
may spend quota and need logins). ``--ci`` runs the no-credential selection
(``-m "not live"``) and prints honest coverage: live tests are reported as
excluded, never as passes; skips are limits, never passes.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSKIT_WHEEL_URL = "https://github.com/x0c/sesskit/releases/download/v0.2.21/sesskit-0.2.21-py3-none-any.whl"
EXPECTED_VERSION = "0.1.1"


def run(label: str, *argv: str, cwd: str = ROOT, env: dict | None = None) -> None:
    print(f":: {label}: {' '.join(argv)}", flush=True)
    proc = subprocess.run(argv, cwd=cwd, env=env or os.environ)
    if proc.returncode != 0:
        raise SystemExit(f"check failed at {label} (exit {proc.returncode}): {' '.join(argv)}")


def capture(label: str, *argv: str, cwd: str = ROOT, env: dict | None = None) -> str:
    print(f":: {label}: {' '.join(argv)}", flush=True)
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=env or os.environ)
    print(proc.stdout, flush=True)
    print(proc.stderr, flush=True)
    if proc.returncode != 0:
        raise SystemExit(f"check failed at {label} (exit {proc.returncode}): {' '.join(argv)}")
    return proc.stdout + proc.stderr


def _current_py() -> str:
    major, minor = sys.version_info[:2]
    return f"{major}.{minor}"


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
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
    if args.ci:
        # No-credential CI selection: never spend quota or need logins.
        # `live` tests are excluded here by marker; the manual live job
        # (canonical repo only, workflow_dispatch) owns them.
        run("pytest-ci", python, "-m", "pytest", "-W", "error", "-q", "-m", "not live", "tests")
        print(
            "CI coverage: live-marked tests EXCLUDED (`-m 'not live'`); "
            "no quota spent, no login required. Live native/model coverage "
            "is deferred to the manual `live` job and reported there, never "
            "counted here. Skips are limits, never passes.",
            flush=True,
        )
    else:
        run("pytest", python, "-m", "pytest", "-W", "error", "-q", "tests")
        print(
            "full coverage: the complete tests/ directory ran, including any "
            "`live`-marked tests present (quota/login may have been spent).",
            flush=True,
        )
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
    sdist = os.path.join(dist, sdists[0])
    _check_wheel_metadata(wheel)
    _check_sdist(sdist)
    if EXPECTED_VERSION not in wheels[0] or EXPECTED_VERSION not in sdists[0]:
        raise SystemExit(f"artifact names must carry version {EXPECTED_VERSION}: {wheels + sdists}")
    # Clean-install matrix: core, history, acp, combined. SessKit is not on
    # PyPI, so history-bearing installs pass its fixed wheel explicitly --
    # the same command shape the README documents. Local `@` references use
    # file:// URIs (PEP 508); bare absolute paths fail under plain pip.
    wheel_uri = _wheel_file_uri(wheel)
    _clean_install(wheel, [], "core")
    _clean_install(wheel, [SESSKIT_WHEEL_URL, f"agent-session-control[history] @ {wheel_uri}"], "history")
    _clean_install(wheel, [f"agent-session-control[acp] @ {wheel_uri}"], "acp")
    _clean_install(
        wheel,
        [SESSKIT_WHEEL_URL, f"agent-session-control[history,acp] @ {wheel_uri}"],
        "combined",
    )
    # Plain-pip path (no uv): the file:// URI must install without uv's
    # bare-path convenience. This runs even when uv exists.
    _clean_install_plain_pip(wheel)
    print("check: all steps passed", flush=True)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Canonical repo gate (full by default, --ci for CI).")
    parser.add_argument(
        "--ci",
        action="store_true",
        help="run the no-credential selection (-m 'not live') instead of the full suite",
    )
    return parser.parse_args(argv)


def _wheel_file_uri(wheel: str) -> str:
    # PEP 508 direct references to a local file must be file:// URIs; a bare
    # absolute path (`pkg @ /abs/path.whl`) is an invalid URL and only works
    # as a uv convenience. Plain pip needs the URI, so every `@` requirement
    # built here uses as_uri() and both installers are exercised.
    return Path(wheel).resolve().as_uri()


def _check_sdist(sdist: str) -> None:
    # The sdist must carry the same skill directory, not just a matching name.
    # Contract: runtime skill is SKILL.md + references/mcp.md (F-owned names);
    # the grading corpus tests/fixtures/skill/evaluation.md must never ship
    # (avoids native-test answer contamination).
    expected = _skill_source_files()
    required = ["SKILL.md", os.path.join("references", "mcp.md")]
    if any(r not in expected for r in required):
        raise SystemExit(f"check failed at sdist-skill: source skill incomplete: {expected}")
    if any(os.path.basename(p) == "evaluation.md" for p in expected):
        raise SystemExit(f"check failed at sdist-skill: assessment fixture must not ship: {expected}")
    with tarfile.open(sdist, "r:gz") as tf:
        names = tf.getnames()
        top = names[0].split("/")[0] if names else ""
        for rel in expected:
            member = f"{top}/skills/agent-session-control/{rel}"
            if member not in names:
                raise SystemExit(f"check failed at sdist-skill: missing {member}")
            disk = os.path.join(ROOT, "skills", "agent-session-control", rel)
            with open(disk, "rb") as fh:
                extracted = tf.extractfile(member)
                if extracted is None or extracted.read() != fh.read():
                    raise SystemExit(f"check failed at sdist-skill: bytes differ for {member}")
            print(f":: sdist-skill: {member}", flush=True)


def _skill_source_files() -> list[str]:
    skill_dir = os.path.join(ROOT, "skills", "agent-session-control")
    out: list[str] = []
    for base, _dirs, files in os.walk(skill_dir):
        for name in sorted(files):
            full = os.path.join(base, name)
            out.append(os.path.relpath(full, skill_dir))
    return sorted(out)


def _check_wheel_metadata(wheel: str) -> None:
    with zipfile.ZipFile(wheel) as zf:
        names = sorted(zf.namelist())
        meta_name = next((n for n in names if n.endswith(".dist-info/METADATA")), None)
        if meta_name is None:
            raise SystemExit("check failed at wheel-metadata: no .dist-info/METADATA in wheel")
        meta = zf.read(meta_name).decode("utf-8")
        print(f":: wheel-metadata: {meta_name}\n{meta}", flush=True)
        for needle in (
            f"Version: {EXPECTED_VERSION}",
            "Provides-Extra: history",
            "Provides-Extra: acp",
        ):
            if needle not in meta:
                raise SystemExit(f"check failed at wheel-metadata: missing {needle!r}")
        # Requires-Python is normalized by the builder (order/spacing vary,
        # e.g. "<3.15,>=3.10"): assert the bounds, not the literal string.
        requires = next((line for line in meta.splitlines() if line.startswith("Requires-Python:")), "")
        if ">=3.10" not in requires or "<3.15" not in requires:
            raise SystemExit(f"check failed at wheel-metadata: bad Requires-Python {requires!r}")
        if "sesskit" not in meta or "agent-client-protocol" not in meta:
            raise SystemExit("check failed at wheel-metadata: extras must name sesskit/agent-client-protocol")
        # Whole maintained skill directory ships (F owns content; D owns the
        # packaging mapping). Every source file must be present byte-identical.
        # Contract: SKILL.md + references/mcp.md required; assessment fixture
        # tests/fixtures/skill/evaluation.md must never appear under the
        # shipped skill prefix (no answer contamination).
        expected = _skill_source_files()
        required = ["SKILL.md", os.path.join("references", "mcp.md")]
        if any(r not in expected for r in required):
            raise SystemExit(f"check failed at wheel-skill: source skill incomplete: {expected}")
        if any(os.path.basename(p) == "evaluation.md" for p in expected):
            raise SystemExit(f"check failed at wheel-skill: assessment fixture must not ship: {expected}")
        skill_prefix = "agent_session_control/skills/agent-session-control/"
        if any(n.startswith(skill_prefix) and os.path.basename(n) == "evaluation.md" for n in names):
            raise SystemExit("check failed at wheel-skill: assessment fixture must not ship in wheel")
        for rel in expected:
            member = "agent_session_control/skills/agent-session-control/" + rel.replace(os.sep, "/")
            if member not in names:
                raise SystemExit(f"check failed at wheel-skill: missing {member}")
            disk = os.path.join(ROOT, "skills", "agent-session-control", rel)
            with open(disk, "rb") as fh:
                if zf.read(member) != fh.read():
                    raise SystemExit(f"check failed at wheel-skill: bytes differ for {member}")
            print(f":: wheel-skill: {member}", flush=True)


def _clean_install(wheel: str, install_args: list[str], label: str) -> None:
    with tempfile.TemporaryDirectory(prefix=f"asc-clean-{label}-") as tmp:
        venv_python = _make_venv(tmp)
        _verify_venv_interpreter(venv_python)
        target = install_args if install_args else [wheel]
        _pip_install(venv_python, target, label)
        # Isolated probes: PYTHONPATH is stripped so a stale source checkout
        # (e.g. an old installed 0.1.0 vs current 0.1.1 source) cannot mask
        # the wheel under test. Module path must resolve inside this venv.
        # Resolved containment, not a textual prefix: macOS aliases /var to
        # /private/var, so startswith() rejects a genuine in-venv import.
        clean_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        probe = (
            "import agent_session_control as asc, pathlib;"
            "from agent_session_control import runtime, store, correlation;"
            "plan = runtime.build_create_plan('codex', '/repo');"
            "assert plan.argv[:2] == ('codex', '--no-daemon');"
            f"assert asc.__version__ == '{EXPECTED_VERSION}', asc.__version__;"
            f"root = pathlib.Path({tmp!r}).resolve();"
            "loc = pathlib.Path(asc.__file__).resolve();"
            "assert loc.is_relative_to(root), (loc, root);"
            "assert 'site-packages' in loc.as_posix(), loc;"
            "print('clean install ok:', asc.__version__, loc)"
        )
        run(f"clean-import[{label}]", venv_python, "-c", probe, cwd=tmp, env=clean_env)
        if label in ("acp", "combined"):
            run(
                f"clean-acp-import[{label}]",
                venv_python,
                "-c",
                "import acp, agent_session_control.acp; print('acp extra ok')",
                cwd=tmp,
                env=clean_env,
            )
        if label in ("history", "combined"):
            run(
                f"clean-history-import[{label}]",
                venv_python,
                "-c",
                "import sesskit; print('history extra ok', sesskit.__version__)",
                cwd=tmp,
                env=clean_env,
            )
        cli_probe = capture(
            f"cli-version[{label}]", venv_python, "-m", "agent_session_control", "version", cwd=tmp, env=clean_env
        )
        compact = cli_probe.lower().replace(" ", "")
        if '"ok":true' not in compact:
            raise SystemExit(f"check failed at cli-version[{label}]: no ok envelope in output above")
        _check_installed_skill_bytes(venv_python, tmp, clean_env, label)
        if label == "combined":
            _external_consumer_probe(venv_python, tmp, clean_env)


def _check_installed_skill_bytes(venv_python: str, tmp: str, env: dict, label: str) -> None:
    # Installed skill resources must equal the source dir byte-for-byte, carry
    # the explicit contract names, and never include the assessment fixture.
    probe = (
        "import agent_session_control as asc, os;"
        "base = os.path.join(os.path.dirname(asc.__file__), 'skills', 'agent-session-control');"
        "import pathlib;"
        f"expected = {sorted(_skill_source_files())!r};"
        "assert 'SKILL.md' in expected, expected;"
        "assert 'references/mcp.md' in [p.replace(chr(92), '/') for p in expected], expected;"
        "assert not [p for p in expected if os.path.basename(p) == 'evaluation.md'], expected;"
        "got = sorted(str(p.relative_to(base)) for p in pathlib.Path(base).rglob('*') if p.is_file());"
        "assert got == expected, (got, expected);"
        "assert not [p for p in got if os.path.basename(p) == 'evaluation.md'], got;"
        "print('installed skills ok:', len(got))"
    )
    run(f"clean-skill-list[{label}]", venv_python, "-c", probe, cwd=tmp, env=env)
    for rel in _skill_source_files():
        check = (
            "import agent_session_control as asc, os;"
            f"rel = {rel!r};"
            "base = os.path.join(os.path.dirname(asc.__file__), 'skills', 'agent-session-control');"
            "data = open(os.path.join(base, rel), 'rb').read();"
            f"disk = {os.path.join(ROOT, 'skills', 'agent-session-control')!r};"
            "want = open(os.path.join(disk, rel), 'rb').read();"
            "assert data == want, rel;"
        )
        run(f"clean-skill-bytes[{label}:{rel}]", venv_python, "-c", check, cwd=tmp, env=env)


def _external_consumer_probe(venv_python: str, tmp: str, env: dict | None = None) -> None:
    # Unique owned temp state (no fixed /tmp paths): create one run, prove
    # idempotent convergence, assert the audit row, and leave nothing behind
    # (the TemporaryDirectory removes the root on exit).
    with tempfile.TemporaryDirectory(prefix="asc-consumer-probe-") as case:
        root = os.path.join(case, "control")
        probe = (
            "from agent_session_control import RunStore, build_create_plan;"
            f"root = {root!r};"
            "plan = build_create_plan('cursor', '/tmp/demo');"
            "assert plan.argv and plan.argv[0], plan.argv;"
            "store = RunStore(root=root, namespace='probe');"
            "record, created = store.create('op', 'k-probe', {'t': 1}, {'execution': 'queued'});"
            "assert created is True, (record, created);"
            "assert record['run_id'].startswith('run_'), record;"
            "again, created2 = store.create('op', 'k-probe', {'t': 1}, {'execution': 'queued'});"
            "assert created2 is False and again['run_id'] == record['run_id'], (again, created2);"
            "trail = store.audit_trail(target=record['run_id']);"
            "assert any(r.get('action') == 'run.create' and r.get('actor') == 'op' for r in trail), trail;"
            "print('external-consumer ok:', record['run_id'], 'audit:', len(trail));"
        )
        run("external-consumer", venv_python, "-c", probe, cwd=case, env=env or os.environ)


def _make_venv(tmp: str) -> str:
    path = os.path.join(tmp, "venv")
    want = _current_py()
    uv = shutil.which("uv")
    if uv is not None:
        # Respect the matrix interpreter: the venv must use this check's
        # Python, not a hardcoded version.
        run("venv", uv, "venv", "--python", want, "--seed", path)
    else:
        # Plain stdlib venv (ensurepip provides pip by default); there is no
        # --seed option here, so it must not be passed.
        run("venv", sys.executable, "-m", "venv", path)
    if os.name == "nt":
        return os.path.join(path, "Scripts", "python.exe")
    return os.path.join(path, "bin", "python")


def _verify_venv_interpreter(venv_python: str) -> None:
    out = subprocess.run(
        [venv_python, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=os.environ,
    )
    if out.returncode != 0:
        raise SystemExit(f"check failed at venv-verify: {out.stderr[-1000:]}")
    got = out.stdout.strip()
    want = _current_py()
    print(f":: venv-verify: want={want} got={got}", flush=True)
    if got != want:
        raise SystemExit(f"check failed at venv-verify: venv python {got} != check python {want}")


def _pip_install(venv_python: str, targets: list[str], label: str) -> None:
    uv = shutil.which("uv")
    if uv is not None:
        run(f"clean-install[{label}]", uv, "pip", "install", "--python", venv_python, *targets)
        return
    run(f"clean-install[{label}]", venv_python, "-m", "pip", "install", *targets)


def _clean_install_plain_pip(wheel: str) -> None:
    # Prove the file:// URI installs without uv (uv's bare-path tolerance
    # must not be load-bearing). Always uses stdlib pip, even when uv exists.
    with tempfile.TemporaryDirectory(prefix="asc-clean-plain-pip-") as tmp:
        venv_python = _make_venv(tmp)
        _verify_venv_interpreter(venv_python)
        uri = _wheel_file_uri(wheel)
        assert uri.startswith("file://"), uri
        run("clean-install[plain-pip]", venv_python, "-m", "pip", "install", uri)
        clean_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        # Resolved containment (macOS /var alias): the module path must
        # resolve inside this venv; a textual prefix rejects /private/var.
        probe = (
            "import agent_session_control as asc, pathlib;"
            f"assert asc.__version__ == '{EXPECTED_VERSION}', asc.__version__;"
            f"root = pathlib.Path({tmp!r}).resolve();"
            "loc = pathlib.Path(asc.__file__).resolve();"
            "assert loc.is_relative_to(root), (loc, root);"
            "print('plain-pip install ok:', asc.__version__, loc)"
        )
        run("clean-import[plain-pip]", venv_python, "-c", probe, cwd=tmp, env=clean_env)


if __name__ == "__main__":
    main()
