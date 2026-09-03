# 推进记录（不随最终提交）

**本文不是笔试答卷的一部分。** 它保留推进过程中的历史观测、被取代的工具状态和已知环境问题，供
内部追踪；最终提交时整个文件删除。答卷正文在 [`README.md`](../README.md)，当前生效的实现层机制
在[附录 B](pipeline_internals.md)。

## 历史控制记录与为何不能计入

紧邻当前状态之前的那份物料 checksum 曾在 Harbor 0.22.0 上跑通两项官方控制：`nop` 返回 0.0，
`oracle` 返回 1.0，无异常。此后任务的 Dockerfile 与 instruction 已发生变化，因此
`evidence/oracle_summary.json` 只作为历史证据保留，**不能证明当前 checksum**，汇总中明确标为
`historical_task_checksum_rerun_required`。

同一份历史 checksum 也通过了全部十项强化的负向与隔离控制。该审计要求精确有序的测试 ID/类别、
详细结果与汇总一致、精确的失败类别、异常匹配，以及逐控制的原子 checkpoint。它不能证明后来变化的
任务。Docker 已停止的运行和被取代的代码运行被显式排除在外。

**代码层与交付层的区分。** 最新一次完整流程测试为 453/453 通过
（`evidence/final_full_test_run.json`）。这只证明代码层测试通过，不证明已交付 5 道正式题。
冻结清单中 `formal_promotion_ready.status` 仍为 `blocked`，原因是：安装快照还不是 clean
transitive hash lock，且正式候选尚未通过人工审核完成逐题模型/镜像晋升。Docker client 29.6.1、
daemon 29.5.2、Compose 5.2.0、Harbor 0.22.0 与 task schema 1.2 已现场观测并冻结。

**已知环境问题。** 大型源码归档、临时目录与容器缓存不随提交；批量构建前仍需检查 Docker
overlay、地址池和宿主磁盘，而不是把资源失败记成模型失败。

## 被取代的人工审核界面状态

人工交付使用动态的一题一页服务，而不是把候选数组冻结在 HTML 中。服务每次加载都重新读取 config
指向的类别分布，验证或复用按分布 SHA 寻址的审核 bundle，并只返回其中 `counted=true` 的候选。

combined-171 页面只用于迁移既有审核记录；正式四项候选池以 README 第 1.4.1 节的统一审计页为准。
默认 config 严格绑定 combined-171 的 19 条历史候选，仅供人工界面迁移与既有标注复用，它不再是
正式能力配额来源。

**UI 字段迁移记录：** 审核界面把「图片提供的不可替代非文字事实」和「结论理由」合并成一个
「判断依据」输入框。旧草稿加载时去重合并，保存时把同一输入镜像到旧 schema 的两个兼容字段，因此
既保持单一人工输入，也不破坏已生成的审核记录。

## 流程模拟 dry-run

冻结后的工作流已在**不调用外部模型**的情况下走通一遍：使用 mock 审核记录、模拟控制、nop/oracle
风格轨迹、一次被替换的 API 失败，以及五条有效 trial。模拟输出被限制在 `tmp/` 与
`crawler-output/`；**mock 批准永远不能写出正式任务**。审计页显著标注 `SIMULATION ONLY`。

冻结产物在 `evidence/simulation_dry_run/`：晋升账本、冻结 manifest、Pass@5 汇总、重算的汇总审计
和紧凑 HTML 审计。它证明的是「状态机与拒绝路径可执行」，**不是**任何真实的 oracle 或 Pass@5
结果。

## 抽取证据的原始运行路径

`evidence/` 下的证据都是从原始编号运行里抽取的。原始运行位于 `crawler-output/` 与 `tmp/`，两者
不随仓库提交，因此下表只供内部回溯。

| 抽取位置 | 原始运行路径 |
| --- | --- |
| `evidence/human_review/` | `crawler-output/multimodal-2025/16_visual_necessity_selection/20260901T075905892457Z/` |
| `evidence/dual_human_calibration/` | `crawler-output/multimodal-2025/18_visual_harbor_pass5/18_40_dual_human_calibration_ui/` |
| `evidence/simulation_dry_run/` | `crawler-output/multimodal-2025/19_pipeline_state_machine/19_29_final_frozen_dry_run/` 与 `19_30_final_frozen_dry_run_pass5/` |

## 首次试运行暴露的运行时网络边界错误

第一次 breadth-first 试运行暴露了运行时网络边界错误：Harbor 的
`environment.extra_allowed_hosts` 贯穿整个 trial，并非只供镜像构建。BPMN 的 reward `1.0` 轨迹
实际访问了 GitHub/raw 上游实现，因此分类为 `invalid_answer_leakage`，不能计入 Pass@1/Pass@5；
随后五道并行 trial 已中止并按基础设施无效单独记账。该泄漏 trial 不进入任何 Pass@1/Pass@5 统计。

由此确立的正式配置要求已写入答卷：task container 的 environment allowlist 为空，agent 仅能访问
各自模型与鉴权端点，依赖必须在 trial 之前的 Docker build 阶段完成。三道独立边界的验收口径见
[附录 C](layout_and_trust.md) 的 C.4.1 与 C.7.1。

## 交卷前仍未完成的项

README 正文不保留待办清单；各处缺口在原位已就地说明（卡片里写明尚不可计算、控制表标注待重跑）。
这里是集中清单，供内部追踪。

| # | 未完成项 | 现状 |
| --- | --- | --- |
| 1 | 执行正式 Docker/manifest 冻结 | 结果待填 |
| 2 | 授权的五次有效 trial Pass@5 | Kimi K3 六题均无有效 Pass@5 trial（API 限流与 endpoint 配置失败）；GPT-5.6 Luna Max 仅 `googlechrome__lighthouse-16403` 尚不可计算，只有三条有效。基础设施失败不计有效 trial |
| 3 | 至少两条真实失败轨迹分析 | 依赖第 2 项 |
| 4 | 重跑 `automattic__wp-calypso-99049` 当前测试 checksum 的控制 | 现只有 Base=0、Gold=1、旧 Codex patch=1 的单轮三态复核 |
| 5 | 重跑 `automattic__wp-calypso-100957` 当前测试 checksum 的 Harbor 控制 | 人工将阈值校准为 `2:1` 后，v4 已直接复核 Base=0、Gold=1；正式 task checksum 下的 oracle、empty、nop、empty-no-reply 尚待重跑 |
| 6 | 补强 `wp-calypso-100957` 的列表层防退化覆盖 | 当前组件测试能阻止删成一个圆点，但尚未单独断言整张 style variation 不得从父列表消失 |
| 7 | 重跑 `mermaid-js__mermaid-7711` v3 checksum 的四道 Harbor 控制 | Base=0、修正 Gold=1 已直接回放；nop、empty-no-reply 与正式冻结尚未按 v3 checksum 重跑 |
| 8 | 整理 Mermaid solver-visible 视觉资产清单 | 静态校验发现 `asset_02.png` 至 `asset_06.png` 未被题面引用；需确认后移入归档或补充引用，不直接删除原文件 |

两项已经定案的范围决策：ood 只交造题方案、不交 ood 实例题，README 第 1.6 节按此表述；「最强
frontier model 的 Overall Pass@5 不为 1」这条难度硬指标暂缓，第 1.4 节的难度判定目前不含这一条。
