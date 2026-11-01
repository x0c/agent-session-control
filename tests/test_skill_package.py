"""Skill packaged-artifact coverage: isolated wheel/sdist + clean install.

Builds the current checkout into an isolated temp outdir (never the
shared ``dist/``), requires the skill source plus every relative link
target to ship with byte-identical contents and resolvable package
paths, then installs the wheel into an isolated venv with
``PYTHONPATH`` cleared and asserts import path, version (read from
``pyproject.toml``, never hardcoded), and resource bytes. No skips, no
mtime gating, no model/terminal sessions.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent

SKILL_DIR = REPO / "skills" / "agent-session-control"
SKILL_MD = SKILL_DIR / "SKILL.md"
PKG_SKILL_PREFIX = "agent_session_control/skills/agent-session-control/"


def intended_version() -> str:
    for line in (REPO / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("version ="):
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    raise AssertionError("version not found in pyproject.toml")


def skill_link_targets() -> list[str]:
    """Relative (non-URL) link targets inside the skill source."""
    text = SKILL_MD.read_text(encoding="utf-8")
    targets = re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)
    return [t for t in targets if not t.startswith(("http://", "https://", "#", "mailto:"))]


@pytest.fixture
def isolated_dirs(tmp_path: Path) -> Iterator[Path]:
    """Scratch dirs for build/venv artifacts, removed after the test
    whether it passes or fails — no persistent temp environments."""
    base = tmp_path / "skill-pkg"
    base.mkdir(parents=True, exist_ok=True)
    yield base
    shutil.rmtree(base, ignore_errors=True)


def build_isolated(base: Path) -> tuple[Path, Path]:
    """Build the current checkout into ``base/dist``; return (wheel, sdist)."""
    outdir = base / "dist"
    outdir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(outdir)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(REPO),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    wheels = sorted(outdir.glob("agent_session_control-*.whl"))
    sdists = sorted(outdir.glob("agent_session_control-*.tar.gz"))
    assert wheels, "isolated build must produce a wheel"
    assert sdists, "isolated build must produce an sdist"
    version = intended_version()
    assert version in wheels[0].name, f"wheel name must carry {version}: {wheels[0].name}"
    assert version in sdists[0].name, f"sdist name must carry {version}: {sdists[0].name}"
    return wheels[0], sdists[0]


def test_wheel_and_sdist_contain_skill_and_linked_references(isolated_dirs: Path) -> None:
    wheel, sdist = build_isolated(isolated_dirs)
    expected = ["SKILL.md", *skill_link_targets()]
    assert expected, "skill must carry at least one resolvable relative link"
    for target in expected:
        assert (SKILL_DIR / target).is_file(), f"link target missing from source: {target}"

    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        for target in expected:
            member = PKG_SKILL_PREFIX + target
            assert member in names, f"wheel must contain packaged {target}: {[n for n in names if 'skills/' in n]}"
            assert zf.read(member) == (SKILL_DIR / target).read_bytes(), f"wheel bytes differ for {target}"

    with tarfile.open(sdist, "r:gz") as tf:
        members = tf.getnames()
        top = next((n for n in members if n.endswith("PKG-INFO")), None)
        assert top is not None
        root = top[: -len("PKG-INFO")]
        for target in expected:
            member = root + "skills/agent-session-control/" + target
            assert member in members, f"sdist must contain {target}"
            handle = tf.extractfile(member)
            assert handle is not None
            assert handle.read() == (SKILL_DIR / target).read_bytes(), f"sdist bytes differ for {target}"


def _make_venv(path: Path) -> Path:
    uv = shutil.which("uv")
    if uv is not None:
        proc = subprocess.run(
            [uv, "venv", "--python", sys.executable, "--seed", str(path)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
    else:
        proc = subprocess.run([sys.executable, "-m", "venv", str(path)], capture_output=True, text=True, timeout=180)
        assert proc.returncode == 0, proc.stderr[-2000:]
    exe = path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    assert exe.is_file(), f"venv python missing: {exe}"
    return exe


def _clean_env() -> dict:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def test_empty_venv_bootstrap_entry_direct(isolated_dirs: Path) -> None:
    """Real bootstrap path from the skill: empty venv, cleared PATH,
    install the trusted locally-built candidate wheel via
    ``python -m pip``, then drive ``ABS=<venv>/bin/agent-
    session-control`` directly — executable bit plus version readback
    with no PATH dependence."""
    wheel, _sdist = build_isolated(isolated_dirs)
    venv_python = _make_venv(isolated_dirs / "bootstrap-venv")
    env = _clean_env()
    install = subprocess.run(
        [str(venv_python), "-m", "pip", "install", str(wheel)],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    assert install.returncode == 0, install.stderr[-3000:]
    entry = venv_python.parent / ("agent-session-control.exe" if os.name == "nt" else "agent-session-control")
    assert entry.is_file(), f"venv entry missing: {entry}"
    assert os.access(entry, os.X_OK), f"venv entry not executable: {entry}"
    no_path = dict(env)
    no_path["PATH"] = ""
    probe = subprocess.run(
        [str(entry), "version", "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        env=no_path,
    )
    assert probe.returncode == 0, probe.stderr[-2000:]
    data = json.loads(probe.stdout)
    assert data["ok"] is True and data["error"] is None
    assert data["data"]["package"] == "agent-session-control"
    assert data["data"]["version"] == intended_version()


def test_clean_install_imports_isolated_wheel_with_resource_bytes(isolated_dirs: Path) -> None:
    wheel, _sdist = build_isolated(isolated_dirs)
    tmp = isolated_dirs
    venv_python = _make_venv(tmp / "venv")
    env = _clean_env()
    assert "PYTHONPATH" not in env

    uv = shutil.which("uv")
    if uv is not None:
        proc = subprocess.run(
            [uv, "pip", "install", "--python", str(venv_python), str(wheel)],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
    else:
        proc = subprocess.run(
            [str(venv_python), "-m", "pip", "install", str(wheel)],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
    assert proc.returncode == 0, proc.stderr[-3000:]

    probe = subprocess.run(
        [str(venv_python), "-c", "import agent_session_control as a; print(a.__file__); print(a.__version__)"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert probe.returncode == 0, probe.stderr[-2000:]
    location, _, version = probe.stdout.strip().partition("\n")
    assert version.strip() == intended_version()
    assert location.startswith(str(tmp / "venv")), f"must import from the isolated venv, got {location}"
    assert str(REPO / "src") not in location

    pkg_dir = Path(location).parent
    for target in ["SKILL.md", *skill_link_targets()]:
        installed = pkg_dir / "skills" / "agent-session-control" / target
        assert installed.is_file(), f"installed wheel must carry resolving link target: {target}"
        assert installed.read_bytes() == (SKILL_DIR / target).read_bytes(), f"installed bytes differ: {target}"
