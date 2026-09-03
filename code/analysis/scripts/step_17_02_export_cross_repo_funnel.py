"""Export a compact funnel index plus complete per-PR evidence pages."""

import argparse
import base64
from collections import Counter
import hashlib
import html
import json
import mimetypes
from pathlib import Path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def esc(value):
    return html.escape(str(value if value is not None else ''), quote=True)


STYLE = """
:root{--bg:#f5f6f8;--card:#fff;--ink:#20242b;--muted:#667085;--line:#d9dee7;--keep:#16794b;--drop:#b54708;--review:#365f91}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:13px/1.42 system-ui,sans-serif}
header{position:sticky;top:0;z-index:4;background:#ffffffef;border-bottom:1px solid var(--line);padding:10px 18px}h1{font-size:20px;margin:0 0 4px}h2{font-size:17px}h3{font-size:14px;margin:14px 0 6px}.muted{color:var(--muted)}
main{max-width:1480px;margin:auto;padding:14px}.stats,.filters{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}.pill{border:1px solid var(--line);border-radius:99px;padding:3px 8px;background:#fff}.keep{color:var(--keep)}.drop{color:var(--drop)}.review{color:var(--review)}
table{width:100%;border-collapse:collapse;background:var(--card)}th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{position:sticky;top:70px;background:#eef1f5;z-index:2}tr:hover{background:#f7fafc}.reason{max-width:340px}.stage{white-space:nowrap}select,input{font:inherit;padding:5px 7px;border:1px solid var(--line);border-radius:5px;background:#fff}
a{color:#145a8d;text-decoration:none}a:hover{text-decoration:underline}article{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;margin:12px 0}details{border-top:1px solid var(--line);padding:7px 0}summary{cursor:pointer;font-weight:650}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:520px;overflow:auto;background:#f6f7f9;padding:9px;border-radius:5px;margin:6px 0}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.media{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px}img{width:100%;max-height:340px;object-fit:contain;border:1px solid var(--line);background:white}.flow{display:grid;grid-template-columns:repeat(4,1fr);gap:7px}.flow>div{border:1px solid var(--line);border-radius:6px;padding:7px;background:#fafbfc}
@media(max-width:850px){.grid,.flow{grid-template-columns:1fr}th{position:static}.wide{display:none}}
"""


def json_block(value):
    return '<pre>' + esc(json.dumps(value, ensure_ascii=False, indent=2)) + '</pre>'


def archive_title(archive):
    return archive['sections']['pull_request']['data'].get('title') or ''


def image_gallery(record_path, archive):
    figures = []
    for asset in archive['sections']['assets']['items']:
        local = asset.get('local_path')
        path = record_path.parent / '11_http_archive' / local if local else None
        if not path or not path.is_file():
            continue
        mime = asset.get('media_type') or mimetypes.guess_type(path.name)[0] or ''
        if not mime.startswith('image/'):
            continue
        uri = 'data:' + mime + ';base64,' + base64.b64encode(path.read_bytes()).decode()
        figures.append('<figure><img loading="lazy" src="' + uri + '"><figcaption>' +
                       esc(asset.get('url') or asset.get('sha256')) + '</figcaption></figure>')
    return ''.join(figures)


def decision_fields(record):
    visual = (record.get('visual_verifier') or {}).get('decision') or {}
    text = record.get('text_decision') or {}
    reconcile = record.get('reconciliation') or {}
    if record['status'] == 'ineligible':
        reconcile = {'queue': 'automatic_exclusion_audit',
                     'reason_code': record.get('ineligible_reason')}
    elif record['status'] == 'failed':
        reconcile = {'queue': 'review', 'reason_code': 'text_stage_failed'}
    return visual, text, reconcile


def detail_page(record, result_path, destination):
    packet = json.loads(Path(record['packet']).read_text())
    archive_path = Path(packet['provenance']['source_archive'])
    archive = json.loads(archive_path.read_text())
    pull = archive['sections']['pull_request']['data']
    visual, text, reconciliation = decision_fields(record)
    documents = archive['archival_view']['documents']
    issue_documents = [item for item in documents if item.get('kind') == 'issue']
    other_documents = [item for item in documents if item.get('kind') != 'issue']
    raw_link = ''
    invocation = record.get('invocation') or {}
    if invocation.get('raw_response'):
        raw_link = ' · <a href="file://' + esc(invocation['raw_response']) + '">text-only 原始响应</a>'
    visual_result = record.get('visual_verifier') or {}
    visual_link = (' · <a href="file://' + esc(visual_result['result_path']) + '">09 原始结果</a>'
                   if visual_result.get('result_path') else '')
    source_link = '<a href="file://' + esc(archive_path) + '">完整 Stage-11 JSON</a>'
    flow = f'''<div class="flow"><div><b>09 图片判断</b><br>{esc(visual.get('bucket','not_run'))}<br><span class="muted">{esc(visual.get('reason_code'))}</span></div>
<div><b>11 来源资格</b><br>{esc(archive.get('status'))}<br><span class="muted">Issue 文档 {len(issue_documents)}</span></div>
<div><b>16 无图判断</b><br>{esc(text.get('bucket',record['status']))}<br><span class="muted">{esc(text.get('reason_code') or record.get('ineligible_reason') or record.get('error'))}</span></div>
<div><b>最终人工队列</b><br>{esc(reconciliation.get('queue','review'))}<br><span class="muted">{esc(reconciliation.get('reason_code'))}</span></div></div>'''
    body = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(record['pr_id'])}</title><style>{STYLE}</style>
<header><h1>{esc(record['pr_id'])} · {esc(archive_title(archive))}</h1><span class="muted"><a href="17_02_cross_repo_funnel.html">返回总览</a> · <a href="{esc(pull.get('html_url'))}">GitHub PR</a> · {source_link}{visual_link}{raw_link}</span></header><main>{flow}
<article><h2>原图</h2><div class="media">{image_gallery(archive_path, archive) or '<span class="muted">没有可嵌入的本地图片</span>'}</div></article>
<article><h2>题面与 PR 原文</h2><div class="grid"><div><h3>关联 Issue 完整文档</h3>{json_block(issue_documents)}</div><div><h3>PR 标题</h3><pre>{esc(pull.get('title'))}</pre><h3>PR 正文</h3><pre>{esc(pull.get('body') or '')}</pre></div></div></article>
<article><h2>模型如何判断</h2><details open><summary>09 图片 Verifier 完整标注</summary>{json_block((record.get('visual_verifier') or {}).get('annotation'))}</details><details open><summary>16 text-only 完整标注</summary>{json_block(record.get('annotation') or {'status':record['status'],'reason':record.get('error') or record.get('ineligible_reason')})}</details><details><summary>16 实际无图输入 packet</summary>{json_block(packet)}</details></article>
<article><h2>完整来源材料</h2><p>{source_link}；以下内容不省略，可折叠查看。</p><details><summary>其他归档文档（PR/评论/review 等）</summary>{json_block(other_documents)}</details><details><summary>全部归档 sections（PR、评论、review、commit、files、diff、patch、Issue、附件、时间线）</summary>{json_block(archive['sections'])}</details><details><summary>完整 Stage-11 记录</summary>{json_block(archive)}</details></article>
<article><h2>本次结果记录与来源哈希</h2>{json_block({'result': record, 'result_path': str(result_path), 'result_sha256': digest(result_path), 'archive_path': str(archive_path), 'archive_sha256': digest(archive_path)})}</article></main></html>'''
    destination.write_text(body)
    return archive, visual, text, reconciliation


def render(run, output):
    run = Path(run).resolve()
    output = Path(output).resolve()
    manifest_path = run / '16_03_run_manifest.json'
    manifest = json.loads(manifest_path.read_text())
    details = output.parent / '17_02_pr_details'
    details.mkdir(exist_ok=False)
    rows, detail_files = [], []
    for index, pr_id in enumerate(manifest['pr_ids'], 1):
        result_path = run / f'16_03_result_{index:04d}.json'
        record = json.loads(result_path.read_text())
        if record['pr_id'] != pr_id:
            raise ValueError('Result order or identity mismatch')
        filename = f'17_02_pr_{index:04d}_{record["case_id"]}.html'
        archive, visual, text, reconciliation = detail_page(record, result_path, details / filename)
        rows.append({'pr_id': pr_id, 'repo': record['repository'], 'title': archive_title(archive),
                     'status': record['status'], 'visual': visual.get('bucket', 'not_run'),
                     'visual_reason': visual.get('reason_code'),
                     'archive': archive['status'], 'issue_documents': sum(
                         item.get('kind') == 'issue' for item in archive['archival_view']['documents']),
                     'text': text.get('bucket', record['status']),
                     'text_reason': text.get('reason_code') or record.get('ineligible_reason') or record.get('error'),
                     'queue': reconciliation.get('queue', 'review'),
                     'queue_reason': reconciliation.get('reason_code'), 'detail': '17_02_pr_details/' + filename})
        detail_files.append(details / filename)
    counts = {'status': Counter(row['status'] for row in rows),
              'visual': Counter(row['visual'] for row in rows),
              'text': Counter(row['text'] for row in rows),
              'queue': Counter(row['queue'] for row in rows),
              'repositories': len(set(row['repo'] for row in rows))}
    stats = ''.join('<span class="pill">' + esc(key) + ': ' + esc(value) + '</span>'
                    for key, value in [('PR', len(rows)), ('仓库', counts['repositories'])] +
                    [(key, value) for key, value in counts['queue'].items()])
    table_rows = ''.join(f'''<tr data-repo="{esc(row['repo'])}" data-queue="{esc(row['queue'])}" data-visual="{esc(row['visual'])}" data-text="{esc(row['text'])}"><td><a href="{esc(row['detail'])}">{esc(row['pr_id'])}</a><br><span class="muted">{esc(row['title'])}</span></td><td class="stage">{esc(row['visual'])}<br><span class="muted">{esc(row['visual_reason'])}</span></td><td class="stage">{esc(row['archive'])}<br><span class="muted">Issue docs={row['issue_documents']}</span></td><td class="stage">{esc(row['text'])}<br><span class="muted">{esc(row['text_reason'])}</span></td><td class="reason"><b>{esc(row['queue'])}</b><br><span class="muted">{esc(row['queue_reason'])}</span></td></tr>''' for row in rows)
    repos = sorted(set(row['repo'] for row in rows))
    queues = sorted(set(row['queue'] for row in rows))
    index = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>17 · 跨仓库视觉筛选漏斗</title><style>{STYLE}</style><header><h1>17 · 跨仓库视觉筛选漏斗</h1><span class="muted">逐 PR 显示为什么保留、淘汰或复核。点击 PR 查看完整原文、图片、模型标注、commit/files/diff/patch 和原始结果。</span><div class="stats">{stats}</div><div class="filters"><select id="repo"><option value="">全部仓库</option>{''.join('<option>'+esc(x)+'</option>' for x in repos)}</select><select id="queue"><option value="">全部队列</option>{''.join('<option>'+esc(x)+'</option>' for x in queues)}</select><input id="search" placeholder="搜索 PR 或标题"></div></header><main><table><thead><tr><th>PR</th><th>09 图片判断</th><th>11 来源归档</th><th>16 无图判断</th><th>最终分流与原因</th></tr></thead><tbody>{table_rows}</tbody></table><article><h2>统计与证据边界</h2>{json_block({'run':str(run),'run_manifest_sha256':digest(manifest_path),'counts':{key:dict(value) if isinstance(value,Counter) else value for key,value in counts.items()},'scope':'Verifier triage and human queue; not final benchmark acceptance or agent ablation'})}</article></main><script>
const rows=[...document.querySelectorAll('tbody tr')],repo=document.querySelector('#repo'),queue=document.querySelector('#queue'),search=document.querySelector('#search');function apply(){{const q=search.value.toLowerCase();rows.forEach(r=>r.hidden=!!((repo.value&&r.dataset.repo!==repo.value)||(queue.value&&r.dataset.queue!==queue.value)||(q&&!r.innerText.toLowerCase().includes(q))))}}[repo,queue,search].forEach(x=>x.oninput=apply);
</script></html>'''
    output.write_text(index)
    audit = {'schema_version': 'cross-repo-funnel-preview-v1', 'status': 'passed',
             'source_run': str(run), 'source_manifest_sha256': digest(manifest_path),
             'rows': len(rows), 'repositories': counts['repositories'],
             'counts': {key: dict(value) if isinstance(value, Counter) else value
                        for key, value in counts.items()},
             'index': output.name, 'index_sha256': digest(output),
             'detail_pages': len(detail_files),
             'detail_sha256': {path.name: digest(path) for path in detail_files},
             'full_archive_links': len(rows), 'model_calls_added': 0}
    audit_path = output.parent / '17_02_preview_audit.json'
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n')
    return audit_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    print(render(args.run, args.output))


if __name__ == '__main__':
    main()
