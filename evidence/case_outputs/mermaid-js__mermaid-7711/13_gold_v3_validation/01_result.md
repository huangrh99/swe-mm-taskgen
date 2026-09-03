# Mermaid 7711：Gold v3 与 Codex 回放

## 结论

Gold 已修正。新 verifier 对同一条 Issue 复现输入得到：

| 被测状态 | reward | F2P | P2P | 结论 |
| --- | ---: | --- | --- | --- |
| Base | 0.0 | 失败 | 17/17 通过 | 正确的负向控制 |
| 旧 Gold v2 | 0.0 | 失败 | 17/17 通过 | 自环仍在节点下方，不满足 Issue |
| 修正后的 Gold v3 | 1.0 | 通过 | 17/17 通过 | 合格 |
| 当前 Codex Pass@1 patch 回放 | 1.0 | 通过 | 17/17 通过 | 合格 |

这次不是根据 Codex 输出倒推断言：先冻结 v3 行为契约，并用它确认 Base 和旧 Gold 都失败；随后才用同一份测试回放新 Gold 与 Codex patch。

## verifier 验收对象

测试最终渲染出的 `stateDiagram-v2` SVG，而不是源码文本或 Gold 的控制流。自环需要：

- 位于状态节点左侧或右侧，左右都允许；
- 除端点外不进入节点内部；
- 约每 1 px 采样，相邻采样段最大转角不超过 45°；
- 仍为一个逻辑自环并保留 `Self Edge` 标签。

不检查精确坐标、尺寸、SVG `d` 字符串或特定实现方式。flowchart 自环和普通边只作为 P2P 回归项。

## 两种通过实现的几何差异

| 指标 | Gold v3 | Codex |
| --- | ---: | ---: |
| 侧边采样比例 | 右侧 1.0 | 右侧 1.0 |
| 进入节点内部 | 否 | 否 |
| 最大局部转角 | 7.43° | 4.90° |
| 标签存在 | 是 | 是 |

Gold v3 合并原来的三段 dummy route，并明确为 state diagram 选择右侧；Codex 则让普通节点自环直接交给 Dagre 原生布局。两者源码策略不同、SVG 路径和节点坐标也不同，但都满足相同视觉行为，因此该 verifier 没有要求模型复刻 Gold。

## 证据

- [`00_summary.json`](00_summary.json)：机器可读汇总与文件哈希。
- [`red_base/logs/verifier/test_results.json`](red_base/logs/verifier/test_results.json)：Base reward=0。
- [`red_old_gold/logs/verifier/test_results.json`](red_old_gold/logs/verifier/test_results.json)：旧 Gold reward=0。
- [`gold_final/logs/verifier/test_results.json`](gold_final/logs/verifier/test_results.json)：新 Gold reward=1。
- [`codex_replay/logs/verifier/test_results.json`](codex_replay/logs/verifier/test_results.json)：Codex reward=1。
- [`codex_pass1.patch`](codex_pass1.patch)：从原 Pass@1 trajectory 中提取并回放的 Codex patch。
- [`archive_v2/`](archive_v2/)：替换前的 Gold 与 verifier 文件。

本轮修改改变了正式测试和 Gold 的 checksum；此前冻结记录及 Pass@1 结果只能作为历史观测。若该题继续进入正式 Pass@5，需要基于当前文件重新生成冻结 manifest，但无需为了本次功能对比重复运行模型。
