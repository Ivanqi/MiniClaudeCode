# MiniClaudeCode

DeepSeek 驱动的命令行编码智能体——一个 Claude Code 的实现。参考 Claude Code 的核心机制（工具调用循环、上下文压缩、多代理协作、权限钩子、定时任务、MCP），用 Python 从零实现，LLM 后端替换为 DeepSeek API。

## 功能特性

- **工具调用循环**：模型通过 function calling 驱动文件操作、bash、任务看板、worktree、cron、技能加载、多代理协作等 27 个内置工具 + 动态 MCP 工具
- **分层上下文压缩**：超大工具结果落盘 → 消息数裁剪 → 旧工具结果折叠 → LLM 摘要压缩 → 上下文超长应急压缩，每层保护 `tool_calls`/`tool` 配对完整性
- **多代理协作**：lead 可 spawn 自治队友线程，通过文件邮箱通信；队友空闲时自动认领无主任务；**计划审批闸门**——队友提交计划后暂停一切模型/工具步骤，直到 lead 审批
- **聚焦子代理**：`task` 工具生成最小工具集的子代理，循环执行后只回传文本摘要
- **MCP 晚绑定**：`connect_mcp` 连接服务器后，其工具以 `mcp__server__tool` 命名动态合并进统一工具池
- **权限钩子管道**：危险命令黑名单、破坏性命令人工确认、文件操作工作区沙箱，全部在工具分发之前拦截
- **定时任务（cron）**：自研 5 字段 cron 表达式解析/校验/匹配，任务可持久化，触发后把计划提示重新注入主循环
- **后台任务**：慢速命令（install/build/test 等）异步执行，完成结果以 `task_notification` 注入对话
- **错误恢复链**：429 限流退避重试 → 503/529 连续过载切换备用模型 → 输出截断升级 max_tokens → 注入续写提示 → 上下文超长应急压缩
- **状态全部落盘**：任务、cron、worktree 事件、transcript、记忆索引、邮箱均为磁盘文件，可审计可恢复

## 整体架构

```
                        ┌────────────────────────────────────────────┐
  用户 ──> main.py ──> │ AgentLoop（编排层）                          │
                        │                                            │
                        │  prepare_context  ──> ContextCompact       │
                        │  call_llm         ──> PromptBuilder        │
                        │                       └─> LlmClient ──> DeepSeek API
                        │                       └─> RecoverySystem（重试/降级）
                        │  工具分发         ──> HookSystem（权限管道）
                        │                       └─> ToolRegistry（工具池）
                        │                             ├─ 各领域系统工具
                        │                             └─ MCP 工具（晚绑定）
                        │  并行线程        ──> CronScheduler / BackgroundTaskSystem
                        │                       / TeammateSystem（队友线程）
                        └────────────────────────────────────────────┘
```

代码按职责分为四层、23 个模块（`src/`），层间单向依赖：

### 基础层

| 模块 | 职责 |
|---|---|
| `Configs` | 全局配置常量：工作目录、路径、token 预算、阈值、模型常量，魔法数字集中管理 |
| `Terminal` | 线程安全的彩色终端输出；后台线程打印时清行并重绘输入提示符 |
| `LlmClient` | DeepSeek API 适配：HTTP 调用、消息/工具格式转换、错误归一化（`DeepSeekApiError`） |
| `RecoverySystem` | LLM 错误恢复：指数退避重试、备用模型降级、上下文超长错误识别 |

### 领域层

| 模块 | 职责 |
|---|---|
| `BasicOper` | 基础工具：bash / 文件读写改 / glob / todo，文件操作限定在工作区内（`safe_path`） |
| `TaskSystem` | 任务看板：JSON 文件持久化的任务记录，状态流转（pending → in_progress → completed）与依赖（blockedBy） |
| `WorktreeSystem` | 隔离 git worktree：名称校验、创建/删除/保留，删除前统计未提交变更 |
| `SkillSystem` | 技能加载：扫描 `skills/` 目录，解析 `SKILL.md` 的 YAML frontmatter |
| `MessageBus` | 消息总线：每个代理一个追加式 JSONL 邮箱，读取即消费（删除） |
| `ProtocolSystem` | 协议状态机：以 `request_id` 匹配请求/响应，防止协议回复串扰 |
| `CronScheduler` | 定时任务：cron 表达式解析/校验/匹配、注册、触发队列、持久化（`.scheduled_tasks.json`） |
| `BackgroundTaskSystem` | 后台任务：慢速命令异步执行，完成结果以 `task_notification` 回注对话 |
| `McpClient` / `McpSystem` | MCP：单服务器客户端 + 多服务器连接管理、工具发现与合并 |

### 智能体层

| 模块 | 职责 |
|---|---|
| `TeammateSystem` | 队友系统：自治队友线程、空闲轮询、lead 侧团队管理与计划审批 |
| `SubagentSystem` | 子代理系统：`task` 工具背后的聚焦式子代理 |
| `HookSystem` | 权限管道：钩子注册/触发，黑名单与破坏性命令确认 |
| `ContextCompact` | 上下文压缩：预算控制、分层剪裁、摘要生成、转录落盘 |
| `PromptBuilder` | 每轮从活上下文重建 system prompt（身份、工具说明、技能目录、记忆、MCP 状态） |
| `Memories` | 轮次上下文组装：记忆索引、MCP 连接、活跃队友 |
| `ToolRegistry` | 工具注册表：聚合各系统工具与 MCP 工具 |

### 编排层

| 模块 | 职责 |
|---|---|
| `AgentLoop` | 主循环：编排 LLM 调用、工具分发、上下文压缩、后台与定时注入，提供 CLI 入口 |
| `main.py` | 程序入口：加载 `.env`、构建客户端、启动 CLI |

### 一轮循环

每个 LLM 轮次经过同一管道：

1. **注入**：cron 触发队列 → 后台任务完成通知 → todo 更新提醒（每 3 轮）
2. **压缩**：tool 结果字节预算（超大输出落盘）→ 消息数裁剪（保留头部 3 条 + 尾部 47 条，不拆散工具配对）→ 旧工具结果折叠（只保留最近 3 条全文）→ 超预算则 LLM 摘要压缩
3. **组装**：PromptBuilder 重建 system prompt，ToolRegistry 合并工具定义
4. **调用**：经 RecoverySystem 包装调用 DeepSeek；`finish_reason == "length"` 时升级 max_tokens 重试，再次截断则注入续写提示
5. **分发**：`compact` 特殊处理 → HookSystem 拦截 → 慢命令放后台 → 处理器执行 → 工具结果回传
6. **循环**：直到模型不再产生工具调用，触发 `Stop` 钩子

## 技术要点

### 1. API 适配隔离（`LlmClient`）

所有 DeepSeek/OpenAI 格式细节收拢在一个模块，上层只面向 `LlmResponse` / `ToolCall` 两个协议无关的数据结构编程：

- `assistant_message()` 把 tool_calls 序列化为 API 可回传的格式，`tool_message()` 与之按 `tool_call_id` 配对
- 工具定义的 `input_schema` → `function.parameters`；`stop_reason == "max_tokens"` → `finish_reason == "length"`
- 错误统一为 `DeepSeekApiError` 并分类（`classify_error`：ratelimit / overload / other）
- 零新增 SDK 依赖（仅 httpx），换服务商只改 base_url 与错误分类

### 2. 工具注册表即扩展点（`ToolRegistry`）

每个系统自持 `tool_defs()` / `handlers()` 两个接口，新增工具 = 在所属系统实现 + 注册该系统，调度代码零改动。MCP 工具晚绑定合并：连接后才发现工具，以 `mcp__server__tool` 命名并入工具池，服务器名/工具名做安全字符归一化。

### 3. 权限在分发之前（`HookSystem`）

钩子管道刻意放在工具处理器之外，循环不改动任何单个工具即可增加行为。内置：危险命令黑名单（`rm -rf /`、`sudo`、`dd if=` 等）、破坏性命令人工确认、`write_file`/`edit_file` 工作区路径沙箱（`resolve()` + `is_relative_to()`）、MCP 危险工具确认、大输出告警、日志与停止统计。

### 4. 分层压缩（`ContextCompact`）

压缩由便宜到昂贵逐层升级，每层都保护 `tool_calls` ↔ `tool` 配对：

| 层 | 触发条件 | 手段 |
|---|---|---|
| 结果预算 | 最近一轮 tool 结果超 200KB | 超大输出落盘 `.task_outputs/tool-results/`，消息中留路径 + 预览 |
| 消息裁剪 | 消息超 50 条 | 保留头 3 条 + 尾 47 条，边界处回扩以不拆散配对 |
| 微压缩 | tool 结果超 3 条 | 更早的结果替换为占位文本 |
| 摘要压缩 | 上下文估算超 50000 字符 | 先写 transcript（`.transcripts/*.jsonl`），再让 LLM 生成保留目标/发现/剩余工作的摘要 |
| 应急压缩 | 报上下文超长错误 | 转录 + 摘要旧历史，保留最近几条 |

### 5. 恢复链条（`RecoverySystem` + `AgentLoop`）

按失败模式逐级应对：429 限流 → 指数退避（500ms 起、25% 抖动、上限 32s、最多 3 次）；503/529 过载 → 连续 2 次切换 `FALLBACK_MODEL_ID`；`finish_reason == "length"` → max_tokens 4096 升级 8192，仍截断则注入续写提示（最多 2 次）；上下文超长 → 应急压缩。每类恢复只尝试一次，避免死循环。

### 6. 协议状态机与审批闸门（`ProtocolSystem` + `TeammateSystem`）

lead 与队友间的 shutdown / plan_approval 请求携带 `request_id`，响应按 id 匹配挂起请求，保证一个回复不会误审批另一个请求。队友 `submit_plan` 后进入等待态：**不再执行任何模型/工具步骤**，只轮询协议回复，直到收到 `plan_approval_response`——审批是真实闸门而非提示词约定。

### 7. 队友自治（`TeammateSystem`）

队友线程结构：内层循环最多 10 轮工具执行（消息窗口 20 条）→ 空闲轮询（每 5s 检查邮箱与任务看板，60s 超时退出）→ 收件箱消息优先于自动认领任务。认领带 worktree 的任务后，文件工具通过闭包 `cwd` 注入透明地改在该目录内执行。队友工具集不经过主循环权限钩子，保证自主性。

### 8. 可测试性

状态类（`TaskSystem` / `WorktreeSystem` / `ContextCompact` / `CronScheduler`）均支持构造函数注入目录，测试不碰真实环境。33 个用例全部离线运行：`test_core.py` 覆盖纯逻辑，`test_agent_loop.py` 用 `FakeClient` 按脚本回放，验证完整消息链路。

### 9. 线程模型

cron 调度线程、cron 自动执行线程、后台任务线程、队友线程并行运行；`ActiveTeammates`、`CronScheduler`、`BackgroundTaskSystem`、`AgentLoop` 均以锁保护共享状态；`Terminal` 保证后台打印不打断用户输入。

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env     # 填入 DEEPSEEK_API_KEY
python main.py
```

CLI 中直接输入问题回车发送，`q` / `exit` / 空行退出。

### 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `DEEPSEEK_API_KEY` | ✅ | — | DeepSeek API 密钥 |
| `DEEPSEEK_BASE_URL` | | `https://api.deepseek.com` | API 地址 |
| `MODEL_ID` | | `deepseek-chat` | 主模型（也可用 `deepseek-reasoner`） |
| `FALLBACK_MODEL_ID` | | 空 | 备用模型，连续 overload 错误后自动切换 |
| `LLM_TIMEOUT` | | `120` | 请求超时（秒） |

## 测试

```bash
python -m unittest discover -s tests -v
```

33 个用例全通过：

- **test_core.py（31 个）**：cron 表达式校验与匹配（`*/5`、范围、列表、dom/dow 或语义）、任务生命周期与依赖阻塞、DeepSeek 消息/工具格式构造与往返、错误分类、压缩预算与配对保护、工具注册表合并（内置 + MCP）、worktree 名称校验、文件工具与路径沙箱
- **test_agent_loop.py（2 个）**：用 `FakeClient` 离线跑通完整主循环——验证工具确实执行、tool 消息与 tool_calls 配对回传、system prompt 注入、`length` 截断升级 max_tokens 后成功收尾

## 项目结构

```
MiniClaudeCode/
├── main.py                    # 入口：加载 .env → 构建客户端 → 启动 CLI
├── src/                       # 23 个模块，四层架构
│   ├── Configs.py             #   全局配置
│   ├── Terminal.py            #   终端输出
│   ├── LlmClient.py           #   DeepSeek API 适配
│   ├── RecoverySystem.py      #   重试与降级
│   ├── BasicOper.py           #   文件/bash/todo 工具
│   ├── TaskSystem.py          #   任务看板
│   ├── WorktreeSystem.py      #   隔离 worktree
│   ├── SkillSystem.py         #   技能加载
│   ├── MessageBus.py          #   文件邮箱总线
│   ├── ProtocolSystem.py      #   协议状态机
│   ├── CronScheduler.py       #   定时任务
│   ├── BackgroundTaskSystem.py#   后台任务
│   ├── McpClient.py           #   MCP 单服务器客户端
│   ├── McpSystem.py           #   MCP 连接管理
│   ├── TeammateSystem.py      #   自治队友
│   ├── SubagentSystem.py      #   聚焦子代理
│   ├── HookSystem.py          #   权限钩子管道
│   ├── ContextCompact.py      #   上下文压缩
│   ├── PromptBuilder.py       #   system prompt 组装
│   ├── Memories.py            #   轮次上下文
│   ├── ActiveTeammates.py     #   活跃队友注册表
│   ├── ToolRegistry.py        #   工具注册表
│   └── AgentLoop.py           #   主循环与 CLI
├── tests/                     # 33 个离线测试用例
├── skills/                    # 技能目录（agent-builder / code-review / mcp-builder / pdf）
├── .env.example               # 环境变量模板
└── requirements.txt           # httpx / python-dotenv / pyyaml
```

## 内置工具清单

| 系统 | 工具 |
|---|---|
| BasicOper | `bash` `read_file` `write_file` `edit_file` `glob` `todo_write` |
| TaskSystem | `create_task` `list_tasks` `get_task` `claim_task` `complete_task` |
| WorktreeSystem | `create_worktree` `remove_worktree` `keep_worktree` |
| SkillSystem | `load_skill` |
| CronScheduler | `schedule_cron` `list_crons` `cancel_cron` |
| McpSystem | `connect_mcp` + 已连接服务器的 `mcp__server__tool`（动态） |
| ContextCompact | `compact`（由主循环特殊处理） |
| SubagentSystem | `task` |
| TeammateSystem | `spawn_teammate` `send_message` `check_inbox` `request_shutdown` `request_plan` `review_plan` |
