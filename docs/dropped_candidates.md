# 已淘汰候选记录

本文件保存未进入正式 IID 题集的历史候选及其淘汰原因。相关目录和运行证据继续保留，避免在候选
淘汰后改写当时的构造过程。

## `carbon-design-system__carbon-22019`

历史材料位于 `cases/carbon-design-system__carbon-22019-drop/`，不计入正式 IID 题集，也不参与
Pass@5。

淘汰原因：范围较宽的关联 Issue #21567 要求 Search 和 Clear 两个控件统一使用 tooltip，而
PR #22019 只修改折叠状态下的 Search 触发器。范围较窄的重复 Issue #21572 虽然与该 PR 匹配，
但文字直接指出 `.cds--icon-tooltip`，不依赖视觉输入也能确定修复方向。因此，这两个来源无法同时
满足“题面忠实对应 PR 范围”和“视觉信息不可由文字替代”。

目录使用 `-drop` 后缀标记淘汰状态；内部 `instance_id`、任务文件、checksum 和历史输出保持原样。
