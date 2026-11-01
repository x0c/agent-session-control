"""Distribution consistency: version, metadata, extras, skill packaging.

Covers the delivery-owned install surface without launching terminals or
spending quota: version strings agree across metadata/lock/docs/tests,
wheel/sdist metadata names both extras, the skill source ships in both
artifacts, extras parse per PEP 508, publish validates the exact artifact
without rebuilding, and the consumer example honors its path/live gates
with asserted return values.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
import zipfile
from importlib import metadata
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "0.1.1"
SESSKIT_WHEEL_URL = "https://github.com/x0c/sesskit/releases/download/v0.2.21/sesskit-0.2.21-py3-none-any.whl"


def _read_version_from_pyproject() -> str:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("version ="):
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    raise AssertionError("version not found in pyproject.toml")


def _skill_files() -> list[str]:
    base = REPO / "skills" / "agent-session-control"
    out: list[str] = []
    for root, _dirs, files in __import__("os").walk(base):
        for name in sorted(files):
            out.append(str((Path(root) / name).relative_to(base)))
    return sorted(out)


def test_version_agrees_everywhere() -> None:
    assert _read_version_from_pyproject() == EXPECTED_VERSION
    import agent_session_control as asc

    assert asc.__version__ == EXPECTED_VERSION
    lock = (REPO / "uv.lock").read_text(encoding="utf-8")
    assert f'name = "agent-session-control"\nversion = "{EXPECTED_VERSION}"' in lock
    for doc in ("README.md", "README.zh-CN.md", "docs/INTEGRATION.md"):
        text = (REPO / doc).read_text(encoding="utf-8")
        assert EXPECTED_VERSION in text, f"{doc} must reference {EXPECTED_VERSION}"
        assert "0.1.0" not in text, f"{doc} must not reference the old 0.1.0"
    # NOTE (cross-area, coordinator): src/agent_session_control/cli.py and
    # acp.py carry "0.1.0" importlib.metadata fallbacks owned by workers A/C;
    # they are intentionally not asserted here and are reported separately.


def test_project_urls_present() -> None:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "[project.urls]" in text
    for key in ("Homepage", "Repository", "Documentation", "Changelog"):
        assert key in text


def test_extras_parse() -> None:
    reqs = metadata.requires("agent-session-control") or []
    by_extra: dict[str, list[str]] = {}
    for req in reqs:
        if "extra ==" in req:
            extra = req.split("extra ==")[1].strip(" '\"),;")
            by_extra.setdefault(extra, []).append(req)
    assert "history" in by_extra
    assert "acp" in by_extra
    assert any("sesskit" in r for r in by_extra["history"])
    assert any("agent-client-protocol" in r for r in by_extra["acp"])


def test_install_recipes_parse() -> None:
    from packaging.requirements import Requirement

    wheel = (
        "https://github.com/x0c/agent-session-control/releases/"
        f"download/v{EXPECTED_VERSION}/agent_session_control-{EXPECTED_VERSION}-py3-none-any.whl"
    )
    recipes = [
        f"agent-session-control @ {wheel}",
        f"sesskit @ {SESSKIT_WHEEL_URL}",
        f"agent-session-control[history] @ {wheel}",
        f"agent-session-control[history,acp] @ {wheel}",
    ]
    for recipe in recipes:
        req = Requirement(recipe)
        assert req.name
    hist = Requirement(f"agent-session-control[history] @ {wheel}")
    assert hist.extras == {"history"}
    both = Requirement(f"agent-session-control[history,acp] @ {wheel}")
    assert both.extras == {"history", "acp"}


def test_skill_source_configured() -> None:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "force-include" in text
    # Whole maintained skill directory ships, not just SKILL.md alone.
    assert '"skills/agent-session-control"' in text
    assert '"agent_session_control/skills/agent-session-control"' in text


def test_skill_source_exists_for_owner() -> None:
    # F owns skills/agent-session-control/* content; D only
    # asserts the packaged source paths exist. Contract: SKILL.md +
    # references/mcp.md required; grading corpus lives under
    # tests/fixtures/skill/ and must never ship as a runtime resource.
    base = REPO / "skills" / "agent-session-control"
    assert (base / "SKILL.md").is_file()
    assert (base / "references" / "mcp.md").is_file(), "runtime skill requires references/mcp.md"
    assert not (base / "references" / "evaluation.md").is_file()
    assert not (base / "evaluation.md").is_file()
    assert len(_skill_files()) >= 2


@pytest.fixture(scope="module")
def built_artifacts(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Build wheel+sdist once per module into owned tmp (no mkdtemp leaks)."""
    outdir = tmp_path_factory.mktemp("asc-dist")
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(outdir)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(REPO),
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    wheels = sorted(outdir.glob("agent_session_control-*.whl"))
    sdists = sorted(outdir.glob("agent_session_control-*.tar.gz"))
    assert len(wheels) == 1 and len(sdists) == 1, f"want 1 wheel+1 sdist: {sorted(p.name for p in outdir.iterdir())}"
    assert EXPECTED_VERSION in wheels[0].name and EXPECTED_VERSION in sdists[0].name
    return {"wheel": wheels[0], "sdist": sdists[0], "outdir": outdir}


def test_skill_in_wheel_byte_identical(built_artifacts: dict[str, Path]) -> None:
    expected = _skill_files()
    assert "SKILL.md" in expected
    assert "references/mcp.md" in [p.replace("\\", "/") for p in expected]
    assert not [p for p in expected if Path(p).name == "evaluation.md"]
    with zipfile.ZipFile(built_artifacts["wheel"]) as zf:
        names = zf.namelist()
        prefix = "agent_session_control/skills/agent-session-control/"
        assert not [n for n in names if n.startswith(prefix) and Path(n).name == "evaluation.md"], (
            "assessment fixture tests/fixtures/skill/evaluation.md must not ship in wheel skill resources"
        )
        for rel in expected:
            member = "agent_session_control/skills/agent-session-control/" + rel
            assert member in names, f"wheel must contain packaged {rel}"
            assert zf.read(member) == (REPO / "skills" / "agent-session-control" / rel).read_bytes(), (
                f"wheel bytes differ for {rel}"
            )


def test_skill_in_sdist_byte_identical(built_artifacts: dict[str, Path]) -> None:
    expected = _skill_files()
    assert "SKILL.md" in expected
    assert "references/mcp.md" in [p.replace("\\", "/") for p in expected]
    assert not [p for p in expected if Path(p).name == "evaluation.md"]
    with tarfile.open(built_artifacts["sdist"], "r:gz") as tf:
        names = tf.getnames()
        top = names[0].split("/")[0] if names else ""
        assert EXPECTED_VERSION in top, f"sdist top dir must carry version: {top}"
        skill_prefix = f"{top}/skills/agent-session-control/"
        assert not [n for n in names if n.startswith(skill_prefix) and Path(n).name == "evaluation.md"], (
            "assessment fixture tests/fixtures/skill/evaluation.md must not ship in sdist skill resources"
        )
        for rel in expected:
            member = f"{top}/skills/agent-session-control/{rel}"
            assert member in names, f"sdist must contain packaged {rel}"
            extracted = tf.extractfile(member)
            assert extracted is not None, member
            assert extracted.read() == (REPO / "skills" / "agent-session-control" / rel).read_bytes(), (
                f"sdist bytes differ for {rel}"
            )


def test_wheel_metadata_names_extras(built_artifacts: dict[str, Path]) -> None:
    with zipfile.ZipFile(built_artifacts["wheel"]) as zf:
        meta_name = next(n for n in zf.namelist() if n.endswith(".dist-info/METADATA"))
        meta = zf.read(meta_name).decode("utf-8")
    assert f"Version: {EXPECTED_VERSION}" in meta
    assert "Provides-Extra: history" in meta and "Provides-Extra: acp" in meta
    assert "sesskit" in meta and "agent-client-protocol" in meta


def _venv_python_for(tmp: Path) -> str:
    import shutil

    venv_dir = tmp / "venv"
    uv = shutil.which("uv")
    if uv is not None:
        make = subprocess.run(
            [uv, "venv", "--python", sys.executable, "--seed", str(venv_dir)],
            capture_output=True,
            text=True,
            timeout=180,
        )
    else:
        make = subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)], capture_output=True, text=True, timeout=180
        )
    assert make.returncode == 0, make.stderr[-2000:]
    exe = venv_dir / ("Scripts/python.exe" if __import__("os").name == "nt" else "bin/python")
    assert exe.is_file()
    return str(exe)


def test_clean_installed_skill_resources_byte_identical(built_artifacts: dict[str, Path], tmp_path: Path) -> None:
    """File-URI install (works under plain pip, not just uv) + path/version guards."""
    import os
    import shutil

    wheel = built_artifacts["wheel"]
    wheel_uri = wheel.resolve().as_uri()
    assert wheel_uri.startswith("file://"), wheel_uri
    venv_python = _venv_python_for(tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    uv = shutil.which("uv")
    if uv is not None:
        install = subprocess.run(
            [uv, "pip", "install", "--python", venv_python, wheel_uri],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
    else:
        install = subprocess.run(
            [venv_python, "-m", "pip", "install", wheel_uri], capture_output=True, text=True, timeout=300, env=env
        )
    assert install.returncode == 0, install.stderr[-2000:]
    probe = subprocess.run(
        [venv_python, "-c", "import agent_session_control as a; print(a.__file__); print(a.__version__)"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert probe.returncode == 0, probe.stderr[-2000:]
    lines = probe.stdout.strip().splitlines()
    assert len(lines) >= 2
    assert lines[-1].strip() == EXPECTED_VERSION
    assert "site-packages" in lines[0]
    assert lines[0].startswith(str(tmp_path)), f"must import from the isolated venv, got {lines[0]}"
    assert str(REPO / "src") not in lines[0]
    expected = _skill_files()
    assert "SKILL.md" in expected
    assert "references/mcp.md" in [p.replace("\\", "/") for p in expected]
    assert not [p for p in expected if Path(p).name == "evaluation.md"]
    pkg_dir = Path(lines[0]).parent
    skill_base = pkg_dir / "skills" / "agent-session-control"
    assert not (skill_base / "evaluation.md").is_file()
    assert not (skill_base / "references" / "evaluation.md").is_file(), (
        "assessment fixture tests/fixtures/skill/evaluation.md must not ship in wheel skill resources"
    )
    for rel in expected:
        installed = pkg_dir / "skills" / "agent-session-control" / rel
        assert installed.is_file(), f"installed wheel must carry {rel}"
        assert installed.read_bytes() == (REPO / "skills" / "agent-session-control" / rel).read_bytes(), (
            f"installed bytes differ for {rel}"
        )


def test_release_helpers_use_file_uri_and_digests() -> None:
    check = (REPO / "scripts" / "check.py").read_text(encoding="utf-8")
    release = (REPO / "scripts" / "release.py").read_text(encoding="utf-8")
    for text, name in ((check, "check.py"), (release, "release.py")):
        assert "as_uri()" in text, f"{name} must build file:// URIs via Path.as_uri()"
        assert "file://" in text, f"{name} must document/prove the file:// requirement"
        assert "_check_sdist" in text or "sdist" in text, f"{name} must verify the sdist skill dir"
        assert "references/mcp.md" in text or 'os.path.join("references", "mcp.md")' in text, (
            f"{name} must require the explicit runtime skill resource references/mcp.md"
        )
        assert "evaluation.md" in text, f"{name} must guard the assessment fixture from shipping"
    assert "SHA256SUMS" in release, "release.py must write digests for the validated artifact"
    assert "_clean_plain_pip_install" in release or "plain-pip" in release, (
        "release.py must verify plain-pip file:// installs"
    )
    assert "_clean_install_plain_pip" in check, "check.py must verify plain-pip file:// installs"
    assert "asc-consumer-probe-" in check, "consumer probe must use unique owned temp, no fixed /tmp path"
    assert "audit_trail" in check and "run.create" in check, "consumer probe must assert the audit row"


def test_publish_validated_artifact_flow() -> None:
    """Structural: build uploads the exact validated artifact; publisher downloads only."""
    text = (REPO / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    # SHA-pinned transfer actions (exact pins live in git; assert the shape, not a stale SHA).
    assert "actions/upload-artifact@" in text and "actions/download-artifact@" in text
    assert "pypa/gh-action-pypi-publish@" in text
    build, _, publish = text.partition("publish:\n")
    # Publisher must not rebuild: no checkout/setup/build there by design.
    assert "actions/checkout@" in build, "build job checks out the exact tag"
    assert "uv build" in build, "build job builds once"
    assert "actions/checkout@" not in publish, "publisher must download, never re-checkout"
    assert "setup-python@" not in publish and "setup-uv@" not in publish, "publisher must not set up a build env"
    assert publish.count("uv build") == 0, "publisher must not rebuild"
    # Job-only OIDC mint for Trusted Publishing; no other permission.
    assert "id-token: write" in publish
    assert "contents: read" not in publish, "publisher must not carry contents: read"
    # Validated-artifact identity: digests + build-info, filename/version checks.
    assert "SHA256SUMS" in text and "build-info.txt" in text
    assert "(cd dist && sha256sum -c SHA256SUMS)" in text, (
        "manifest must validate basename-relative in dist cwd (never `sha256sum -c dist/SHA256SUMS` from root)"
    )
    assert "dist/* > dist/SHA256SUMS" not in text, (
        "glob `dist/*` includes the manifest itself (truncated at redirection) so self-digest always fails"
    )
    assert "manifest must not self-list" in text, "build must refuse self-entry in manifest"
    assert "manifest must list exactly wheel+sdist" in text, "manifest must list only wheel+sdist"
    assert "artifact tag mismatch" in text and "wheel version mismatch" in text
    assert "ls dist/ | grep" not in text, "publisher must not parse ls output (SC2010)"
    assert "ls dist/*.whl" not in text, "publisher must not parse ls output (SC2012)"
    assert "for f in dist/*" in text, "filename check must use a glob/for-loop, not ls|grep"
    assert "wheels=(dist/*.whl)" in text, "wheel selection must use a glob array, not ls"
    assert 'test -f "${wheels[0]}"' in text, "wheel array must verify the file exists"
    # Trusted build-commit chain: exact tag resolve + outputs.commit comparison.
    assert "refs/tags/${TAG}" in text, "build must validate the exact tag resolves (not an arbitrary ref)"
    assert "steps.record.outputs.commit" in text, "build must record the resolved tag commit"
    assert "needs.build.outputs.commit" in text, "publisher must compare to trusted build output"
    assert "EXPECTED_COMMIT" in text and "EXPECTED_TAG" in text, "tag/commit travel via env"
    assert "build-info commit != trusted build output" in text, "commit mismatch must refuse"
    assert "rm -f dist/SHA256SUMS dist/build-info.txt" in text, "digests are validation-only, never published"


def test_publish_manifest_shell_roundtrip_regression(tmp_path: Path) -> None:
    """Executable shell roundtrip: preexisting manifest, same layout, tamper rejection.

    Reproduces the `sha256sum dist/* > dist/SHA256SUMS` self-digest failure when a
    validated manifest already exists, then proves the fixed basename-relative flow
    (`cd dist && sha256sum -c SHA256SUMS`) passes clean, passes after an
    upload/download copy with the same path layout, and refuses tampered bytes.
    """
    import hashlib
    import shutil

    case = tmp_path / "manifest-case"
    dist = case / "dist"
    dist.mkdir(parents=True)
    wheel_name = f"agent_session_control-{EXPECTED_VERSION}-py3-none-any.whl"
    sdist_name = f"agent_session_control-{EXPECTED_VERSION}.tar.gz"
    (dist / wheel_name).write_bytes(b"wheel-bytes")
    (dist / sdist_name).write_bytes(b"sdist-bytes")
    # scripts/release.py shape: basenames only, exactly wheel+sdist.
    lines = []
    for name in sorted((wheel_name, sdist_name)):
        lines.append(f"{hashlib.sha256((dist / name).read_bytes()).hexdigest()}  {name}")
    manifest = dist / "SHA256SUMS"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Broken glob includes the preexisting manifest (truncated at redirection).
    broken = subprocess.run(
        "sha256sum dist/* > dist/SHA256SUMS; sha256sum -c dist/SHA256SUMS",
        shell=True,
        cwd=str(case),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert broken.returncode != 0, "glob `dist/*` self-include must fail, not silently pass"
    assert "FAILED" in (broken.stdout + broken.stderr), broken.stdout + broken.stderr
    # Restore the validated manifest and prove the fixed flow passes.
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    fixed = subprocess.run(
        "(cd dist && sha256sum -c SHA256SUMS)",
        shell=True,
        cwd=str(case),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    assert fixed.stdout.count("OK") == 2, fixed.stdout
    # Same path layout as the workflow: upload `dist/*`, download to `dist/`.
    upload = case / "upload"
    upload.mkdir()
    for item in dist.iterdir():
        shutil.copy2(item, upload / item.name)
    assert (upload / "SHA256SUMS").is_file() and (upload / wheel_name).is_file()
    downloaded = case / "dl" / "dist"
    downloaded.mkdir(parents=True)
    for item in upload.iterdir():
        shutil.copy2(item, downloaded / item.name)
    after_dl = subprocess.run(
        "(cd dist && sha256sum -c SHA256SUMS)",
        shell=True,
        cwd=str(case / "dl"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert after_dl.returncode == 0, after_dl.stdout + after_dl.stderr
    # Tampered bytes must be refused.
    with (downloaded / wheel_name).open("ab") as fh:
        fh.write(b"tamper")
    tampered = subprocess.run(
        "(cd dist && sha256sum -c SHA256SUMS)",
        shell=True,
        cwd=str(case / "dl"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert tampered.returncode != 0, "tampered payload must fail digest verification"
    assert "FAILED" in (tampered.stdout + tampered.stderr), tampered.stdout + tampered.stderr


def test_publish_tag_never_interpolates_in_run() -> None:
    """Behavioral refusal shape: the tag travels via env, never `${{ inputs.tag }}` in run:."""
    text = (REPO / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    bad = [line for line in text.splitlines() if line.strip().startswith("run:") and "inputs.tag" in line]
    assert not bad, f"tag must travel via env TAG, never interpolate in run:: {bad}"
    assert "TAG: ${{ inputs.tag }}" in text


def test_ci_no_live_selection_and_tmux() -> None:
    text = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "uv sync --group dev --all-extras" in text, "history/acp extras are required to import sesskit/acp"
    assert "tmux" in text and "tmux -V" in text, "tmux backend dependency must install per-OS"
    assert "command -v tmux" in text, "tmux install must be check-first (preinstalled must not error)"
    assert "brew install tmux" in text
    assert "scripts/check.py --ci" in text, "CI gate must use the explicit no-credential selection"
    assert '"not live"' in text or "'not live'" in text or '-m "not live"' in text or "-m 'not live'" in text
    assert "workflow_dispatch" in text, "live job stays manual"
    assert "x0c/agent-session-control" in text, "live job is canonical-repo-only, never forks"


def test_example_plans_and_cli_envelope(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    proc = subprocess.run(
        [sys.executable, str(REPO / "examples" / "basic_control.py"), "--cwd", str(project)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(REPO),
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "live phases skipped" in proc.stdout
    bad = subprocess.run(
        [sys.executable, str(REPO / "examples" / "basic_control.py"), "--cwd", "relative/path"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO),
    )
    assert bad.returncode == 2
    unknown = subprocess.run(
        [sys.executable, str(REPO / "examples" / "basic_control.py"), "--runtime", "nope", "--cwd", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO),
    )
    assert unknown.returncode == 2


def test_sdk_lifecycle_snippets_execute(tmp_path: Path) -> None:
    """Executed SDK snippets with bounded fixtures: redaction + audited store, public API only."""
    from agent_session_control import RunStore, build_create_plan

    project = str(tmp_path / "project")
    (tmp_path / "project").mkdir()
    with_mcp = build_create_plan(
        "claude",
        project,
        mcp_servers=[{"name": "docs", "command": "/usr/local/bin/docs-mcp", "env": {"API_KEY": "sekret-live"}}],
    )
    shown = with_mcp.to_dict()
    assert "sekret-live" not in json.dumps(shown), "display projection must redact live secret values"
    assert "docs" in json.dumps(shown), "server name stays visible for debugging"
    assert "sekret-live" in dict(with_mcp.env).values(), "live values travel only in plan.env for the child"
    store = RunStore(root=tmp_path / "control", namespace="probe")
    record, created = store.create("op", "k-probe", {"t": 1}, {"execution": "queued"})
    assert created is True and record["run_id"].startswith("run_")
    again, created2 = store.create("op", "k-probe", {"t": 1}, {"execution": "queued"})
    assert created2 is False and again["run_id"] == record["run_id"], "immutable request id converges, not forks"
    trail = store.audit_trail(target=record["run_id"])
    assert any(r.get("action") == "run.create" for r in trail), trail
