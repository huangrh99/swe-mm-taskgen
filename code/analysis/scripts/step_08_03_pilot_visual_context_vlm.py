"""Small, auditable Codex VLM pilot. Defaults to preparation only; --run invokes the model."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import warnings

from PIL import Image
from report_pipeline.paths import CODE_ROOT, WORKSPACE_ROOT

ROOT = WORKSPACE_ROOT
sys.path.insert(0, str(CODE_ROOT))
from pr_crawler.assets import bounded_download

BASE = ROOT / 'crawler-output/multimodal-2025/image-screening'
SOURCE = BASE / '06_merged_default_branch_images/06_prs_with_non_badge_images_merged_to_default_branch.jsonl'
PROMPT = ROOT / 'analysis/prompts/08_01_visual_context_screening.system.md'
SCHEMA = ROOT / 'analysis/prompts/08_02_visual_context_screening.schema.json'
TMP = ROOT / 'tmp/multimodal-2025/05_vlm_screening'
CACHE = ROOT / 'tmp/multimodal-2025/03_image_sample_preview/downloads'
DEFAULT_PRS = ['chartjs/chart.js#11984', 'grommet/grommet#7655', 'eslint/eslint#20010',
               'googlechrome/lighthouse#16746', 'prismjs/prism#3898']


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def prepare(row, directory):
    directory.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    packet = {'pr_id': f"{row['repo']}#{row['number']}", 'title': row['title'],
              'body': row.get('body') or '', 'html_url': row['html_url'],
              'scope': 'PR-body non-decoration image evidence only; no Issue, comments, patch or tests.',
              'images': [], 'other_media_not_attached': []}
    paths = []
    for asset in row['image_screening']['assets']:
        if asset['media_kind'] != 'image' or asset['decoration_reason']:
            packet['other_media_not_attached'].append({'asset_id': asset['asset_id'],
                'media_kind': asset['media_kind'], 'decoration_reason': asset['decoration_reason']})
            continue
        entry = {'asset_id': asset['asset_id'], 'url': asset['url'], 'attachment_index': None,
                 'status': 'unavailable'}
        cache_path = CACHE / (asset['asset_id'] + '.json')
        result = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        local = CACHE / result.get('local_path', 'missing')
        if result.get('status') != 'complete' or not local.is_file() or digest(local) != result.get('sha256'):
            result = bounded_download(asset, CACHE, 8 * 1024 * 1024, timeout=35)
            write_json(cache_path, result)
            local = CACHE / result.get('local_path', 'missing')
        if result.get('status') == 'complete':
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('error', Image.DecompressionBombWarning)
                    with Image.open(local) as picture:
                        if getattr(picture, 'n_frames', 1) != 1:
                            raise ValueError('Animated asset not supported by this still-image pilot')
                        extension = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp'}.get(picture.format)
                        if extension is None:
                            raise ValueError('Unsupported image format')
                        size = list(picture.size)
                        picture.verify()
                target = directory / (f'image-{len(paths)+1}' + extension)
                shutil.copyfile(local, target)
                paths.append(target)
                entry.update(status='attached', attachment_index=len(paths), size=size,
                             sha256=result['sha256'], bytes=result['bytes'], local_path=str(target))
            except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
                entry['reason'] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        else:
            entry['reason'] = result.get('reason', result.get('status', 'unknown'))
        packet['images'].append(entry)
    write_json(directory / 'input.packet.json', packet)
    return packet, paths


def validate(annotation, packet, schema_path=None):
    import jsonschema
    jsonschema.validate(annotation, json.loads(Path(schema_path or SCHEMA).read_text()))
    if annotation['pr_id'] != packet['pr_id']:
        raise ValueError('PR identity mismatch')
    expected = [a['asset_id'] for a in packet['images']]
    if [a['asset_id'] for a in annotation['images']] != expected:
        raise ValueError('Image identity/order/coverage mismatch')
    for image, asset in zip(annotation['images'], packet['images']):
        if image['observed'] and asset['status'] != 'attached':
            raise ValueError('Model claims to observe an unavailable image')
        if annotation['prompt_version'] == 'visual-context-v2':
            if not image['observed'] and image['content_kind'] is not None:
                raise ValueError('Unobserved image requires a null content category')
            if image['content_kind'] is None and annotation['disposition'] != 'review':
                raise ValueError('Unclassified image requires review')
            if not image['content_kind_reason'].strip():
                raise ValueError('Content category requires a nonempty reason')
        quote = image['body_quote']
        if quote is not None and quote not in packet['body'] and quote not in packet['title']:
            raise ValueError('Evidence quote is not an exact source substring')
    candidates = annotation['candidate_asset_ids']
    if len(candidates) != len(set(candidates)) or not set(candidates).issubset(expected):
        raise ValueError('Invalid candidate asset IDs')
    qualifying = {a['asset_id'] for a in annotation['images'] if a['observed'] and
        a['relation_to_fix'] == 'relevant' and a['ocr_sufficient'] == 'no' and
        a['temporal_role'] in ('before', 'expected', 'mixed') and
        a['visual_contribution'] in ('helpful', 'necessary_candidate')}
    if not set(candidates).issubset(qualifying):
        raise ValueError('Candidate asset IDs must qualify as visual evidence')
    if annotation['disposition'] == 'visual_candidate' and not candidates:
        raise ValueError('Visual candidate lacks qualifying visual evidence')
    if annotation['disposition'] != 'review' and any(not a['observed'] for a in annotation['images']):
        raise ValueError('Unobserved image requires review in this pilot')
    relevant = [a for a in annotation['images'] if a['relation_to_fix'] == 'relevant']
    if annotation['disposition'] == 'ocr_auxiliary' and (not relevant or any(a['ocr_sufficient'] != 'yes' for a in relevant)):
        raise ValueError('OCR auxiliary requires all relevant evidence to pass transcription')
    if annotation['disposition'] in ('ocr_auxiliary', 'not_visual') and candidates:
        raise ValueError('Nonvisual disposition cannot nominate visual candidates')
    if annotation['leakage_risk'] == 'none_verified':
        raise ValueError('This PR-only pilot supplies no verified pre-repair provenance')
    if any(a['temporal_role'] in ('after', 'mixed') for a in annotation['images']) and annotation['leakage_risk'] != 'present':
        raise ValueError('After/mixed images require explicit leakage risk')


def command(directory, images, result, prompt=PROMPT, schema=SCHEMA):
    args = ['codex', 'exec', '--ignore-user-config', '--ephemeral', '--skip-git-repo-check',
            '--sandbox', 'read-only', '--model', 'gpt-5.6-luna',
            '-c', 'model_reasoning_effort="max"', '-c', f'model_instructions_file={json.dumps(str(prompt))}',
            '-c', 'features.shell_tool=false', '-c', 'features.multi_agent=false',
            '-c', 'features.apps=false', '-c', 'web_search="disabled"',
            '-c', 'project_doc_max_bytes=0', '--cd', str(directory), '--color', 'never',
            '--output-schema', str(schema), '--output-last-message', str(result)]
    for path in images:
        args.extend(['--image', str(path)])
    return args + ['-']


def run_process(args, user_input, out, err, timeout):
    # Codex's Node launcher may spawn a native child. Bound the whole process group.
    with subprocess.Popen(args, stdin=subprocess.PIPE, text=True, stdout=out, stderr=err,
                          start_new_session=True, env={**os.environ, 'RUST_LOG': 'off'}) as process:
        try:
            process.communicate(user_input, timeout=timeout)
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
                if sig == signal.SIGTERM:
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            process.wait()
            raise
        return process.returncode


def invoke(item, output, timeout):
    packet, images, directory = item
    destination = output / (directory.name + '.json')
    raw_result = directory / 'model.raw.json'
    prompt, schema = output / '08_system_prompt.md', output / '08_output_schema.json'
    args = command(directory, images, raw_result, prompt, schema)
    write_json(directory / 'invocation.json', {'argv': args, 'prompt_sha256': digest(prompt),
                                              'schema_sha256': digest(schema)})
    # User data stays in a JSON packet; the system instructions are loaded separately by Codex.
    user_input = 'Annotate this single PR. Image attachments follow attachment_index order.\n' + json.dumps(packet, ensure_ascii=False)
    start = time.monotonic()
    record = {'pr_id': packet['pr_id'], 'requested_model': 'gpt-5.6-luna',
              'requested_effort': 'max', 'input_packet': str(directory / 'input.packet.json'),
              'packet_sha256': digest(directory / 'input.packet.json'),
              'input_image_count': len(images), 'status': 'failed'}
    try:
        with (directory / 'stdout.log').open('w') as out, (directory / 'stderr.log').open('w') as err:
            returncode = run_process(args, user_input, out, err, timeout)
        record['returncode'] = returncode
        log = (directory / 'stderr.log').read_text()
        model = re.search(r'^model: (.+)$', log, re.M)
        effort = re.search(r'^reasoning effort: (.+)$', log, re.M)
        record.update(reported_model=model[1] if model else None, reported_effort=effort[1] if effort else None)
        tokens = re.search(r'tokens used\s*\n([\d,]+)', log)
        record['cli_reported_tokens_used'] = int(tokens[1].replace(',', '')) if tokens else None
        if returncode != 0:
            raise ValueError('Codex failed; inspect stderr.log')
        if record['reported_model'] != 'gpt-5.6-luna' or record['reported_effort'] != 'max':
            raise ValueError('CLI did not confirm the requested model/effort; no fallback accepted')
        annotation = json.loads(raw_result.read_text())
        validate(annotation, packet, schema_path=schema)
        record.update(status='complete', annotation=annotation)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        record['error'] = type(exc).__name__ + ': ' + str(exc)
    except Exception as exc:
        # Schema validation failures remain visible; never publish malformed annotations as success.
        record['error'] = type(exc).__name__ + ': ' + str(exc)
    record['elapsed_seconds'] = round(time.monotonic() - start, 2)
    write_json(destination, record)
    print(json.dumps({'pr_id': record['pr_id'], 'status': record['status'],
                      'disposition': record.get('annotation', {}).get('disposition'),
                      'seconds': record['elapsed_seconds']}, ensure_ascii=False), flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pr', action='append', help='Explicit retained repo#number; repeat for a small pilot.')
    parser.add_argument('--run', action='store_true', help='Actually invoke the logged-in Codex account.')
    parser.add_argument('--workers', type=int, default=2, choices=(1, 2))
    parser.add_argument('--timeout', type=int, default=480)
    args = parser.parse_args()
    if args.run:
        import jsonschema
        jsonschema.Draft202012Validator.check_schema(json.loads(SCHEMA.read_text()))
    wanted = args.pr or DEFAULT_PRS
    if len(wanted) > 20 or len(set(wanted)) != len(wanted):
        parser.error('Pilot requires 1–20 distinct explicit PRs; bulk execution is intentionally not enabled')
    rows = {}
    with SOURCE.open() as stream:
        for line in stream:
            row = json.loads(line)
            identity = f"{row['repo']}#{row['number']}"
            if identity in wanted:
                rows[identity] = row
    if set(wanted) != set(rows):
        parser.error('Requested PRs missing from retained dataset: ' + str(set(wanted) - set(rows)))
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output = BASE / '08_vlm_visual_context_pilot' / run_id
    output.mkdir(parents=True)
    shutil.copyfile(PROMPT, output / '08_system_prompt.md')
    shutil.copyfile(SCHEMA, output / '08_output_schema.json')
    working = TMP / run_id
    prepared = []
    for identity in wanted:
        directory = working / re.sub(r'[^a-zA-Z0-9_.-]', '_', identity)
        packet, images = prepare(rows[identity], directory)
        prepared.append((packet, images, directory))
        print(json.dumps({'prepared': identity, 'images_attached': len(images),
                          'image_references': len(packet['images'])}), flush=True)
    manifest = {'run_id': run_id, 'input': str(SOURCE), 'input_sha256': digest(SOURCE),
                'system_prompt': str(PROMPT), 'system_prompt_sha256': digest(PROMPT),
                'schema': str(SCHEMA), 'schema_sha256': digest(SCHEMA),
                'model': 'gpt-5.6-luna', 'reasoning_effort': 'max',
                'codex_version': subprocess.check_output(['codex', '--version'], text=True).strip(),
                'selection': 'Purposeful smoke test from existing stage-07 examples; not representative or a labeled accuracy benchmark.',
                'pr_ids': wanted, 'temporary_directory': str(working), 'invoked': args.run,
                'packets': [str(d / 'input.packet.json') for _, _, d in prepared]}
    write_json(output / '08_run_manifest.json', manifest)
    if args.run:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            records = list(pool.map(lambda item: invoke(item, output, args.timeout), prepared))
        with (output / '08_pilot_results.jsonl').open('w') as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        manifest['completed'] = sum(r['status'] == 'complete' for r in records)
        manifest['failed'] = len(records) - manifest['completed']
        write_json(output / '08_run_manifest.json', manifest)
    print(json.dumps({'output': str(output), 'run_id': run_id}, ensure_ascii=False), flush=True)
    if args.run and manifest['failed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
