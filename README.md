# MiniClaudeCode

DeepSeek 驱动的命令行编码智能体——一个 Claude Code 的实现。参考 Claude Code 的核心机制（工具调用循环、上下文压缩、多代理协作、权限钩子、定时任务、MCP），用 Python 从零实现，LLM 后端替换为 DeepSeek API。

## 功能特性

- **工具调用循环**：模型通过 function calling 驱动文件操作、bash、任务看板、worktree、cron、技能加载、多代理协作等 30 个内置工具 + 动态 MCP 工具
- **分层上下文压缩**：超大工具结果落盘 → 消息数裁剪 → 旧工具结果折叠 → LLM 摘要压缩 → 上下文超长应急压缩，每层保护 `tool_calls`/`tool` 配对完整性
- **多代理协作**：lead 可 spawn 自治队友线程，通过文件邮箱通信；队友空闲时自动认领无主任务；**计划审批闸门**——队友提交计划后暂停一切模型/工具步骤，直到 lead 审批
- **聚焦子代理**：`task` 工具生成最小工具集的子代理，循环执行后只回传文本摘要
- **MCP 晚绑定**：`connect_mcp` 连接服务器后，其工具以 `mcp__server__tool` 命名动态合并进统一工具池；按 `readOnlyHint`/`destructiveHint` 注解决定是否需要确认
- **统一工具策略与分发**：lead/本地/子代理/队友/后台全部经过同一个 `ToolDispatcher.authorize → execute`；敏感文件与控制面保护、逐次人工确认、写前备份、输出脱敏、常开审计；bash 优先 bubblewrap 沙箱（只读根、可写工作区、默认无网络）
- **预算与失败可见**：主循环、子代理、队友、后台、cron、团队规模都有轮数/工具调用/墙钟/API 调用/并发上限；超限强制无工具收尾；后台任务失败/超时/取消和队友异常都有显式通知
- **定时任务（cron）**：自研 5 字段 cron 表达式解析/校验/匹配，任务可持久化，触发后把计划提示重新注入主循环；限制任务数、prompt 长度与最小触发间隔
- **后台任务**：慢速命令（install/build/test 等）异步执行，状态 `queued/running/completed/failed/cancelled/timed_out`，支持 `get_background_task` / `cancel_background_task`
- **错误恢复链**：429 限流退避重试 → 503/529 连续过载切换备用模型 → 输出截断升级 max_tokens → 注入续写提示 → 上下文超长应急压缩
- **状态全部落盘**：任务、cron、worktree 事件、transcript、记忆索引、邮箱均为磁盘文件；任务带租约/心跳，邮箱原子消费，写操作有 `.backups/` 备份与 `.audit/` 审计

## 整体架构

```
                        ┌────────────────────────────────────────────┐
  用户 ──> main.py ──> │ AgentLoop（编排层）                          │
                        │                                            │
                        │  prepare_context  ──> ContextCompact       │
                        │  call_llm         ──> PromptBuilder        │
                        │                       └─> LlmClient ──> DeepSeek API
                        │                       └─> RecoverySystem（重试/降级）
                        │  工具分发         ──> ToolDispatcher（策略/确认/沙箱/审计）
                        │                       ├─> ToolRegistry（工具池）
                        │                       │     ├─ 各领域系统工具
                        │                       │     └─ MCP 工具（晚绑定）
                        │                       └─> HookSystem（生命周期钩子）
                        │  并行线程        ──> CronScheduler / BackgroundTaskSystem
                        │                       / TeammateSystem（队友线程）
                        └────────────────────────────────────────────┘
```

代码按职责分为四层、32 个模块（`src/`），层间单向依赖：

### 基础层

| 模块 | 职责 |
|---|---|
| `Configs` | 全局配置常量：工作目录、路径、token 预算、阈值、模型常量，魔法数字集中管理 |
| `Terminal` | 线程安全的彩色终端输出；后台线程打印时清行并重绘输入提示符 |
| `LlmClient` | DeepSeek API 适配：HTTP 调用、消息/工具格式转换、错误归一化（`DeepSeekApiError`） |
| `RecoverySystem` | LLM 错误恢复：指数退避重试、备用模型降级、上下文超长错误识别 |
| `FileLock` | 跨线程/进程文件锁、原子写入、私有目录 |
| `SecurityPolicy` | 敏感/控制面/状态路径分类、secret 脱敏、子进程环境清洗、shlex bash 分析、名称/ID 校验 |

### 领域层

| 模块 | 职责 |
|---|---|
| `BasicOper` | 基础工具：bash / 文件读写改 / glob / todo，文件操作限定在工作区内（`safe_path`） |
| `TaskSystem` | 任务看板：JSON 文件持久化，文件锁内 claim/complete/fail/release，租约/心跳/过期回收 |
| `WorktreeSystem` | 隔离 git worktree：名称校验、创建/删除/保留，删除前统计未提交变更；有 in-progress 绑定任务时拒绝删除 |
| `SkillSystem` | 技能加载：扫描 `skills/` 目录，解析 `SKILL.md` 的 YAML frontmatter |
| `MessageBus` | 消息总线：每个代理一个追加式 JSONL 邮箱，flock + 原子 rename 消费，支持崩溃恢复 |
| `ProtocolSystem` | 协议状态机：以 `request_id` 匹配请求/响应，防止协议回复串扰 |
| `CronScheduler` | 定时任务：cron 解析/校验/匹配、注册、触发队列、原子持久化；限制任务数/prompt 长度/最小间隔 |
| `BackgroundTaskSystem` | 后台任务：并发上限、状态机、超时/取消、`get_background_task` / `cancel_background_task` |
| `McpClient` / `McpSystem` | MCP：单服务器客户端 + 连接管理、工具发现与合并、调用超时/输出上限、readOnly/destructive 注解 |

### 智能体层

| 模块 | 职责 |
|---|---|
| `TeammateSystem` | 队友系统：自治队友线程、空闲轮询、lead 侧团队管理、计划审批；worktree 写约束、失败可见、任务释放 |
| `SubagentSystem` | 子代理系统：`task` 工具背后的聚焦式子代理；本地路由强制只读 |
| `HookSystem` | 生命周期钩子：UserPromptSubmit/PreToolUse/PostToolUse/Stop；权限判断已移到 ToolDispatcher |
| `ToolPolicy` | 各角色策略（lead/local/subagent/teammate）、TurnBudget、ToolResult |
| `ToolDispatcher` | 唯一 `authorize → execute` 收口：策略、确认、沙箱、备份、审计、脱敏 |
| `ApprovalGate` | 逐次人工确认；非 TTY/工作线程 fail-closed；`AUTO_APPROVE_TOOLS` 显式逃生舱 |
| `AuditLog` | 常开 JSONL 审计：参数脱敏/正文哈希、按大小轮转 |
| `Sandbox` | bwrap 沙箱：只读根、可写工作区、默认无网络；auto/require/off |
| `ContextCompact` | 上下文压缩：预算控制、分层剪裁、摘要生成、转录落盘（脱敏） |
| `PromptBuilder` | 每轮从活上下文重建 system prompt；工具清单来自实际路由后的 registry 工具集 |
| `Memories` | 轮次上下文组装：记忆索引、MCP 连接、活跃队友、任务看板摘要 |
| `ToolRegistry` | 工具注册表：聚合各系统工具与 MCP 工具；重名/缺 handler 检测 |

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
5. **分发**：`compact` 特殊处理 → `ToolDispatcher.authorize`（预算/策略/确认）→ 慢命令放后台（主线程先授权）→ `execute`（沙箱/备份/审计/脱敏）→ 工具结果回传
6. **循环**：直到模型不再产生工具调用，触发 `Stop` 钩子；预算耗尽时补齐未执行的 tool 消息，再做一次无工具总结

## 技术要点

### 1. API 适配隔离（`LlmClient`）

所有 DeepSeek/OpenAI 格式细节收拢在一个模块，上层只面向 `LlmResponse` / `ToolCall` 两个协议无关的数据结构编程：

- `assistant_message()` 把 tool_calls 序列化为 API 可回传的格式，`tool_message()` 与之按 `tool_call_id` 配对
- 工具定义的 `input_schema` → `function.parameters`；`stop_reason == "max_tokens"` → `finish_reason == "length"`
- 错误统一为 `DeepSeekApiError` 并分类（`classify_error`：ratelimit / overload / other）
- 零新增 SDK 依赖（仅 httpx），换服务商只改 base_url 与错误分类

### 2. 工具注册表即扩展点（`ToolRegistry`）

每个系统自持 `tool_defs()` / `handlers()` 两个接口，新增工具 = 在所属系统实现 + 注册该系统，调度代码零改动。MCP 工具晚绑定合并：连接后才发现工具，以 `mcp__server__tool` 命名并入工具池，服务器名/工具名做安全字符归一化。

### 3. 权限在分发之前（`ToolDispatcher`）

所有 agent 循环（lead / 本地路由 / 子代理 / 队友 / 后台任务）都经过同一个 `authorize → execute`，不再有"某个 agent 忘了加护栏"的路径：

- `ToolPolicy` 按角色定义允许工具、只读、可写根目录、bash 模式与需确认工具；本地路由和本地子代理强制只读，队友只有分到 worktree 才能写；
- `SecurityPolicy` 拒绝敏感文件（`.env*`、密钥、`.ssh/**`、`credentials*` 等）与状态面写入，保护控制面（HookSystem/Configs/ToolRegistry/策略模块）写入需确认；工具输出统一脱敏，bash 子进程环境剔除 API Key；
- `ApprovalGate` 做逐次确认：控制面写、`remove_worktree(discard_changes)`、MCP 破坏性工具、网络/破坏性命令；非 TTY/后台线程 fail-closed，`AUTO_APPROVE_TOOLS` 是显式逃生舱；
- `Sandbox` 优先用 bubblewrap（只读根、只写工作区/worktree、默认无网络）；命令黑名单只是 UX 守卫，不是安全边界；
- `AuditLog` 常开记录每次决策/结果（参数脱敏、正文只记哈希），写文件前备份到 `.backups/<call_id>/`。

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

队友线程结构：内层循环最多 10 轮工具执行（消息窗口 20 条）→ 空闲轮询（每 5s 检查邮箱与任务看板，60s 超时退出）→ 收件箱消息优先于自动认领任务。队友现在也经过统一 `ToolDispatcher`：认领带 worktree 的任务后，只允许在该目录内写，bash cwd 强制到该目录并套沙箱；没有 worktree 时文件写拒绝、bash 只允许只读白名单。线程异常/超时/生命周期或 API 调用上限都会给 lead 发 `status=failed` 的 `result`，并在 finally 中释放或标记失败当前任务。

### 8. 可测试性

状态类（`TaskSystem` / `WorktreeSystem` / `ContextCompact` / `CronScheduler` / `AuditLog` / `Sandbox`）均支持构造函数注入目录或探测函数，测试不碰真实环境。209 个用例全部离线运行：`test_core.py` 覆盖纯逻辑，`test_agent_loop.py` 用 `FakeClient` 按脚本回放，`test_dispatcher.py` / `test_budget.py` 覆盖安全与预算收尾，并发用例覆盖任务认领和邮箱消费。

### 9. 线程模型

cron 调度线程、cron 自动执行线程、后台任务线程、队友线程并行运行；`ActiveTeammates`、`CronScheduler`、`BackgroundTaskSystem`、`TaskSystem`、`MessageBus` 均以锁/文件锁保护共享状态；后台任务在 worker 中复用主线程完成的授权，不再弹确认；`Terminal` 保证后台打印不打断用户输入。

### 10. 预算、审计与状态原子性

- **预算**：`TurnBudget` 管理轮数/工具调用/墙钟/token；主循环超限时先为每个未执行的 `tool_call` 补一条 `[skipped]` 工具消息，再发一次 `tools=None` 收尾调用，保证 API 配对合法。子代理 30 轮 + 工具调用/墙钟上限；队友生命周期/API 调用/认领任务/团队规模上限；后台并发/排队/单任务超时；cron 任务数/prompt 长度/最小间隔。
- **审计与备份**：`.audit/tool_calls.jsonl` 常开（与训练轨迹分开），记录 agent/tool/脱敏参数/决策/耗时/状态；写文件前复制到 `.backups/<call_id>/`。
- **原子性**：任务认领用 `fcntl.flock` 串行化，带租约/心跳/过期回收；邮箱读取在锁内原子 rename 到 draining 文件；cron 持久化用临时文件 + `os.replace`。

### 11. 沙箱与 MCP 信任模型

- bash 默认 `SANDBOX_MODE=auto`：有 bwrap 时只读根、只写工作区/worktree、无网络；网络命令需确认后才放开 `--unshare-net`；没有 bwrap 时降级并告警（黑名单不是安全边界），`require` 模式直接失败。
- MCP 工具按 `readOnlyHint` / `destructiveHint` 注解处理；无注解工具保守要求确认；破坏性工具在非交互/队友线程直接拒绝；调用带超时与输出上限。mock 服务器与真实服务器的描述标记都会参与推断。

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
| `ROUTER_MODE` | | `auto` | 混合路由开关：`auto` / `api-only`（逃生舱）/ `local-only`（测试）/ `shadow`（只记录不生效） |
| `API_MODEL_ID` | | 取 `MODEL_ID` | API 侧模型 |
| `LOCAL_MODEL_ID` | | 空 | 本地小模型名（vLLM 服务名），配置后启用本地路由 |
| `LOCAL_BASE_URL` | | 空 | 本地 vLLM OpenAI 兼容地址，如 `http://<gpu-box>:8000/v1` |
| `LOCAL_API_KEY` | | `local` | 本地服务鉴权（vLLM 通常不需要） |
| `LOCAL_LLM_TIMEOUT` | | `300` | 本地推理超时（秒），本地生成慢需放宽 |
| `COLLECT_TRAINING_DATA` | | `0` | Phase 3 轨迹收集开关：`1` 开启，落盘 `.training_data/*.jsonl` |
| `TURN_MAX_ROUNDS` / `TURN_MAX_TOOL_CALLS` / `TURN_MAX_WALL_SECONDS` / `TURN_MAX_TOTAL_TOKENS` | | `40` / `100` / `600` / `200000` | 主循环单轮预算；token=0 表示不限制 |
| `SUBAGENT_MAX_TOOL_CALLS` / `SUBAGENT_MAX_WALL_SECONDS` | | `60` / `600` | 子代理预算（轮数固定 30） |
| `TEAMMATE_MAX_LIFETIME_SECONDS` / `TEAMMATE_MAX_API_CALLS` / `TEAMMATE_MAX_TASKS` / `MAX_TEAMMATES` | | `1800` / `200` / `20` / `5` | 队友生命周期与团队上限 |
| `BACKGROUND_MAX_CONCURRENT` / `BACKGROUND_MAX_QUEUED` / `BACKGROUND_TASK_TIMEOUT_SECONDS` | | `4` / `16` / `600` | 后台任务并发、排队与单任务超时 |
| `TASK_LEASE_SECONDS` / `TASK_MAX_ATTEMPTS` | | `300` / `3` | 任务租约与最大重试次数 |
| `BASH_TIMEOUT_DEFAULT` / `BASH_TIMEOUT_MAX` / `READ_FILE_MAX_LINES` / `MCP_CALL_TIMEOUT` | | `120` / `600` / `2000` / `60` | 工具硬上限 |
| `SANDBOX_MODE` / `SANDBOX_MEMORY_MB` / `SANDBOX_CPU_SECONDS` | | `auto` / `4096` / `600` | bash 沙箱：`auto` / `require`（无 bwrap 失败）/ `off` |
| `AUTO_APPROVE_TOOLS` | | 空 | 非交互场景的确认逃生舱（精确名或 `prefix*`）；配置后控制面写/破坏性操作不再询问，慎用 |
| `CRON_MAX_JOBS` / `CRON_MAX_PROMPT_CHARS` / `CRON_MIN_INTERVAL_MINUTES` | | `50` / `2000` / `5` | 定时任务上限 |
| `AUDIT_MAX_BYTES` / `AUDIT_BACKUP_COUNT` | | `5000000` / `3` | 审计日志轮转 |

**混合路由**（`ModelRouter`）：不配置 `LOCAL_*` 时退化为纯 API 行为。配置后按调用点静态路由（摘要、探索子代理 → 本地；长时队友 → API），主循环走保守 AND 规则——只有"明确简单"的查询（短消息、信息查询意图、无动作/复杂标记、上下文不超过阈值）才走本地小模型；纯问答会剥离工具集，其余本地调用使用**只读精简工具集**（`read_file`/`glob`/`list_tasks`/`get_task`/`list_crons`/`load_skill`，无 bash/写操作/MCP，Phase 4）。本地模型的质量信号（工具参数解析失败、空回复、length 截断、工具报错、同参数空转）触发**整 turn 升级 API**，升级后的历史不携带失败消息。

**Catering 探索模式**（`Catering.AnomalyAgent.explore`）同样接入路由（`caller=catering`）：默认 API，只有纯概念问答（如"什么是 ROI?"）走本地且不带工具；含门店/客流/营业额/数据等业务分析词的问题一律 API，避免小模型误路由浪费时间。

**轨迹收集**（Phase 3，`TrajectoryLogger`）：开启后每个 turn 落一条 ShareGPT 格式 JSONL（`{"messages": [...], "metadata": {"outcome": ...}}`），按 outcome 分型——`local-ok`（小模型正样本）、`escalated`（困难样本，带升级原因）、`api`（强模型示范，SFT 模仿目标）、`error`（诊断）。数据在 24GB 机器上用 Llama-Factory/Unsloth 做 QLoRA 微调。

**本地小模型**（4GB 显存开发机）：`local_model/server.py` 是本地 OpenAI 兼容服务 shim（llama-cpp-python + Qwen 模板，解析 `<tool_call>` 块为标准 tool_calls，坏 JSON 原样上报以触发升级阶梯）。启动方式：

```bash
conda create -n llama python=3.10 llama-cpp-python -c conda-forge
conda run -n llama python local_model/server.py \
    --model /root/models/qwen2.5-3b-instruct-q4_k_m.gguf --port 8080
```

配合 `LOCAL_BASE_URL=http://127.0.0.1:8080/v1`、`LOCAL_MODEL_ID=local-3b` 接入。3B 级只用于管线验证；正式效果以 24GB 机上微调的 14B+（vLLM 服务，接口一致）为准。

## 测试

```bash
python -m unittest discover -s tests -v
```

209 个用例全通过：

- **test_core.py（31 个）**：cron 表达式校验与匹配、任务生命周期与依赖阻塞、DeepSeek 消息/工具格式构造与往返、错误分类、压缩预算与配对保护、工具注册表合并（内置 + MCP）、worktree 名称校验、文件工具与路径沙箱
- **test_agent_loop.py（2 个）**：用 `FakeClient` 离线跑通完整主循环——验证工具确实执行、tool 消息与 tool_calls 配对回传、system prompt 注入、`length` 截断升级 max_tokens 后成功收尾
- **test_model_router.py（24 个）**：路由决策、静态调用点、模式开关、升级阶梯、本地只读工具集、轨迹收集
- **test_catering.py（39 个）**：LMDI 拆解、数据源校验、告警扫描、ToolGuard、工作流端到端、Reasoner 降级、探索模式权限拦截/步数熔断、状态机、诊断链、评测场景
- **新增 113 个**：敏感路径/控制面/脱敏/bash 分析、分发器确认与备份、预算收尾、后台完成/失败/取消/超时、任务并发认领与租约、邮箱并发消费、cron 限额、子代理只读、队友失败可见与任务释放、MCP 注解/超时、Catering 写工具逐次确认

## Catering 业务包

`Catering/` 是第一个餐饮业务 Agent：**门店经营异常分析**。发现"某店营业额异常"后沿确定性诊断链（拆解 → 客流/客单价 → 时段 → 堂食/外卖 → 菜品结构 → 活动 → 评分 → 人员 → 外部因素 → 历史案例）逐层取数判定，不靠 LLM 猜原因：

```bash
python -m Catering.main --eval                        # 离线评测（无需 API Key）
python -m Catering.main --store A店 --auto            # 工作流诊断 + LLM 归纳
python -m Catering.main --agentic "A店客流为什么降?"  # 探索模式：LLM 自由调用餐饮工具
```

架构、数据契约、八类数据源映射与使用说明见 `Catering/readme.md`。

## 项目结构

```
MiniClaudeCode/
├── main.py                    # 入口：加载 .env → 构建客户端 → 启动 CLI
├── src/                       # 32 个模块，四层架构
│   ├── Configs.py             #   全局配置
│   ├── Terminal.py            #   终端输出
│   ├── LlmClient.py           #   DeepSeek API 适配
│   ├── ModelRouter.py         #   本地/API 混合路由
│   ├── RecoverySystem.py      #   重试与降级
│   ├── FileLock.py            #   文件锁/原子写入
│   ├── SecurityPolicy.py      #   路径/脱敏/bash 安全策略
│   ├── BasicOper.py           #   文件/bash/todo 工具
│   ├── TaskSystem.py          #   任务看板（锁/租约）
│   ├── WorktreeSystem.py      #   隔离 worktree
│   ├── SkillSystem.py         #   技能加载
│   ├── MessageBus.py          #   文件邮箱总线（原子消费）
│   ├── ProtocolSystem.py      #   协议状态机
│   ├── CronScheduler.py       #   定时任务（限额/原子持久化）
│   ├── BackgroundTaskSystem.py#   后台任务（状态机/取消）
│   ├── McpClient.py           #   MCP 单服务器客户端
│   ├── McpSystem.py           #   MCP 连接管理/注解
│   ├── TeammateSystem.py      #   自治队友
│   ├── SubagentSystem.py      #   聚焦子代理
│   ├── HookSystem.py          #   生命周期钩子
│   ├── ToolPolicy.py          #   角色策略/预算/ToolResult
│   ├── ToolDispatcher.py      #   统一授权与执行
│   ├── ApprovalGate.py        #   逐次确认
│   ├── AuditLog.py            #   常开审计
│   ├── Sandbox.py             #   bwrap 沙箱
│   ├── ContextCompact.py      #   上下文压缩
│   ├── TrajectoryLogger.py    #   路由轨迹/训练数据
│   ├── PromptBuilder.py       #   system prompt 组装
│   ├── Memories.py            #   轮次上下文
│   ├── ActiveTeammates.py     #   活跃队友注册表
│   ├── ToolRegistry.py        #   工具注册表
│   └── AgentLoop.py           #   主循环与 CLI
├── Catering/                  # 餐饮业务包：门店经营异常分析 Agent（详见 Catering/readme.md）
│   ├── Configs.py             #   业务阈值/目录配置
│   ├── Models.py              #   告警/发现/证据/报告/案例结构
│   ├── DataHub.py             #   八类数据源抽象 + 模拟适配器
│   ├── Metrics.py             #   LMDI 贡献度拆解等确定性计算
│   ├── Tools.py               #   餐饮工具集（12 只读 + 1 受控写）
│   ├── Workflow.py            #   诊断链引擎 + 状态机
│   ├── Guardrails.py          #   规则/输出校验/工具权限
│   ├── Reasoner.py            #   LLM 归纳 + 模板降级
│   ├── Evaluator.py           #   离线评测体系
│   ├── AnomalyAgent.py        #   双模式编排（工作流/探索，ToolRegistry+MCP 集成）
│   ├── Scheduler.py           #   定时自动诊断（复用 CronScheduler）
│   ├── gen_fixtures.py        #   样本数据生成器（确定性）
│   └── main.py                #   CLI：--eval/--store/--agentic/--schedule/--tasks
├── tests/                     # 离线测试用例（209 个）
├── skills/                    # 技能目录（agent-builder / code-review / mcp-builder / pdf / catering-ops / code-analysis-ops）
├── .env.example               # 环境变量模板
└── requirements.txt           # httpx / python-dotenv / pyyaml
```

## 内置工具清单

| 系统 | 工具 |
|---|---|
| BasicOper | `bash` `read_file` `write_file` `edit_file` `glob` `todo_write` |
| TaskSystem | `create_task` `list_tasks` `get_task` `claim_task` `complete_task` `reclaim_tasks` |
| WorktreeSystem | `create_worktree` `remove_worktree` `keep_worktree` |
| SkillSystem | `load_skill` |
| CronScheduler | `schedule_cron` `list_crons` `cancel_cron` |
| McpSystem | `connect_mcp` + 已连接服务器的 `mcp__server__tool`（动态） |
| ContextCompact | `compact`（由主循环特殊处理） |
| SubagentSystem | `task` |
| TeammateSystem | `spawn_teammate` `send_message` `check_inbox` `request_shutdown` `request_plan` `review_plan` |
| BackgroundTaskSystem | `get_background_task` `cancel_background_task` |

system prompt 的工具清单由 `PromptBuilder` 根据本轮实际路由后的 `ToolRegistry` 工具集生成，不再维护第二份静态清单。
