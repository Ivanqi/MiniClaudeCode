# Catering —— 餐饮门店经营异常分析 Agent

基于 `src/` 基础设施(LLM 客户端、工具注册表模式、统一工具分发、错误恢复链)实现的第一个餐饮业务 Agent:
发现"某店营业额异常"后,不靠 LLM 猜原因,而是沿**确定性诊断链**逐层取数、判定、取证,
最终给出**每个结论都能回溯到工具数据**的可验证诊断报告与行动建议。

## 架构

```
                        ┌────────────────────────────────────────────┐
 告警/用户 ──> main.py ──> AnomalyAgent(编排层)                      │
                        │                                            │
                        │  工作流模式 run_alert:                      │
                        │    Planner(诊断链校验) ─> DiagnosisStateMachine(持久化) │
                        │      └─> DiagnosticEngine: 逐步执行         │
                        │            ├─ Tools ─> DataHub(八类数据源)  │
                        │            ├─ Metrics(数据分析服务,确定性)  │
                        │            └─ Guardrails(规则/校验/权限)    │
                        │      └─> Reasoner: LLM 归纳(无工具) ─ 失败 ─> 模板降级 │
                        │  探索模式 explore:                          │
                        │    LLM function calling 自由调用工具        │
                        │      └─> ToolGuard 分发前拦截               │
                        │      └─> 步数上限 + 拒绝熔断 + 强制收敛      │
                        └────────────────────────────────────────────┘
```

分层(单向依赖,与 src 四层同构):

| 层 | 模块 | 职责 |
|---|---|---|
| 基础层 | `Configs` `Models` | 阈值/目录配置;告警、发现、证据、报告、案例等数据结构 |
| 领域层 | `DataHub` `Metrics` `Tools` `Cases` | 八类数据源抽象+模拟适配器;LMDI 贡献度拆解等确定性计算;餐饮工具集;案例版本化/去重存储 |
| 智能体层 | `Workflow` `Guardrails` `Reasoner` | 诊断链引擎+状态机;规则求值/报告校验/工具权限;LLM 归纳+降级 |
| 编排层 | `AnomalyAgent` `Evaluator` `main.py` | 双模式组装;离线评测;CLI 入口 |

## 核心设计

### 1. 诊断链(需求 1)——数据驱动,不让 LLM 猜

诊断步骤、工具、判定规则、分支条件全部声明在 `data/diagnosis_chain.json`,
**运营团队直接改 JSON 即可调整诊断逻辑,不用改代码**:

```
拆解(营业额=客流×客单价) → 客流 → 客单价 → 时段 → 堂食/外卖
  → 菜品结构 → 活动 → 评分 → 人员 → 外部因素(天气/竞对) → 历史类似案例
```

- 每步 `rules`: `{metric(点路径), op(白名单: le/ge/lt/gt/eq/abs_ge), threshold, verdict, summary}`,
  无任意代码执行;`when` 白名单(always/any_anomaly/culprit_is:X)实现链分支;
- 判定规则不命中 → `normal`;命中 anomaly 规则 → `anomaly`;数据缺失 → `no_data`(显式标记,不编造)。

### 2. Tool Calling(需求 2)——八类数据源

`DataHub` 按 `DataSource.query(store_id, date, **params) -> 归一化 dict` 抽象八类数据源,
当前为样本模拟实现,**换真实数据库/POS/外卖/第三方系统 = 新增一个适配器类,业务代码零改动**:

| 数据源 | 适配器 | 数据文件 |
|---|---|---|
| 门店经营数据库 | `StoreDbSource` | `data/stores/{store_id}.json`(日总/菜品/评分/人员) |
| POS 收银 | `PosSource` | `data/pos/{store_id}.json`(堂食+时段) |
| 外卖平台 | `TakeoutSource` | `data/takeout/{store_id}.json`(外卖+时段) |
| 内部业务系统 | `InternalSystemSource` | `data/internal/promotions.json` |
| 第三方 API | `ExternalApiSource` | `data/external/weather.json` `market.json` |
| 企业知识库 | `KnowledgeBaseSource` | `data/cases/*.json`(历史案例+Agent 归档) |
| 数据分析服务 | `Metrics` | 纯计算,无存储 |

`Tools.py` 暴露 12 个只读工具 + 1 个受控写工具(`archive_case`,默认禁用;开启后探索模式仍需逐次人工确认),
`tool_defs()/handlers()` 双接口可直接注册进 `src.ToolRegistry`。

案例归档走 `Cases.py` 的版本化 upsert:保留 `<CASES_DIR>/archive/<case_id>.json` 与
`{"cases":[record]}` 信封,新增 `version/updated_at/source/confidence/content_hash`;
同一 `case_id` 内容变化时原地升 version,不再覆盖丢失历史。`all_cases()` 递归扫描
legacy fixtures 与 `archive/`,`search_cases` 按 `case_id` 去重并加入时间衰减。

### 3. Workflow(需求 3)

- **Planning**: `Planner` 加载/校验诊断链(未知工具/非法 op/非法 when 拒绝启动),
  参数模板 `{{store_id}}` 绑定告警;
- **State Management**: `DiagnosisStateMachine` 事件驱动
  NEW→ACCEPTED→PLANNING→DIAGNOSING→REASONING→REVIEWING→DONE(错误→FAILED→retry),
  全程持久化 `run_dir/{run_id}.json`,可审计可重试;
- **Memory**: 批准后的报告归档为企业知识库案例,供后续 `search_cases` 检索;
- **Context Management**: LLM 只收到 ≤4KB 的证据摘要;探索模式工具结果截断;
- **Human-in-the-loop**: `ReviewGate` 人工审批(交互模式),拒绝只影响归档,报告必落盘;
- **Agent Evaluation**: `Evaluator` 离线场景集(见下)。

### 4. 可靠性(需求 4)

| 机制 | 落地 |
|---|---|
| 数据校验 | 行级必填校验;`missing_fields` 显式标记不填 0;加和自洽校验(Σ时段=Σ渠道=Σ菜品=日总);LMDI 残差校验 |
| 规则约束 | 诊断链数据驱动 + op/when 白名单;步骤状态仅 normal/anomaly/no_data/excluded |
| 输出结构化 | `DiagnosisReport` 单一 schema + `OutputValidator` 手写校验;证据引用必须闭合 |
| 权限控制 | `ToolGuard`:工具白名单+门店范围+日期范围+写工具能力开关;探索模式在 guard 通过后还会调用 `confirm` 逐次确认,无确认回调即 fail-closed |
| 错误恢复 | LLM 429/503 走 `src.RecoverySystem.with_retry`;单步数据失败→no_data 不炸链;LLM 输出非法→重试 1 次→模板降级(永不空报告) |

### 5. 主因判定口径(Metrics)

营业额 = 客流 × 客单价,用 **LMDI 对数均值法**拆解,精确无残差:

- 客流贡献 = L·ln(T_t/T_0),客单价贡献 = ΔR − 客流贡献;
- 贡献 share 可负、可超 100%(一方回补另一方,如 A店:客流下拉 145%、客单价回补 −45%),
  **主因按贡献额判定,不看 share 绝对值**;
- 菜品结构用 mix/price 法拆解:Δ加权均价 = 结构效应 + 价格效应(同样精确无残差)。

### 6. 双模式

- **工作流模式(默认,`run_alert`)**: 诊断链全由代码确定性执行,
  LLM 只在推理环节归纳结论与建议,且**无任何工具**;结构化校验失败自动模板降级;
- **探索模式(`explore`)**: LLM 经 function calling 自由调用只读餐饮工具,
  `ToolGuard` 分发前拦截,步数上限(12)+ 连续拒绝熔断(2)+ 强制收敛。

### 7. 与 src 基础设施的集成

| src 能力 | 集成方式 |
|---|---|
| LlmClient / RecoverySystem / Terminal / BasicOper | 直接复用(API 适配、429/503 恢复链、终端输出、工具分发) |
| **ToolRegistry + MCP** | 探索模式经 `src.ToolRegistry` 聚合餐饮工具与 MCP 工具,每轮重新 assemble;**晚绑定**:LLM 调 `connect_mcp` 连接数据源服务后,`mcp__server__tool` 自动并入下轮工具池;ToolGuard 对 MCP 工具执行"只读放行、deploy/trigger 类拒绝"策略。真实 POS/外卖/三方数据源可按 MCP 服务接入 |
| **CronScheduler** | `Catering/Scheduler.py` 复用 `src.CronScheduler`(每个 agent 独立实例+注入 durable 路径):`--schedule "30 8 * * *"` 每天定时扫描告警并自动诊断,报告落盘 `reports/` |
| **TaskSystem** | `--tasks` 或注入 `TaskSystem` 后,诊断链每步在主代理的任务看板创建/认领/完成(owner=diagnostic-engine),诊断过程可观测可审计 |
| **SkillSystem** | 新增 `skills/catering-ops/SKILL.md`,主代理可 `load_skill("catering-ops")` 获取 Catering 运营知识 |
| HookSystem | 未直接复用:Catering 的权限是业务级(门店范围/写工具/诊断链白名单),由 `ToolGuard` 承接;若要接入主循环权限管道,注册一个 PreToolUse 回调即可 |
| SubagentSystem / TeammateSystem | 未接入:前者工具集面向编码领域(bash/文件)不可参数化;诊断链是确定性顺序执行,无并行需求。多门店并行诊断或多维深挖时,可按 `SubagentSystem` 的聚焦循环模式扩展(探索模式本身就是该模式的餐饮版) |
| Memories(轮次记忆) | 未接入:src 记忆索引服务于编码代理;Catering 的"记忆"是领域知识库(案例归档→检索),已由 DataHub 承接 |
| ContextCompact / BackgroundTaskSystem | 未接入:探索模式自带工具结果截断+步数上限;诊断查询均为亚秒级,无后台任务需求 |

接入点全部在 Catering 侧构造/注入,src 零改动。

## 快速开始

```bash
# 离线评测(无需 API Key)
python -m Catering.main --eval

# 诊断 A店(工作流模式,LLM 归纳;.env 需 DEEPSEEK_API_KEY)
python -m Catering.main --store A店 --auto

# 纯规则诊断,不用 LLM(模板结论)
python -m Catering.main --store A店 --auto --no-llm

# 扫描异常告警 / 自然语言输入
python -m Catering.main --list-alerts
python -m Catering.main "A店昨日营业额下降18%"

# 探索模式(LLM 自由调用餐饮工具,可 connect_mcp 接入外部数据源)
python -m Catering.main --agentic "A店2026-09-01客流为什么下降?"

# 定时自动诊断(复用 src.CronScheduler,常驻,Ctrl+C 退出)
python -m Catering.main --schedule "30 8 * * *"
python -m Catering.main --list-jobs          # 查看已注册任务

# 诊断步骤镜像到主代理任务看板
python -m Catering.main --tasks --store A店 --auto

# 重新生成样本数据(确定性,自校验)
python Catering/gen_fixtures.py
```

运行产物默认在 `.catering/`:状态 `run_*.json`、证据 `evidence/`、报告 `reports/`。

### 环境变量

| 变量 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` | 探索模式与 LLM 归纳所需;不配置时工作流自动模板降级 |
| `CATERING_SCOPE` | 授权门店列表(逗号分隔),如 `A店,B店`;空=全部 |
| `CATERING_ENABLE_WRITES` | 置 `1` 暴露写工具 `archive_case`;探索模式仍需逐次人工确认(TTY 交互),非 TTY 自动拒绝 |
| `CATERING_RUN_DIR` | 运行产物目录(默认 `.catering`) |

## 样本数据与评测

`gen_fixtures.py` 确定性生成 3 店 × 8 日数据(基准=前 7 日均值),内置三种场景:

| 门店 | 场景 | 告警日变化 | 主因 |
|---|---|---|---|
| A店 | 客流型异常 | 营收 -18% | 客流 -25%(暴雨+竞对新店促销,堂食午市最重) |
| B店 | 客单价型异常 | 营收 -10% | 客单价 -25%(9.9 引流套餐,客流反而 +20%) |
| C店 | 正常对照 | 营收 +2% | 无(不触发告警) |

`python -m Catering.main --eval` 跑 3 个场景,指标:
主因命中、贡献额误差 ≤0.05 元、证据覆盖、结构合规、步骤效率(3/3 全绿)。
LMDI 期望值由生成器**内联独立实现**写入 `data/expected_contributions.json`,
与 `Metrics` 生产实现交叉验证。

## 测试

```bash
python -m unittest discover -s tests -v    # 272 个用例(含 Catering 39 个),全部离线
```

## 已知限制(v1)

- 基准口径为前 7 日均值,未做周末/节假日校准;
- 样本数据窗口无停业日,LMDI 零/负基期场景由单测覆盖(标 structural_change,不猜值);
- 知识库检索为关键词/标签打分,未用向量检索;
- 探索模式产出为自由文本(不强制报告 schema),评测重点为调用效率与权限零违规;
- 写工具逐次确认依赖 TTY;cron/非交互场景会 fail-closed,不自动归档;
- 品类客单以"订单"为分母,与门店客单价(客流分母)口径不同,不可直接比较;
- 案例版本以 `case_id + content_hash` 去重,legacy fixtures 默认 `source=legacy/confidence=0.5`;真实知识库迁移时需保留稳定 `case_id`。
