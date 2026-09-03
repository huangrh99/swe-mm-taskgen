# 四项视觉能力候选池

本目录只保存可复现配置；大体积运行、图片和审计页位于
`crawler-output/multimodal-2025/16_11_*`。正式能力体系是四项多标签：

- `rendering_appearance_understanding`
- `spatial_layout_understanding`
- `element_state_understanding`
- `interaction_temporal_understanding`

每项按不同 PR 计数，目标均为至少 5。图片内容类型、领域、before/after、泄漏和视觉必要性不是
能力标签，由独立阶段判断。

## 编号产物

- `16_11_00_four_capabilities_target_5.config.json`：19 条 V3 显式迁移基线，分布 `9/8/6/1`。
- `16_11_01_temporal_targeted_v4.config.json`：8 条交互时序定向 V4 调用输入。
- `16_11_02_four_capabilities_target_5.config.json`：最终 23 PR 候选配置；只从定向批次加入 4 条。
- `16_10_00_target_5_each.config.json`：冻结的旧六桶实验配置，仅用于迁移追溯，不是当前接口。
- `16_13_00_v3_to_v4_conversion.md`：冻结 V3 证据到 V4 四类标签的零模型调用转换说明。

## 标准命令

```bash
PYTHONPATH=report/code uv run --with jsonschema --with pillow --with openai \
  python report/run.py convert-v3-capabilities \
  --config report/evidence/capability_candidate_pool/16_10_00_target_5_each.config.json \
  --output crawler-output/multimodal-2025/16_13_v3_to_v4_conversion/<run>

PYTHONPATH=report/code uv run --with jsonschema --with pillow --with openai \
  python report/run.py classify-capabilities \
  --config report/evidence/capability_candidate_pool/16_11_01_temporal_targeted_v4.config.json \
  --output crawler-output/multimodal-2025/16_11_capability_verifier/<run> \
  --run --backend gemini --key-file envs/gemini_key_env.sh

PYTHONPATH=report/code uv run --with jsonschema --with pillow --with openai \
  python report/run.py build-capability-pool \
  --config report/evidence/capability_candidate_pool/16_11_02_four_capabilities_target_5.config.json \
  --output crawler-output/multimodal-2025/16_11_capability_candidate_pool/<run> \
  --required-per-category 5

# 不重新调用模型、不重放已冻结的旧 engine，只从原候选 manifest 生成可筛选视图
PYTHONPATH=report/code python report/run.py render-capability-pool \
  --source-run crawler-output/multimodal-2025/16_11_capability_candidate_pool/<frozen-run> \
  --output crawler-output/multimodal-2025/16_11_capability_candidate_pool/<view-run>
```

Gemini 默认并行度为 10，可用 `--workers 1..16` 覆盖。最终运行重新校验 source archive、图片角色
结果、solver-visible packet、模型请求/响应、prompt/schema/runner 和全部哈希，再生成便携视觉资产、
JSON、HTML 与 manifest。

## 当前结果

`20260903_four_capabilities_target_5_run_01`：23 个不同 PR，8 个多标签 PR；分布为
`11/10/7/5`，四项缺口均为 0。所有记录仍是 `pending_human_visual_gate`，候选配额通过不等于正式题
准入。

`20260903_four_capabilities_target_5_view_02` 是对上述冻结池的零模型调用派生视图。它校验原
manifest、候选 JSON、44 个展示资产及可重算计数，然后提供“全部/四类能力”交互筛选；派生 manifest
显式记录 `model_invoked=false` 和原 manifest 的路径与 SHA-256。

冻结 V3 直接转换运行 `20260903_direct_conversion_run_01` 未调用模型：23 条输入中 17 条完成转换，
2 条旧“图形符号与领域语义”记录进入待复核，4 条因旧运行证据绑定不完整被排除。转换后的四类
覆盖为 `8/6/5/1`；它是迁移审计结果，不能替代上面的最终 V4 候选池计数。
