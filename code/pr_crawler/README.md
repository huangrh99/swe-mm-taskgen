# GitHub PR 采集器

整套流程的当前状态、筛选规则、输入输出及变更记录统一维护在 [00 · Pipeline 统一文档](../analysis/00_pipeline_index.md)。本 README 说明采集器的详细用法。

只读采集 GitHub.com 上 API 可访问的 PR，为后续 SWE-bench Multimodal 类数据集保留原始证据。支持所有状态、时间筛选、独立分页、断点恢复、快照刷新，以及附件清单和可选下载。**不执行来源仓库代码，也不生成已验证的 benchmark。**

## 运行

这是正式 pipeline 的内部库。从本工作区根目录通过隔离入口执行；直接
`python -m pr_crawler` 会被拒绝：

```bash
VERIFIER_PYTHON report/run.py collect --help
```

认证优先读取 `GH_TOKEN`，其次 `GITHUB_TOKEN`，最后尝试本机已登录的 `gh auth token --hostname github.com`。不要把 token 写进命令参数或输出文件。公开 REST 数据可匿名读取，但限额较小；GraphQL 关联 Issue 和线程查询需要有效认证。认证失败会记录不可访问状态，不能算完整采集。

### 完整索引与有界详情

```bash
# 遍历全部 open/closed/merged/draft PR；两遍核对，不使用受搜索结果上限限制的枚举。
VERIFIER_PYTHON report/run.py collect index PrismJS/prism --output crawler-output/prism

# 大仓库可并发读取 4 个独立索引页面（也支持 2 或 8）；每页仍单独保存，默认是串行。
VERIFIER_PYTHON report/run.py collect index owner/large-repo --output crawler-output/large --page-workers 4

# 将上一步输出的 Run ID 替换 SOURCE_RUN。
# 使用已保存索引，按 PR 创建时间筛选，仅为命中项采集详情。
VERIFIER_PYTHON report/run.py collect enrich --output crawler-output/prism \
  --source-run SOURCE_RUN --start 2018-01-01 --end 2019-01-01

# 小规模验证：显式限制 PR，不改变源索引完整性。
VERIFIER_PYTHON report/run.py collect enrich --output crawler-output/prism \
  --source-run SOURCE_RUN --pr 'PrismJS/prism#1500' --download-assets
```

### 从索引到详情一次运行

```bash
VERIFIER_PYTHON report/run.py collect crawl owner/repo owner/another-repo \
  --output crawler-output/batch --axis merged_at \
  --start 2023-01-01 --end 2024-01-01

# 不带时间范围意味着所有已索引 PR 的详情，可能有大量请求与磁盘开销。
VERIFIER_PYTHON report/run.py collect crawl owner/repo --output crawler-output/full
```

### 恢复、刷新和离线使用

```bash
# 恢复同一 Run ID，沿用原参数及已落盘成功响应。
VERIFIER_PYTHON report/run.py collect resume --output crawler-output/prism --run RUN_ID

# 再次运行 index/crawl/enrich 创建新 Run ID，重新观察可变记录；旧快照保留。
# enrich 使用旧索引但重新抓详情；要刷新 PR 列表和筛选时间字段，先新建 index/crawl。
VERIFIER_PYTHON report/run.py collect index PrismJS/prism --output crawler-output/prism

# 纯离线筛选：输出 JSON 到标准输出，不访问 API 或读取认证。
VERIFIER_PYTHON report/run.py collect select --output crawler-output/prism --run SOURCE_RUN \
  --axis updated_at --start 2020-01-01 --end 2021-01-01

# 纯离线导出报告及标准化 JSON；API 原始响应仍保存在 SQLite 中。
VERIFIER_PYTHON report/run.py collect report --output crawler-output/prism --run RUN_ID
```

同一 run 的恢复不是“观察现在”的刷新：成功请求会复用，失败请求可重试；新 run 会重读子资源，即使父 PR 的 updated_at 没变。恢复期间观察时间可能跨越多次执行，见每条响应 fetched_at。需要一致的新观察窗口时，开启新 run。

详情终检的缓存键绑定其材料的 response ID 集合哈希（material_fingerprint）。补取页面、终检失败或在终检前中断，均不能退回到旧材料的成功检查；检测到 PR 版本漂移时保持 partial，并用新 run 重新观察。

默认时间轴 `created_at`，可选 `updated_at`、`merged_at`。范围统一为 UTC 的 `[start,end)`，开始包含、结束不包含；单边可省略。日期 `2024-01-01` 表示 UTC 零点；完整时间必须携带 `Z` 或偏移，例如 `2024-01-01T08:00:00+08:00`。不接受无时区完整时间、非法日期或 start >= end。merged_at 为 null 的未合并 PR 不命中 merged_at 筛选。指定 `--pr` 时仍需满足时间范围，找不到的编号记录为缺失，不静默跳过。

## 输出与字段

```text
OUTPUT/
  archive.sqlite3              # 主档案；WAL/SHM 文件可能在连接期间存在
  assets/<sha256>               # 可选下载的二进制内容，使用哈希命名
  exports/<run-id>/
    report.json                # 机器可读完整性摘要
    report.md                  # 人可读摘要
    selection.json             # 时间/显式选择参数和选中编号
    index/<owner>/<repo>.json   # 完整索引及两遍核对状态
    pr/<owner>/<repo>/<n>.json  # 标准化 PR 记录
```

SQLite 是恢复的权威来源，JSON 是可重新生成的导出。备份时关闭采集器再复制目录，或使用 SQLite 官方备份机制，不要在写入期间只复制主数据库文件。一个输出目录请只运行一个写入采集进程。

| SQLite 表 | 内容 |
| --- | --- |
| metadata | 档案 schema_version |
| runs | 参数、开始/结束时间、运行状态 |
| responses | 原始响应字节、SHA-256、API 路径和查询、响应状态、安全响应头、fetched_at；失败 HTTP/GraphQL 响应也保留 |
| documents | 按 run 保存索引、选择结果、标准化详情和附件恢复状态 |
| document_chunks | 大索引每 500 条 PR 分块；与元数据同一事务提交，避免 SQLite 单值约 1 GB 的默认上限 |

每次 API 响应入库提交后才交给上层。恢复重复遍历已存分页时读取缓存，未成功页重新请求；不依赖“文件存在即完成”。同一 run 中标准化文档可能随恢复重建，原始响应历史不丢失；不同 run 的文档与原始响应相互独立。

大索引在 SQLite 的 documents 表中可能以 `_storage=chunked-index-v1` 元数据引用 document_chunks。通过 Store.get/documents、CLI select/report 或验证脚本读取会自动还原完整 items；JSON 导出的外部结构不变。并发索引沿 GitHub Link 的 last 页界限读取，支持 GitHub 将 owner/name 路径规范化为数字 repository ID；页数变化或失败均标明 partial。大量仓库建议分仓库存储，避免一个进程同时持有所有仓库的大索引。

PR 标准化顶层包含 `schema_version`、`api_version`、`instance_id`、`repo`、`number`、`collected_at`、`status`、`sections`、`provenance` 与 `derived`。provenance 的 response_ids 可定位 responses 表，逐条校验原始字节哈希和采集时间。

主要 sections：

- `pull_request`：GitHub 原始 PR 元数据，包括正文、作者、状态、draft、合并信息、base/head、SHA 和时间。
- `labels`、`comments`、`reviews`、`review_comments`：各自独立翻页。
- `commits`、`files`、`diff`、`patch`：原始变更证据；文件缺少 patch 标记为 `missing_binary_or_omitted`，不能武断认定为二进制文件。
- `closing_issues`：GitHub 报告的关闭关系，包含已关闭 Issue。
- `review_threads`：线程解决/过时状态、代码位置及独立分页的线程评论。
- `linked_issues`：关闭关系及 PR/commit/讨论中的文本引用；保留来源、关系类型、Issue 或 PR 身份，以及正文/评论/标签。普通引用不等于修复了该 Issue，不递归爬取无限引用网络。
- `assets`：多模态 URL 与出处；下载状态、错误、字节数、媒体类型、哈希和本地路径。
- `consistency`：详情边界重读结果。并不保证评论、标签等子资源是原子快照。

### 完整性与退出码

状态区分 `complete`、`partial`、`error`、`unavailable` 和 `not_requested`。空列表可以是确实没有，也可能请求失败，必须一起读取状态和原因。摘要报告提供选择数量、实际详情数量及非完整 sections；细节见标准化记录和原始响应。

- 退出 0：本次显式选择的请求范围完成；离线 select/report 操作成功。
- 退出 2：采集存在 partial/error/unavailable 或显式选择缺失。
- 退出 130：用户中断，已提交页面保留，可 resume。
- 退出 1：输入/环境等错误；保留已有事务。

默认不下载媒体，`assets.status=not_requested` 不影响“元数据范围 complete”，但绝不表示媒体已镜像。加 `--download-assets` 后，下载失败会让 PR 部分完成。媒体大小默认最多 20 MiB/文件，可用 `--max-asset-bytes` 设置。

## 限制与安全

- 所有 PR 指 API 可访问集合，不包括删除、隐藏、无权限资源。集合按运行开始时间设 cutoff，两遍核对 ID 和 updated_at；并发改动可导致 partial，不伪称原子/历史快照。
- PR commits 接口最多 250 条，files 最多 3,000 个文件。超过上限明确报告截断；当前不自动执行 Git clone 或扩展为全仓库历史遍历。原始 diff/patch 请求单独保留，成功获取不抹掉文件或 commit 列表缺失。
- REST 每页至多 100 条，所有已实现嵌套连接均分页。GitHub GraphQL 返回 data 加 errors 时不视作完整成功，原始错误保留并可重试。
- API 使用当前官方文档示例版本 `2026-03-10`，最大响应 32 MiB。认证失败/仓库重命名引起的重定向不会跟随并携带凭据，需使用当前规范仓库名重新运行。
- API 短暂失败最多尝试三次。长限流等待写入 retry_at 和档案级 cooldown 后退出部分完成，同一档案的新进程/新 run 也会遵守；不长时间占用前台，不自动创建后台定时任务，到期后 resume。
- 附件仅允许无 userinfo 的 HTTPS、标准 443 端口、公网地址；每次重定向重新验证，连接固定到已验证 IP，TLS 仍校验原始域名，不发送 GitHub 凭据/cookies。每个附件在独立进程内有 60 秒整体期限，覆盖 DNS/重定向/慢速读取，超时终止并清理本次临时目录；核对 Content-Length，不渲染内容、不使用来源文件名落盘。
- URL 清单不是完整任意网页镜像；目前识别 Markdown 图片、HTML img/video/source 及常见媒体后缀和 GitHub 附件链接。认证第三方站点或失效 URL 会明确失败。
- 来源正文与原始响应是未信任内容，可能含用户主动公开的敏感信息。档案应按源仓库访问级别保护；工具不补充私人身份信息，不把抓取内容当指令。

## SWE-bench M 的后续衔接

### 2025+ PR 正文图片筛选

此可选流程使用 `01_requirements_image_screening.txt` 中的 markdown-it-py（当前环境已有 4.0.0），核心采集器不需要该依赖。它筛选**图片引用**，不判断图片对解题是否必要，也不把尚未采集的 Issue/测试文件当作无图。

```sh
# 01 正文媒体发现、02 非徽章图片、03 初始待确认附件；完全离线。
PYTHONDONTWRITEBYTECODE=1 python3 analysis/scripts/step_01_screen_pr_body_images.py

# 对无后缀等类型不明附件做匿名、有界前缀探测，生成 04 最终图片子集。
PYTHONDONTWRITEBYTECODE=1 python3 analysis/scripts/step_04_01_type_attachments.py --workers 12

# 离线核对每阶段 ID、原始记录保真、文件哈希和探测证据。
PYTHONDONTWRITEBYTECODE=1 python3 analysis/scripts/step_04_02_audit_image_screening.py
```

正式结果位于 `crawler-output/multimodal-2025/image-screening/`，文件名带步骤编号和具体筛选条件。缓存、日志、原子写入 staging 均位于 `tmp/multimodal-2025/`；前一轮单仓库审计和 batch 日志也已移入其中。原始 SQLite、2025+ 源数据和最终总审计不移动、不删除。

注意：HTML/Markdown 图片声明或图片后缀只是存在性证据，默认不访问这些链接；仅类型不明的附件会进行最多 512 字节、25 秒期限的响应探测。HTTP MIME/文件头成功不是完整媒体下载或解码成功。视频与 GIF（图片）分开，徽章保留在宽口径结果中并另出非徽章子集；相对路径不擅自解析到某个提交。已失效或不确定来源仍保留为 unknown。

`instance_id` 使用 owner__repo-number，原始 ID 和 SHA 保留。已观察 base SHA 不直接冒充历史修复前 base_commit；引用 Issue 也不自动等价于问题来源。原始 patch、Issue 正文、讨论时间与附件可供后续派生 problem_statement、hints_text、test_patch、image_assets，并追踪规则版本和时间截断。

当前 derived 明确为未执行验证，base_commit/test_patch/hints_text 未推导；不会伪造 FAIL_TO_PASS、PASS_TO_PASS、eval_script、Docker image、eval_type 或 log_parser。原数据集的 image 是执行环境字段，不是图片附件字段。

## 官方参考

- [本次官方 collect 源码对照](../research/swe-bench-official-collector.md)：代码位于官方 main 的 swebench/collect 目录。其默认 closed-only 枚举和候选筛选不等于本工具的全状态档案范围；可参考字段、问题与补丁提取语义。
- [Pull requests API](https://docs.github.com/en/rest/pulls/pulls)
- [分页](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api)
- [限流](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)
