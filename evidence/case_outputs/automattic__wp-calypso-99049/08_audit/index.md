# automattic__wp-calypso-99049 · test provenance

Active tests: 3 (F2P 1, P2P 2).

> 来源只说明测试由谁提供；F2P/P2P 分类只来自 Base/Gold 实际执行结果。此索引位于 outputs，未改变 task checksum。

| Class | Source | Test ID | 功能目的 | Purpose |
| --- | --- | --- | --- | --- |
| F2P | verifier_generated | WPC-99049-LINK-COLOR | 编译真实 SCSS，并通过 Chromium 的 getComputedStyle 验证“+ Add forward”解析为 --color-link。 | Compile the real SCSS and use Chromium getComputedStyle to verify that + Add forward resolves to --color-link. |
| P2P | verifier_generated | WPC-99049-P2P-COLOR-SCOPE | 验证无关的 link-button 保持原有计算颜色，防止修复范围过宽。 | Reject an over-broad implementation by checking that an unrelated link-button keeps its original computed color. |
| P2P | repository_existing | WPC-99049-P2P-DOMAIN-OVERVIEW | 运行仓库既有 DomainOverviewPane 测试套件，保护原有渲染与交互行为。 | Run the existing DomainOverviewPane test suite to preserve previously supported rendering and behavior. |

Source evidence is recorded per test in `test_provenance.json`.
