"""Core runtime plan tests: exact argv, defaults, failures, purity."""

import dataclasses
import json
import os
from typing import Any

import pytest

from agent_session_control.errors import ControlError
from agent_session_control.runtime import (
    LaunchPlan,
    RuntimeProfile,
    RuntimeRegistry,
    build_create_plan,
    build_resume_plan,
    check_executable,
    composer_region,
    default_registry,
    describe_runtimes,
    is_available,
    runtime_profile,
    validate_mcp_servers,
)


def test_launch_plan_immutable_and_redacted():
    plan = LaunchPlan(argv=("claude", "--resume", "x"), cwd="/repo", env={"TOKEN": "secret"})
    assert plan.argv == ("claude", "--resume", "x")
    assert isinstance(plan.argv, tuple)
    frozen: Any = plan
    with pytest.raises(dataclasses.FrozenInstanceError):
        frozen.cwd = "/other"
    mutable_env: Any = plan.env
    with pytest.raises(TypeError):
        mutable_env["TOKEN"] = "other"
    assert "secret" not in repr(plan)
    assert "TOKEN" in repr(plan)
    shown = plan.to_dict()
    assert shown["env_keys"] == ["TOKEN"]
    assert "secret" not in json.dumps(shown)
    assert plan.to_dict(include_env=True)["env"] == {"TOKEN": "secret"}


def test_default_registry_has_five_builtins_no_kimi():
    registry = default_registry()
    assert registry.ids == ("claude", "codex", "opencode", "cursor", "pi")
    assert registry.resolve("cursor-agent") == "cursor"
    assert registry.resolve("agent") == "cursor"
    assert registry.resolve("kimi") is None


def test_create_plans_exact_argv_auto_approve(tmp_path):
    cwd = str(tmp_path)
    assert build_create_plan("claude", cwd, auto_approve=True).argv[0] == "claude"
    claude = build_create_plan("claude", cwd, auto_approve=True).argv
    assert "--dangerously-skip-permissions" in claude

    codex = build_create_plan("codex", cwd, auto_approve=True).argv
    assert codex[:3] == ("codex", "--no-daemon", "--dangerously-bypass-approvals-and-sandbox")

    opencode = build_create_plan("opencode", cwd, auto_approve=True).argv
    assert opencode == ("opencode", "--auto")

    cursor = build_create_plan("cursor", cwd, auto_approve=True).argv
    assert cursor == ("agent", "--force", "--trust")

    pi = build_create_plan("pi", cwd, auto_approve=True).argv
    assert pi == ("pi", "--approve")


def test_create_plans_preserve_defaults(tmp_path):
    cwd = str(tmp_path)
    for runtime in ("claude", "codex", "opencode", "cursor", "pi"):
        argv = build_create_plan(runtime, cwd).argv
        assert "--model" not in argv
        assert not any(a.startswith("--dangerously") or a in ("--auto", "--approve", "--force") for a in argv)


def test_resume_plans_exact_positions(tmp_path):
    assert build_resume_plan("claude", {"id": "abc", "cwd": str(tmp_path)}, auto_approve=True).argv == (
        "claude",
        "--dangerously-skip-permissions",
        "--resume",
        "abc",
    )
    assert build_resume_plan("codex", {"id": "abc", "cwd": str(tmp_path)}, auto_approve=True).argv == (
        "codex",
        "resume",
        "--no-daemon",
        "--dangerously-bypass-approvals-and-sandbox",
        "abc",
    )
    assert build_resume_plan("opencode", {"id": "abc", "cwd": str(tmp_path)}, auto_approve=True).argv == (
        "opencode",
        "--auto",
        "-s",
        "abc",
    )
    assert build_resume_plan("cursor", {"id": "abc", "cwd": str(tmp_path)}, auto_approve=True).argv == (
        "agent",
        "--force",
        "--trust",
        "--resume",
        "abc",
    )
    # Pi resume addresses the native history path, never the short id.
    pi_plan = build_resume_plan(
        "pi", {"id": "short", "path": "/hist/pi.jsonl", "cwd": str(tmp_path)}, auto_approve=True
    )
    assert pi_plan.argv == ("pi", "--approve", "--session", "/hist/pi.jsonl")
    assert "short" not in pi_plan.argv


def test_resume_fails_rather_than_blank_replacement(tmp_path):
    with pytest.raises(ControlError):
        build_resume_plan("claude", {"cwd": str(tmp_path)})
    with pytest.raises(ControlError):
        build_resume_plan("pi", {"id": "short", "cwd": str(tmp_path)})
    not_a_mapping: Any = "not-a-mapping"
    with pytest.raises(ControlError):
        build_resume_plan("codex", not_a_mapping)


def test_model_selection_behavior(tmp_path):
    cwd = str(tmp_path)
    argv = build_create_plan("claude", cwd, model="opus").argv
    assert argv[-2:] == ("--model", "opus")
    argv = build_create_plan("codex", cwd, model="gpt-5").argv
    assert argv[-2:] == ("--model", "gpt-5")
    argv = build_create_plan("cursor", cwd, model="sonnet").argv
    assert argv[-2:] == ("--model", "sonnet")
    argv = build_create_plan("pi", cwd, model="provider/model").argv
    assert argv[-2:] == ("--model", "provider/model")
    # OpenCode has no verified per-launch model flag: fail, never fall back.
    with pytest.raises(ControlError) as exc:
        build_create_plan("opencode", cwd, model="anything")
    assert exc.value.code == "model_selection_unsupported"
    # Omitted model inherits runtime defaults: no flag anywhere.
    for runtime in ("claude", "codex", "cursor", "pi"):
        assert "--model" not in build_create_plan(runtime, cwd).argv


def test_model_validation(tmp_path):
    cwd = str(tmp_path)
    for bad in ("", "-x", "a\x01b"):
        with pytest.raises(ControlError) as exc:
            build_create_plan("claude", cwd, model=bad)
        assert exc.value.code == "usage_error"


def test_unsupported_runtime_and_options(tmp_path):
    cwd = str(tmp_path)
    with pytest.raises(ControlError) as exc:
        build_create_plan("kimi", cwd)
    assert exc.value.code == "unsupported"
    # Cursor/Pi only read persistent config files: per-launch servers refused.
    for refused in ("cursor", "pi"):
        with pytest.raises(ControlError) as exc:
            build_create_plan(refused, cwd, mcp_servers=[{"name": "s", "command": "/bin/echo"}])
        assert exc.value.code == "unsupported"
    with pytest.raises(ControlError) as exc:
        build_create_plan("codex", cwd, native_id="abc")
    assert exc.value.code == "unsupported"
    # Claude natively accepts a preassigned session id.
    plan = build_create_plan("claude", cwd, native_id="native-1")
    assert plan.argv[-2:] == ("--session-id", "native-1")


def test_native_id_and_session_id_flag_matrix(tmp_path):
    cwd = str(tmp_path)
    profiles = {p.id: p for p in default_registry()}
    assert profiles["claude"].session_id_flag == "--session-id"
    for runtime in ("opencode", "cursor", "pi", "codex"):
        assert profiles[runtime].session_id_flag is None
        with pytest.raises(ControlError):
            build_create_plan(runtime, cwd, native_id="x")


def test_claude_root_omits_rejected_flag(tmp_path, monkeypatch):
    cwd = str(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_BUBBLEWRAP", raising=False)
    argv = build_create_plan("claude", cwd, auto_approve=True).argv
    assert "--dangerously-skip-permissions" not in argv


def test_cwd_rules(tmp_path):
    with pytest.raises(ControlError):
        build_create_plan("claude", "relative/path")
    plan = build_create_plan("claude", None)
    assert plan.cwd is None
    # Pure plans never touch the filesystem: a missing absolute dir passes through.
    plan = build_create_plan("claude", "/definitely/not/here-xyz")
    assert plan.cwd == "/definitely/not/here-xyz"


def test_third_party_registry(tmp_path):
    registry = default_registry()
    custom = RuntimeProfile(
        id="acme",
        display_name="Acme",
        executable="acme-agent",
        model_flag="--model",
        supports_mcp=True,
        mcp_channel="env",
        mcp_target="ACME_MCP_SERVERS",
        mcp_encode=lambda servers: json.dumps([s["name"] for s in servers]),
    )
    registry.register(custom)
    plan = build_create_plan(
        "acme", str(tmp_path), mcp_servers=[{"name": "fs", "command": "/bin/echo"}], registry=registry
    )
    assert plan.argv[0] == "acme-agent"
    assert json.loads(plan.to_dict(include_env=True)["env"]["ACME_MCP_SERVERS"]) == ["fs"]
    assert "fs" not in repr(plan)
    with pytest.raises(ValueError):
        registry.register(custom)
    with pytest.raises(ControlError):
        build_resume_plan("ghost", {"id": "x"}, registry=registry)


def test_mcp_channel_rendering(tmp_path):
    cwd = str(tmp_path)
    server = {"name": "db", "command": "/bin/echo", "args": ["--x"], "env": {"TOKEN": "SECRET-1"}}
    claude = build_create_plan("claude", cwd, mcp_servers=[server])
    assert "--mcp-config" in claude.argv
    assert "--strict-mcp-config" in claude.argv
    assert claude.argv.index("--mcp-config") < claude.argv.index("--strict-mcp-config")
    document = json.loads(claude.argv[claude.argv.index("--mcp-config") + 1])
    # Secrets travel as process-env references, never inline on argv.
    assert document == {
        "mcpServers": {"db": {"command": "/bin/echo", "args": ["--x"], "env": {"TOKEN": "${ASC_MCP_DB_TOKEN}"}}}
    }
    live_env = claude.to_dict(include_env=True)["env"]
    assert live_env == {"ASC_MCP_DB_TOKEN": "SECRET-1"}
    assert "SECRET-1" not in repr(claude)
    assert "SECRET-1" not in json.dumps(claude.to_dict())
    assert "SECRET-1" not in json.dumps(claude.to_dict(include_env=True)["argv"])
    assert "db" in json.dumps(claude.to_dict())

    codex = build_create_plan("codex", cwd, mcp_servers=[server])
    assert "-c" in codex.argv
    joined = " ".join(codex.argv)
    assert "mcp_servers.db.command=" in joined
    assert "mcp_servers.db.args=" in joined
    assert "mcp_servers.db.env_vars=" in joined
    assert ".env=" not in joined.replace(".env_vars=", "")
    assert codex.to_dict(include_env=True)["env"] == {"TOKEN": "SECRET-1"}
    assert "SECRET-1" not in json.dumps(codex.to_dict())
    assert "SECRET-1" not in repr(codex)

    oc = build_create_plan("opencode", cwd, auto_approve=True, mcp_servers=[server])
    assert oc.argv == ("opencode", "--standalone", "--auto")
    content = json.loads(oc.to_dict(include_env=True)["env"]["OPENCODE_CONFIG_CONTENT"])
    assert content["mcp"]["servers"]["db"]["command"] == ["/bin/echo", "--x"]
    assert content["mcp"]["servers"]["db"]["environment"] == {"TOKEN": "SECRET-1"}
    assert "SECRET-1" not in json.dumps(oc.to_dict())
    assert oc.to_dict()["env_keys"] == ["OPENCODE_CONFIG_CONTENT"]
    # No-MCP preserves exact legacy argv (no standalone); resume matches.
    assert build_create_plan("opencode", cwd, auto_approve=True).argv == ("opencode", "--auto")
    assert build_resume_plan("opencode", {"id": "abc", "cwd": cwd}, auto_approve=True).argv == (
        "opencode",
        "--auto",
        "-s",
        "abc",
    )
    assert build_resume_plan("opencode", {"id": "abc", "cwd": cwd}, auto_approve=True, mcp_servers=[server]).argv == (
        "opencode",
        "--standalone",
        "--auto",
        "-s",
        "abc",
    )

    resumed = build_resume_plan("claude", {"id": "abc", "cwd": cwd}, mcp_servers=[server])
    assert "--mcp-config" in resumed.argv and "--strict-mcp-config" in resumed.argv

    empty = build_create_plan("claude", cwd, mcp_servers=[])
    assert "--mcp-config" not in empty.argv


def test_mcp_envless_servers_carry_no_secrets(tmp_path):
    cwd = str(tmp_path)
    server = {"name": "plain", "command": "/bin/echo"}
    assert build_create_plan("claude", cwd, mcp_servers=[server]).to_dict(include_env=True)["env"] == {}
    assert build_create_plan("codex", cwd, mcp_servers=[server]).to_dict(include_env=True)["env"] == {}


def test_mcp_refuses_unrepresentable_secrets(tmp_path):
    cwd = str(tmp_path)
    secret = {"name": "s", "command": "/bin/echo", "env": {"TOKEN": "SECRET-9"}}
    custom_flag = RuntimeProfile(
        id="flaggy",
        display_name="Flaggy",
        executable="flaggy",
        supports_mcp=True,
        mcp_channel="flag",
        mcp_target="--mcp",
        mcp_encode=lambda servers: "{}",
    )
    registry = RuntimeRegistry([custom_flag])
    with pytest.raises(ControlError) as exc:
        build_create_plan("flaggy", cwd, mcp_servers=[secret], registry=registry)
    assert exc.value.code == "unsupported"
    # Explicit opt-in restores the host-owned encoder path.
    opted = RuntimeProfile(
        id="flaggy2",
        display_name="Flaggy2",
        executable="flaggy",
        supports_mcp=True,
        mcp_channel="flag",
        mcp_target="--mcp",
        mcp_encode=lambda servers: "{}",
        mcp_env_on_argv=True,
    )
    registry.register(opted)
    plan = build_create_plan("flaggy2", cwd, mcp_servers=[secret], registry=registry)
    assert "--mcp" in plan.argv
    # Non-identifier env keys cannot be forwarded by any channel.
    with pytest.raises(ControlError) as exc:
        build_create_plan("claude", cwd, mcp_servers=[{"name": "s", "command": "/bin/echo", "env": {"bad-key": "v"}}])
    assert exc.value.code == "usage_error"
    # Conflicting values for one forwarded key cannot both be delivered.
    with pytest.raises(ControlError) as exc:
        build_create_plan(
            "codex",
            cwd,
            mcp_servers=[
                {"name": "a", "command": "/bin/echo", "env": {"TOKEN": "one"}},
                {"name": "b", "command": "/bin/echo", "env": {"TOKEN": "two"}},
            ],
        )
    assert exc.value.code == "usage_error"


def test_mcp_validation_matrix(tmp_path):
    cwd = str(tmp_path)
    good = {"name": "ok-1", "command": "/bin/echo"}
    assert validate_mcp_servers([good])[0]["args"] == []
    assert validate_mcp_servers([good])[0]["env"] == {}
    assert validate_mcp_servers([]) == ()
    bad_inputs = [
        "not-a-mapping",
        {"command": "/bin/echo"},  # missing name
        {"name": "x", "command": "relative/echo"},  # must be absolute
        {"name": "x", "command": "/bin/echo", "args": "nope"},
        {"name": "x", "command": "/bin/echo", "env": [("K", "V")]},
        {"name": "bad name!", "command": "/bin/echo"},
        {"name": "x", "command": "/bin/echo", "url": "https://x/mcp"},  # unknown key
        {"name": "x", "command": "/bin/echo", "type": "http"},  # remote transport
        {"name": "x", "command": "/bin/echo\x00hidden"},
    ]
    for bad in bad_inputs:
        with pytest.raises(ControlError):
            build_create_plan("claude", cwd, mcp_servers=[bad])
    with pytest.raises(ControlError) as exc:
        build_create_plan(
            "claude",
            cwd,
            mcp_servers=[{"name": "dup", "command": "/bin/echo"}, {"name": "dup", "command": "/bin/echo"}],
        )
    assert exc.value.code == "usage_error"


def test_mcp_profile_descriptor():
    assert runtime_profile("claude")["mcp"] == {"channel": "flag", "target": "--mcp-config"}
    assert runtime_profile("codex")["mcp"] == {"channel": "override", "target": "mcp_servers"}
    assert runtime_profile("opencode")["mcp"] == {"channel": "env", "target": "OPENCODE_CONFIG_CONTENT"}
    assert runtime_profile("cursor")["mcp"] is None
    assert runtime_profile("pi")["mcp"] is None
    assert runtime_profile("cursor")["mcp_configuration"] is False


def test_runtime_profile_json_and_availability():
    profile = runtime_profile("cursor")
    assert profile["executable"] == "agent"
    assert profile["executable_aliases"] == ["cursor-agent"]
    assert profile["interrupt_keys"] == ["C-c"]
    assert profile["model_selection"] is True
    assert isinstance(profile["available"], bool)
    assert profile["available"] == is_available("cursor")
    assert json.dumps(profile)
    assert json.dumps(describe_runtimes())
    assert {p["id"] for p in describe_runtimes()} == {"claude", "codex", "opencode", "cursor", "pi"}


def test_interrupt_keys_per_runtime():
    assert runtime_profile("claude")["interrupt_keys"] == ["Escape"]
    assert runtime_profile("opencode")["interrupt_keys"] == ["Escape", "Escape"]
    assert runtime_profile("pi")["interrupt_keys"] == ["Escape"]


def test_composer_regions():
    rule = "─" * 40
    screen = f"transcript\n{rule}\n  draft here\n{rule}\nfooter"
    assert composer_region("claude", screen) == "  draft here"
    assert composer_region("pi", screen) == "  draft here"
    assert composer_region("claude", "no rules here") is None
    assert composer_region("opencode", "┃ prompt") == "┃ prompt"
    assert composer_region("opencode", "plain") is None
    assert composer_region("codex", "out\n› hello") == "› hello"
    assert composer_region("codex", "plain") is None
    followup = "work\n→ Add a follow-up\nmore"
    assert composer_region("cursor", followup) == ""
    assert composer_region("cursor", "plain") is None
    custom_registry = RuntimeRegistry([RuntimeProfile(id="x", display_name="X", executable="x")])
    assert composer_region("x", screen, registry=custom_registry) is None


def test_extra_args_host_hook(tmp_path):
    plan = build_create_plan("claude", str(tmp_path), extra_args=("--settings", "{}"))
    assert plan.argv[-2:] == ("--settings", "{}")
    plan = build_resume_plan("codex", {"id": "a", "cwd": str(tmp_path)}, extra_args=("--tag", "t"))
    assert "--tag" in plan.argv


def test_check_executable_missing():
    with pytest.raises(ControlError) as exc:
        check_executable(LaunchPlan(argv=("definitely-not-a-real-exe-xyz",), cwd=None))
    assert exc.value.code == "unavailable"
