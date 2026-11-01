# Agent Session Control

**语言：** [English](README.md) | 简体中文

从 Python 启动并跟踪本机 **Claude Code**、**Codex CLI**、**OpenCode**、**Cursor**、**Pi** 编码助手会话：纯启动计划、可审计运行、按会话 stdio MCP 接线，并自带 [agent-client-protocol](https://github.com/agentclientprotocol/agent-client-protocol)（ACP）适配器。

这是 **Python 库 + JSON 命令行 + ACP 适配器**，不是终端界面、不是会话管理器、也不是历史解析器（历史观测委托给 [SessKit](https://github.com/x0c/sesskit)）。

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/x0c/agent-session-control/actions/workflows/ci.yml/badge.svg)](https://github.com/x0c/agent-session-control/actions/workflows/ci.yml)

状态：0.1.1（见 [docs/INTEGRATION.md](docs/INTEGRATION.md) 与 [docs/CONTRACT.md](docs/CONTRACT.md)）。各运行时限制见下文。

## 安装

请在项目的虚拟环境中安装固定发布版本的 wheel。

需要 **Python 3.10–3.14（推荐 3.12）**。默认 tmux 后端运行于 **macOS 与 Linux**；Windows/WSL 不受支持。Homebrew 的 `python3` 可能是官方 `agent-client-protocol` SDK 不支持的 3.15——安装 `acp` extra 时请使用 3.10–3.14 解释器。

```bash
VERSION=0.1.1
BASE=https://github.com/x0c/agent-session-control/releases/download/v${VERSION}
WHEEL=${BASE}/agent_session_control-${VERSION}-py3-none-any.whl
SESSKIT=https://github.com/x0c/sesskit/releases/download/v0.2.21/sesskit-0.2.21-py3-none-any.whl

# 仅核心：启动计划、存储、证据关联、JSON 命令行（仅标准库）。
python -m pip install "agent-session-control @ ${WHEEL}"

# 可观测控制器（+ SessKit 原生历史观测）。
# SessKit 尚未上架 PyPI：请把其固定 wheel 与 extra 写在同一条安装命令里。
#（tool.uv.sources 只是 uv 设置，不是 wheel 元数据，pip 需要命令行上的 URL。）
python -m pip install "sesskit @ ${SESSKIT}" "agent-session-control[history] @ ${WHEEL}"

# ACP 适配器（基于可观测控制器，默认完整安装）。
python -m pip install "sesskit @ ${SESSKIT}" "agent-session-control[history,acp] @ ${WHEEL}"

python -c "import agent_session_control; print(agent_session_control.__version__)"
```

尚无虚拟环境时，先运行 `python3 -m venv .venv && . .venv/bin/activate`。

## 今日可用（已测试）

- **纯启动计划**（`agent_session_control.runtime`）：五个内置运行时
  （`claude`、`codex`、`opencode`、`cursor`、`pi`）的 `build_create_plan` /
  `build_resume_plan`，以及第三方 `RuntimeProfile` 显式注册。计划是不可变
  argv 数组——构造计划永不启动进程、修改存储、改全局配置或发起模型请求。
  调用方默认值被保留（不传 model 就没有 `--model`，不传 `auto_approve`
  就不放行审批）。不支持的选项在启动前直接失败，永不静默降级。
- **按会话 stdio MCP**（`mcp_servers=`）：`claude` / `codex` / `opencode`
  的已验证仅启动期通道，每条都用一次性 stdio fixture 对真实已安装 CLI
  做过端到端证明（未改全局配置）。密钥永不上 argv；真实值只走
  `plan.env`，且不在任何展示投影中出现。`cursor` 与 `pi` 只从持久
  配置文件读 MCP 服务，因此按启动配置会被诚实拒绝（`unsupported`）。
- **持久化审计运行**（`agent_session_control.store`）：`RunStore`，同一事务
  actor 审计、不可变幂等键、跨进程 `UNIQUE` 准入、按命名空间分库、
  可配置 root 与 supervisor 身份。只读访问永不创建存储。
- **证据关联**（`agent_session_control.correlation`）：`marker` /
  `wire_prompt` / `own_message` / `correlate` / `build_result`。只有原生
  用户消息证据能证明输入已记录。缺失用量保持 `null`；重复的用量观测去重，
  永不加总成虚假账单。`completed` 只表示关联回合结束，不表示任务通过验收。
- **共享模型与端口**（`agent_session_control.models`）：`SessionHandle` /
  `ControlEvent` / `RunResult`、提示块辅助函数（标准 `text` +
  `resource_link`）与引擎 `LocalController`、JSON CLI、ACP 适配器共用的异步
  `SessionController` 端口。
- **稳定错误**（`agent_session_control.errors`）：`ControlError`，构造器、
  可观察字段与退出码稳定，另有 `{ok, data, error, meta}` 信封辅助函数。

核心导入仅需标准库。可选 extra：

- `history`：原生历史观测用 SessKit `>=0.2.21`。
- `acp`：ACP 适配器用官方 `agent-client-protocol >=0.12.1,<0.13`。

## 快速使用

```python
from agent_session_control import build_create_plan, build_resume_plan, runtime_profile

# 审批默认永不放行：auto_approve=False。仅当宿主明确策略允许时才传 True。
plan = build_create_plan("codex", "/path/to/project")
print(plan.argv)

resume = build_resume_plan("claude", {"id": "<exact-native-id>", "cwd": "/path/to/project"})
print(resume.argv)

with_mcp = build_create_plan(
    "claude", "/path/to/project",
    mcp_servers=[{"name": "docs", "command": "/usr/local/bin/docs-mcp", "env": {"API_KEY": "..."}}],
)
print(with_mcp.to_dict())  # 脱敏投影：无密钥值、无密钥 argv
# 真实值只走 plan.env（子进程用）；argv 只带 ${NAME} 引用（claude）或 env_vars 转发名（codex）。

print(runtime_profile("cursor"))  # 能力、中断键、局限
```

```python
from agent_session_control import RunStore

store = RunStore()  # ~/.config/agent-session-control/control/runs.sqlite3
record, created = store.create("local-user", "request-key-1",
                               {"target": "claude:<id>", "text": "..."},
                               {"execution": "queued", "delivery": "accepted"})
```

SDK 生命周期（live，会消耗模型配额，需要运行时登录）：

```python
import asyncio
import tempfile
from pathlib import Path
from agent_session_control.controller import LocalController

async def main() -> None:
    # 选用临时项目 + 隔离控制根：永不使用固定 /tmp 路径。
    with tempfile.TemporaryDirectory(prefix="asc-sdk-") as scratch:
        project = str(Path(scratch, "project"))
        Path(project).mkdir(parents=True, exist_ok=True)
        controller = LocalController(root=str(Path(scratch, "control")), namespace="demo")
        handle = None
        try:
            handle = await controller.create("claude", project)
            run_id = await controller.start_turn(
                handle.id, [{"type": "text", "text": "Reply with ACK."}], request_id="demo-1")
            assert run_id.startswith("run_"), run_id  # 精确 run 身份
            result = await controller.wait(run_id, timeout=120.0)
            print(result.state, result.evidence)
            assert result.run_id == run_id
            if result.final_response:
                print(result.final_response[:200])  # 返回的最终文本
        finally:
            # 仅公开 API 清理：释放 pane；保留历史。
            if handle is not None:
                await controller.close_session(handle.id)

asyncio.run(main())
```

JSON 命令行（stdout 为机器信封；ACP 拥有自己独立的 stdout）：

```sh
python -m agent_session_control version --json
# {"ok": true, "data": {"package": "agent-session-control", "version": "0.1.1", ...}, "error": null, "meta": {...}}

python -m agent_session_control plan create --runtime codex --cwd /tmp/demo-proj --json
# {"ok": true, "data": {"dry_run": true, "status": "planned",
#   "launch": {"argv": ["codex", "--no-daemon"], "cwd": "/tmp/demo-proj", "env_keys": []}}, ...}

python -m agent_session_control session create --runtime codex --cwd /tmp/demo-proj \
  --dry-run --json --root /tmp/demo-control
# {"ok": true, "data": {"dry_run": true, "status": "planned", "session": {...}}, ...}
```

ACP 适配器（每次调用指定一个运行时；stdout 只有协议消息）：

```sh
python -m agent_session_control acp --runtime codex --root /tmp/demo-control --namespace demo
```

接入指南、隔离 root/命名空间与平台说明见
[docs/INTEGRATION.md](docs/INTEGRATION.md)。协议细节见
[docs/ACP.md](docs/ACP.md)，完整契约见
[docs/CONTRACT.md](docs/CONTRACT.md)。
`examples/basic_control.py`（计划 + CLI 信封；`--live` 跑真实回合）与
`examples/native_smoke.py`（有界原生 + stdio 握手）覆盖了上述端到端路径。

## 能力矩阵

| 能力 | claude | codex | opencode | cursor | pi |
|---|---|---|---|---|---|
| 纯启动计划 | 有 | 有 | 有 | 有 | 有 |
| 自有托管 + 存活 + 关闭（不发提示） | 有 | 有 | 有 | 有 | 有 |
| 原生回合（需登录/配额，消耗配额） | 受限 | 受限 | 受限 | 受限 | 受限 |
| 按会话 MCP 启动通道 | headless 已验证 | headless 已验证 | headless 已验证 | 拒绝 | 拒绝 |
| 托管 TUI MCP 投递 | 待宿主运行 | 待宿主运行 | 仅启动方自有 | 拒绝（`unsupported`） | 拒绝（`unsupported`） |
| 基于该运行时的 ACP 服务 | 有 | 有 | 有 | 仅 SDK/CLI | 仅 SDK/CLI |

Headless 证明：对真实已安装 CLI 的一次性 stdio fixture
（`claude -p`、`codex exec`、`opencode --standalone run`），未改全局配置。
托管 TUI 投递共享同一 argv/env 通道，但待宿主运行（最终托管 MCP 结论以
适配器/后端负责人文档为准，见 `docs/ACP.md`、`docs/BACKENDS.md`）。
可用性探测只反映可执行文件是否存在，不代表登录、配额或模型权益。
缺失的登录/配额会作为明确限制返回，不算通过。

配套 Skill：使用本库的 agent 应安装
[`agent-session-control`](https://github.com/x0c/agent-session-control/blob/v0.1.1/skills/agent-session-control/SKILL.md)
（[`references/mcp.md`](https://github.com/x0c/agent-session-control/blob/v0.1.1/skills/agent-session-control/references/mcp.md)），
随 wheel 发布于 `agent_session_control/skills/agent-session-control`：

```sh
npx skills add x0c/agent-session-control@v0.1.1 --skill agent-session-control
```

该 Skill 只教授既有 JSON CLI 生命周期（plan/host/observe/release），不新增生命周期。

## 相关工具

| 工具 | 角色 | 关系 |
|---|---|---|
| [SessKit](https://github.com/x0c/sesskit) | 原生历史观测（`history` extra，固定 wheel `v0.2.21`） | 以显式固定安装消费；永不 vendored |
| [agent-client-protocol](https://github.com/agentclientprotocol/agent-client-protocol) | ACP 线协议 + Python SDK（`acp` extra，`>=0.12.1,<0.13`） | 从 PyPI 消费；适配器每次调用服务一个运行时 |
| `tmux` | 默认终端后端（macOS/Linux） | 外部依赖；CI 按 OS 安装 |

## 局限

- Cursor 与 Pi 只从持久配置文件读 MCP 服务，按会话 `mcp_servers=` 会以 `unsupported` 在启动前直接拒绝，不静默丢弃。
- OpenCode 按会话 MCP 仅当启动方拥有 server 时生效，常驻后台服务会忽略它。
- `completed` 只表示关联回合结束，不表示任务通过验收；缺失用量保持 `null`。
- 真实控制会消耗模型配额，需要可执行文件与登录。一次性项目/状态目录只隔离控制状态：托管仍可能在运行时标准位置留下原生历史，`close_session` 永不删除原生历史。

## 开发检查

仅贡献者需要，与安装无关：

```sh
uv sync --group dev --all-extras
uv run --python 3.12 --group dev scripts/check.py       # FULL：含 live 标记测试
uv run --python 3.12 --group dev scripts/check.py --ci  # CI：-m "not live"，不耗配额/免登录
```

依次运行 Ruff、Pyrefly、pytest（警告即失败）、wheel/sdist 打包、
元数据检查，以及干净安装（核心、`history`、`acp`、组合；uv 与纯 pip 双路验证
file:// URI）与唯一临时目录的外部消费者验证。默认 CI 永不消耗配额；
`live` 测试仅经手动 canonical-repo 任务运行。

## 许可证

MIT，见 [LICENSE](LICENSE)。
