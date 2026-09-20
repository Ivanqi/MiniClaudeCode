# CodeAnalysis —— 代码资产全生命周期安全管控 Agent

基于 `src/` 基础设施(LLM 客户端、工具注册表、权限钩子、错误恢复链、定时调度、任务看板)
实现的代码资产安全管控业务包: 代码入库后沿**确定性安全分析链**逐层扫描、评估、审查、
部署、验证,最终给出**每个结论都能回溯到工具数据**的安全等级与整改建议,
不靠 LLM 猜漏洞。

## 架构

```
                        ┌────────────────────────────────────────────┐
 入库/定时 ──> main.py ──> CodeAnalysisAgent(编排层)                │
                        │                                            │
                        │  工作流模式 run_analysis:                   │
                        │    Planner(分析链校验) ─> CodeAnalysisStateMachine(持久化) │
                        │      └─> SecurityAnalysisEngine: 逐步执行   │
                        │            ├─ Tools ─> Scanner(多语言规则引擎) │
                        │            │        ├> DataHub(五类数据源)   │
                        │            │        └> Deployer(一键部署)    │
                        │            ├─ Metrics(风险评估模型,确定性)   │
                        │            └─ Guardrails(规则/校验/权限)     │
                        │      └─> Reasoner: LLM 归纳(无工具) ─ 失败 ─> 模板降级 │
                        │  探索模式 explore:                          │
                        │    LLM function calling 自由调用只读工具     │
                        │      └─> ToolGuard 分发前拦截               │
                        │      └─> 步数上限 + 拒绝熔断 + 强制收敛      │
                        └────────────────────────────────────────────┘
```

分层(单向依赖,与 src 四层同构):

| 层 | 模块 | 职责 |
|---|---|---|
| 基础层 | `Configs` `Models` | 权重/阈值/目录配置;资产、发现、证据、部署记录、分析报告、案例等数据结构 |
| 领域层 | `DataHub` `Scanner` `Metrics` `Deployer` `Tools` | 五类数据源抽象;多语言规则扫描引擎;风险模型与确定性计算;一键部署模块;工具集 |
| 智能体层 | `Workflow` `Guardrails` `Reasoner` | 分析链引擎+状态机;规则求值/报告校验/工具权限;LLM 归纳+降级 |
| 编排层 | `CodeAnalysisAgent` `Evaluator` `Scheduler` `ResearchReports` `main.py` | 双模式组装;离线评测;定时扫描;研究交付物;CLI 入口 |

## 核心设计

### 1. 分析链(数据驱动,不让 LLM 猜)

分析步骤、工具、判定规则、分支条件全部声明在 `data/analysis_chain.json`,
**安全团队直接改 JSON 即可调整分析逻辑,不用改代码**:

```
资产盘点 → 静态扫描 → 安全自评估 → 运行环境审查 → 一键部署 → 部署验证(可用性/真实性/完整性) → 整改案例检索
```

- 每步 `rules`: `{metric(点路径), op(白名单: le/ge/lt/gt/eq/abs_ge/in),
  threshold, verdict, summary}`,无任意代码执行;
- `when` 白名单(always/never/any_anomaly/no_anomaly/deploy_done/level_is:X)
  实现链分支:部署验证仅在 `deploy_done`(部署成功)后执行,被拦截/失败则跳过;
  无异常不翻历史案例;
- 判定规则不命中 → `normal`;命中 anomaly 规则 → `anomaly`;数据缺失 → `no_data`(显式标记,不编造)。

### 2. 静态代码自评估标准(需求 1)

参照 ISO/IEC 27001(A.8.28 安全开发)、OWASP Top 10 2021 与 CWE 构建风险评估模型:

- **单条风险分 = 严重度权重 × 可能性**,按风险来源三套权重:

| 严重度 | 静态漏洞 | 运行环境 | 部署验证失败 |
|---|---|---|---|
| critical | 10 | 8 | 完整性 15 / 真实性 12 |
| high | 7 | 5 | 可用性 10 |
| medium | 4 | 2 | — |
| low | 1 | 1 | — |

- **项目总分 = 静态 + 环境 + 部署 三类子项之和**;
- **安全等级**: A(优秀,0) / B(良好,<10) / C(一般,<25) / D(较差,<45) / E(危险,≥45);
- **critical 硬门槛**(按严重度标签跨三维度计数,只降不升):1 个不优于 C,2 个不优于 D,≥3 个直接 E;
- **主风险 = 三类子项最高者**(平局按 静态>环境>部署),全 0 → none。

### 3. 多语言自动化扫描(需求 2)

规则全部声明在 `data/rules/{java,csharp,php,python}.json`,引擎要点:

- 逐行正则匹配,模式禁止跨行(防灾难性回溯),编译缓存;
- `not_match` 净化器排除:同一行命中主模式但含净化函数
  (参数化绑定/htmlspecialchars)时判定为安全,降误报;
- 三重上限:文件 1MB / 行 2000 字符 / 每文件 50 条发现;
- 每条发现附文件/行号/证据行/CWE/OWASP/ISO 映射/修复建议,结论可逐条复核;
- 每条规则配 `data/snippets/{rule_id}_planted.txt / _clean.txt` 精度片段对,
  自动化测试保证命中植入样本且不误报净化对照;
- 换真实 SAST 工具 = 实现同一 find 契约,业务代码零改动;
- 定时调度复用 `src.CronScheduler`(`--schedule '30 8 * * *'` 每天自动扫描全部资产)。

### 4. 动态运行环境安全性审查(需求 3)

`data/envs/{project_id}.json` 声明环境配置快照与审查基线
(纯 eq 语义比对,无正则无代码注入):如生产调试模式、硬编码云凭证、
过期基础镜像、TLS 版本、session 加固等,逐项审查并给出修复建议。

### 5. 一键部署与验证(需求 4)

`Deployer` 与公司云环境联动(当前为模拟适配器,换真实云平台 = 换适配器):

```
安全门禁(等级 E 拒) → 完整性(SHA-256 vs 制品注册表) → 真实性(签名/版本)
  → 云部署 → 可用性(健康检查) → 部署后独立复核(三项重新验证)
```

- 任一环节失败即拦截,失败原因与风险事件显式记录(风险分计入总评估);
- **可用性/真实性/完整性**三项验证闭环,篡改制品会被完整性校验拦截
  (样本场景 C 即此案例);
- 支持**部分部署**(资产元数据 `deploy_scope=partial + required_modules`)。

### 6. 双模式

- **工作流模式(默认,`run_analysis`)**: 分析链全由代码确定性执行,
  LLM 只在推理环节归纳结论与建议,且**无任何工具**;结构化校验失败自动模板降级;
- **探索模式(`explore`)**: LLM 经 function calling 自由调用只读分析工具,
  `ToolGuard` 分发前拦截,步数上限(12)+ 连续拒绝熔断(2)+ 强制收敛。

### 7. 与 src 基础设施的集成

| src 能力 | 集成方式 |
|---|---|
| LlmClient / RecoverySystem / Terminal / BasicOper | 直接复用(API 适配、429/503 恢复链、终端输出、工具分发);LLM 调用统一转发 `model=state.current_model` 使备用模型切换生效 |
| **ToolRegistry + MCP** | 探索模式经 `src.ToolRegistry` 聚合分析工具与 MCP 工具,每轮重新 assemble **晚绑定**;MCP deploy/trigger 类工具仅 `CODEANALYSIS_ENABLE_WRITES=1` 时放行(部署是本包正规业务能力) |
| **CronScheduler** | `Scheduler.py` 复用 `src.CronScheduler`(独立实例+注入 durable 路径):`--schedule` 定时自动扫描全部资产,报告落盘 `reports/` |
| **TaskSystem** | `--tasks` 注入后,分析链每步在主代理任务看板创建/认领/完成(owner=security-analysis-engine),过程可观测可审计 |
| **SkillSystem** | 新增 `skills/code-analysis-ops/SKILL.md`,主代理可 `load_skill("code-analysis-ops")` 获取 CodeAnalysis 运维知识 |
| HookSystem | 未直接复用:业务级权限由 `ToolGuard` 承接;接入主循环权限管道只需注册一个 PreToolUse 回调 |
| SubagentSystem / TeammateSystem | 未接入:分析链是确定性顺序执行,无并行需求;多项目并行分析时可按 SubagentSystem 的聚焦循环模式扩展 |
| Memories / ContextCompact / BackgroundTaskSystem | 未接入:"记忆"是整改案例知识库(DataHub 承接);探索模式自带截断+上限;分析均为亚秒级 |

接入点全部在 CodeAnalysis 侧构造/注入,src 零改动。

## 快速开始

```bash
# 离线评测(无需 API Key)
python -m CodeAnalysis.main --eval

# 分析指定项目(工作流模式,LLM 归纳;.env 需 DEEPSEEK_API_KEY)
python -m CodeAnalysis.main --project PJ-A-001 --auto

# 纯规则分析,不用 LLM(模板结论)
python -m CodeAnalysis.main --no-llm --project PJ-A-001 --auto

# 资产清单 / 自然语言输入
python -m CodeAnalysis.main --list-assets
python -m CodeAnalysis.main "科技管理系统入库,请做安全分析"

# 探索模式(LLM 自由调用分析工具,可 connect_mcp 接入外部数据源)
python -m CodeAnalysis.main --agentic "科技管理系统有哪些安全问题?"

# 研究交付物(量化指标 1/3/4)
python -m CodeAnalysis.main --research all

# 定时自动扫描(复用 src.CronScheduler,常驻,Ctrl+C 退出)
python -m CodeAnalysis.main --schedule "30 8 * * *"
python -m CodeAnalysis.main --list-jobs

# 分析步骤镜像到主代理任务看板
python -m CodeAnalysis.main --tasks --project PJ-A-001 --auto

# 重新生成样本数据(确定性,自校验)
python -m CodeAnalysis.gen_fixtures
```

运行产物默认在 `.codeanalysis/`:状态 `run_*.json`、证据 `evidence/`、报告 `reports/`。

### 环境变量

| 变量 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` | 探索模式与 LLM 归纳所需;不配置时工作流自动模板降级 |
| `CODEANALYSIS_SCOPE` | 授权项目列表(逗号分隔),如 `PJ-A-001,PJ-B-002`;空=全部 |
| `CODEANALYSIS_ENABLE_WRITES` | 置 `1` 向 LLM 探索模式暴露写工具(部署/入库登记) |
| `CODEANALYSIS_RUN_DIR` | 运行产物目录(默认 `.codeanalysis`) |

## 样本数据与评测

`gen_fixtures.py` 确定性生成 4 个项目(覆盖量化指标的部署范围要求),
期望值由**内联独立重实现**写入 `data/expected_assessments.json` 交叉验证:

| 项目 | 语言 | 场景 | 等级 | 主风险 |
|---|---|---|---|---|
| PJ-A-001 科技管理系统 | Java | 环境配置型(生产调试+硬编码凭证),全量部署成功 | D | env_config |
| PJ-B-002 工程技术一体化系统 | C# | 静态漏洞型(MD5/反序列化),部分部署 2/3 模块 | C | static_vuln |
| PJ-C-003 二级单位质量管理系统 | PHP | 部署完整性型(制品被篡改,校验和拦截) | C | deploy_integrity |
| PJ-D-004 数据分析工具集 | Python | 对照组,零命中 | A | none |

`python -m CodeAnalysis.main --eval` 跑 4 个场景,指标:
主风险命中、等级命中、部署状态命中、验证失败项命中、分支排除命中、
证据覆盖、结构合规、步骤效率(4/4 全绿)。

## 研究交付物

`--research all` 从真实配置渲染 3 份交付物到 `data/reports/`:

- `静态代码自评估技术研究报告.md` —— 量化指标 3;
- `自动化代码安全扫描技术研究报告.md` —— 量化指标 4;
- `自动化部署技术规范.md` —— 量化指标 1。

## 测试

```bash
python -m unittest discover -s tests -v    # 全离线
```

## 已知限制(v1)

- 规则引擎为逐行正则匹配,无法识别语义级缺陷(跨函数污点传播),高检出率需接真实 SAST;
- 云平台为模拟适配器,真实部署需实现同一接口契约的云适配器;
- 环境审查为纯 eq 基线比对,未覆盖组合条件与版本区间判断;
- 可能性(likelihood)为规则内常量,未引入 CVSS 完整向量与业务影响因子。
