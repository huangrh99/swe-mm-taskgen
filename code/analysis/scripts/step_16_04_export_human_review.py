"""Export a text-first human adjudication page from one frozen stage-16 run."""

import argparse
import base64
import hashlib
import html
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
from report_pipeline.paths import CODE_ROOT, WORKSPACE_ROOT
from report_pipeline.atomic import write_bytes as _safe_write_bytes, write_json as _safe_write_json

ROOT = WORKSPACE_ROOT
sys.path.insert(0, str(CODE_ROOT))
from pr_crawler import repair_sufficiency as policy
from pr_crawler.assets import apply_recovery


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _assert_no_symlink(path, boundary):
    """Reject any existing symlink from boundary through the publication target."""
    path, boundary = Path(path).absolute(), Path(boundary).absolute()
    if path != boundary and boundary not in path.parents:
        raise ValueError('Review asset target escapes its output directory')
    current = path
    while True:
        if current.is_symlink():
            raise ValueError(f'Unsafe symlink in review asset target: {current}')
        if current.parent == current:
            return
        current = current.parent


def _directory_digest(path):
    root = Path(path)
    entries = [{'path': item.relative_to(root).as_posix(), 'sha256': digest(item)}
               for item in sorted(root.rglob('*')) if item.is_file()]
    return hashlib.sha256(json.dumps(entries, separators=(',', ':')).encode()).hexdigest()


def _atomic_json(path, value):
    _safe_write_json(Path(path), value)


def _bundle_paths(output, staging_token=None):
    output = Path(output)
    parent = output.parent
    targets = {
        'html': output,
        'assets': output.with_name('16_04_review_assets'),
        'builder': output.with_name('16_04_review_builder.py'),
        'seed': output.with_name('16_04_human_review_seed.json'),
        'manifest': output.with_name('16_04_review_manifest.json'),
    }
    staging = ({
        name: target.with_name(f'.{target.name}.{staging_token}.staging')
        for name, target in targets.items()
    } if staging_token else {})
    return (targets, staging,
            parent / '.16_04_review_bundle.transaction.json',
            parent / '16_04_review_bundle.commit.json')


def _validate_bundle_entries(output, entries):
    targets, _, _, _ = _bundle_paths(output)
    if (not isinstance(entries, list) or len(entries) != len(targets)
            or {item.get('name') for item in entries if isinstance(item, dict)} != set(targets)):
        raise ValueError('Committed review bundle inventory is invalid')
    for entry in entries:
        target = targets[entry['name']]
        if entry.get('kind') == 'directory':
            if target.is_symlink() or not target.is_dir() or _directory_digest(target) != entry.get('sha256'):
                raise ValueError(f'Committed review bundle changed: {target.name}')
        elif entry.get('kind') == 'file':
            if target.is_symlink() or not target.is_file() or digest(target) != entry.get('sha256'):
                raise ValueError(f'Committed review bundle changed: {target.name}')
        else:
            raise ValueError('Committed review bundle entry kind is invalid')
    expected = hashlib.sha256(json.dumps(entries, separators=(',', ':')).encode()).hexdigest()
    return expected


def _recover_review_bundle(output):
    """Recover only hash-bound files from an interrupted pre-commit publication."""
    targets, _, transaction, commit = _bundle_paths(output)
    if commit.exists():
        if commit.is_symlink():
            raise ValueError('Unsafe review bundle commit symlink')
        commit_value = json.loads(commit.read_text())
        if commit_value.get('schema_version') != 'visual-review-bundle-commit-v1':
            raise ValueError('Review bundle commit schema is invalid')
        expected = _validate_bundle_entries(output, commit_value.get('entries'))
        if commit_value.get('bundle_sha256') != expected:
            raise ValueError('Review bundle commit hash is invalid')
        if transaction.exists():
            if commit_value.get('transaction_sha256') != digest(transaction):
                raise ValueError('Committed review bundle transaction changed; manual recovery required')
            transaction.unlink()
        return
    if not transaction.exists():
        return
    value = json.loads(transaction.read_text())
    if value.get('schema_version') != 'visual-review-bundle-transaction-v1':
        raise ValueError('Review bundle transaction is invalid')
    staging_token = value.get('staging_token')
    if not isinstance(staging_token, str) or not re.fullmatch(r'[0-9a-f]{32}', staging_token):
        raise ValueError('Review bundle staging identity is invalid')
    _, staging, _, _ = _bundle_paths(output, staging_token)
    for entry in value.get('entries', []):
        name = entry.get('name')
        if name not in targets:
            raise ValueError('Review bundle transaction contains an unknown target')
        target = targets[name]
        if not target.exists() and not target.is_symlink():
            continue
        if entry.get('kind') == 'directory':
            if target.is_symlink() or not target.is_dir() or _directory_digest(target) != entry.get('sha256'):
                raise ValueError('Interrupted review asset bundle changed; manual recovery required')
            shutil.rmtree(target)
        else:
            if target.is_symlink() or not target.is_file() or digest(target) != entry.get('sha256'):
                raise ValueError(f'Interrupted review bundle file changed: {target.name}')
            target.unlink()
    for path in staging.values():
        if path.is_symlink():
            raise ValueError('Unsafe symlink in review bundle staging')
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    transaction.unlink()


def translation_source_digest(case_id, pr_title, problem_statement):
    value = case_id + '\0' + pr_title + '\0' + problem_statement
    return hashlib.sha256(value.encode()).hexdigest()


def media_suffix(path):
    """Return a browser-safe suffix from bytes, not the extensionless archive path."""
    data = Path(path).read_bytes()[:512]
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if data.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return '.gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return '.webp'
    if b'<svg' in data.lower():
        return '.svg'
    if data.startswith(b'\x1aE\xdf\xa3'):
        return '.webm'
    if len(data) >= 12 and data[4:8] == b'ftyp':
        return '.mov' if data[8:12].rstrip() == b'qt' else '.mp4'
    raise ValueError(f'Unsupported archived visual media format: {path}')


def replace_visuals(text, source_id, assets):
    """Replace archived image markup with stable human-review references."""
    for asset in assets:
        if source_id not in asset.get('source_ids', []):
            continue
        marker = f"\n\n> **视觉材料 {asset['display_index']}**：见下方对应图片。\n\n"
        url = re.escape(asset['url'])
        patterns = [rf'<img\b[^>]*\bsrc=["\']{url}["\'][^>]*>',
                    rf'!\[[^\]]*\]\(\s*<?{url}>?(?:\s+["\'][^"\']*["\'])?\s*\)']
        replaced = 0
        for pattern in patterns:
            text, count = re.subn(pattern, marker, text, flags=re.I)
            replaced += count
        if not replaced and asset['url'] in text:
            text = text.replace(asset['url'], marker)
            replaced = 1
        if not replaced:
            text += marker
    return text.strip()


def source_archive_documents(packet):
    archive = json.loads(Path(packet['provenance']['source_archive']).read_text())
    return {item['source_id']: item for item in archive['archival_view']['documents']}


def pr_curator_assets(packet):
    """Return downloaded PR/conversation images for human review only."""
    from report_pipeline.pre_review_classification import _bound_media_path

    archive_path = Path(packet['provenance']['source_archive'])
    archive = json.loads(archive_path.read_text())
    prefixes = ('pr:', 'comments:', 'review_comments:', 'thread_comment:')
    media_kinds = {item.get('url'): item.get('media_kind')
                   for item in archive.get('archival_view', {}).get('media', [])}
    documents = {item['source_id']: item.get('text', '')
                 for item in archive['archival_view']['documents']
                 if item['source_id'].startswith(prefixes)}
    items = []
    for raw_asset in archive['sections']['assets']['items']:
        asset = apply_recovery(archive_path, raw_asset)
        url = asset.get('url')
        media_type = asset.get('media_type') or ''
        if (media_kinds.get(url) not in (None, 'image')
                or (media_type and not media_type.startswith('image/'))):
            continue
        source_ids = [source_id for source_id in asset.get('sources', [])
                      if source_id.startswith(prefixes)]
        source_ids.extend(source_id for source_id, text in documents.items()
                          if url and url in text and source_id not in source_ids)
        if not source_ids:
            continue
        local_path = asset.get('local_path')
        path = None
        available = asset.get('status') == 'complete' and bool(local_path)
        if available:
            supplied = archive_path.parent / '11_http_archive' / Path(local_path)
            path = _bound_media_path(
                {'asset_id': asset.get('sha256'), 'local_path': str(supplied)},
                archive_path, {'sections': {'assets': {'items': [asset]}}})
        items.append({'asset_id': asset.get('sha256') or url, 'url': url,
            'status': 'available' if available else 'unavailable', 'source_ids': source_ids,
            'local_path': str(path) if available else None,
            'sha256': asset.get('sha256'), 'evidence_role': 'curator_only_pr_repair_evidence',
            'recovered_from_status': asset.get('recovered_from_status'),
            'recovered_from_reason': asset.get('recovered_from_reason'),
            'recovery_manifest': asset.get('recovery_manifest')})
    return items


def candidate_problem_statement(packet, assets, documents):
    """Build an editable issue-only draft without exposing PR repair prose."""
    grouped = []
    by_url = {}
    for source in packet['problem_sources']:
        group = by_url.get(source['url'])
        if group is None:
            group = {'url': source['url'], 'title': '', 'body': ''}
            by_url[source['url']] = group
            grouped.append(group)
        if source['field'] in ('title', 'body'):
            original = (documents.get(source['source_id']) or {}).get('text', source['text'])
            group[source['field']] = replace_visuals(original, source['source_id'], assets)
    parts = []
    for index, group in enumerate(grouped, 1):
        content = '\n\n'.join(value for value in (group['title'], group['body']) if value)
        if len(grouped) > 1:
            content = f"## 关联问题 {index}\n\n{content}"
        if content:
            parts.append(content)
    return '\n\n---\n\n'.join(parts)


def load_rows(run, classifications=None):
    from report_pipeline.pre_review_classification import load_for_source
    manifest = json.loads((run / '16_03_run_manifest.json').read_text())
    classification_records = load_for_source(run, classifications)
    translation_path = run / '16_04_04_translations_zh.json'
    translations = {}
    if translation_path.exists():
        translation_value = json.loads(translation_path.read_text())
        if translation_value['source_run_manifest_sha256'] != digest(run / '16_03_run_manifest.json'):
            raise ValueError('Chinese translations belong to another source run')
        translations = {item['case_id']: item for item in translation_value['items']}
    rows = []
    for index, number in enumerate(manifest['pr_numbers'], 1):
        result_path = run / f'16_03_result_{index:04d}.json'
        record = json.loads(result_path.read_text())
        packet_path = Path(record['packet'])
        curator_path = Path(record['curator_assets'])
        if digest(packet_path) != record['packet_sha256'] or digest(curator_path) != record['curator_assets_sha256']:
            raise ValueError('Packet or curator asset index changed')
        packet, curator = json.loads(packet_path.read_text()), json.loads(curator_path.read_text())
        for asset_index, asset in enumerate(curator['assets'], 1):
            asset['display_index'] = asset_index
        pr_assets = pr_curator_assets(packet)
        for asset_index, asset in enumerate(pr_assets, 1):
            asset['display_index'] = asset_index
        documents = source_archive_documents(packet)
        recovery_path = Path(packet['provenance']['source_archive']).parent / '11_01_asset_recovery_manifest.json'
        recovery_provenance = ({'path': str(recovery_path.resolve()), 'sha256': digest(recovery_path)}
                               if recovery_path.exists() else None)
        visual = record.get('visual_verifier')
        text = record.get('text_decision')
        reconcile = ({'policy_version': policy.POLICY_VERSION, 'visual_bucket': 'not_run',
            'text_bucket': 'ineligible', 'human_required_for_acceptance': True,
            'agent_ablation_required_now': False, 'queue': 'human_problem_statement_required',
            'reason_code': record['ineligible_reason']} if record['status'] == 'ineligible' else
            record.get('reconciliation') or policy.reconcile((visual or {}).get('decision'), text))
        problem_statement = candidate_problem_statement(packet, curator['assets'], documents)
        problem_statement_status = ('generated_from_linked_issue' if packet['problem_sources']
                                    else 'needs_human_problem_statement')
        pr_body = replace_visuals((documents.get('pr:body') or {}).get('text', ''),
                                  'pr:body', pr_assets).replace(
                                      '**视觉材料 ', '**PR 证据图片 ')
        human_seed = policy.human_record(packet['case_id'], record['packet_sha256'],
            visual.get('result_sha256') if visual else None,
            digest(result_path) if record['status'] == 'complete' else None)
        human_seed['problem_statement'] = problem_statement
        human_seed['problem_statement_source_ids'] = [source['source_id'] for source in packet['problem_sources']]
        human_seed['problem_statement_status'] = problem_statement_status
        pr_title = (documents.get('pr:title') or {}).get('text', '')
        translation = translations.get(packet['case_id'])
        if translation and translation['source_text_sha256'] != translation_source_digest(
                packet['case_id'], pr_title, problem_statement):
            raise ValueError('Chinese translation source text changed')
        rows.append({'case_id': packet['case_id'], 'pr_number': number, 'packet': packet,
            'pr_url': f"https://github.com/{packet['repository']}/pull/{number}",
            'pr_title': pr_title, 'translation': translation,
            'packet_sha256': record['packet_sha256'], 'result_status': record['status'],
            'result_sha256': digest(result_path),
            'result_error': record.get('error'), 'ineligible_reason': record.get('ineligible_reason'),
            'text_annotation': record.get('annotation'), 'text_decision': text,
            'visual_verifier': visual, 'reconciliation': reconcile, 'assets': curator['assets'],
            'pre_review_classification': classification_records.get(packet['case_id']),
            'pr_assets': pr_assets, 'pr_body': pr_body,
            'problem_statement_status': problem_statement_status,
            'asset_recovery': recovery_provenance,
            'human_seed': human_seed,
            'curator_links': {'source_archive': packet['provenance']['source_archive'],
                'asset_recovery': str(recovery_path) if recovery_path.exists() else None,
                'case_manifest': packet['provenance'].get('case_manifest')}})
    return manifest, rows


def render(run, output, classifications=None):
    from report_pipeline.pre_review_classification import _bound_media_path

    run, output = Path(run).resolve(), Path(output).resolve()
    default_classification = Path(run).resolve() / '16_03_08_pre_review_classifications.json'
    selected_classification = Path(classifications) if classifications else default_classification
    classification_path = (selected_classification.resolve()
                           if selected_classification.is_file() else None)
    manifest, rows = load_rows(run, classification_path)
    classification_sha256 = digest(classification_path) if classification_path else None
    classification_complete = bool(rows) and all(
        row['pre_review_classification']
        and row['pre_review_classification']['change_scale']['label'] != '无法分类'
        and row['pre_review_classification']['visual_capability']['status']
        in {'complete', 'ineligible'}
        for row in rows)
    if classification_path is not None:
        declared_ready = json.loads(classification_path.read_text()).get('human_review_ready')
        if declared_ready is not classification_complete:
            raise ValueError('Pre-review classification readiness differs from its records')
    output.parent.mkdir(parents=True, exist_ok=True)
    targets, _, transaction, commit = _bundle_paths(output)
    _recover_review_bundle(output)
    for target in (*targets.values(), transaction, commit):
        _assert_no_symlink(target, output.parent)
        if os.path.lexists(target):
            raise FileExistsError(target)
    review_assets = targets['assets']
    _assert_no_symlink(review_assets, output.parent)
    staging_token = secrets.token_hex(16)
    _, bundle_staging, _, _ = _bundle_paths(output, staging_token)
    for target in bundle_staging.values():
        _assert_no_symlink(target, output.parent)
        if os.path.lexists(target):
            raise FileExistsError(target)
    staging = bundle_staging['assets']
    staging.mkdir()
    _atomic_json(transaction, {
        'schema_version': 'visual-review-bundle-transaction-v1',
        'phase': 'preparing',
        'staging_token': staging_token,
        'entries': [],
    })
    published = []
    try:
        for row in rows:
            archive_path = Path(row['packet']['provenance']['source_archive']).resolve()
            archive = json.loads(archive_path.read_text())
            for asset in row['assets']:
                if asset.get('status') == 'available':
                    source = _bound_media_path(asset, archive_path, archive)
                    suffix = media_suffix(source)
                    expected = digest(source)
                    target = staging / f'{expected}{suffix}'
                    _assert_no_symlink(target, staging)
                    shutil.copy2(source, target)
                    if digest(target) != expected:
                        raise ValueError('Review asset changed while copying')
                    asset['review_src'] = (review_assets / target.name).relative_to(
                        output.parent).as_posix()
                    asset['review_media_kind'] = (
                        'video' if suffix in ('.mov', '.mp4', '.webm') else 'image')
                    published.append((target.name, expected))
            for asset in row['pr_assets']:
                if asset.get('status') == 'available':
                    source = Path(asset['local_path'])
                    suffix = media_suffix(source)
                    expected = digest(source)
                    target = staging / f'{expected}{suffix}'
                    _assert_no_symlink(target, staging)
                    shutil.copy2(source, target)
                    if digest(target) != expected:
                        raise ValueError('Review asset changed while copying')
                    asset['review_src'] = (review_assets / target.name).relative_to(
                        output.parent).as_posix()
                    asset['review_media_kind'] = (
                        'video' if suffix in ('.mov', '.mp4', '.webm') else 'image')
                    published.append((target.name, expected))
        for name, expected in published:
            target = staging / name
            _assert_no_symlink(target, staging)
            if digest(target) != expected:
                raise ValueError('Staged review asset hash mismatch')
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        transaction.unlink(missing_ok=True)
        raise
    for row in rows:
        for key, value in row['curator_links'].items():
            if value:
                row['curator_links'][key] = Path(value).resolve().relative_to(
                    output.parent.resolve(), walk_up=True).as_posix()
    payload = {'run_id': manifest['run_id'], 'manifest_sha256': digest(run / '16_03_run_manifest.json'),
               'classification_path': str(classification_path) if classification_path else None,
               'classification_sha256': classification_sha256,
               'classification_ready': classification_complete,
               'rows': rows, 'labels': list(policy.HUMAN_LABELS)}
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    title = html.escape('16 · IID 视觉必要性人工仲裁')
    document = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
:root{{--bg:#f4f1ea;--panel:#fffdf8;--ink:#20211f;--muted:#68685f;--line:#d8d2c5;--accent:#275d50;--warn:#9b572c}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,sans-serif}}
header{{position:sticky;top:0;z-index:3;background:#f4f1eaf2;border-bottom:1px solid var(--line);padding:16px 5vw}}
h1{{font:700 24px/1.2 Georgia,serif;margin:0 0 6px}}.muted{{color:var(--muted)}}main{{max-width:1180px;margin:auto;padding:24px}}.toolbar{{display:flex;align-items:center;gap:8px;margin-top:10px}}#counter{{min-width:82px;text-align:center}}.toolbar #export{{margin-left:auto}}.pr-title{{margin:6px 0 0;font-size:16px}}
.case{{background:var(--panel);border:1px solid var(--line);border-radius:12px;margin:0 0 20px;padding:20px;box-shadow:0 3px 14px #443c2c10}}.case-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;border-bottom:1px solid var(--line);padding-bottom:14px}}.case-head h2{{margin:0 0 6px}}.pr-link{{display:inline-block;background:var(--accent);color:white;text-decoration:none;border-radius:8px;padding:10px 14px;white-space:nowrap}}
.badge{{display:inline-block;border:1px solid var(--line);border-radius:99px;padding:2px 8px;margin:2px 4px 2px 0;font-size:12px}}
.queue{{border-color:#d7a36b;color:#7c451c}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1eee6;border-radius:8px;padding:12px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}label{{display:block;font-weight:650;margin-top:10px}}textarea,select,input{{width:100%;font:inherit;padding:8px;border:1px solid var(--line);border-radius:6px;background:white}}
.problem-draft{{min-height:240px;resize:none;overflow:hidden;line-height:1.55}}.verifier-summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;margin:10px 0}}.verifier-summary article{{border:1px solid var(--line);border-radius:8px;padding:10px;background:#f8f5ee}}.verifier-summary b,.verifier-summary span{{display:block}}.verifier-summary strong{{display:block;font-size:16px;margin:4px 0;color:var(--accent)}}details{{border:1px solid var(--line);border-radius:8px;margin:8px 0;background:white}}summary{{cursor:pointer;font-weight:650;padding:10px}}details>pre,details>.details-body{{margin:0 10px 10px}}.source-links a{{margin-right:10px}}
button{{padding:8px 12px;border:0;border-radius:7px;background:var(--accent);color:white;cursor:pointer}}button.secondary{{background:#736f65}}
.assets{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px}}img,video{{width:100%;max-height:360px;object-fit:contain;background:white;border:1px solid var(--line)}}
.warning{{color:var(--warn)}}.attention{{border-left:4px solid var(--warn);background:#fff3e7;padding:10px 12px;border-radius:6px}}@media(max-width:760px){{.grid{{grid-template-columns:1fr}}main{{padding:12px}}}}
</style></head><body><header><h1>{title}</h1><div class="muted">每题必须先记录并持久化无图判断；随后才会渲染图片、PR 修复证据与视觉 Verifier 结论。</div><div class="toolbar"><button class="secondary" id="prev">上一题</button><strong id="counter"></strong><button id="next">下一题</button><button class="secondary" id="language">切换为中文</button><button id="export">导出人工标注 JSON</button></div><div id="errors" class="warning"></div></header><main id="root"></main>
<script>const DATA=JSON.parse(new TextDecoder().decode(Uint8Array.from(atob('{encoded}'),c=>c.charCodeAt(0))));
const KEY='stage16-human-'+DATA.manifest_sha256+'-'+(DATA.classification_sha256||'unclassified');const saved=JSON.parse(localStorage.getItem(KEY)||'{{}}');
const PAGE_KEY=KEY+'-page',LANG_KEY=KEY+'-language';let current=Math.min(Math.max(Number(localStorage.getItem(PAGE_KEY)||0),0),DATA.rows.length-1);let language=localStorage.getItem(LANG_KEY)==='zh'?'zh':'original';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
function persist(){{localStorage.setItem(KEY,JSON.stringify(saved))}}function field(id,k,v){{saved[id]??={{}};saved[id][k]=v;persist()}}
function verifierCard(label,value,detail){{return `<article><b>${{esc(label)}}</b><strong>${{esc(value||'—')}}</strong><span>${{esc(detail||'')}}</span></article>`}}
function showCase(row){{const id=row.case_id,s=saved[id]||{{}},revealed=!!s.images_revealed_at,sources=row.packet.problem_sources.map(x=>`<h4><a href="${{esc(x.url)}}">${{esc(x.source_id)}}</a></h4><pre>${{esc(x.text)}}</pre>`).join('');
const needsStatement=row.problem_statement_status==='needs_human_problem_statement';const textState=row.result_status==='complete'?'完成':row.result_status==='ineligible'?'未调用（需先整理题面）':'已调用但校验失败';
const shortError=String(row.result_error||'').split('\\n')[0];const textBucket=row.text_decision?.bucket||row.ineligible_reason||shortError||'无有效结果';
const textDetail=row.text_annotation?`repair=${{row.text_annotation.repair_contract?.completeness||'—'}} · test=${{row.text_annotation.test_contract?.constructible||'—'}} · confidence=${{row.text_annotation.confidence||'—'}}`:shortError||row.ineligible_reason||'';
const visualDecision=row.visual_verifier?.decision||{{}};const visualTask=row.visual_verifier?.annotation?.task||{{}};const pre=row.pre_review_classification||{{}};const capability=pre.visual_capability||{{}};const capabilityAnn=capability.annotation||{{}};const scale=pre.change_scale||{{}};
const textSummaries=verifierCard('Text-only Verifier',textState,textBucket)+verifierCard('文字判断',row.text_decision?.bucket||'—',textDetail);const visualSummaries=verifierCard('视觉必要性 Verifier',visualDecision.bucket||'—',visualDecision.reason_code||'')+verifierCard('图片贡献',visualTask.necessity||'—',`OCR 可替代=${{visualTask.image_transcription_sufficient||'—'}}`)+verifierCard('参考修改规模',scale.label||'未运行',scale.reason||'人工审核前必须冻结')+verifierCard('视觉能力分类',capabilityAnn.primary_visual_category||capability.status||'未运行',capabilityAnn.category_purity||'人工审核前必须完成');
const ann=row.text_annotation?`<details class="verifier-full"><summary>Text-only Verifier · 完整输出</summary><pre>${{esc(JSON.stringify(row.text_annotation,null,2))}}</pre></details>`:`<details class="verifier-full"><summary>Text-only Verifier · 状态与错误详情</summary><pre>${{esc(JSON.stringify({{status:row.result_status,ineligible_reason:row.ineligible_reason,error:row.result_error}},null,2))}}</pre></details>`;
const vis=row.visual_verifier?`<details class="verifier-full"><summary>视觉 Verifier · 完整输出</summary><pre>${{esc(JSON.stringify(row.visual_verifier,null,2))}}</pre></details>`:'<p class="warning">视觉 Verifier 结果尚未接入。</p>';
const preFull=row.pre_review_classification?`<details class="verifier-full"><summary>修改规模与视觉能力分类 · 完整输出</summary><pre>${{esc(JSON.stringify(row.pre_review_classification,null,2))}}</pre></details>`:'<p class="warning"><b>审核前分类未完成：</b>该页只能用于查看材料，不能形成正式人工准入结论。</p>';
const media=a=>a.review_media_kind==='video'?`<video controls preload="metadata" src="${{esc(a.review_src)}}"></video>`:`<img src="${{esc(a.review_src)}}">`;
const assets=row.assets.map(a=>a.status==='available'?`<figure>${{media(a)}}<figcaption><b>题面视觉材料 ${{esc(a.display_index)}}</b> · ${{esc(a.source_ids.join(', '))}} · <code>${{esc(a.asset_id)}}</code></figcaption></figure>`:`<p><b>题面视觉材料 ${{esc(a.display_index)}}</b>不可用：${{esc(a.url)}}</p>`).join('');
const prAssets=row.pr_assets.map(a=>a.status==='available'?`<figure>${{media(a)}}<figcaption><b>PR 证据图片/视频 ${{esc(a.display_index)}}</b> · ${{esc(a.source_ids.join(', '))}} · <code>${{esc(a.asset_id)}}</code></figcaption></figure>`:`<p><b>PR 证据图片/视频 ${{esc(a.display_index)}}</b>不可用：${{esc(a.url)}}</p>`).join('');
const labelNames={{'':'待判断',human_confirmed_visual_candidate:'确认需要视觉输入',human_confirmed_text_sufficient:'确认文字已经足够',visual_helpful_only:'图片仅有帮助但非必要',needs_agent_ablation:'需要 Agent 消融',needs_human_problem_statement:'需人工整理题面',invalid_or_leaky:'无效或存在泄漏'}};const queueNames={{human_problem_statement_required:'需人工整理题面'}};const opts=[''].concat(DATA.labels).map(x=>`<option value="${{esc(x)}}" ${{s.decision===x?'selected':''}}>${{esc(labelNames[x]||x)}}</option>`).join('');
const originalStatement=s.problem_statement??row.human_seed.problem_statement;const zhStatement=s.problem_statement_zh??row.translation?.problem_statement_zh??'';const statement=language==='zh'?zhStatement:originalStatement;const statementKey=language==='zh'?'problem_statement_zh':'problem_statement';const prTitle=language==='zh'?(row.translation?.pr_title_zh||row.pr_title):row.pr_title;
const postReveal=revealed?`<h3>③ PR 修复证据（仅人工可见）</h3><p class="muted">用于理解 PR 实际修复了什么、判断图片是否构成必要视觉证据；正文与图片不会进入最终 agent 题面。</p><pre>${{esc(row.pr_body||'PR 正文为空。')}}</pre><div class="assets">${{prAssets||'<p>PR 正文及讨论中没有已固化图片。</p>'}}</div>
<h3>④ 题面视觉材料（将提供给 agent）</h3><div class="assets">${{assets||'<p>关联 Issue 正文中没有已固化图片。</p>'}}</div><p class="source-links"><a href="${{esc(row.curator_links.source_archive)}}">完整来源档案</a>${{row.curator_links.case_manifest?`<a href="${{esc(row.curator_links.case_manifest)}}">case manifest / patch-test provenance</a>`:''}}</p>
<h3>⑤ 审核前分类与视觉 Verifier 结论</h3><div class="verifier-summary">${{visualSummaries}}</div>${{vis}}${{preFull}}
<label>审核人<input data-k="reviewer" value="${{esc(s.reviewer||'')}}" autocomplete="name"></label>
<div class="grid"><div><label>图片新增的、文字无法恢复的事实<textarea data-k="visual_delta">${{esc(s.visual_delta||'')}}</textarea></label></div><div><label>reference patch 与测试是否验证该事实<textarea data-k="patch_and_test_alignment">${{esc(s.patch_and_test_alignment||'')}}</textarea></label><label>人工标签<select data-k="decision">${{opts}}</select></label><label>判断理由<textarea data-k="decision_reason">${{esc(s.decision_reason||'')}}</textarea></label></div></div><label><input style="width:auto" type="checkbox" data-k="ablation_required" ${{s.ablation_required?'checked':''}}> 仍需第三级 agent 消融</label>`:`<button id="reveal" type="button">持久化无图判断并揭示视觉证据</button>`;
return `<section class="case" data-id="${{esc(id)}}"><div class="case-head"><div><h2>${{esc(row.packet.repository)}} · PR #${{esc(row.pr_number)}}</h2><p class="pr-title">${{esc(prTitle)}}</p><span class="badge">text=${{esc(row.reconciliation.text_bucket)}}</span>${{revealed?`<span class="badge queue">${{esc(queueNames[row.reconciliation.queue]||row.reconciliation.queue)}}</span><span class="badge">visual=${{esc(row.reconciliation.visual_bucket)}}</span>`:''}}${{language==='zh'?'<span class="badge">中文机器翻译 · 仅供核验</span>':''}}</div>${{revealed?`<a class="pr-link" href="${{esc(row.pr_url)}}">打开 GitHub PR</a>`:''}}</div>
<h3>① 候选题面草案</h3>${{needsStatement?'<p class="attention"><b>需人工整理题面：</b>当前没有合格的关联 Issue。请仅根据问题文字整理题面；揭示前不要查看 PR 修复证据。</p>':''}}<p class="muted">先仅基于关联 Issue 的文字材料做判断。</p><textarea class="problem-draft" data-k="${{statementKey}}">${{esc(statement)}}</textarea>
<details class="problem-sources"><summary>查看题面来源原文与 Issue 链接</summary><div class="details-body">${{sources||'<p class="warning">没有合格的关联 Issue，当前题面为空。</p>'}}</div></details>
<h3>② Text-only Verifier 与无图判断</h3><div class="verifier-summary">${{textSummaries}}</div>${{ann}}<label>只看文字时的判断与缺失信息<textarea data-k="text_first_notes" ${{revealed?'disabled':''}}>${{esc(s.text_first_notes||'')}}</textarea></label>${{postReveal}}</section>`}}
function grow(el){{if(el.classList.contains('problem-draft')){{el.style.height='auto';el.style.height=el.scrollHeight+'px'}}}}
function renderCurrent(){{document.querySelector('#root').innerHTML=showCase(DATA.rows[current]);document.querySelector('#counter').textContent=`${{current+1}} / ${{DATA.rows.length}}`;document.querySelector('#language').textContent=language==='zh'?'查看原文':'切换为中文';document.querySelector('#prev').disabled=current===0;document.querySelector('#next').disabled=current===DATA.rows.length-1;document.querySelectorAll('[data-k]').forEach(el=>{{grow(el);el.addEventListener('input',()=>{{field(el.closest('.case').dataset.id,el.dataset.k,el.type==='checkbox'?el.checked:el.value);grow(el)}})}});const reveal=document.querySelector('#reveal');if(reveal)reveal.onclick=()=>{{const row=DATA.rows[current],s=saved[row.case_id]||{{}};if(!String(s.text_first_notes||'').trim()){{document.querySelector('#errors').textContent='揭示视觉证据前必须填写无图判断';return}}s.text_first_recorded_at=new Date().toISOString();saved[row.case_id]=s;persist();s.images_revealed_at=new Date().toISOString();persist();document.querySelector('#errors').textContent='';renderCurrent()}}}}
function move(delta){{current=Math.min(Math.max(current+delta,0),DATA.rows.length-1);localStorage.setItem(PAGE_KEY,String(current));renderCurrent();window.scrollTo(0,0)}}document.querySelector('#prev').onclick=()=>move(-1);document.querySelector('#next').onclick=()=>move(1);document.querySelector('#language').onclick=()=>{{language=language==='zh'?'original':'zh';localStorage.setItem(LANG_KEY,language);renderCurrent()}};renderCurrent();
document.querySelector('#export').disabled=!DATA.classification_ready;document.querySelector('#export').onclick=()=>{{const rows=DATA.rows.map(r=>Object.assign({{}},r.human_seed,saved[r.case_id]||{{}},{{reviewed_at:(saved[r.case_id]?.decision?new Date().toISOString():null),agent_ablation:{{required:!!saved[r.case_id]?.ablation_required,reason:saved[r.case_id]?.ablation_required?'human requested':'',status:'not_run'}}}}));if(rows.some(r=>r.decision&&(!String(r.reviewer||'').trim()||!r.text_first_recorded_at||!r.images_revealed_at))){{document.querySelector('#errors').textContent='完成正式人工审核必须填写审核人并保留 text-first 揭示证据';return}}document.querySelector('#errors').textContent='';const blob=new Blob([JSON.stringify({{schema_version:'visual-necessity-human-export-v1',source_manifest_sha256:DATA.manifest_sha256,pre_review_classification:DATA.classification_path,pre_review_classification_sha256:DATA.classification_sha256,pre_review_classification_ready:DATA.classification_ready,rows}},null,2)],{{type:'application/json'}});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='16_04_human_decisions.json';a.click();URL.revokeObjectURL(a.href)}};
</script></body></html>'''
    output_staging = bundle_staging['html']
    _safe_write_bytes(output_staging, document.encode())
    builder_path = targets['builder']
    builder_staging = bundle_staging['builder']
    _safe_write_bytes(builder_staging, Path(__file__).read_bytes())
    seed = {'schema_version': 'visual-necessity-human-export-v1',
            'source_manifest_sha256': payload['manifest_sha256'],
            'pre_review_classification': payload['classification_path'],
            'pre_review_classification_sha256': classification_sha256,
            'pre_review_classification_ready': classification_complete,
            'rows': [row['human_seed'] for row in rows]}
    seed_path = targets['seed']
    seed_staging = bundle_staging['seed']
    _atomic_json(seed_staging, seed)
    manifest_path = targets['manifest']
    manifest_staging = bundle_staging['manifest']
    status_counts = {status: sum(row['result_status'] == status for row in rows)
                     for status in sorted({row['result_status'] for row in rows})}
    queue_counts = {queue: sum(row['reconciliation']['queue'] == queue for row in rows)
                    for queue in sorted({row['reconciliation']['queue'] for row in rows})}
    problem_status_counts = {status: sum(row['problem_statement_status'] == status for row in rows)
                             for status in sorted({row['problem_statement_status'] for row in rows})}
    _atomic_json(manifest_staging, {'status': ('ready_for_human_review' if classification_complete else 'materials_only_pre_review_classification_incomplete'), 'run': str(run.resolve()),
        'run_manifest_sha256': payload['manifest_sha256'], 'cases': len(rows), 'html': output.name,
        'pre_review_classification': (str(classification_path) if classification_path else None),
        'pre_review_classification_sha256': classification_sha256,
        'pre_review_classification_ready': classification_complete,
        'pre_review_classification_complete': classification_complete,
        'html_sha256': digest(output_staging), 'seed': seed_path.name, 'seed_sha256': digest(seed_staging),
        'builder': builder_path.name, 'builder_sha256': digest(builder_staging),
        'review_assets': review_assets.name,
        'task_review_asset_count': sum(asset.get('status') == 'available'
                                       for row in rows for asset in row['assets']),
        'pr_curator_asset_count': sum(asset.get('status') == 'available'
                                      for row in rows for asset in row['pr_assets']),
        'review_asset_count': sum(asset.get('status') == 'available'
                                  for row in rows for asset in row['assets'] + row['pr_assets']),
        'asset_recoveries': sorted({(row['asset_recovery']['path'], row['asset_recovery']['sha256'])
                                    for row in rows if row['asset_recovery']}),
        'model_calls_added': 0, 'agent_ablation': 'not_run',
        'counts': {'result_status': status_counts, 'queue': queue_counts,
                   'problem_statement_status': problem_status_counts},
        'case_index': [{'case_id': row['case_id'], 'result_status': row['result_status'],
                        'queue': row['reconciliation']['queue'],
                        'task_asset_count': len(row['assets']),
                        'pr_curator_asset_count': len(row['pr_assets']),
                        'problem_statement_status': row['problem_statement_status'],
                        'result_sha256': row['result_sha256']} for row in rows]})
    entries = []
    for name in ('assets', 'html', 'builder', 'seed', 'manifest'):
        staged = bundle_staging[name]
        kind = 'directory' if staged.is_dir() else 'file'
        entries.append({
            'name': name,
            'kind': kind,
            'sha256': _directory_digest(staged) if kind == 'directory' else digest(staged),
        })
    transaction_record = {
        'schema_version': 'visual-review-bundle-transaction-v1',
        'phase': 'publishing',
        'staging_token': staging_token,
        'entries': entries,
    }
    _atomic_json(transaction, transaction_record)
    try:
        for entry in entries:
            os.replace(bundle_staging[entry['name']], targets[entry['name']])
        bundle_sha256 = hashlib.sha256(
            json.dumps(entries, separators=(',', ':')).encode()).hexdigest()
        _atomic_json(commit, {
            'schema_version': 'visual-review-bundle-commit-v1',
            'bundle_sha256': bundle_sha256,
            'transaction_sha256': digest(transaction),
            'entries': entries,
        })
        transaction.unlink()
    except Exception:
        _recover_review_bundle(output)
        raise
    return manifest_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--classifications', type=Path,
                        help='16_03_08_pre_review_classifications.json from classify-before-review')
    args = parser.parse_args()
    run = args.run.resolve()
    output = args.output.resolve() if args.output else run / '16_04_human_review.html'
    print(render(run, output, args.classifications))


if __name__ == '__main__':
    main()
