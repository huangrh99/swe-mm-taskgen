# V3 → V4 能力标签转换

该阶段只复用已经冻结的 V3 分类证据，不调用 Gemini 或其他模型。转换规则是：

- 外观与渲染属性理解 → 渲染外观理解；
- 空间布局与几何理解 → 空间布局理解；
- 元素结构与视觉状态理解 → 元素与状态理解；
- 动态交互与时序理解 → 交互与时序理解；
- 混合视觉能力按既有原子约束拆成多个 V4 标签；
- 图形符号与领域语义没有直接映射。既有约束没有明确落入前四类时标记待复核，不猜测、不补调模型。

标准命令：

```bash
PYTHONPATH=report/code uv run --with jsonschema --with pillow --with openai \
  python report/run.py convert-v3-capabilities \
  --config report/evidence/capability_candidate_pool/16_10_00_target_5_each.config.json \
  --output crawler-output/multimodal-2025/16_13_v3_to_v4_conversion/<run>
```

运行产物固定为：

- `16_13_01_v3_to_v4_classifications.json`：逐 PR 转换状态、标签、原始证据及来源哈希；
- `16_13_02_v3_to_v4_audit.html`：紧凑审计页面；
- `16_13_03_manifest.json`：JSON 与 HTML 的 SHA-256 绑定。

`20260903_direct_conversion_run_01` 的结果为：23 条 V3 输入，17 条完成转换，2 条领域语义项待复核，
4 条旧运行绑定无效而排除；四类计数为渲染 8、空间 6、元素与状态 5、交互与时序 1，共有 3 条
多标签记录。该结果只说明旧 V3 证据能确定性迁移到什么程度；最终目标池仍以 `16_11` 的
`11/10/7/5` 为准。
