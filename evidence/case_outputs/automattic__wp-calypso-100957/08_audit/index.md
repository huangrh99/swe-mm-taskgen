# automattic__wp-calypso-100957 · test provenance v4

Active tests: 4 (F2P 2, P2P 2).

> v4 只检查最终渲染中两个圆点分别与卡片背景达到至少 `2:1` 的区分，不限定颜色字段、palette 排序、helper 或代码结构。阈值来自人工校准；F2P/P2P 分类仍需由本版 Base/Gold 实测确认。

| Class | Source | Test ID | 功能目的 |
| --- | --- | --- | --- |
| F2P | curator_revised_from_screenshot_evidence | GlobalStylesVariationPreview visual acceptance contract separates both swatches from the light card background shown in the PR | 浅色卡片保留两个圆点，且两个圆点分别与背景可辨识。 |
| F2P | curator_revised_from_screenshot_evidence | GlobalStylesVariationPreview visual acceptance contract separates both swatches from the lilac card background shown in the PR | 淡紫卡片保留两个圆点，且两个圆点分别与背景可辨识。 |
| P2P | curator_revised | GlobalStylesVariationPreview visual acceptance contract preserves the configured preview background color | 保留卡片背景色。 |
| P2P | curator_revised | GlobalStylesVariationPreview visual acceptance contract preserves the configured heading color used by the title frame | 保留标题颜色。 |

截图测量见 `../12_contrast_verifier_v3/12_02_screenshot_measurements.json`；人工阈值决定与本版执行证据见 `../14_contrast_verifier_v4/`。
