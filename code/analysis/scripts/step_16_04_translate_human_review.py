"""Translate frozen PR titles and candidate problem statements for curator display."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil

from analysis.scripts.step_16_04_export_human_review import load_rows
from pr_crawler.api_engines import ApiEvaluator, digest
from report_pipeline.paths import CODE_ROOT

PROMPT = CODE_ROOT / 'analysis/prompts/16_04_01_translation.system.md'
SCHEMA = CODE_ROOT / 'analysis/prompts/16_04_02_translation.schema.json'


def json_digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def text_digest(item):
    value = item['case_id'] + '\0' + item['pr_title'] + '\0' + item['problem_statement']
    return hashlib.sha256(value.encode()).hexdigest()


def urls(text):
    trailing = '.,;:!?。，；：！？、)]}'
    # Stop before CJK punctuation. Otherwise a translation such as
    # ``https://example.test/a，该页面`` is misread as one changed URL.
    return {value.rstrip(trailing) for value in re.findall(
        r'https?://[^\s)>，。；：！？、]+', text)}


def validate(source, translated):
    if translated['case_id'] != source['case_id']:
        raise ValueError('Translation case identity changed')
    if not source['problem_statement'] and translated['problem_statement_zh']:
        raise ValueError('Empty problem statement gained translated content')
    original = source['pr_title'] + '\n' + source['problem_statement']
    rendered = translated['pr_title_zh'] + '\n' + translated['problem_statement_zh']
    if re.findall(r'视觉材料\s+\d+', original) != re.findall(r'视觉材料\s+\d+', rendered):
        raise ValueError('Visual material markers changed during translation')
    if original.count('```') != rendered.count('```'):
        raise ValueError('Code fence count changed during translation')
    if not urls(original).issubset(urls(rendered)):
        raise ValueError('URL changed or disappeared during translation')


def run(source_run, key_file, model=None, batch_size=7, timeout=480, resume=False):
    source_run = Path(source_run).resolve()
    output = source_run / '16_04_04_translations_zh.json'
    calls = source_run / '16_04_translation_calls'
    if output.exists() or (calls.exists() and not resume):
        raise FileExistsError(output if output.exists() else calls)
    _, rows = load_rows(source_run)
    items = [{'case_id': row['case_id'], 'pr_title': row['pr_title'],
              'problem_statement': row['human_seed']['problem_statement']} for row in rows]
    evaluator = ApiEvaluator('gemini', model=model, key_file=key_file, attempts=2,
                             min_interval=1.0, max_tokens=32768,
                             cooldown_path=calls / 'cooldown.json')
    frozen_prompt = source_run / '16_04_01_translation_system.md'
    frozen_schema = source_run / '16_04_02_translation_schema.json'
    frozen_runner = source_run / '16_04_03_translation_runner.py'
    contract_path = calls / '00_translation_resume_contract.json'
    model_config = {
        'backend': evaluator.backend,
        'profile': evaluator.profile,
        'attempts': evaluator.attempts,
        'min_interval': evaluator.min_interval,
        'max_tokens': evaluator.max_tokens,
        'timeout': timeout,
    }
    expected_contract = {
        'schema_version': 'human-review-translation-resume-contract-v1',
        'source_run_manifest_sha256': digest(source_run / '16_03_run_manifest.json'),
        'prompt_sha256': digest(PROMPT),
        'schema_sha256': digest(SCHEMA),
        'runner_sha256': digest(Path(__file__)),
        'model_config': model_config,
        'batch_size': batch_size,
    }
    if resume:
        if not contract_path.is_file():
            raise ValueError('Translation resume contract is missing')
        if json.loads(contract_path.read_text()) != expected_contract:
            raise ValueError('Translation resume contract changed')
        for frozen, expected in ((frozen_prompt, PROMPT), (frozen_schema, SCHEMA),
                                 (frozen_runner, Path(__file__))):
            if not frozen.is_file() or digest(frozen) != digest(expected):
                raise ValueError('Frozen translation contract changed')
    else:
        calls.mkdir()
        shutil.copyfile(PROMPT, frozen_prompt)
        shutil.copyfile(SCHEMA, frozen_schema)
        shutil.copyfile(Path(__file__), frozen_runner)
        write_json(contract_path, expected_contract)
    contract_sha256 = digest(contract_path)
    translated = []
    invocations = []
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        batch_number = start // batch_size + 1
        base_directory = calls / f'16_04_call_{batch_number:02d}'
        batch_contract = {
            'schema_version': 'human-review-translation-batch-contract-v1',
            'run_contract_sha256': contract_sha256,
            'batch': batch_number,
            'packet_sha256': json_digest({'items': batch}),
            'case_ids': [item['case_id'] for item in batch],
        }
        reusable = []
        existing = ([candidate for candidate in [base_directory] + sorted(
            calls.glob(base_directory.name + '_retry_*')) if candidate.exists()]
            if resume else [])
        for candidate in existing:
            if candidate.is_symlink() or not candidate.is_dir():
                raise ValueError('Unsafe translation retry directory')
            candidate_contract = candidate / '00_resume_contract.json'
            if (not candidate_contract.is_file()
                    or json.loads(candidate_contract.read_text()) != batch_contract):
                raise ValueError('Translation batch resume contract changed or is missing')
            candidate_receipt = candidate / '12_resume_receipt.json'
            if candidate_receipt.is_file():
                reusable.append(candidate)
        if len(reusable) > 1:
            raise ValueError('Multiple complete translation retry batches are ambiguous')
        if reusable:
            directory = reusable[0]
        elif resume and base_directory.exists():
            retry_number = 1
            directory = calls / f'{base_directory.name}_retry_{retry_number:02d}'
            while directory.exists():
                retry_number += 1
                directory = calls / f'{base_directory.name}_retry_{retry_number:02d}'
        else:
            directory = base_directory
        raw = directory / '09_model_raw.json'
        invocation_path = directory / '10_api_invocation.json'
        batch_contract_path = directory / '00_resume_contract.json'
        result_invocation_path = directory / '11_result_invocation.json'
        receipt_path = directory / '12_resume_receipt.json'
        if reusable:
            if not (raw.is_file() and invocation_path.is_file()
                    and result_invocation_path.is_file()):
                raise ValueError('Complete translation receipt has missing batch artifacts')
            receipt = json.loads(receipt_path.read_text())
            if (receipt.get('batch_contract_sha256') != digest(batch_contract_path)
                    or receipt.get('raw_sha256') != digest(raw)
                    or receipt.get('provider_invocation_sha256') != digest(invocation_path)
                    or receipt.get('result_invocation_sha256')
                    != digest(result_invocation_path)):
                raise ValueError('Translation batch resume receipt changed')
            result = json.loads(raw.read_text())
            invocation = {**json.loads(result_invocation_path.read_text()), 'reused': True}
        else:
            directory.mkdir()
            write_json(batch_contract_path, batch_contract)
            result, invocation = evaluator(packet={'items': batch}, image_paths=[],
                system_prompt=frozen_prompt, schema=frozen_schema,
                workdir=directory, timeout=timeout)
            write_json(result_invocation_path, invocation)
        values = result.get('translations') or []
        if [item.get('case_id') for item in values] != [item['case_id'] for item in batch]:
            raise ValueError('Translation batch identity/order mismatch')
        for source, value in zip(batch, values):
            validate(source, value)
            translated.append({**value, 'source_text_sha256': text_digest(source)})
        if not receipt_path.exists():
            if not raw.is_file() or not invocation_path.is_file():
                raise ValueError('Translation evaluator did not freeze reusable artifacts')
            write_json(receipt_path, {
                'schema_version': 'human-review-translation-batch-receipt-v1',
                'batch_contract_sha256': digest(batch_contract_path),
                'raw_sha256': digest(raw),
                'provider_invocation_sha256': digest(invocation_path),
                'result_invocation_sha256': digest(result_invocation_path),
            })
        invocations.append({'batch': start // batch_size + 1,
                            'case_ids': [item['case_id'] for item in batch], **invocation})
    value = {'schema_version': 'human-review-zh-translations-v1',
             'notice': 'Machine translation for curator display only; never benchmark input.',
             'source_run_manifest_sha256': digest(source_run / '16_03_run_manifest.json'),
             'prompt_sha256': digest(PROMPT), 'schema_sha256': digest(SCHEMA),
             'runner_sha256': digest(Path(__file__)),
             'resume_contract_sha256': contract_sha256, 'model_config': model_config,
             'items': translated, 'invocations': invocations}
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', type=Path, required=True)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--key-file', type=Path)
    parser.add_argument('--model')
    parser.add_argument('--batch-size', type=int, default=7)
    parser.add_argument('--timeout', type=int, default=480)
    parser.add_argument('--resume', action='store_true',
                        help='Reuse and revalidate complete frozen batches; invoke only missing batches.')
    args = parser.parse_args()
    if not args.run or not args.key_file:
        parser.error('Translation API calls require explicit --run and --key-file')
    print(run(args.source_run, args.key_file, args.model, args.batch_size, args.timeout,
              args.resume))


if __name__ == '__main__':
    main()
