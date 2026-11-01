"""Pure native launch/resume plans for the five supported runtimes.

Adapted from Corral ``src/corral/runtime/{base,claude,codex,opencode,cursor,pi}.py``
(MIT, corral contributors). Only launch-argument semantics, composer/readiness
screen rules, interrupt keys and model-flag support are carried over; history
scanning, handoff formatting, titles, search and host session claiming stay
with the host and SessKit.

A plan is pure data: building one never launches a process, never mutates
storage or global configuration, and never sends a model request. Unsupported
MCP configuration and unsupported options fail here, before any launch.

Per-session stdio MCP servers travel launch-only: OpenCode through the
private process env ``OPENCODE_CONFIG_CONTENT`` with the launcher-owned
``--standalone`` private server (verified, opencode v2.2.5: ``--standalone
debug config`` shows the virtual document while the ambient service ignores
it; headless proof via ``--standalone run``), Claude through
``--mcp-config <json>`` argv plus ``--strict-mcp-config`` (verified, claude
2.1.314), Codex through repeated ``-c mcp_servers.<name>.*`` argv overrides
(verified, codex 0.166.0). Cursor and Pi only read MCP servers from
persistent config files, so they honestly refuse per-launch servers.

Secret safety: server ``env`` values NEVER travel on argv. Claude argv
carries ``${NAME}`` references (officially expanded from the process
environment at load; unset names warn and stay unexpanded) while the live
values travel in the private ``plan.env``. Codex argv carries
``env_vars=[...]`` forwarding names with values in ``plan.env``. Display
projections (``repr``/``to_dict``) redact every secret value; only the
executable plan seen by the launcher keeps live values.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from agent_session_control.errors import (
    CODE_MODEL_UNSUPPORTED,
    EXIT_USAGE,
    ControlError,
    unavailable,
    unsupported,
    usage_error,
)
from agent_session_control.models import RESOURCE_LINK_BLOCK, TEXT_BLOCK

BUILTIN_RUNTIME_IDS = ("claude", "codex", "opencode", "cursor", "pi")


@dataclass(frozen=True)
class LaunchPlan:
    """An executable argv array plus its working directory and private env.

    Never a shell command. ``env`` carries per-session/process configuration
    only; its values are redacted from ``repr`` and excluded from the default
    JSON projection so credentials never leak into logs or snapshots.
    """

    argv: tuple[str, ...]
    cwd: str | None
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "env", MappingProxyType(dict(self.env or {})))

    def __repr__(self) -> str:
        keys = sorted(self.env.keys())
        return f"LaunchPlan(argv={display_argv(self.argv)!r}, cwd={self.cwd!r}, env_keys={keys!r})"

    def to_dict(self, *, include_env: bool = False) -> dict:
        data: dict = {"argv": display_argv(self.argv), "cwd": self.cwd}
        if include_env:
            data["env"] = dict(self.env)
        else:
            data["env_keys"] = sorted(self.env.keys())
        return data


#: MCP channel kinds for :attr:`RuntimeProfile.mcp_channel`.
MCP_CHANNEL_UNSUPPORTED = "unsupported"
#: Private per-process environment variable carrying the encoded MCP config.
MCP_CHANNEL_ENV = "env"
#: Argv flag taking one encoded MCP config document (e.g. ``--mcp-config``).
MCP_CHANNEL_FLAG = "flag"
#: Repeated ``-c <dotted-prefix>.<name>.<field>=<toml>`` argv overrides.
MCP_CHANNEL_OVERRIDE = "override"

_MCP_NAME_RE = "^[A-Za-z0-9][A-Za-z0-9_-]*$"
#: Environment keys forwarded to MCP servers must be plain process-env
#: identifiers so ``${NAME}`` references (Claude) and ``env_vars``
#: forwarding (Codex) resolve unambiguously.
_MCP_ENV_KEY_RE = "^[A-Za-z_][A-Za-z0-9_]*$"


@dataclass(frozen=True)
class RuntimeProfile:
    """Launch-relevant capabilities of one runtime.

    Third-party hosts register their own profiles; behavior of unknown
    runtimes is never guessed from the built-ins.
    """

    id: str
    display_name: str
    executable: str
    executable_aliases: tuple[str, ...] = ()
    interrupt_keys: tuple[str, ...] = ("Escape",)
    model_flag: str | None = None
    session_id_flag: str | None = None
    supports_mcp: bool = False
    mcp_channel: str = MCP_CHANNEL_UNSUPPORTED
    mcp_target: str = ""
    mcp_encode: Callable[[tuple[dict, ...]], str] | None = None
    #: Opt-in for custom flag/override-channel profiles whose encoder output
    #: may carry server env values on argv. Built-ins never need this: their
    #: channels reference secrets instead of inlining them. Defaults to False
    #: so a secret-bearing config is refused rather than exposed.
    mcp_env_on_argv: bool = False
    auto_approve_args: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    composer_parser: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "executable_aliases", tuple(self.executable_aliases))
        object.__setattr__(self, "interrupt_keys", tuple(self.interrupt_keys))
        object.__setattr__(self, "auto_approve_args", tuple(self.auto_approve_args))
        object.__setattr__(self, "limitations", tuple(self.limitations))


def _claude_auto_approve_args() -> tuple[str, ...]:
    """Claude refuses ``--dangerously-skip-permissions`` as uid 0 outside a sandbox.

    Adapted from ``ClaudeRuntime.auto_approve_args``: adding the flag there
    produces a plan that exits 1 on launch, which is worse than no relaxation.
    """
    base = ("--dangerously-skip-permissions",)
    if os.name == "nt":
        return base
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0:
        return base
    if os.environ.get("IS_SANDBOX") or os.environ.get("CLAUDE_CODE_BUBBLEWRAP"):
        return base
    return ()


def _claude_mcp_parts(servers: tuple[dict, ...]) -> tuple[list[str], dict[str, str]]:
    """Claude ``.mcp.json`` argv document with secrets referenced, not inlined.

    ``env`` values become ``${LOOKUP}`` references expanded from the child
    process environment at load (official Claude Code MCP behavior; an unset
    name warns and stays unexpanded rather than leaking). Live values travel
    in the returned private env mapping, keyed to avoid collisions with the
    launcher's own environment.
    """
    import json as _json

    document: dict = {"mcpServers": {}}
    private_env: dict[str, str] = {}
    for server in servers:
        entry: dict = {"command": server["command"], "args": list(server["args"])}
        if server["env"]:
            entry["env"] = {}
            for key, value in sorted(server["env"].items()):
                lookup = _claude_env_lookup(server["name"], key)
                entry["env"][key] = "${" + lookup + "}"
                private_env[lookup] = value
        document["mcpServers"][server["name"]] = entry
    return ["--mcp-config", _json.dumps(document, ensure_ascii=False)], private_env


def _claude_env_lookup(server_name: str, key: str) -> str:
    return "ASC_MCP_" + server_name.upper().replace("-", "_") + "_" + key


def _codex_mcp_parts(servers: tuple[dict, ...]) -> tuple[list[str], dict[str, str]]:
    """Codex ``-c mcp_servers.*`` overrides with secrets forwarded, not inlined.

    ``env`` values travel in the private process env and are forwarded by
    name via ``env_vars`` (verified: ``codex mcp get`` resolves and itself
    redacts the values). No ``env=`` inline table ever reaches argv, so
    process listing cannot observe secrets.
    """
    argv: list[str] = []
    private_env: dict[str, str] = {}
    forwarded: dict[str, str] = {}
    for server in servers:
        base = f"mcp_servers.{server['name']}"
        argv.extend(["-c", f"{base}.command={_toml_string(server['command'])}"])
        argv.extend(["-c", f"{base}.args=[{', '.join(_toml_string(a) for a in server['args'])}]"])
        if server["env"]:
            names = sorted(server["env"])
            argv.extend(["-c", f"{base}.env_vars=[{', '.join(_toml_string(n) for n in names)}]"])
            for key in names:
                if key in forwarded and forwarded[key] != server["env"][key]:
                    raise usage_error(
                        f"MCP env key {key!r} carries different values in two servers; "
                        "process-env forwarding cannot deliver both under one name."
                    )
                forwarded[key] = server["env"][key]
                private_env[key] = server["env"][key]
    return argv, private_env


def _opencode_mcp_document(servers: tuple[dict, ...]) -> str:
    """opencode.json shape (verified against ``mcp add`` on v2.2.2)."""
    import json as _json

    document: dict = {"mcp": {"servers": {}}}
    for server in servers:
        entry = {
            "type": "local",
            "command": [server["command"], *server["args"]],
            "enabled": True,
        }
        if server["env"]:
            entry["environment"] = dict(server["env"])
        document["mcp"]["servers"][server["name"]] = entry
    return _json.dumps(document, ensure_ascii=False)


def _toml_string(value: str) -> str:
    out = ['"']
    for char in value:
        code = ord(char)
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04X}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def validate_mcp_servers(servers: Iterable[Any]) -> tuple[dict, ...]:
    """Validate the neutral stdio MCP server interchange shape.

    Each entry is a mapping with ``name``, ``command`` (absolute path),
    optional ``args`` (list of strings) and optional ``env`` (string map).
    An optional ``type`` must be ``"stdio"``; remote servers (``url``) and any
    other key are rejected loudly instead of being silently dropped. Command
    existence is NOT probed here (plans perform no I/O); launchers verify
    executability before spawning.
    """
    import re as _re

    name_pattern = _re.compile(_MCP_NAME_RE)
    env_key_pattern = _re.compile(_MCP_ENV_KEY_RE)
    normalized: list[dict] = []
    seen: set[str] = set()
    for entry in servers or ():
        if not isinstance(entry, Mapping):
            raise usage_error("Each MCP server must be a mapping with name/command fields.")
        unknown = set(entry) - {"name", "command", "args", "env", "type"}
        if unknown:
            raise usage_error(
                f"Unsupported MCP server fields: {sorted(unknown)}. "
                "Only name/command/args/env plus type 'stdio' are accepted; "
                "ACP mcpServers entries are a different schema and are not auto-converted."
            )
        if entry.get("type", "stdio") != "stdio":
            raise unsupported(
                "Only stdio MCP servers are supported per-session; "
                f"server {entry.get('name')!r} needs a remote transport.",
                hint="Omit remote servers or serve them through a local stdio bridge.",
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name_pattern.match(name):
            raise usage_error("Each MCP server needs a name of [A-Za-z0-9_-] characters.")
        if name in seen:
            raise usage_error(f"Duplicate MCP server name: {name!r}.")
        seen.add(name)
        command = entry.get("command")
        if not isinstance(command, str) or not command or "\x00" in command:
            raise usage_error(f"MCP server {name!r} needs a nonempty command string.")
        if any(ord(c) < 32 for c in command):
            raise usage_error(f"MCP server {name!r} command contains control characters.")
        if not os.path.isabs(command):
            raise usage_error(
                f"MCP server {name!r} command must be an absolute path: {command!r}.",
                hint="Resolve the executable before submitting; PATH lookup is not per-session.",
            )
        args = entry.get("args", [])
        if not isinstance(args, list) or any(not isinstance(a, str) or "\x00" in a for a in args):
            raise usage_error(f"MCP server {name!r} args must be a list of strings.")
        env = entry.get("env", {})
        if (
            not isinstance(env, Mapping)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items())
            or any("\x00" in k or "\x00" in v for k, v in env.items())
        ):
            raise usage_error(f"MCP server {name!r} env must be a string-to-string mapping.")
        for key in env:
            if not env_key_pattern.match(key):
                raise usage_error(
                    f"MCP server {name!r} env key {key!r} is not a plain process-env identifier "
                    "[A-Za-z_][A-Za-z0-9_]*; launch-only forwarding cannot represent it."
                )
        normalized.append({"name": name, "command": command, "args": list(args), "env": dict(env)})
    return tuple(normalized)


def display_argv(argv: Iterable[str]) -> list[str]:
    """Display projection of a plan argv with MCP secret values redacted.

    Server names, commands and structure stay visible; environment secret
    values carried by ``--mcp-config`` JSON or ``-c ...env=`` TOML overrides
    are replaced. The executable plan (``LaunchPlan.argv``) still carries the
    live values the child process needs.
    """
    import json as _json

    shown: list[str] = []
    items = list(argv)
    index = 0
    while index < len(items):
        token = items[index]
        shown.append(token)
        if token == "--mcp-config" and index + 1 < len(items):
            try:
                document = _json.loads(items[index + 1])
                for entry in document.get("mcpServers", {}).values():
                    if isinstance(entry, dict) and entry.get("env"):
                        entry["env"] = {k: "***" for k in entry["env"]}
                shown.append(_json.dumps(document, ensure_ascii=False))
            except ValueError:
                shown.append("<unparseable-mcp-document>")
            index += 2
            continue
        if token == "-c" and index + 1 < len(items) and ".env=" in items[index + 1]:
            shown.append("<redacted-mcp-env>")
            index += 2
            continue
        index += 1
    return shown


def _builtin_profiles() -> list[RuntimeProfile]:
    return [
        RuntimeProfile(
            id="claude",
            display_name="Claude",
            executable="claude",
            interrupt_keys=("Escape",),
            model_flag="--model",
            session_id_flag="--session-id",
            supports_mcp=True,
            mcp_channel=MCP_CHANNEL_FLAG,
            mcp_target="--mcp-config",
            auto_approve_args=("--dangerously-skip-permissions",),
            composer_parser="between_rules",
            limitations=(
                "Auto-approval is omitted when running as uid 0 outside a sandbox; "
                "the runtime itself rejects the flag there.",
                "MCP travels on argv as a --mcp-config document plus --strict-mcp-config; "
                "server env values travel only as ${NAME} references resolved from the "
                "private process env, never as inline secrets. Flags are process-level "
                "(shared with hosted argv); live delivery proven headless via -p, "
                "interactive TUI delivery pending host run.",
            ),
        ),
        RuntimeProfile(
            id="codex",
            display_name="Codex",
            executable="codex",
            interrupt_keys=("Escape",),
            model_flag="--model",
            supports_mcp=True,
            mcp_channel=MCP_CHANNEL_OVERRIDE,
            mcp_target="mcp_servers",
            auto_approve_args=("--dangerously-bypass-approvals-and-sandbox",),
            composer_parser="codex_chevron",
            limitations=(
                "MCP travels on argv (-c mcp_servers.<name>.* overrides); "
                "server env values travel only in the private process env and are "
                "forwarded by name via env_vars, never as inline secrets. Overrides "
                "are per-invocation (shared with hosted argv); live delivery proven "
                "headless via exec, interactive TUI delivery pending host run.",
            ),
        ),
        RuntimeProfile(
            id="opencode",
            display_name="OpenCode",
            executable="opencode",
            interrupt_keys=("Escape", "Escape"),
            model_flag=None,
            supports_mcp=True,
            mcp_channel=MCP_CHANNEL_ENV,
            mcp_target="OPENCODE_CONFIG_CONTENT",
            mcp_encode=_opencode_mcp_document,
            auto_approve_args=("--auto",),
            composer_parser="opencode_bars",
            limitations=(
                "No verified per-launch model flag on the hosted root command; "
                "--model requests fail instead of falling back to another model.",
                "MCP travels in the private process env (OPENCODE_CONFIG_CONTENT) and "
                "takes effect only with the launcher-owned --standalone private server "
                "(verified via --standalone debug config and --standalone run on v2.2.5); "
                "an ambient background service ignores it, so hosted launches with MCP "
                "include --standalone and never write persistent config.",
            ),
        ),
        RuntimeProfile(
            id="cursor",
            display_name="Cursor",
            executable="agent",
            executable_aliases=("cursor-agent",),
            interrupt_keys=("C-c",),
            model_flag="--model",
            auto_approve_args=("--force", "--trust"),
            composer_parser="cursor_arrow",
            limitations=(
                "MCP servers come only from persistent .cursor/mcp.json files; "
                "per-launch MCP configuration is unsupported and refused.",
            ),
        ),
        RuntimeProfile(
            id="pi",
            display_name="Pi",
            executable="pi",
            interrupt_keys=("Escape",),
            model_flag="--model",
            auto_approve_args=("--approve",),
            composer_parser="between_rules",
            limitations=(
                "Resume addresses the native history path, not the short session id.",
                "MCP servers come only from persistent mcp.json files; "
                "per-launch MCP configuration is unsupported and refused.",
            ),
        ),
    ]


class RuntimeRegistry:
    """Explicit registry of runtime profiles; support is never hard-coded."""

    def __init__(self, profiles: Iterable[RuntimeProfile] = ()):
        self._profiles: dict[str, RuntimeProfile] = {}
        for profile in profiles:
            self.register(profile)

    def register(self, profile: RuntimeProfile) -> RuntimeProfile:
        if profile.id in self._profiles:
            raise ValueError(f"Runtime already registered: {profile.id}")
        self._profiles[profile.id] = profile
        return profile

    def get(self, runtime_id: str) -> RuntimeProfile:
        try:
            return self._profiles[runtime_id]
        except KeyError:
            raise unsupported(
                f"Unsupported runtime: {runtime_id}",
                hint="Supported runtimes: " + ", ".join(sorted(self._profiles)),
            ) from None

    def resolve(self, token: str) -> str | None:
        """Resolve a runtime id, executable name or alias to a runtime id."""
        if token in self._profiles:
            return token
        for profile in self._profiles.values():
            if token == profile.executable or token in profile.executable_aliases:
                return profile.id
        return None

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self._profiles)

    def __iter__(self) -> Iterator[RuntimeProfile]:
        return iter(self._profiles.values())


def default_registry() -> RuntimeRegistry:
    """Registry with the initial five built-ins. No dormant runtime is included."""
    return RuntimeRegistry(_builtin_profiles())


def _registry_or_default(registry: RuntimeRegistry | None) -> RuntimeRegistry:
    return registry if registry is not None else default_registry()


def _mcp_parts(profile: RuntimeProfile, mcp_servers: Iterable[Any]) -> tuple[list[str], dict[str, str]]:
    """Validate servers and render the profile's verified launch-only channel.

    Returns ``(argv_additions, env_additions)``. Empty input renders nothing;
    the plan is then identical to a no-MCP plan.
    """
    servers = validate_mcp_servers(mcp_servers)
    if not servers:
        return [], {}
    if not profile.supports_mcp or profile.mcp_channel == MCP_CHANNEL_UNSUPPORTED:
        raise unsupported(
            f"Runtime {profile.id!r} has no verified per-session MCP configuration path; "
            "refusing to launch rather than dropping the servers silently.",
            hint="Omit mcp_servers to inherit the runtime's own configuration. "
            "Cursor and Pi only read persistent config files, which this library never writes.",
        )
    if profile.mcp_channel == MCP_CHANNEL_ENV:
        if not profile.mcp_target or profile.mcp_encode is None:
            raise usage_error(
                f"Runtime profile {profile.id!r} declares env-channel MCP support without "
                "mcp_target/mcp_encode; there is no private channel for the configuration."
            )
        return [], {profile.mcp_target: profile.mcp_encode(servers)}
    if profile.mcp_channel == MCP_CHANNEL_FLAG:
        if profile.mcp_target == "--mcp-config":
            # Verified Claude shape: secrets referenced, never inlined.
            return _claude_mcp_parts(servers)
        if not profile.mcp_encode:
            raise usage_error(f"Runtime profile {profile.id!r} declares flag-channel MCP support without mcp_encode.")
        if any(server["env"] for server in servers) and not profile.mcp_env_on_argv:
            raise unsupported(
                f"Runtime profile {profile.id!r} has no verified secret-safe argv channel; "
                "refusing this secret-bearing MCP config rather than exposing it on argv.",
                hint="Use an env-channel profile, drop server env values, or explicitly opt the "
                "profile in with mcp_env_on_argv=True after verifying its channel.",
            )
        return [profile.mcp_target, profile.mcp_encode(servers)], {}
    if profile.mcp_channel == MCP_CHANNEL_OVERRIDE:
        if profile.mcp_target == "mcp_servers":
            # Verified Codex shape: secrets forwarded by name, never inlined.
            return _codex_mcp_parts(servers)
        raise usage_error(
            f"Runtime profile {profile.id!r} uses an unverified config-override prefix "
            f"{profile.mcp_target!r}; only the verified 'mcp_servers' shape is supported."
        )
    raise usage_error(f"Runtime profile {profile.id!r} has an unknown MCP channel {profile.mcp_channel!r}.")


def _validate_model(profile: RuntimeProfile, model: str | None) -> str | None:
    if model is None:
        return None
    if not isinstance(model, str) or not model or model.startswith("-") or any(ord(c) < 32 for c in model):
        raise usage_error("Model must be a nonempty identifier without control characters or a leading dash.")
    if profile.model_flag is None:
        raise ControlError(
            CODE_MODEL_UNSUPPORTED,
            f"{profile.id} does not support an explicit launch model",
            EXIT_USAGE,
            hint="Omit model to inherit the runtime's own default, or pick another runtime.",
        )
    return model


def _approve_args(profile: RuntimeProfile, auto_approve: bool) -> tuple[str, ...]:
    if not auto_approve:
        return ()
    if profile.id == "claude":
        return _claude_auto_approve_args()
    return profile.auto_approve_args


def _require_absolute_cwd(cwd: str | None, *, what: str = "--cwd") -> str | None:
    if cwd is None:
        return None
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise usage_error(f"{what} must be an absolute project path.")
    return cwd


def _fresh_argv(profile: RuntimeProfile, *, auto_approve: bool, extra_args: tuple[str, ...]) -> list[str]:
    approve = _approve_args(profile, auto_approve)
    if profile.id == "codex":
        return [profile.executable, "--no-daemon", *approve, *extra_args]
    return [profile.executable, *approve, *extra_args]


def _ensure_opencode_standalone(argv: list[str], *, has_mcp: bool) -> list[str]:
    """Insert the launcher-owned ``--standalone`` flag for requested MCP.

    Verified against installed opencode v2.2.5: ``OPENCODE_CONFIG_CONTENT``
    is honored only by a private server (``--standalone debug config`` shows
    the virtual document; ambient ``debug config`` omits it and ambient
    ``mcp list`` reports only persistent servers). Hosted launches with MCP
    therefore use the proven ``--standalone`` path; without MCP the argv is
    unchanged to preserve exact resume identity and no-MCP behavior. No
    persistent config is written and ambient inheritance is never relied on.
    """
    if has_mcp and "--standalone" not in argv:
        argv.insert(1, "--standalone")
    return argv


def _exact_id(session: Mapping[str, Any], field: str, *, what: str, hint: str | None = None) -> str:
    value = session.get(field)
    if not isinstance(value, str) or not value:
        raise usage_error(what, hint=hint)
    return value


def _resume_argv(
    profile: RuntimeProfile, session: Mapping[str, Any], *, auto_approve: bool, extra_args: tuple[str, ...]
) -> list[str]:
    approve = _approve_args(profile, auto_approve)
    if profile.id == "claude":
        native_id = _exact_id(
            session,
            "id",
            what="Resume needs the exact native session id; refusing to start a blank replacement.",
            hint="Copy the full id from search or show output; prefixes are not accepted.",
        )
        return [profile.executable, *approve, *extra_args, "--resume", native_id]
    if profile.id == "codex":
        native_id = _exact_id(
            session, "id", what="Resume needs the exact native session id; refusing to start a blank replacement."
        )
        return [profile.executable, "resume", "--no-daemon", *approve, *extra_args, native_id]
    if profile.id == "opencode":
        native_id = _exact_id(
            session, "id", what="Resume needs the exact native session id; refusing to start a blank replacement."
        )
        return [profile.executable, *approve, *extra_args, "-s", native_id]
    if profile.id == "cursor":
        native_id = _exact_id(
            session, "id", what="Resume needs the exact native session id; refusing to start a blank replacement."
        )
        return [profile.executable, *approve, *extra_args, "--resume", native_id]
    if profile.id == "pi":
        # Pi resume addresses the native history path (``--session <path>``);
        # the short id alone cannot locate the conversation.
        path = _exact_id(
            session,
            "path",
            what="Resume needs the exact native history path for pi; refusing to start a blank replacement.",
            hint="Copy the full path from search or show output.",
        )
        return [profile.executable, *approve, *extra_args, "--session", path]
    # Third-party profiles: exact id resume is the only generic semantic.
    native_id = _exact_id(
        session, "id", what="Resume needs the exact native session id; refusing to start a blank replacement."
    )
    return [profile.executable, *approve, *extra_args, "--resume", native_id]


def build_create_plan(
    runtime: str | RuntimeProfile,
    cwd: str | None,
    *,
    model: str | None = None,
    native_id: str | None = None,
    auto_approve: bool = False,
    mcp_servers: Iterable[Any] = (),
    registry: RuntimeRegistry | None = None,
    extra_args: Iterable[str] = (),
) -> LaunchPlan:
    """Pure fresh-launch plan.

    Caller defaults are preserved: no model flag unless ``model`` is given,
    no approval relaxation unless ``auto_approve`` is true. ``native_id``
    preassigns the native session id only where the runtime natively accepts
    one. ``extra_args`` carries host-specific preparation (hooks, identity
    binding); the generic plan never invents it. ``mcp_servers`` renders the
    profile's verified launch-only channel (argv additions appended last, env
    additions private); empty input renders nothing.
    """
    reg = _registry_or_default(registry)
    profile = runtime if isinstance(runtime, RuntimeProfile) else reg.get(runtime)
    mcp_argv, mcp_env = _mcp_parts(profile, mcp_servers)
    wanted_model = _validate_model(profile, model)
    if native_id is not None:
        if profile.session_id_flag is None:
            raise unsupported(
                f"Runtime {profile.id!r} has no verified flag to preassign a native session id.",
                hint="Omit native_id, or pass host identity preparation through extra_args.",
            )
        if not isinstance(native_id, str) or not native_id:
            raise usage_error("native_id must be a nonempty string.")
    argv = _fresh_argv(profile, auto_approve=auto_approve, extra_args=tuple(extra_args))
    if native_id is not None and profile.session_id_flag is not None:
        argv.extend([profile.session_id_flag, native_id])
    if wanted_model is not None and profile.model_flag is not None:
        argv.extend([profile.model_flag, wanted_model])
    if profile.id == "claude" and mcp_argv:
        # --mcp-config is variadic (<configs...>) and would swallow following
        # positionals; it stays last, and --strict-mcp-config scopes the
        # session to exactly the requested servers.
        argv.extend([*mcp_argv, "--strict-mcp-config"])
    else:
        argv.extend(mcp_argv)
    if profile.id == "opencode":
        argv = _ensure_opencode_standalone(argv, has_mcp=bool(mcp_env))
    return LaunchPlan(argv=tuple(argv), cwd=_require_absolute_cwd(cwd), env=mcp_env)


def build_resume_plan(
    runtime: str | RuntimeProfile,
    session: Mapping[str, Any],
    *,
    auto_approve: bool = False,
    mcp_servers: Iterable[Any] = (),
    registry: RuntimeRegistry | None = None,
    extra_args: Iterable[str] = (),
) -> LaunchPlan:
    """Pure native-resume plan for an exact session mapping.

    ``session`` is used as given (``id``/``path``/``cwd``); the library never
    rescans to guess the session. Missing identity fails instead of creating
    a blank replacement conversation. ``mcp_servers`` renders as in
    :func:`build_create_plan`.
    """
    reg = _registry_or_default(registry)
    profile = runtime if isinstance(runtime, RuntimeProfile) else reg.get(runtime)
    mcp_argv, mcp_env = _mcp_parts(profile, mcp_servers)
    if not isinstance(session, Mapping):
        raise usage_error("Resume needs a session mapping with exact id/path/cwd fields.")
    argv = _resume_argv(profile, session, auto_approve=auto_approve, extra_args=tuple(extra_args))
    if profile.id == "claude" and mcp_argv:
        argv.extend([*mcp_argv, "--strict-mcp-config"])
    else:
        argv.extend(mcp_argv)
    if profile.id == "opencode":
        argv = _ensure_opencode_standalone(argv, has_mcp=bool(mcp_env))
    return LaunchPlan(argv=tuple(argv), cwd=_require_absolute_cwd(session.get("cwd")), env=mcp_env)


def is_available(runtime: str | RuntimeProfile, *, registry: RuntimeRegistry | None = None) -> bool:
    """Whether the runtime executable resolves on PATH. No process is launched."""
    reg = _registry_or_default(registry)
    profile = runtime if isinstance(runtime, RuntimeProfile) else reg.get(runtime)
    names = (profile.executable, *profile.executable_aliases)
    return any(shutil.which(name) is not None for name in names)


def runtime_profile(runtime: str | RuntimeProfile, *, registry: RuntimeRegistry | None = None) -> dict:
    """JSON-serializable capability description for one runtime.

    Backend-specific behavior does not leak into host callers beyond this map.
    ``available`` reflects only executable presence, never login or quota.
    """
    reg = _registry_or_default(registry)
    profile = runtime if isinstance(runtime, RuntimeProfile) else reg.get(runtime)
    mcp_descriptor = None
    if profile.supports_mcp and profile.mcp_channel != MCP_CHANNEL_UNSUPPORTED:
        mcp_descriptor = {"channel": profile.mcp_channel, "target": profile.mcp_target}
    return {
        "id": profile.id,
        "display_name": profile.display_name,
        "executable": profile.executable,
        "executable_aliases": list(profile.executable_aliases),
        "available": is_available(profile),
        "interrupt_keys": list(profile.interrupt_keys),
        "model_selection": profile.model_flag is not None,
        "model_flag": profile.model_flag,
        "session_id_assignment": profile.session_id_flag is not None,
        "mcp_configuration": profile.supports_mcp,
        "mcp": mcp_descriptor,
        "prompt_blocks": [TEXT_BLOCK, RESOURCE_LINK_BLOCK],
        "auto_approve_args": list(profile.auto_approve_args),
        "composer_parser": profile.composer_parser,
        "limitations": list(profile.limitations),
    }


def describe_runtimes(*, registry: RuntimeRegistry | None = None) -> list[dict]:
    reg = _registry_or_default(registry)
    return [runtime_profile(profile, registry=reg) for profile in reg]


# -- composer / readiness screen rules (pure string parsing) -------------------
# Adapted from the runtimes' ``composer_region`` implementations. Completion
# popups and transcript output sit outside these regions; an unreadable
# composer must be recorded as unknown, never as empty.


def _is_full_width_rule(line: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= 20 and stripped.count("─") >= 0.9 * len(stripped)


def _composer_between_rules(screen: str) -> str | None:
    lines = screen.splitlines()
    rules = [i for i, line in enumerate(lines) if _is_full_width_rule(line)]
    if len(rules) < 2:
        return None
    return "\n".join(lines[rules[-2] + 1 : rules[-1]])


def _composer_codex(screen: str) -> str | None:
    lines = screen.splitlines()
    starts = [i for i, line in enumerate(lines) if line.lstrip().startswith("›")]
    if not starts:
        return None
    body = lines[starts[-1] :]
    while body and not body[-1].strip():
        body.pop()
    blanks = [i for i, line in enumerate(body) if not line.strip()]
    return "\n".join(body[: blanks[-1]] if blanks else body)


def _composer_opencode(screen: str) -> str | None:
    lines = screen.splitlines()
    marked = [i for i, line in enumerate(lines) if line.lstrip().startswith("┃")]
    if not marked:
        return None
    start = end = marked[-1]
    while start > 0 and lines[start - 1].lstrip().startswith("┃"):
        start -= 1
    return "\n".join(lines[start : end + 1])


def _composer_cursor(screen: str) -> str | None:
    idx = screen.rfind("→")
    if idx < 0:
        idx = screen.rfind("->")
    if idx < 0:
        return None
    window = screen[idx : idx + 1600]
    return "" if "Add a follow-up" in window else window


_COMPOSER_PARSERS: dict[str, Callable[[str], str | None]] = {
    "between_rules": _composer_between_rules,
    "codex_chevron": _composer_codex,
    "opencode_bars": _composer_opencode,
    "cursor_arrow": _composer_cursor,
}


def composer_region(
    runtime: str | RuntimeProfile, screen: str, *, registry: RuntimeRegistry | None = None
) -> str | None:
    """Extract the runtime's input box from a plain-text pane capture.

    Returns ``None`` when the runtime has no rule or the box is not on
    screen; the send is then unverified. Never invents emptiness.
    """
    reg = _registry_or_default(registry)
    profile = runtime if isinstance(runtime, RuntimeProfile) else reg.get(runtime)
    parser = _COMPOSER_PARSERS.get(profile.composer_parser)
    if parser is None:
        return None
    return parser(screen)


def check_executable(plan: LaunchPlan) -> str:
    """Resolve the plan's executable without launching anything.

    Raises ``unavailable`` when it does not resolve on PATH.
    """
    if not plan.argv:
        raise usage_error("A launch plan needs a nonempty argv.")
    executable = plan.argv[0]
    if shutil.which(executable) is None:
        raise unavailable(f"Runtime executable is not installed: {executable}")
    return executable
