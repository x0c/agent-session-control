"""Reusable coding-agent session control: foundation layer.

Core imports are lightweight and side-effect free: importing this package
never touches ACP, SessKit, native histories, storage, terminals or the
network. Generating a launch plan, reading capability maps or building an
unopened :class:`RunStore` performs no I/O beyond PATH lookup for
availability probes.
"""

from __future__ import annotations

from agent_session_control import correlation, errors, models, runtime, store
from agent_session_control.correlation import (
    build_result,
    compact_event,
    correlate,
    marker,
    own_message,
    project_event,
    validate_prompt,
    wire_prompt,
)
from agent_session_control.errors import ControlError
from agent_session_control.models import (
    RESOURCE_LINK_BLOCK,
    ControlEvent,
    RunResult,
    SessionController,
    SessionHandle,
    normalize_prompt,
    prompt_text,
    resource_link,
    text_block,
)
from agent_session_control.runtime import (
    MCP_CHANNEL_ENV,
    MCP_CHANNEL_FLAG,
    MCP_CHANNEL_OVERRIDE,
    MCP_CHANNEL_UNSUPPORTED,
    LaunchPlan,
    RuntimeProfile,
    RuntimeRegistry,
    build_create_plan,
    build_resume_plan,
    composer_region,
    default_registry,
    describe_runtimes,
    display_argv,
    is_available,
    runtime_profile,
    validate_mcp_servers,
)
from agent_session_control.store import RunStore, default_home, digest, run_version

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "ControlError",
    "ControlEvent",
    "LaunchPlan",
    "MCP_CHANNEL_ENV",
    "MCP_CHANNEL_FLAG",
    "MCP_CHANNEL_OVERRIDE",
    "MCP_CHANNEL_UNSUPPORTED",
    "RESOURCE_LINK_BLOCK",
    "RunResult",
    "RunStore",
    "RuntimeProfile",
    "RuntimeRegistry",
    "SessionController",
    "SessionHandle",
    "build_create_plan",
    "build_resume_plan",
    "build_result",
    "compact_event",
    "composer_region",
    "correlate",
    "correlation",
    "default_home",
    "default_registry",
    "describe_runtimes",
    "digest",
    "display_argv",
    "errors",
    "is_available",
    "marker",
    "models",
    "normalize_prompt",
    "own_message",
    "project_event",
    "prompt_text",
    "resource_link",
    "run_version",
    "runtime",
    "runtime_profile",
    "store",
    "text_block",
    "validate_mcp_servers",
    "validate_prompt",
    "wire_prompt",
]
