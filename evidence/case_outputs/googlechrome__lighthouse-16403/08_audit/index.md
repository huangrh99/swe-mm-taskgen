# googlechrome__lighthouse-16403 · test provenance

Active tests: 5 (F2P 4, P2P 1).

> 来源只说明测试由谁提供；F2P/P2P 分类只来自 Base/Gold 实际执行结果。此索引位于 outputs，未改变 task checksum。

| Class | Source | Test ID | 功能目的 | Purpose |
| --- | --- | --- | --- | --- |
| F2P | curator_revised | LHM-16403-TITLE | 验证渲染后的标题字形与 Logo 尺寸符合视觉契约。 | Verify the rendered title typography and logo size match the visual contract. |
| F2P | curator_revised | LHM-16403-CAPTION | 验证移除聚合根节点标题，并分别保留清晰可读的 bundle 名称与数值样式。 | Verify the aggregate root caption is removed and each bundle caption retains separate, readable name/value styling. |
| F2P | curator_revised | LHM-16403-COLOR | 验证 bundle 使用不同的柔和色系、同一色相内的后代节点逐层加深，且聚合视图不显示边框。 | Verify bundles use distinct pastel families, descendants darken within the same hue, and aggregate-view borders are removed. |
| F2P | curator_revised | LHM-16403-SELECTION | 验证选择 bundle 后展示其详情，同时页头仍保留报告总量。 | Verify choosing a bundle reveals its details without replacing the report-wide total in the header. |
| P2P | curator_revised | LHM-16403-TABLE-P2P | 验证既有资源表仍完整显示 fixture 中的叶节点及字节数。 | Verify the existing resource table remains populated with all fixture leaves and byte values. |

Source evidence is recorded per test in `test_provenance.json`.
