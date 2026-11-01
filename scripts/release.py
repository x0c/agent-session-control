"""Local release preparation: version gate, build, artifact checks.

Run locally (CI-independent)::

    uv sync --group dev --all-extras
    uv run --python 3.12 --group dev python scripts/release.py
    uv run --python 3.12 --group dev python scripts/release.py --extras   # also verify history/acp installs

This script never tags, uploads, pushes, or publishes. It prepares and
verifies ``dist/`` artifacts, then prints the exact coordinator handoff
(tag name, expected asset names, publish workflow). GitHub wheel recipes
stay until actual PyPI readback passes.

Exit nonzero on any mismatch; nothing is skipped silently.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPECTED_VERSION = "0.1.1"
SESSKIT_WHEEL_URL = "https://github.com/x0c/sesskit/releases/download/v0.2.21/sesskit-0.2.21-py3-none-any.whl"


def run(label: str, *argv: str, cwd: str = ROOT) -> None:
    print(f":: {label}: {' '.join(argv)}", flush=True)
    proc = subprocess.run(argv, cwd=cwd, env=os.environ)
    if proc.returncode != 0:
        raise SystemExit(f"release failed at {label} (exit {proc.returncode})")


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    _version_gate()
    dist = os.path.join(ROOT, "dist")
    shutil.rmtree(dist, ignore_errors=True)
    run("build", sys.executable, "-m", "build", "--outdir", dist)
    wheels = sorted(f for f in os.listdir(dist) if f.endswith(".whl"))
    sdists = sorted(f for f in os.listdir(dist) if f.endswith(".tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit(f"expected exactly one wheel and one sdist, got: {wheels + sdists}")
    wheel, sdist = wheels[0], sdists[0]
    for name in (wheel, sdist):
        if EXPECTED_VERSION not in name:
            raise SystemExit(f"artifact {name} must carry version {EXPECTED_VERSION}")
    wheel_path = os.path.join(dist, wheel)
    sdist_path = os.path.join(dist, sdist)
    _check_wheel(wheel_path)
    _check_sdist(sdist_path)
    _write_digests(dist, [wheel, sdist])
    _clean_core_install(wheel_path)
    _clean_plain_pip_install(wheel_path)
    if args.extras:
        _clean_extra_install(os.path.join(dist, wheel))
    print(f"release artifacts ready: dist/{wheel} dist/{sdist}", flush=True)
    print(
        "handoff (coordinator-owned): create tag v0.1.1, create GitHub Release "
        "from that tag attaching dist/*, then run the Trusted Publisher workflow "
        "(.github/workflows/publish.yml). This script performed no upload/tag/push.",
        flush=True,
    )
    return 0


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare and verify 0.1.1 release artifacts locally.")
    parser.add_argument(
        "--extras",
        action="store_true",
        help="also verify history/acp clean installs (needs network for PyPI + fixed wheels)",
    )
    return parser.parse_args(argv)


def _version_gate() -> None:
    import agent_session_control as asc

    text = open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8").read()
    if f'version = "{EXPECTED_VERSION}"' not in text:
        raise SystemExit("pyproject.toml version mismatch")
    if asc.__version__ != EXPECTED_VERSION:
        raise SystemExit(f"package __version__ is {asc.__version__}, want {EXPECTED_VERSION}")
    lock = open(os.path.join(ROOT, "uv.lock"), encoding="utf-8").read()
    if f'version = "{EXPECTED_VERSION}"' not in lock:
        raise SystemExit("uv.lock version mismatch (run: uv lock)")
    print(f":: version-gate: {EXPECTED_VERSION} ok", flush=True)


def _skill_source_files() -> list[str]:
    skill_dir = os.path.join(ROOT, "skills", "agent-session-control")
    out: list[str] = []
    for base, _dirs, files in os.walk(skill_dir):
        for name in sorted(files):
            out.append(os.path.relpath(os.path.join(base, name), skill_dir))
    return sorted(out)


def _wheel_file_uri(wheel: str) -> str:
    # PEP 508: local direct references must be file:// URIs; a bare absolute
    # path only works as a uv convenience. Plain pip needs the URI.
    return Path(wheel).resolve().as_uri()


def _check_sdist(sdist: str) -> None:
    expected = _skill_source_files()
    required = ["SKILL.md", os.path.join("references", "mcp.md")]
    if any(r not in expected for r in required):
        raise SystemExit(f"sdist source skill incomplete: {expected}")
    if any(os.path.basename(p) == "evaluation.md" for p in expected):
        raise SystemExit(f"sdist assessment fixture must not ship: {expected}")
    with tarfile.open(sdist, "r:gz") as tf:
        names = tf.getnames()
        top = names[0].split("/")[0] if names else ""
        if EXPECTED_VERSION not in top:
            raise SystemExit(f"sdist top dir must carry version {EXPECTED_VERSION}: {top}")
        for rel in expected:
            member = f"{top}/skills/agent-session-control/{rel}"
            if member not in names:
                raise SystemExit(f"sdist missing packaged skill file {member}")
            with open(os.path.join(ROOT, "skills", "agent-session-control", rel), "rb") as fh:
                extracted = tf.extractfile(member)
                if extracted is None or extracted.read() != fh.read():
                    raise SystemExit(f"sdist skill bytes differ for {member}")
    print(":: sdist: ok (version dir, skill dir + bytes)", flush=True)


def _write_digests(dist: str, names: list[str]) -> None:
    lines = []
    for name in names:
        digest = hashlib.sha256(open(os.path.join(dist, name), "rb").read()).hexdigest()
        lines.append(f"{digest}  {name}")
        print(f":: digest: {digest}  {name}", flush=True)
    with open(os.path.join(dist, "SHA256SUMS"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _check_wheel(wheel: str) -> None:
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        meta_name = next((n for n in names if n.endswith(".dist-info/METADATA")), None)
        if meta_name is None:
            raise SystemExit("no .dist-info/METADATA in wheel")
        meta = zf.read(meta_name).decode("utf-8")
        for needle in (
            f"Version: {EXPECTED_VERSION}",
            "Provides-Extra: history",
            "Provides-Extra: acp",
        ):
            if needle not in meta:
                raise SystemExit(f"wheel METADATA missing {needle!r}")
        # Requires-Python is normalized by the builder (order/spacing vary,
        # e.g. "<3.15,>=3.10"): assert the bounds, not the literal string.
        requires = next((line for line in meta.splitlines() if line.startswith("Requires-Python:")), "")
        if ">=3.10" not in requires or "<3.15" not in requires:
            raise SystemExit(f"wheel METADATA bad Requires-Python {requires!r}")
        if "sesskit" not in meta or "agent-client-protocol" not in meta:
            raise SystemExit("wheel METADATA must name both extras")
        expected = _skill_source_files()
        required = ["SKILL.md", os.path.join("references", "mcp.md")]
        if any(r not in expected for r in required):
            raise SystemExit(f"wheel source skill incomplete: {expected}")
        if any(os.path.basename(p) == "evaluation.md" for p in expected):
            raise SystemExit(f"wheel assessment fixture must not ship: {expected}")
        skill_prefix = "agent_session_control/skills/agent-session-control/"
        if any(n.startswith(skill_prefix) and os.path.basename(n) == "evaluation.md" for n in names):
            raise SystemExit("wheel assessment fixture must not ship in wheel")
        for rel in expected:
            member = "agent_session_control/skills/agent-session-control/" + rel.replace(os.sep, "/")
            if member not in names:
                raise SystemExit(f"wheel missing packaged skill file {member}")
            with open(os.path.join(ROOT, "skills", "agent-session-control", rel), "rb") as fh:
                if zf.read(member) != fh.read():
                    raise SystemExit(f"wheel skill bytes differ for {member}")
    print(":: wheel-metadata: ok (version, requires-python, extras, skill dir + bytes)", flush=True)


def _venv_python(tmp: str) -> str:
    path = os.path.join(tmp, "venv")
    major, minor = sys.version_info[:2]
    uv = shutil.which("uv")
    if uv is not None:
        run("venv", uv, "venv", "--python", f"{major}.{minor}", "--seed", path)
    else:
        run("venv", sys.executable, "-m", "venv", path)
    return os.path.join(path, "Scripts", "python.exe") if os.name == "nt" else os.path.join(path, "bin", "python")


def _pip(venv_python: str, *targets: str) -> None:
    uv = shutil.which("uv")
    if uv is not None:
        run("pip-install", uv, "pip", "install", "--python", venv_python, *targets)
    else:
        run("pip-install", venv_python, "-m", "pip", "install", *targets)


def _check_version_envelope(venv_python: str, tmp: str) -> None:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [venv_python, "-m", "agent_session_control", "version"],
        capture_output=True,
        text=True,
        cwd=tmp,
        env=env,
    )
    print(proc.stdout, flush=True)
    print(proc.stderr, flush=True)
    if proc.returncode != 0 or '"ok":true' not in proc.stdout.replace(" ", "").lower():
        raise SystemExit("installed version envelope check failed")
    if f'"version":"{EXPECTED_VERSION}"'.lower() not in proc.stdout.replace(" ", "").lower():
        raise SystemExit(f"installed version envelope must carry {EXPECTED_VERSION}: {proc.stdout[:500]}")
    probe = subprocess.run(
        [venv_python, "-c", "import agent_session_control as a; print(a.__file__); print(a.__version__)"],
        capture_output=True,
        text=True,
        cwd=tmp,
        env=env,
    )
    print(probe.stdout, flush=True)
    print(probe.stderr, flush=True)
    if probe.returncode != 0:
        raise SystemExit("installed module probe failed")
    lines = probe.stdout.strip().splitlines()
    if len(lines) < 2 or lines[-1].strip() != EXPECTED_VERSION:
        raise SystemExit(f"installed __version__ must be {EXPECTED_VERSION}: {probe.stdout[:500]}")
    location = lines[0].strip()
    # Resolved containment, not a textual prefix: macOS aliases /var to
    # /private/var, so startswith() rejects a genuine in-venv import.
    # Resolve both sides and require containment, keeping site-packages.
    resolved = Path(location).resolve()
    if not resolved.is_relative_to(Path(tmp).resolve()) or "site-packages" not in resolved.as_posix():
        raise SystemExit(f"must import from the isolated venv ({tmp}), got {location}")
    _check_installed_skill_bytes(venv_python, tmp, env)


def _check_installed_skill_bytes(venv_python: str, tmp: str, env: dict) -> None:
    expected = _skill_source_files()
    required = ["SKILL.md", os.path.join("references", "mcp.md")]
    if any(r not in expected for r in required):
        raise SystemExit(f"installed source skill incomplete: {expected}")
    if any(os.path.basename(p) == "evaluation.md" for p in expected):
        raise SystemExit(f"installed assessment fixture must not ship: {expected}")
    probe = (
        "import agent_session_control as asc, os, pathlib;"
        "base = os.path.join(os.path.dirname(asc.__file__), 'skills', 'agent-session-control');"
        f"expected = {expected!r};"
        "assert 'SKILL.md' in expected, expected;"
        "assert 'references/mcp.md' in [p.replace(chr(92), '/') for p in expected], expected;"
        "assert not [p for p in expected if os.path.basename(p) == 'evaluation.md'], expected;"
        "got = sorted(str(p.relative_to(base)) for p in pathlib.Path(base).rglob('*') if p.is_file());"
        "assert got == expected, (got, expected);"
        "assert not [p for p in got if os.path.basename(p) == 'evaluation.md'], got;"
        "print('installed skills ok:', len(got))"
    )
    proc = subprocess.run([venv_python, "-c", probe], capture_output=True, text=True, cwd=tmp, env=env)
    print(proc.stdout, flush=True)
    print(proc.stderr, flush=True)
    if proc.returncode != 0:
        raise SystemExit(f"installed skill listing failed: {proc.stderr[-1000:]}")
    for rel in expected:
        check = (
            "import agent_session_control as asc, os;"
            f"rel = {rel!r};"
            "base = os.path.join(os.path.dirname(asc.__file__), 'skills', 'agent-session-control');"
            "data = open(os.path.join(base, rel), 'rb').read();"
            f"disk = {os.path.join(ROOT, 'skills', 'agent-session-control')!r};"
            "want = open(os.path.join(disk, rel), 'rb').read();"
            "assert data == want, rel;"
        )
        proc = subprocess.run([venv_python, "-c", check], capture_output=True, text=True, cwd=tmp, env=env)
        if proc.returncode != 0:
            raise SystemExit(f"installed skill bytes differ for {rel}: {proc.stderr[-1000:]}")


def _clean_core_install(wheel: str) -> None:
    with tempfile.TemporaryDirectory(prefix="asc-release-core-") as tmp:
        venv_python = _venv_python(tmp)
        _pip(venv_python, _wheel_file_uri(wheel))
        _check_version_envelope(venv_python, tmp)
    print(":: clean-core-install: ok", flush=True)


def _clean_plain_pip_install(wheel: str) -> None:
    # Plain-pip path without uv (file:// URI must work on its own).
    with tempfile.TemporaryDirectory(prefix="asc-release-plain-pip-") as tmp:
        venv_python = _venv_python(tmp)
        uri = _wheel_file_uri(wheel)
        assert uri.startswith("file://"), uri
        proc = subprocess.run(
            [venv_python, "-m", "pip", "install", uri],
            capture_output=True,
            text=True,
            cwd=tmp,
            env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        )
        print(proc.stdout, flush=True)
        print(proc.stderr, flush=True)
        if proc.returncode != 0:
            raise SystemExit(f"plain-pip install failed: {proc.stderr[-1000:]}")
        _check_version_envelope(venv_python, tmp)
    print(":: clean-plain-pip-install: ok", flush=True)


def _clean_extra_install(wheel: str) -> None:
    with tempfile.TemporaryDirectory(prefix="asc-release-extras-") as tmp:
        venv_python = _venv_python(tmp)
        _pip(
            venv_python,
            SESSKIT_WHEEL_URL,
            f"agent-session-control[history,acp] @ {_wheel_file_uri(wheel)}",
        )
        run(
            "extra-imports",
            venv_python,
            "-c",
            "import sesskit, acp, agent_session_control.acp;print('extras ok', sesskit.__version__)",
            cwd=tmp,
        )
        _check_version_envelope(venv_python, tmp)
    print(":: clean-extra-install: ok", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
