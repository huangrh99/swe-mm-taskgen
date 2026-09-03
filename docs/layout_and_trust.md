# 附录 C：目录布局、静态校验与信任边界

本文是 [`report/README.md`](../README.md) 的附录，说明三件事：产物放在哪里（存储边界）、
如何机器校验一份提交是否自洽（静态与终局校验），以及这些校验在什么假设下才成立（信任边界）。

## C.1 提交布局

```text
report/
  README.md                 # 唯一权威答卷
  SUBMISSION_CONTRACT.md    # 目录格式硬性契约
  docs/                     # 附录：pipeline 内部机制、布局与信任边界
  pipeline_design.svg       # 造题 pipeline 设计图
  <owner>__<repo>-<number>/ # 正式 Harbor 任务，至少五道
  code/                     # 造题 pipeline 与其测试
  evidence/                 # 紧凑的 curator 侧验收记录
  reproducibility/          # 冻结的模型/依赖/Docker/Harbor 绑定
  schemas/                  # 机器可读的 sidecar schema
  manifests/                # 仓库卫生记录
```

完整的目录格式硬性要求见 [`SUBMISSION_CONTRACT.md`](../SUBMISSION_CONTRACT.md)。

## C.2 存储边界

边界是固定的，三层各有职责：

| 位置 | 存放内容 | 是否随仓库提交 |
| --- | --- | --- |
| `report/` | 正式代码、任务目录、紧凑证据 | 是 |
| `crawler-output/multimodal-2025/` | 完整来源归档与原始编号运行 | 否（本地大文件） |
| `tmp/multimodal-2025/` | 一次性变体与探针 | 否（可丢弃） |

正式任务目录、临时导出、安装探针和失败试验不能混放：只有正式 iid 任务以
`<owner>__<repo>-<number>/` 的形式直接位于 `report/` 之下。

导出 manifest 是 `evidence/export_manifests/` 下的 curator sidecar，**绝不作为文件出现在
agent 任务内部**。README 正文引用的证据都已抽取进 `evidence/`；原始运行路径记录在
README 附录 D，并标注为本地不入库。

## C.3 判分链路与 oracle 质量控制

判分的设计原则见 README 第 1.3 节，逐题的实际取值见 README 第 2 节各题卡片；本节只写六题共用的
可执行细节。

### C.3.1 `tests/config.json`

`tests/config.json` 保存 `repo`、`instance_id`、`base_commit`、**非空且互斥**的
`FAIL_TO_PASS` / `PASS_TO_PASS`，以及 `log_parser`。

### C.3.2 `test.sh` 的判分链路

1. 使用 **`git apply`** 加载测试补丁——兼容二进制 fixture；
2. 运行仓库原生测试命令（容器内一条命令可跑）；
3. 用该 repo 对应的 parser 从测试输出中提取每个测试 ID 的 pass/fail；
4. 若 F2P 全过且 P2P 全过，写 `/logs/verifier/reward.txt = 1.0`，否则写 `0.0`；
5. 未观察到、skip、超时、parser 崩溃和基础设施失败**单独记录**，不静默当作普通模型失败。

导出的 verifier 绑定一份精确有序的测试 ID/类别清单，并在执行前对 verifier、manifest 和清单
取哈希。`pass`、`fail`、`skip`、`missing`、`error` 保持彼此独立；**reward=1 要求每一条冻结的
必需测试都真的执行并通过**。判分脚本崩溃时不写 reward——容器标记异常可重试，绝不静默记 0。

### C.3.3 两个已验证的坑

| 坑 | 规避方式 |
| --- | --- |
| F2P/P2P 用例未在日志中观察到即计为失败 | 测试命令必须真的能跑到清单里的全部用例；`missing` 是独立状态，reward=1 要求全部必需测试实际执行 |
| test_patch 含二进制文件（如图片 fixture） | 应用 patch 必须用 `git apply`，而非 `patch(1)` |

### C.3.4 测试生成合同

测试生成合同要求先输出**实现无关的行为契约**，再为每个新增 bundle 记录等价实现兼容性、非空洞
通过检查和弱表面信号抵抗说明。当前流程会把人工准入的 solver-visible 图片按 SHA-256 绑定后真正作为
VLM 附件传入，并要求测试命令、工作目录和收集根只能来自冻结上下文；缺图片、命令或收集证据时
必须返回上下文不足，不得编造测试。每个相对 import/mock 还必须绑定 packet 中保存的完整源码；
模型只生成完整测试文件，unified diff 由 runner 根据 Base 字节确定性产生，避免重复表示造成坏
hunk。gold diff 只用于理解影响范围与发现测试入口；gold 的私有
class、DOM 层级、函数名、常量、文件组织和调用顺序**不是正确性定义**。当前唯一的测试构造合同、
schema、runner 与实测入口见 README [第 1.3.1 节](../README.md#131-如何约束-vlm-合理补全测试)。

### C.3.5 正式测量与 oracle 质量

`measure-source-tests` 只是快速的源码级预筛。正式的 `tests_measured` 证据由
`record-harbor-measurement` 从**已执行的 Harbor trial** 确定性地产生：至少传入两个不同的
`--baseline-result` 和两个不同的 `--reference-result`。同一份冻结 test patch 必须用于两侧运行，
最终标签只由逐 test ID 的稳定转移生成。**它不调用模型，也不创造合成结果。**

正式测量还必须传入一份 curator-only 的 **oracle 质量记录**，它至少绑定两个反例：

| 反例 | 要求 |
| --- | --- |
| 一个错误/不完整实现 | reward 必须为 0 |
| 一个结构不同但语义等价的实现 | reward 必须为 1，且通过全部冻结 test ID |

第二个反例是防止"judge 只认 gold 形状"的关键控制。校验入口：

```bash
python3 run.py validate-oracle-quality \
  --task <harbor-task-dir> \
  --dossier <dossier.json> \
  --oracle-quality <oracle_quality.json>
```

`mode=real` 的测量若缺少这份通过记录，`export-harbor-task` 会拒绝导出。负例和等价正例只在
构造/审计侧保存，**不进入** solver-visible instruction、图片或 `/app`；正式 Pass@5 仍只运行
冻结的离线确定性测试。

## C.4 防泄漏与运行时隔离

### C.4.1 防泄漏边界

Agent 只看到题面、baseline 代码和 solver 可见的图片；gold patch、隐藏测试、PR 解法材料和审核
记录都不进入 Agent 环境。

确定性的 Harbor 导出器强制执行这条边界：

- 只把 dossier 中已标记安全的 Issue 侧资产复制到 `/testbed/assets`；
- **删除 upstream `.git`，初始化恰好一个全新的 baseline commit，不留任何 remote**——避免从
  Git 历史抄到答案；
- PR 正文、评论、diff、参考代码/补丁、curator-only 图片和隐藏测试一律不给 Agent；
- `solution/` 只对 Harbor 的 oracle 控制可见。

正式运行还要绑定 task checksum、镜像 ID、依赖快照、Harbor 版本和测试清单。Git 历史清理记录见
[`evidence/git_history_scrub.json`](../evidence/git_history_scrub.json)。

### C.4.2 隐藏测试的运行时隔离

导出的任务显式使用 Harbor 的 `separate` verifier 模式。一个 root 所有的 collect hook 先停掉属于
非特权 agent UID 的全部残留非 init 进程，然后把工作区改动对照一个不可变的 squashed baseline
序列化出来。Harbor 传输这份 root 所有的产物，停止 agent 容器，再从 `tests/Dockerfile` 构建一个
全新的 verifier 镜像，隐藏 payload 在其中被写入 `/tests` 下。

因此**作者隐藏测试与期望图片绝不进入 agent 环境**，残留的 watcher 也观察不到它们。题面输入
资产被排除在传输之外——它们不是解题产出。

外层 verifier 仅在打补丁和发布 reward 时保持 root：它先把 `/logs/verifier` 设为 root-only，
再以 UID/GID 10002 运行任务专属的 functional runner，停掉该 UID 的残留进程，最后才写 reward。
`/tests` 在 verifier 镜像中为 root 所有且只读，因此**仓库代码无法替换测试契约或 reward 文件**。

## C.5 Agent 与 provider 的协议边界

任务本身是 schema 1.2 且 `allow_internet = false`，因此任务期间需要的依赖和 agent 工具必须已经
装进镜像。需要远程 provider 的模型评测，必须使用一份**单独记录的 Harbor job/scaffold 网络配置**，
不得改动任务的正式文件或其 checksum。

provider endpoint 必须匹配 agent 的 wire protocol：

- `gemini-cli` 期望原生 Gemini API 契约；一个 Azure/OpenAI 风格的 chat-completions endpoint
  **不会**因为它提供了一个 Gemini 命名的模型就可以互换。
- `kimi-code` 接受自定义模型 base URL，并声明 `image_in` 与 `thinking` 能力。它是对现有内部
  endpoint 首选的官方 agent 探针，需通过安装与单次 trial 的协议检查。
- `mini-swe-agent` 支持 OpenAI 兼容 base URL 与 Responses 路由，但**纯文本 shell 轨迹本身并不
  证明图片字节到达了模型**。在该输入路径拿到证据之前，它不能用于这个多模态试点。

协议/安装失败是基础设施无效记录，**不是失败的修复**。

容器 egress、agent endpoint allowlist 与 provider hosted-tool allowlist 是三道独立边界。正式
trial 的 task container 必须断网，agent 只通模型/鉴权端点；发往 provider 的请求不得注册
`web_search`、browser、`file_search`、remote MCP 或 connector。仅关闭容器公网不能证明服务端
未代替 agent 联网。任一实际远程搜索、浏览、上游源码或答案访问都使该 trial 成为
`invalid_answer_leakage`，无论 reward 为何都不进入有效 trial 分母。

## C.6 静态校验与终局校验

两条命令的性质完全不同，不能互相替代。

`validate-submission` 有意只做静态检查，**永远不能宣称运行时的应试就绪**。它检查的是布局与
文件层面的自洽。

`audit-completion` 是 fail-closed 的机器校验，也是唯一的终局完成声明。它只在以下全部成立时返回
成功：

1. 六个视觉桶各含至少 5 条合格候选；
2. 五道互不相同的 iid 任务已正式冻结；
3. 恰好三次独立的只读审阅，且无未解决的 P0/P1；
4. 完整测试套件与冻结记录就绪；
5. 至少一次真实的五次有效 trial 的 Pass@5 被绑定；
6. 最终 HTML 的哈希匹配。

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=code \
  .runtime/venv/bin/python test.py --evidence

python3 run.py audit-completion \
  --packet evidence/completion_packet.json \
  --output evidence/completion_audit.json
```

该 packet 以哈希绑定类别分布、五份正式任务账本/冻结记录、人工审核记录、完整测试运行、至少一次
真实 K3 Pass@5 审计和紧凑提交 HTML。**当前工作区尚不具备能通过这道校验的证据 packet**，主要缺口
是五次有效 trial 与若干题的控制重跑。

`--evidence` 测试模式原子地发布精确发现的测试 ID、原始日志哈希、Git HEAD、runner 哈希、冻结
哈希和正式清单哈希。终局校验会**重新发现**该清单，而不是接受一个自报的通过数。

不要用仓库根目录的 `unittest discover` 替代 `report/test.py`：工作区保留了历史上的同名包，而
`report/test.py` 有意隔离正式实现的导入路径，同时保持工作区根目录为运行时工作目录，以适配
依赖路径的测试。

## C.7 信任边界

`audit-completion` 是一次**确定性的本地自洽审计**，其前提是"诚实 curator 的工作区"边界。它能
检测缺失、过期、被替换和内部不一致的产物，并同时冻结生产代码与测试代码。

它**不是**密码学意义上的 CI 证明。能够改写每一份证据文件的工作区所有者，同样能够伪造本地的
测试记录或审阅者记录。同理，本地的授权记录与人工审核记录是完整性/审计绑定，不是密码学身份
证明：它们防的是意外漂移和本工作区内的普通 nonce 重放。

审阅者的独立性由保留的 Codex 任务/轨迹来源和人工流程建立，而不是由本地签名建立。能够同时修改
`report/` 与审批产物的敌意写入者在当前信任边界之外。

因此：

- 若部署场景需要抵抗敌意维护者，必须补上外部签名的 CI 与审阅者回执；
- 生产用途必须从用户控制的审核 UI 导入决定，或增加外部签名/操作系统级批准锚点，才能把审阅者
  身份当作已认证；
- 上述两点都在这份离线笔试提交的范围之外。

### C.7.1 三层网络与工具能力验收

模型评测同时冻结三层能力，任一层都不能替代另一层：

1. task/environment phase 的 egress 默认拒绝，不允许 GitHub、raw、npm registry 或任意公网依赖；
2. agent phase 仅允许所选模型与鉴权 endpoint，不允许代码托管、包仓库和搜索服务；
3. provider 请求不注册 `web_search`、browser、`file_search`、remote MCP、connector 或同类 hosted
   tool，Codex/Kimi 的搜索、浏览和远程 MCP 能力显式关闭，不能依赖默认值。

API 与 trajectory 审计只接受题目所需的本地 shell、文件和图片工具。出现
`web_search_call`、browser/computer、`FetchURL`/`WebSearch`、remote MCP/connector、
`file_search`，或运行时抓取上游 PR、commit、tag、release、diff、raw 源码和 reference solution，
统一分类为 `invalid_answer_leakage`；即便 verifier reward 为 `1.0` 也必须作废并补跑。未被调用的
工具定义与实际调用事件分开审计，避免把单纯的 schema snapshot 误报成泄漏。

正式运行前的 capability probe 分别验证：task container 访问 GitHub/raw/npm 失败；agent phase
只有模型 endpoint 可达；CLI 搜索/浏览/FetchURL 不可用；模型响应和落盘 trajectory 不含 hosted
search/browser/MCP 调用。该探针必须保存配置哈希、网络阶段、命令退出状态和安全化的轨迹事件，
且不能用「容器 curl 失败」代替 hosted-tool 检查。未完成三层探针时，只能写
`pending_validation`，不能启动可计分 trial。

## C.8 冻结与清理记录

清理决定记录在 [`manifests/cleanup_migration.json`](../manifests/cleanup_migration.json)。精确的
模型、依赖、schema、Docker 与 Harbor pin 位于独立的可复现性 manifest；**历史观测不等于当前保证**。

两份 manifest 都有可执行的校验器。一个 `partial` 冻结可以描述已知证据，但永远不能把一个可运行
的选定任务证明为已冻结。在当前的 partial 冻结中，已绑定的是精确的源码树基础镜像、它的离线
归档、运行时版本和选定任务清单；仍待完成的是两道人工审核、五次有效模型 trial 和一次干净宿主的
归档重载。
