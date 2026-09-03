# excalidraw__excalidraw-9002 · test provenance

Active tests: 2 (F2P 1, P2P 1).

> 来源只说明测试由谁提供；F2P/P2P 分类只来自 Base/Gold 实际执行结果。此索引位于 outputs，未改变 task checksum。

| Class | Source | Test ID | 功能目的 | Purpose |
| --- | --- | --- | --- | --- |
| F2P | curator_revised | [excalidraw__excalidraw-9002::constraint_001] reroutes an elbow arrow when bound text shrinks | 验证缩小绑定文本后会重新计算肘形箭头起点，同时保留绑定关系与正交路径。 | Verify that decreasing bound text size recomputes the elbow-arrow start point while preserving its binding and orthogonal route. |
| P2P | curator_revised | [excalidraw__excalidraw-9002::p2p_text_resize] resizes standalone text itself | 验证同一字号操作仍能正确缩小独立文本。 | Verify that the same font-size action continues to shrink standalone text. |

Source evidence is recorded per test in `test_provenance.json`.
