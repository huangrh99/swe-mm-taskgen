# OOD 候选仓库静态核验摘要

核验时间：2026-09-01。该材料是仓库级题源调研，不是已完成的 OOD 实例清单。候选仓库尚未经过
逐 PR 的视觉必要性、Base/Gold、Harbor controls 或 Pass@5 准入。

## 同语言、跨仓库候选

这些仓库不在 SWE-bench Multimodal 的来源仓库集合中，主要实现仍是 JS/TS，适合构造
`repo shift`，同时尽量保持语言和前端任务类型不变。

| 优先级 | 仓库 | 视觉任务形态 | 已核验的测试或环境基础 | 主要风险 |
| ---: | --- | --- | --- | --- |
| 1 | `bytedance/flowgram.ai` | workflow 节点、连线、缩放、拖放、布局 | Rush lock；free/fixed-layout Playwright | 需锁定画布几何、字体和交互时序 |
| 2 | `readest/readest` | EPUB/PDF 排版、分页、主题、字体 | pnpm lock；官方 Docker；web Playwright | Tauri、字体、WASM 和文档 fixture 较重 |
| 3 | `dyad-sh/dyad` | Electron editor、preview、组件截图 | package-lock；Playwright；fake LLM | Electron/native addon；只能使用离线 fake 服务 |
| 4 | `simstudioai/sim` | workflow canvas、block、edge、run state | Bun lock；Vitest；desktop Playwright | 完整应用依赖数据库、Redis、认证和 integrations |
| 5 | `DayuanJiang/next-ai-draw-io` | draw.io 画布、节点、连线和布局 | package-lock；官方 Docker；Playwright | draw.io iframe 与模型接口必须本地化和固定版本 |
| 6 | `onlook-dev/onlook` | selection box、拖放、responsive、style panel | Bun lock；Storybook/Vitest-browser | 完整 editor 依赖数据库、sandbox 和协作服务 |

补充候选包括 `OpenCut-app/OpenCut`、`CyberTimon/RapidRAW`、`cosscom/coss`、
`ahmedkhaleel2004/gitdiagram`、`stagewise-io/stagewise`、`twentyhq/twenty` 和
`assistant-ui/assistant-ui`。前两者的媒体、GPU 或原生依赖较重；其余仓库应优先截取可由
Storybook、固定 JSON 状态、fake transport 或本地 renderer 独立复现的 UI 子路径。

## 跨语言或渲染栈候选

这些仓库用于构造 `language/runtime shift`。最终归类必须检查目标 PR 的生产代码 patch，不能只按
GitHub 展示的仓库主要语言归类。

| 仓库 | 目标实现语言 / 渲染领域 | 已核验的验证基础 | 主要冻结要求 |
| --- | --- | --- | --- |
| `matplotlib/matplotlib` | Python；科学绘图与文字布局 | pytest image comparison | 固定字体、FreeType、backend 和容差 |
| `Kozea/WeasyPrint` | Python；HTML/CSS 到 PDF 排版 | 像素结果断言 | 固定 Cairo/Pango、字体和 PDF 资源 |
| `typst/typst` | Rust；文档、表格、公式排版 | PNG reference 与 image diff | 固定 Rust、字体、renderer 和 reference assets |
| `fyne-io/fyne` | Go；桌面/移动 GUI | 虚拟窗口、软件绘制和图像断言 | 排除依赖真实 GPU/窗口系统的任务 |
| `AvaloniaUI/Avalonia` | C#/XAML；跨平台 UI | headless 测试，可启用 Skia 绘制 | 固定 .NET、Skia、字体和真实绘制模式 |
| `flutter/flutter` | Dart；widget 布局、滚动、裁剪 | widget test 与 golden comparison | 固定 Flutter engine、Dart、字体和平台 |

## 当时采用的仓库级筛选

1. **排除来源仓重叠**：候选仓库名不得出现在 SWE-bench Multimodal 来源仓库集合中。
2. **保留时间证据**：优先选择 2024-10-04 之后创建的仓库；更早创建的仓库只有在主要 Issue/PR
   活跃发生于该日期之后时才进入补充池。正式任务仍执行与 IID 相同的 `created_at >= 2025-01-01`。
3. **要求真实视觉修复面**：仓库需要存在布局、渲染、图形、编辑器、交互或时序类用户可见行为，
   而不是只有 landing page 或新增模板。
4. **检查候选产量**：查看公开 Issue/PR 规模以及 Issue、PR 正文中出现截图、GIF、视频和
   before/expected 证据的可能性。该检查只决定是否值得爬取，不代替逐 PR 视觉必要性判断。
5. **检查可执行验证基础**：优先有 lockfile、单元测试、Playwright/Cypress、Storybook、golden 或
   image-comparison 入口的仓库；“存在测试目录”不视为 Base/Gold 已跑通。
6. **检查离线 Harbor 可行性**：外部模型、CDN、数据库、认证、云截图服务和第三方 editor 必须能被
   固定资源或本地 fake 替换；否则只选择可独立运行的前端子树，仍无法隔离的仓库降级或排除。
7. **逐 PR 重新走共同漏斗**：merged/default-branch、Issue-first 媒体归档、防泄漏、视觉必要性、
   F2P/P2P 实测、empty=0、gold=1、离线复现和稳定性要求与 IID 完全相同。

仓库创建时间晚或 repo-name 不重叠，只能证明来源仓分布发生变化，不能证明不存在任务模式重叠、
代码继承或训练数据污染。正式报告应把这些风险与 OOD 归属分开记录。
