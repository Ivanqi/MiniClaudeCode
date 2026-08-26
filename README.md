1. 分析 old_code/full.py 并按职责 OOP 化拆分到 src/（23 个模块）

  保留了参考代码中的模块划分（Configs、BasicOper、ContextCompact、CronScheduler、McpClient、McpSystem、ActiveTeammates、Memories），并为
  未覆盖的职责新增模块：

  层: 基础层
  模块: Configs / Terminal / LlmClient / RecoverySystem
  职责: 配置常量、终端输出、DeepSeek API 适配、重试降级
  ────────────────────────────────────────
  层: 领域层
  模块: BasicOper / TaskSystem / WorktreeSystem / SkillSystem / MessageBus / ProtocolSystem / CronScheduler / BackgroundTaskSystem /
    McpClient / McpSystem
  职责: 文件工具、任务、worktree、技能、邮箱总线、协议状态机、定时任务、后台任务、MCP
  ────────────────────────────────────────
  层: 智能体层
  模块: TeammateSystem / SubagentSystem / HookSystem / ContextCompact / PromptBuilder / Memories / ToolRegistry
  职责: 队友线程、子代理、权限管道、上下文压缩、提示组装、工具注册表
  ────────────────────────────────────────
  层: 编排层
  模块: AgentLoop + main.py
  职责: 主循环与 CLI 入口

  2. Anthropic API → DeepSeek API（src/LlmClient.py）
  - 用已安装的 httpx 直接调 DeepSeek 的 OpenAI 兼容 /chat/completions 接口，零新增 SDK 依赖
  - 关键格式转换：内容块消息 → assistant tool_calls + tool 消息配对；工具定义 input_schema →
  function.parameters；stop_reason=="max_tokens" → finish_reason=="length"；错误分类适配 429/503
  - API 细节全部隔离在 LlmClient，上层只面向 LlmResponse/ToolCall 编程（接口而非实现编程）；换服务商只改这一个文件
  - 配置：DEEPSEEK_API_KEY（必填）、MODEL_ID（默认 deepseek-chat），见 .env.example

  3. 遵循 代码编写前提.md 的设计要点
  - 单一职责/高内聚低耦合：工具定义跟着实现走（参考代码里工具表集中在 Configs 的耦合点已改为各系统自持 + ToolRegistry
  聚合，新工具只需在所属系统实现并注册一行）
  - 可扩展性：钩子管道在工具处理器之外、MCP 晚绑定工具、ToolRegistry 即扩展点
  - 可测试性：状态类（TaskSystem/WorktreeSystem/ContextCompact/CronScheduler）均支持构造函数注入目录，测试不碰真实环境

  4. 验证（tests/，33 个用例全通过）
  - test_core.py：cron 匹配/校验、任务生命周期与依赖、消息格式构造、压缩预算/剪裁、工具注册表合并、worktree 名称校验、文件工具与路径安全
  - test_agent_loop.py：用 FakeClient 离线跑通完整主循环，验证 tool 消息与 tool_calls 配对、截断升级 max_tokens 等 DeepSeek 格式链路

  运行方式：pip install -r requirements.txt → cp .env.example .env 填入 DEEPSEEK_API_KEY → python main.py

  一处说明：两个测试失败曾暴露测试预期错误（非代码问题）——safe_path 拒绝工作目录外路径、claim_task 先查状态再查归属，均为原版 full.py
  的既有行为，测试已按原语义修正。