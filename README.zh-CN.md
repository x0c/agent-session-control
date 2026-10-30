# agent-session-control

可复用的编码 Agent 会话控制：纯启动计划、持久化审计运行、原生证据关联。
MIT 许可证（见 [LICENSE](LICENSE)）。

状态：0.1.0 已发布并通过测试（见 [docs/DESIGN.md](docs/DESIGN.md) 与
[docs/CONTRACT.md](docs/CONTRACT.md)）。本 README 只描述本版本已交付
并经测试证明的能力；各运行时限制见下文。

## 今日可用（已测试）

- **纯启动计划**（`agent_session_control.runtime`）：五个内置运行时
  （`claude`、`codex`、`opencode`、`cursor`、`pi`）的 `build_create_plan` /
  `build_resume_plan`，以及第三方 `RuntimeProfile` 显式注册。计划是不可变
  argv 数组——构造计划永不启动进程、修改存储、改全局配置或发起模型请求。
  调用方默认值被保留（不传 model 就没有 `--model`，不传 `auto_approve`
  就不放行审批）。不支持的选项（未知运行时、OpenCode 的 `--model`、无对应
  flag 的预置原生 id）在启动前直接失败，永不静默降级。
- **按会话 stdio MCP**（`mcp_servers=`）：已验证的仅启动期通道，每条都用
  一次性 stdio fixture 对真实已安装 CLI 做过端到端证明（未改全局配置）。
  密钥永不上 argv——Claude argv 只带 `${NAME}` 引用，Codex argv 只带
  `env_vars` 转发名，OpenCode 把文档放在私有进程 env；真实值只走
  `plan.env`，且不在任何展示投影中出现：
  - Claude 2.1.314：argv `--mcp-config '<.mcp.json 文档>'` 加
    `--strict-mcp-config`（headless `claude -p` 已证明；交互式 TUI 投递待
    宿主侧运行）。
  - Codex 0.166.0：argv 重复 `-c mcp_servers.<name>.command/args/env_vars`
    覆盖（headless `codex exec` 已证明密钥经转发到达；
    `~/.codex/config.toml` 无残留）。
  - OpenCode v2.2.2：私有进程 env `OPENCODE_CONFIG_CONTENT` 携带
    `mcp.servers.<name>` 文档（headless `--standalone run` 已证明；全局
    `opencode.json`/`config.json` 校验和不变）。仅当启动方拥有 server 时
    生效，常驻后台服务会忽略它。
  - Cursor 与 Pi 只从持久配置文件读 MCP 服务，因此诚实拒绝按启动配置
    （`unsupported`）。
  服务规格经过校验（绝对路径命令、字符串 args/env、标识符 env 键、
  `[A-Za-z0-9_-]` 唯一名、仅 stdio、无远程传输、无未知键）；没有已验证
  私有通道的密钥配置在启动前直接拒绝。
- **持久化审计运行**（`agent_session_control.store`）：`RunStore`，同一事务
  actor 审计、不可变幂等键（相同重试收敛、载荷变化冲突）、`UNIQUE` 约束的
  跨进程准入、按命名空间分库、可配置 root 与 supervisor 身份。只读访问永不
  创建存储。改编自 Corral `src/corral/control_store.py`。
- **证据关联**（`agent_session_control.correlation`）：`marker` /
  `wire_prompt` / `own_message` / `correlate` / `build_result`，标记命名空间
  显式可配（中性默认 `session-control`；兼容宿主可用旧 `Corral` 命名空间）。
  只有原生用户消息证据能证明输入已记录；引用在助手/工具输出中的标记永不算数。
  缺失用量保持 `null`；重复的用量观测去重，永不加总成虚假账单。
  `completed` 只表示关联回合结束，不表示任务通过验收。
- **共享模型与端口**（`agent_session_control.models`）：`SessionHandle` /
  `ControlEvent` / `RunResult`、提示块辅助函数（标准 `text` + `resource_link`；
  旧中性 `resource` 显式作为 URI 链接兼容输入接受；富内容与内嵌资源数据显式
  拒绝）与引擎 `LocalController`、JSON CLI、ACP 适配器共用的异步
  `SessionController` 端口。
- **稳定错误**（`agent_session_control.errors`）：`ControlError`，构造器、
  可观察字段与退出码与抽取来源一致，另有 `{ok, data, error, meta}` 信封辅助函数。

核心导入仅需标准库。可选 extra：

- `history`：原生历史观测用 [SessKit](https://github.com/x0c/sesskit)
  `>=0.2.21`（由引擎层消费，本层不解析）。
- `acp`：ACP 适配器用官方 `agent-client-protocol >=0.12.1,<0.13`。

## 安装

需要 Python `>=3.10,<3.15`（请用 3.12；macOS 默认 `python3` 可能是 ACP SDK
不支持的 3.15）。

```sh
WHEEL=https://github.com/x0c/agent-session-control/releases/download/v0.1.0/agent_session_control-0.1.0-py3-none-any.whl
pip install "agent-session-control @ $WHEEL"              # 核心，仅标准库
pip install "agent-session-control[history] @ $WHEEL"    # + SessKit 原生历史观测
pip install "agent-session-control[acp] @ $WHEEL"         # + 官方 agent-client-protocol SDK
```

`uv` 下 SessKit 从其不可变发布 wheel 解析：

```toml
[tool.uv.sources]
sesskit = { url = "https://github.com/x0c/sesskit/releases/download/v0.2.21/sesskit-0.2.21-py3-none-any.whl" }
```

## 快速使用

```python
from agent_session_control import build_create_plan, build_resume_plan, runtime_profile

plan = build_create_plan("codex", "/path/to/project", auto_approve=True)
print(plan.argv)  # ('codex', '--no-daemon', '--dangerously-bypass-approvals-and-sandbox')

resume = build_resume_plan("claude", {"id": "<exact-native-id>", "cwd": "/path/to/project"},
                           auto_approve=True)
print(resume.argv)  # ('claude', '--dangerously-skip-permissions', '--resume', '<exact-native-id>')

print(runtime_profile("cursor"))  # 能力、中断键、局限
```

```python
from agent_session_control import RunStore

store = RunStore()  # ~/.config/agent-session-control/control/runs.sqlite3
record, created = store.create("local-user", "request-key-1",
                               {"target": "claude:<id>", "text": "..."},
                               {"execution": "queued", "delivery": "accepted"})
```

接入指南、隔离 root/命名空间与平台说明见
[docs/INTEGRATION.md](docs/INTEGRATION.md)。完整契约见
[docs/CONTRACT.md](docs/CONTRACT.md)。

## 检查

```sh
uv run --python 3.12 --group dev scripts/check.py
```

依次运行 Ruff、Pyrefly、完整 pytest（警告即失败）、wheel/sdist 打包，
以及仅装核心的干净离线安装验证。
