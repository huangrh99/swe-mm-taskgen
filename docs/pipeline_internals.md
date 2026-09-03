# 附录 B：造题 pipeline 内部机制

本文是 [`report/README.md`](../README.md) 的附录，记录候选召回、来源归档、图片角色分类、
Solver 输入选取、覆盖度审计和多模态必要性与防泄漏审核的**实现层细节**。方案层面的回答在 README 第 1 节；本文只解释这些方案是怎么落地的，以及各阶段的失败语义如何界定。

阅读本文不需要先读代码。所有命令统一以 `python3 run.py <command>` 形式给出，运行环境
见 README 第 3 节。命令中出现的 `crawler-output/...` 与 `tmp/...` 是运行时输出目录，不是随仓库
提交的文件；存储边界见 [附录 C](layout_and_trust.md)。

贯穿全文的一条原则：**技术失败与语义淘汰必须分开记账。** API 限流、下载超时、解码失败、进程
中断都不构成"这个候选不合格"的结论，它们进入有界重试队列并保留失败类型；只有确定性规则或
经过审核的语义判断才能淘汰候选。

## B.0 五个统一操作入口

对外操作收敛为五个 plan-bound 编排命令；本附录后文中的细粒度命令仍是内部实现与故障恢复
interface，不再要求操作者手工串联。五个入口依次是：

```bash
python3 run.py prepare-pr-pool --plan <plan.json> --output <run>
python3 run.py recall-and-archive --plan <plan.json> --output <run>
python3 run.py construct-solver-inputs --plan <plan.json> --output <run>
python3 run.py screen-multimodal-candidates --plan <plan.json> --output <run>
python3 run.py review-visual-gate --plan <plan.json> --output <run>
```

不加 `--execute` 时只校验计划并生成 `stage_manifest.json`；确认后追加 `--execute` 执行。中断或
子命令失败后使用 `--execute --resume`：只有计划 SHA-256 未变化、且声明产物的路径、大小和内容
哈希均与 checkpoint 一致时，成功步骤才会被复用。失败步骤重新执行，已经绑定的成功步骤不重复
调用 GitHub 或模型。

计划使用 [`pipeline_stage_plan_v1.schema.json`](../schemas/pipeline_stage_plan_v1.schema.json)。每个
step 必须声明稳定 ID、内部命令、参数和至少一个预期产物；编排器按高层阶段实施命令白名单，拒绝
把图片分类命令放进 PR 基础池等跨阶段调用。可选 `metrics` 用 JSON Pointer 从已绑定的 JSON
产物读取非负整数，因此每个阶段的 `remaining_prs` 等计数与原始统计文件共同冻结，而不是人工
抄写进日志。例如：

```json
{
  "schema_version": "pipeline-stage-plan-v1",
  "stage": "prepare-pr-pool",
  "steps": [{
    "id": "filter_merged",
    "command": "filter-merged",
    "arguments": ["--input", "tmp/input.jsonl", "--output", "tmp/merged"],
    "outputs": ["tmp/merged/06_merge_default_branch_summary.json"]
  }],
  "metrics": [{
    "name": "remaining_prs",
    "path": "tmp/merged/06_merge_default_branch_summary.json",
    "pointer": "/counts/kept"
  }]
}
```

每个内部命令仍保存自己的语义账本。编排层只把非零退出、缺少声明产物和计数绑定失败记录为
`semantic_rejection=false` 的运行失败，绝不将其改写为某个 PR 的负向分类。VLM 请求、原始响应、
图片角色、视觉必要性和能力标签仍由对应内部阶段分别落盘。人工页面的长驻服务器继续使用
`serve-visual-review` 单独启动；统一的 `review-visual-gate` 只编排有限时的生成、导出和审计操作。

## B.1 候选召回与选取（Stage 06 → 08）

可复现的扩展路径起点是 Stage-06 的 5,084 条 PR：含非装饰性图片，且真实合并进采集时的仓库默认
分支。

`step_08_02_select_cross_repo_candidate_batch.py` 保留最初的 28 条 case，再跨 14 个仓库补齐固定
的 100 条配额。补齐只使用召回信号：Issue 引用、before/after 或 expected/actual 配对、视觉类词
汇、图片数量，以及带种子的哈希 tie-break。**选取器不是准入分类器**，它的冻结 manifest 和逐 PR
账本记录来源哈希、配额、排序信号、原有/新增状态和精确的源行哈希。

静态确认的直接 Issue 引用超过 10 条的 PR 标记为 `temporarily_excluded_over_complex`，写入独立
的排除账本；来源仍然保留，同时由备选 case 补上该仓库的配额。进入这道硬性筛选的只有 `/issues/N`
形式的 URL 和 `Fixes` / `Closes` / `Resolves` 引用；普通 `#N`、PR 引用和文档锚点在来源归档阶段
解析出 GitHub 对象类型之前，只作为召回信号。

### Issue-first 主召回路径

`probe-linked-issue-media` 只使用硬性来源约束：2025 年及以后、真实合并进采集的默认分支、
1 到 10 条显式 linked Issue、排除已有身份、跨仓库配额。它**不检查 PR 的视觉关键词，也不分配
视觉能力类别**。它的输出是一份有界的 Issue 媒体探针，不是视觉候选：

```bash
python3 run.py probe-linked-issue-media \
  --source crawler-output/.../00_01_indexed_prs.jsonl \
  --exclude crawler-output/.../previous_selected.jsonl \
  --output crawler-output/.../08_03_linked_issue_probe/<run> \
  --limit 100 --per-repo 15
```

产出的 manifest 交给 `archive-selection-waves`。归档后的 Issue 材料若含媒体，先由
`classify-pr-images` 评估这些资产，然后才考虑 PR-only 回退。无关图、after-only 图、可被 OCR
替代的图和泄漏答案的图，是在像素和来源都已存在之后才被排除的，不再从 PR 标题去猜。若没有
任何安全的 Issue 资产留下，PR 侧的 `before_only` 证据可以进入人工题面/泄漏队列。manifest 中
显式声明：探针行还不是语义候选。

### 类别定向的次级召回路径

当 Issue-first 池仍留下类别缺口时，`select-balanced-recall` 生成下一个确定性高召回批次。它要求
PR 属于 2025 年及以后、真实合并进采集的默认分支、含 1 到 10 条静态确认的 Issue 引用。PR 正文
含图是正向召回信号但不是必需条件：无图但类别信号强的 PR 进入 `issue_probe_required`，以便来源
归档阶段发现 Issue 图片。

它排除此前选取过的身份。早期版本曾把每条 PR 分配到六个互斥召回桶；该字段现在只作为迁移期的
**采集启发式**，不会被复制为正式能力标签，也不能用于满足配额。正式能力由 V4 在无泄漏输入上
按四项多标签重新判断。每个召回分片的默认单仓库上限是 3，避免一个大型 UI 仓库挤掉其他来源；若合格全集无法满足该
多样性约束，选取器输出显式缺口，而不是静默放宽。部分桶达标后，重复 `--bucket` 只针对未解决的
采集桶扩展，不为了凑成矩形批次而重新扩展已达标的类别。

### 召回入口的排除参数

平衡召回入口支持可重复的 `--exclude-repo owner/repo`。仓库排除**只约束该次及后续的新召回**，
写入 `08_03_manifest.json` 留痕，绝不删除或改写此前已经进入候选池的记录。当前策略是保留已有
Carbon 候选供人工复核与 smoke test，但后续扩充四项能力候选时使用
`--exclude-repo carbon-design-system/carbon`，优先从其他仓库补齐配额。

入口同时支持可重复的 `--exclude-category-audit`：从既有分布审计中读取并校验每条 `source_result`
哈希，再排除所有已经进入分类池的 PR。因此跨召回路径的重复项不会被重新归档。

### 文本 Verifier 的失败重试归并

文本 Verifier 的失败重试通过 `aggregate-text-runs --resolve-retries` 归并。只有题面 packet、
图片索引、绑定 schema 和 case ID **完全一致**时，唯一的更高状态结果才能覆盖旧失败；两个成功
结果互相竞争时直接拒绝。所有尝试路径、状态和 SHA-256 都保存在聚合 manifest。API/格式失败不会
被改写成语义淘汰，也不会为了归并而重复调用已经成功的样本。

### partial 归档的唯一允许条件

在 solver 输入选择处，**只有一种 partial 归档可以继续形成待审提案**：除 `assets` 之外的全部
来源区段（尤其是关联 Issue、closing Issue 和一致性检查）均为 complete，且实际入选的每张
Issue/PR 图片已完整下载并绑定哈希。此时只忽略未入选资源的下载缺口并写入 warning。其他任何
partial 一律留在重试队列。

### 视频抽帧表示

共享媒体入口使用冻结的 FFmpeg 二进制按时长均匀抽取 6 帧，生成按左到右、上到下排列的 3×2
帧拼图，并绑定原视频 SHA-256、衍生图 SHA-256、采样时间点、FFmpeg SHA-256 和版本。图片角色
Verifier 与视觉必要性分类器复用同一表示。帧拼图**只能形成待人工审核的 `temporal_sequence`
候选**；无法解码或未采样到动作的情形作为显式限制保留，不静默淘汰。

### 批次切分与聚合

`step_09_06_run_selected_candidate_waves.py` 和 `step_11_02_archive_selected_candidate_waves.py`
只把尚未覆盖的 case 切成每轮最多 20 条 PR。成功的历史运行经哈希校验后复用。模型失败保持显式，
可以重试而不重复已完成的 case；来源归档保持为独立的不可变运行，因此 GitHub 限流或媒体部分
缺失的状态不会被隐藏。

新 case 完成 Stage 16 后，`step_16_07_aggregate_runs.py` 校验每一个来源运行、结果、packet、
curator 索引、绑定 schema 和决定，然后把结果记录逐字节复制进选定的 100-case 顺序。这个展示层
聚合不产生任何模型调用，可以直接交给既有的人工审核导出器。

## B.2 来源归档与媒体质量（Stage 11）

正式高召回入口先执行 `probe-linked-issue-media --limit 0`。`0` 表示穷举所有满足
`created_at >= 2025-01-01`、真实 merge 到采集时默认分支且可静态关联 Issue 的 PR；这一阶段不看
PR 标题、正文里的视觉关键词，也不要求 PR 自身有图。它只建立待归档集合，尚不声称 Issue 有媒体
或候选在语义上合格。Stage 11 归档关联 Issue 后，有可用 Issue 媒体的条目默认进入图片角色
Verifier；Issue 没有合格媒体时，才把 PR 中的媒体作为 `pr_derived` 补充路径检查。

```bash
python3 run.py probe-linked-issue-media \
  --source <all_merged_default_branch_2025_plus_prs.jsonl> \
  --output <08_03_issue_first_recall> --limit 0
```

有界 `--limit N --per-repo M` 只用于 smoke/预算试跑，不是完整召回证据。

`archive-selection-waves` 在任何 VLM 分类之前，以每批最多 20 条的规模归档哈希绑定的选取结果。
来源或媒体失败保持为 partial/failed 记录，不转化为负向语义标签。

独立批次可以用 `--workers 1..8` 并发抓取；每个批次使用独立的 HTTP 归档和不可变 manifest，
orchestration manifest 恢复确定性的批次顺序。**并发只改变采集延迟，绝不改变候选身份或语义标签。**
每个 PR 内部无需凭据的图片/视频体可独立使用 `--asset-workers 1..8`；SQLite 缓存写入保持串行，
最终资产列表保持来源顺序。受支持的固定调用形式是：

```bash
python3 run.py archive-selection-waves \
  --selection-run <08_03_run> --output <11_archive_root> \
  --orchestration-output <11_02_run_root> \
  --batch-size 5 --workers 8 --asset-workers 4 --fetch --download-media
```

同一条命令写出哈希绑定的 `11_03_archive_quality.json`。由**代码**而非日志检查为每个 PR 判定
`ready_for_image_verifier`、`ready_with_media_gaps` 或 `retry_required`。重复字节/SHA 计为
归一化的重复内容组，API 和瞬时下载失败成为重试记录，任何技术结果都不是语义拒绝。

### 资产下载的重试语义

只对瞬时失败重试：连接重置、超时、响应截断、worker 失败和 HTTP 5xx，采用有界指数退避并保留
逐次尝试的审计记录。确定性失败——404、不安全 URL、尺寸超限、非媒体响应——不做盲目重试。

对已冻结的 Stage-11 运行，`step_11_01_retry_failed_assets.py` 写出仅追加的
`11_01_asset_recovery_manifest.json` 和内容寻址文件，不改写原始记录。Stage 16 在使用该 sidecar
之前，先对照原始 manifest 和恢复文件的哈希做校验。

## B.3 图片角色分类器（classify-pr-images）

这是紧邻 Solver 输入构造之前的 curator 侧阶段。它读取来源归档，按 SHA-256 归一化完全相同的
字节同时保留每一个 URL 与出现位置，再由一个独立的 VLM 把每张图分类为 `before_only`、
`after_only`、`before_after_composite`、`expected_design`、`temporal_sequence` 或 `unclear`。
它同时记录实际缺陷证据、修复后内容、解法泄漏、与任务的关系、裁剪可行性，以及一条
solver 可见性建议。

准备与执行：

```bash
python3 run.py classify-pr-images \
  --archive-orchestration crawler-output/.../11_02_manifest.json \
  --output crawler-output/.../08_04_pr_image_roles_prepare

python3 run.py classify-pr-images \
  --archive-orchestration crawler-output/.../11_02_manifest.json \
  --output crawler-output/.../08_04_pr_image_roles_gemini \
  --run --backend gemini --key-file /path/to/gemini_key_env.sh
```

orchestration manifest 是首选的阶段接口：命令自行展开既有批次与新抓取的批次，校验每个嵌套
manifest 的哈希以及 `11_03` 质量审计，并拒绝重复的 PR 身份。直接传 `--archive` 和
`--archive-manifest` 只作为狭窄的调试接口。

持久化结果是 `08_04_03_results.json`，紧凑的逐 PR 审计页是 `08_04_04_audit.html`。模型的
请求/响应轨迹按 case 保留，但凭据绝不会被复制进结果或 HTML。每个完成的 case 另有一份不可变的
`08_04_03_checkpoints/case_NNNN.json`。

**失败分开记账：** provider/基础设施失败与语义 schema/policy 失败使用各自独立的账本，两者都可能
消耗该阶段的有界重试次数；某个 case 耗尽后成为 `failed`，后续 case 继续执行。输入准备同样按 PR
隔离：归档哈希变化、不安全路径或图片准备异常成为 `input_preparation` 技术失败，不会中断后续
PR。不支持或不可用的像素被路由到重试/视频队列，不发生模型调用。进程本身被中断时，已完成的
checkpoint 与 `08_04_99_interrupted.json` 留在磁盘上，而不是被删除。

只重试失败记录、写入一个新的不可变运行：

```bash
python3 run.py classify-pr-images \
  --retry-from crawler-output/.../08_04_previous_run \
  --output crawler-output/.../08_04_retry_01 \
  --run --backend gemini --key-file /path/to/gemini_key_env.sh
```

复用前一运行的失败归档身份之前，先校验该运行。已完成的记录绝不会被静默覆盖。

在把某个运行用于下游之前，重新校验全部来源、packet、模型尝试、复制的 runner 和渲染审计绑定：

```bash
python3 run.py audit-pr-images \
  --run crawler-output/.../08_04_pr_image_roles_gemini
```

审计通过意味着**被记录的分类器执行在内部是自绑定的**；它不代表批准这些候选图片或 PR 侧题面
可以进入 solver。

## B.4 Solver 输入选取（select-solver-inputs）

从一个或多个已审计的图片角色运行出发，构造下一阶段的提案与显式人工队列：

```bash
python3 run.py select-solver-inputs \
  --image-role-run crawler-output/.../08_04_pr_image_roles_part_01 \
  --image-role-run crawler-output/.../08_04_pr_image_roles_part_02 \
  --output crawler-output/.../08_05_solver_input_selection
```

`08_05_01_issue_derived_selected.jsonl` 只包含来源完整的 Issue-derived before 提案。
`08_05_02_human_followup.jsonl` 保留每一条 PR-derived/both 路径、归档不完整、无候选结果和语义
失败，并附显式原因。被选中的文件是 V3 分类的白名单，**不是人工批准，也不是正式任务准入**。

## B.5 V4 能力分类与覆盖度审计

`classify-capabilities` 只判断四项可多选视觉能力，默认以 10 个 worker 并发调用 Gemini；调用失败按
transport 重试与语义 schema 重试分别留痕，不会变成语义淘汰。它不负责图片角色、泄漏、OCR
可替代性或视觉必要性。

`build-capability-pool` 在计数前重新校验冻结 runner、prompt、schema、配置、图片角色运行、来源
归档、VLM request/provider response/raw annotation 和每个 SHA-256。同一 PR 在每个命中的能力池
各计一次，但配置中只能出现一次。四项各至少 5 个不同 PR 时配额通过。

旧 V3 结果只能经 `convert-v3-capabilities` 的显式确定性映射进入过渡池；该转换不调用模型。
“图形符号与领域语义”没有直接映射：只有既有原子约束明确落入四类时才转换，否则进入人工复核，
不得猜测。紧凑 HTML 展示完整无泄漏题面、实际图片/视频、能力证据、PR 链接以及模型和来源哈希。
当前配置与结果见 README 第 1.4.1 节和
[`evidence/capability_candidate_pool/`](../evidence/capability_candidate_pool/)。

`unify-visual-review` 把旧审核池与冻结 V4 候选池合并成唯一人工审计数据源。旧题保留原 V3 证据并
附加确定性 V4 标签；原生 V4 多标签题不伪造 V3 结果。合并按 `case_id` 去重，V4 标签不进入旧题的
`candidate_binding_sha256`，因此只有题面、solver-visible 资产、代码规模或来源绑定未变化的旧决定
才允许迁移。命令同时生成统一索引、审核 bundle、决定迁移审计和可选的 live config 激活记录：

```bash
python3 run.py unify-visual-review \
  --live-config crawler-output/.../16_04_00_server_config.json \
  --capability-pool crawler-output/.../16_11_capability_candidate_pool/<run_id> \
  --output crawler-output/.../16_14_unified_visual_review/<run_id> \
  --state-root crawler-output/.../16_04_live_review/<run_id> \
  --activate
```

## B.6 多模态必要性与防泄漏审核的实现（render / unify / audit-visual-gate-review）

统一 V3/V4 候选池经由一道**纯视觉**的人工审核。它询问来源路径、一份无泄漏的可编辑题面、文字充分性、
OCR 可替代性、决定性的非文字事实，以及逐图的时序/泄漏/solver 可见性判定。它**有意不包含任何
F2P/P2P 决策字段**；测试校准是第二道独立的人工审核。

```bash
python3 run.py render-visual-gate-review \
  --distribution crawler-output/.../16_03_09_02_category_distribution.json \
  --output crawler-output/.../16_04_visual_gate_review/<run_id>

python3 run.py audit-visual-gate-review \
  --run crawler-output/.../16_04_visual_gate_review/<run_id> \
  --decisions /path/to/16_04_visual_gate_decisions.json
```

生成器先在一个私有 staging 目录中校验每一份 V3 分类、来源结果、来源 packet、归档、题面和图片
哈希。静态 HTML/JavaScript 检查与离线安全检查必须通过，该目录才会被发布。浏览器端草稿使用
local storage；只有通过冻结 schema 与绑定审计的导出 JSON 才算审核证据。即使是一份合法的
`keep` 导出，也仍然只是人工审核证据，**不能单独晋升任务或宣称 benchmark 准入**。

### 审核页的呈现规则

审核页每次只渲染一个 case，带上一条/下一条控制。每个 case 以 GitHub PR 链接开头，随后是可编辑
的 issue-only `problem_statement` 草稿和原始 Issue 图片。草稿中的图片引用被还原为稳定的
`visual material N` 标记，并与下方同样编号的图片说明配对。

curator 侧的原文/中文切换覆盖 PR 标题与草稿；机器翻译是来源绑定的，绝不替代 benchmark 的权威
原文。原文与中文的编辑独立保存。文字/视觉 Verifier 的紧凑摘要默认可见；完整结构化输出与来源
Issue 原文保留在折叠区。编辑保存在本地，与人工决定一同导出。

正式 UI 不要求审阅者身份；审计身份来自绑定的来源 manifest、逐题决定、资产哈希、时间戳和
不可变导出文件哈希。展示任何图片或视觉 Verifier 证据之前，仍须先锁定一条 text-first 判断。

### 文字充分性与来源范围 Verifier

纯文字反事实判断明确隐藏图片、PR 解法、patch 与测试，其 system prompt、schema 和 runner 分别见
[`16_01_text_only_repair_sufficiency.system.md`](../code/analysis/prompts/16_01_text_only_repair_sufficiency.system.md)、
[`16_02_text_only_repair_sufficiency.schema.json`](../code/analysis/prompts/16_02_text_only_repair_sufficiency.schema.json)
和
[`step_16_03_prepare_and_run_text_only_verifier.py`](../code/analysis/scripts/step_16_03_prepare_and_run_text_only_verifier.py)。
父 Issue/来源范围判断另用
[`01_01_source_scope_verifier.system.md`](../code/analysis/prompts/01_01_source_scope_verifier.system.md)、
[`01_02_source_scope_verifier.schema.json`](../code/analysis/prompts/01_02_source_scope_verifier.schema.json)
和
[`source_scope.py`](../code/report_pipeline/source_scope.py)，避免把兄弟或后续 PR 的需求误并入题面。

审核页强制 text-first：审阅者必须先记录纯文字充分性判断，才能揭示任何 Issue 图片，该判断随后被
锁定。当证据可被 OCR 替代时，视觉必要性审核**不能**通过；通过必须引用至少一张绑定图片加一条非文字
视觉事实。测试有效性审核则要求对每一条冻结的 F2P/P2P 测试给出显式的 valid/invalid/unclear 判断
与理由。审核页嵌入图片语义、纯文字可修复性和父 Issue 范围三个 Verifier 的完整 checksum 绑定
结构化输出。

### curator-only 的 PR 修复证据

PR 正文图与 PR 会话图作为 curator-only 的修复证据单独渲染，附未截断的 PR 正文和稳定图片标记。
它们帮助人工理解参考改动，但**绝不进入 agent 可见的 `problem_statement` 或任务图片白名单**。
视频等非图片附件留在来源归档中，走各自独立的审核路径。

`human_problem_statement_required` 队列中的 case 保留上游 Text-only Verifier 的 `ineligible`
事实，同时请 curator 撰写一份不泄漏的题面。它们可以被标为 `needs_human_problem_statement` 保持
pending，也可以在草稿被编辑并审核后获得正常的视觉必要性标签；它们绝不会被自动晋升进最终候选集。

### 历史审核轮次

完整的 28-case 跨仓库筛选已推进到第一道人工边界，冻结产物是
[`evidence/human_review/16_04_human_review.html`](../evidence/human_review/16_04_human_review.html)。
绑定的审核 manifest 记录每个 case 及其结果 SHA：14 条具备完整的文字/视觉 Verifier 输出、等待
实质性的视觉必要性审核；10 条缺少合格的 linked-Issue 题面来源、进入
`human_problem_statement_required` 队列；4 条保留失败的文字 Verifier 结果为
`text_verifier_pending`。**没有候选被静默丢弃。**

高置信度、来源完整的 V3 Verifier 候选可以在人工审核之前进入临时环境与 F2P/P2P 构造；这**只是
节省造题时间，绝不授予正式准入**。两道独立人工审核在晋升之前始终是必需的。该页面是第一轮审核的
历史证据，其 manifest 含状态/队列计数与逐 case 审计索引。

两道人工审核的校准页历史样例见
[`evidence/dual_human_calibration/18_40_review.html`](../evidence/dual_human_calibration/18_40_review.html)，
两道审核的设计说明在 README 第 1.5 节，入选题集总览在 README 第 2.1 节。

## B.7 测试扩展契约（curator-only）

curator-only 的测试扩展契约冻结在
[`20_14_existing_tests_extension_v3.system.md`](../code/analysis/prompts/20_14_existing_tests_extension_v3.system.md)
及其 [schema](../code/analysis/prompts/20_15_existing_tests_extension_v3.schema.json)。

它必须以完整可执行文件和一份 test-only patch 扩展最邻近的既有测试，或者返回一个显式的
"上下文不足"状态。V3 把已准入图片作为真实 VLM 附件传入，并在模型输出后硬校验冻结命令、测试
收集根、parser-visible test ID 与相对 import/mock 的源码证据；test-only patch 由 runner 从完整
文件内容和 Base 字节确定性生成。它给出的 `candidate_f2p` /
`candidate_p2p` 只是**假设**；最终的 F2P/P2P 标签
仍然只能来自在干净 baseline/reference 两臂上对同一测试的重复执行。判分链路见[附录 C.3](layout_and_trust.md#c3-判分链路与-oracle-质量控制)。

## B.8 内部模块与不受支持的调用方式

`code/analysis/scripts/` 和 `code/pr_crawler/` 下的模块是内部实现。直接执行 `step_*.py` 不受
支持，直接 `python -m pr_crawler` 和任意宿主 `python3` 也不是受支持的正式调用形式。唯一受支持
的入口是 `run.py`，它有意把导入隔离到 `code`，防止工作区根目录下同名模块静默成为
被执行的实现。

已废弃的 bpmn-js-only Stage-14/15 适配器及其试验入口有意不在正式快照中；相关历史运行证据留在
`crawler-output/`。

各阶段的语义步骤 ID 与产出边界见 README 第 3 节的阶段表。

## B.9 Verifier 信息输入完整性检查

测试补全 Verifier 不直接读取人工挑选的少量代码片段。正式调用前先用统一接口将 packet 绑定到
Base commit 的精确 git blob：生产代码是 `sut`，相对依赖是 `sut_dependency`，既有测试是
`test_template`，测试配置与 package manifest 是 `test_config`。每个文件都保存内容、SHA-256、
Base blob 是否一致、来源、请求它的依赖边和依赖深度；读取使用 `git show`，不会切换或污染工作树。

```bash
python3 run.py prepare-test-context \
  --packet tmp/input_packet.json \
  --repository tmp/collected-repositories/example \
  --base-commit <BASE_SHA> \
  --output tmp/context-complete-packet.json
```

该命令同时验证冻结测试命令：仓库 package script 必须真实存在，命令引用的配置/脚本保存哈希；
测试收集根、SUT、直接相对 import 和内容哈希缺失均产生结构化 blocker。只有
`repository_test_context.completeness.status=complete` 时，`verify-test-coverage --run` 才允许调用
模型。附近没有可复用测试模板、或命令只能由 curator 冻结而不能绑定到 package script 时保留为
warning，不伪装成仓库原生证据。

依赖展开默认限制为一层：它提供生成测试所需的直接接口证据，而不是把大型前端仓库的整个运行时
依赖树塞进模型上下文；完整可执行性仍由相同 Docker 环境中的 Base/Gold 实测决定。

## B.10 环境构建与批次审计

```bash
python3 run.py build-case-environments --workers 2 \
  --output meta/environment_build.json

python3 run.py audit-case-environments --workers 3 \
  --output meta/environment_audit.json

python3 run.py audit-case-batch \
  --cases-root cases --case-id <instance-id> [--case-id ...] \
  --output meta/case_batch_audits/<audit-run>
```

环境审计会核对内容寻址镜像 ID、精确 base commit、空 remote、干净 worktree、题图哈希，并实际
构建最终 task image，确认 `/testbed` 只有一个清理后的提交。

**候选档案约定：** 小文件随题复制；过大的仓库树或媒体可以内容哈希绑定为 `external_bound`，
但不能仅留一个无哈希路径。`cases/` 下这一区域是 curator 候选档案，**不能因为进入 `cases/`
就标记为 `frozen`**。尚未补齐运行文件的候选保持为 `meta/`-only，不用占位脚本伪装完成。

## B.11 运行审计与轨迹转换

统一审计命令重新扫描各题目录，仅把已经过 `audit-case-pass5` 绑定的 trial 计为有效结果：

```bash
python3 run.py audit-seven-case-runtime \
  --cases-root cases \
  --output evidence/seven_case_runtime
```

未审计的已结束 trial 记为 pending，仍在写入的 trial 记为 running；API、鉴权、安装、Docker 或
Harbor 异常在逐题审计后记为 infrastructure-invalid，均不计入模型失败或五次有效 trial。紧凑审计页
输出到 `evidence/seven_case_runtime/seven_case_runtime.html`。

Kimi Code 不原生写 Harbor ATIF。每个真实 trial 完成后，把原生 `wire.jsonl` 规范化为 Viewer
读取的 `agent/trajectory.json`：

```bash
python3 run.py convert-kimi-trace --source <trial-or-job-results-directory>
```

命令支持单个 `wire.jsonl`、trial、job 或 results 根目录；批量模式分别写入每个 trial 的
`agent/trajectory.json`。原生 wire 是权威记录，ATIF 是带源文件 SHA-256 的展示索引；默认拒绝
覆盖已有轨迹。转换器对已损坏的旧运行失败关闭，不会把猜测恢复写入正式 evidence。

**一个影响冻结配置的约束。** Harbor 0.22.0 会把 secret-classified 环境变量的精确字符串值在全部
文本产物中全局替换。`KIMI_MODEL_MAX_COMPLETION_TOKENS=0` 会破坏大量 JSON；直接写 `131072` 也会
把 Kimi wire 中同值的裸数字替换成非 JSON token。完全省略该变量时，Kimi Code 0.29.0 又会把
1,048,576 的 context 上限误作 completion 上限，被 provider 拒绝。因此冻结配置记录数值
`max_completion_tokens=131072`，传给 Kimi Code 的 Harbor env 则使用等价且可被 `Number()`
解析的 `+131072`，避免与 wire 中的裸数字匹配。正式校验拒绝不安全的普通字符串写法。
