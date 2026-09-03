"""Offline audit of stage identities, original-record fidelity and probe evidence."""

import argparse
import base64
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from report_pipeline.paths import CODE_ROOT, WORKSPACE_ROOT

ROOT = WORKSPACE_ROOT
sys.path.insert(0, str(CODE_ROOT))
from analysis.scripts.step_01_screen_pr_body_images import PARTITIONS, ALL_EVIDENCE, ALL_IMAGES
from pr_crawler.store import now


def rows(path):
    with path.open() as stream:
        for line in stream:
            yield json.loads(line)


def sha_file(path):
    sha = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            sha.update(chunk)
    return sha.hexdigest()


def digest(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def audit(output):
    summary = json.loads((output / '00_pr_body_image_screening_summary.json').read_text())
    final_dir = output / '04_pr_body_images_after_attachment_typing'
    final_summary = json.loads((final_dir / '04_image_screening_summary.json').read_text())
    originals = {}
    errors = []
    def check(condition, reason):
        if not condition:
            errors.append(reason)
    source = Path(summary['input'])
    check(sha_file(source) == summary['input_sha256'] == final_summary['input_sha256'], 'source_hash_mismatch')
    for row in rows(source):
        key = row['repo'], row['number']
        check(key not in originals, 'duplicate_source_id')
        originals[key] = digest(row)
    for directory, manifest in ((output, summary), (final_dir, final_summary)):
        for name, info in manifest['outputs'].items():
            check(sha_file(directory / name) == info['sha256'], 'output_hash_mismatch:' + name)
    expected_partitions = {category: set() for category in PARTITIONS}
    expected_probes = set()
    evidence_ids = set()
    for row in rows(output / ALL_EVIDENCE):
        key = row['repo'], row['number']
        check(key not in evidence_ids, 'duplicate_discovery_id')
        evidence_ids.add(key)
        value = row['image_screening']
        expected_partitions[value['category']].add(key)
        expected_probes.update(a['asset_id'] for a in value['assets'] if a['media_kind'] in {'untyped_attachment','conflicting'})
    check(evidence_ids == set(originals), 'discovery_identity_mismatch')

    def check_file(path, expected, full=True):
        seen = set()
        for row in rows(path):
            key = row['repo'], row['number']
            check(key not in seen, 'duplicate_output_id:' + path.name)
            seen.add(key)
            if full:
                raw = {k:v for k,v in row.items() if k != 'image_screening'}
                check(key in originals and digest(raw) == originals[key], 'original_record_changed:' + path.name)
        check(seen == expected, 'output_id_set_mismatch:' + path.name)
        return len(seen)

    for category,name in PARTITIONS.items():
        check_file(output / name, expected_partitions[category], full=category != 'no_detected_media_in_pr_body')
        check(len(expected_partitions[category]) == summary['counts'].get(category,0), 'stage_count_mismatch:' + category)
    check_file(output / ALL_IMAGES, expected_partitions['non_badge_image_evidence'] | expected_partitions['only_badge_or_decoration_image_evidence'])
    typed_partitions = {category:set() for category in PARTITIONS}
    final_ids, extra = set(), set()
    for row in rows(final_dir / '04_all_prs_classification_ledger.jsonl'):
        key = row['repo'], row['number']
        check(key not in final_ids,'duplicate_final_ledger_id')
        final_ids.add(key)
        typed_partitions[row['category']].add(key)
        if row['category'] == 'non_badge_image_evidence' and row['pre_type_check_category'] != row['category']:
            extra.add(key)
    check(final_ids == set(originals),'final_ledger_id_set_mismatch')
    image_count = check_file(final_dir / '04_prs_with_non_badge_images.jsonl',typed_partitions['non_badge_image_evidence'])
    all_image_count = check_file(final_dir / '04_prs_with_image_evidence_including_badges.jsonl',typed_partitions['non_badge_image_evidence'] | typed_partitions['only_badge_or_decoration_image_evidence'])
    check_file(final_dir / '04_additional_non_badge_image_prs_from_attachment_typing.jsonl',extra)
    check_file(final_dir / '04_prs_with_video_evidence_no_image_evidence.jsonl',typed_partitions['video_without_image_evidence'])
    check_file(final_dir / '04_pr_ids_with_unresolved_media_type_no_image_evidence.jsonl',typed_partitions['untyped_attachment_without_image_evidence'],full=False)
    for category,ids in typed_partitions.items():
        check(len(ids) == final_summary['counts'].get(category,0),'final_count_mismatch:'+category)
    probe_ids = set()
    probe_statuses = Counter()
    for row in rows(final_dir / '03_attachment_media_type_checks.jsonl'):
        check(row['asset_id'] not in probe_ids,'duplicate_probe')
        probe_ids.add(row['asset_id'])
        check(hashlib.sha256(row['url'].encode()).hexdigest() == row['asset_id'],'probe_url_id_mismatch')
        probe_statuses[row['status']] += 1
        if 'prefix_base64' in row:
            prefix = base64.b64decode(row['prefix_base64'],validate=True)
            check(len(prefix) <= 512 and len(prefix) == row['prefix_bytes'],'probe_byte_limit_mismatch')
            check(hashlib.sha256(prefix).hexdigest() == row['prefix_sha256'],'probe_prefix_hash_mismatch')
    check(probe_ids == expected_probes,'probe_asset_set_mismatch')
    check(dict(probe_statuses) == final_summary['probe_status_counts'],'probe_status_count_mismatch')
    return {'checked_at':now(),'passed':not errors,'input_prs':len(originals),'non_badge_image_prs':image_count,
            'all_image_prs_including_badges':all_image_count,'additional_image_prs_from_typing':len(extra),
            'probed_asset_urls':len(probe_ids),'error_count':len(errors),'errors':errors[:30],
            'scope':'Identity coverage, original-record fidelity, output hashes and prefix evidence; not image decoding, visual necessity or recall beyond PR bodies'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default=str(ROOT/'crawler-output/multimodal-2025/image-screening'))
    parser.add_argument('--tmp',default=str(ROOT/'tmp/multimodal-2025/02_pr_body_image_screening'))
    args = parser.parse_args()
    output,temporary=Path(args.output),Path(args.tmp)
    result=audit(output)
    temporary.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w',dir=temporary,delete=False) as stream:
        json.dump(result,stream,ensure_ascii=False,indent=2)
        path=Path(stream.name)
    os.replace(path,output/'00_image_screening_audit.json')
    print(json.dumps(result,ensure_ascii=False))
    raise SystemExit(0 if result['passed'] else 2)
