"""Validate model evidence and route PRs. No network or model calls in this module."""

import json
from pathlib import Path
from report_pipeline.paths import CODE_ROOT

ROOT = CODE_ROOT
PROMPT = ROOT / 'analysis/prompts/09_01_visual_verifier.system.md'
SCHEMA = ROOT / 'analysis/prompts/09_02_visual_verifier.schema.json'
POLICY_VERSION = 'strict-nontext-visual-v1'
BUCKETS = ('visual_necessary', 'visual_helpful', 'ocr_auxiliary', 'excluded', 'review')
MISSING_SOURCES = ['issue', 'comments', 'patch', 'tests', 'history']


def quote_candidates(packet):
    if packet.get('packet_version') == 'pr-only-verifier-v2':
        # Reconstruct the historical schema even though Codex rejected that run.
        return list(dict.fromkeys(line.strip() for text in (packet['title'], packet['body'])
                                  for line in text.splitlines() if line.strip()))
    # Codex strict-output enums reject literal double quotes. Split citation units,
    # not the original body: every resulting fragment is still an exact substring.
    return list(dict.fromkeys(fragment.strip() for text in (packet['title'], packet['body'])
                              for line in text.splitlines() for fragment in line.split('"')
                              if fragment.strip()))


def bind_schema(packet, schema_path=SCHEMA):
    """Constrain generated references to this packet; never repair model IDs afterwards."""
    schema = json.loads(Path(schema_path).read_text())
    schema['properties']['pr_id']['enum'] = [packet['pr_id']]
    images = schema['properties']['images']
    evidence = schema['properties']['task']['properties']['evidence_asset_ids']
    ids = [a['asset_id'] for a in packet['images']]
    if ids:
        images['items']['properties']['asset_id']['enum'] = ids
        evidence['items']['enum'] = ids
    else:
        images['maxItems'] = evidence['maxItems'] = 0
    if 'source_quote_candidates' in packet:
        quotes = quote_candidates(packet)
        if packet['source_quote_candidates'] != quotes:
            raise ValueError('Quote candidates differ from source text')
        images['items']['properties']['source_quote']['enum'] = quotes + [None]
        problem_quotes = schema['properties']['task']['properties']['problem_evidence_quotes']
        if quotes:
            problem_quotes['items']['enum'] = quotes
        else:
            problem_quotes['maxItems'] = 0
    return schema


def validate(annotation, packet, schema_path=SCHEMA):
    import jsonschema
    jsonschema.validate(annotation, json.loads(Path(schema_path).read_text()))
    if annotation['pr_id'] != packet['pr_id']:
        raise ValueError('PR identity mismatch')
    ids = [a['asset_id'] for a in packet['images']]
    if len(ids) != len(set(ids)) or ids != [a['asset_id'] for a in annotation['images']]:
        raise ValueError('Image identity/order/coverage mismatch')
    task, quality = annotation['task'], annotation['quality']
    evidence_ids = task['evidence_asset_ids']
    if len(evidence_ids) != len(set(evidence_ids)) or not set(evidence_ids) <= set(ids):
        raise ValueError('Invalid evidence asset IDs')
    if not set(packet['missing_sources']) <= set(quality['missing_sources']):
        raise ValueError('Missing source coverage was omitted')
    quotes = list(task['problem_evidence_quotes'])
    for image, asset in zip(annotation['images'], packet['images']):
        if image['observed'] and asset['status'] != 'attached':
            raise ValueError('Claimed observation of unavailable image')
        if not image['observed'] and (image['content_kind'] is not None or
                any(image[k] != 'unknown' for k in ('relevance', 'temporal_role',
                    'faithful_text_representation', 'ocr_task_sufficient'))):
            raise ValueError('Unobserved images must retain unknown judgments')
        if image['source_quote'] is not None:
            quotes.append(image['source_quote'])
        if image['temporal_role'] != 'unknown' and not image['source_quote']:
            raise ValueError('Temporal role requires source quote')
        for key in ('content_reason', 'observation', 'text_reason'):
            if not image[key].strip():
                raise ValueError('Empty image reason')
        if image['temporal_role'] in ('after', 'mixed') and quality['leakage_risk'] != 'present':
            raise ValueError('After/mixed evidence requires leakage flag')
    for quote in quotes:
        if not quote.strip() or (quote not in packet['body'] and quote not in packet['title']):
            raise ValueError('Evidence quote is not an exact source substring')
    if quality['problem_evidence_separable'] == 'yes' and not task['problem_evidence_quotes']:
        raise ValueError('Separable problem requires quoted problem evidence')
    if not task['reason'].strip() or not quality['reason'].strip():
        raise ValueError('Empty task/quality reason')


def decide(annotation):
    """Conservative mutually exclusive buckets; call validate first."""
    task, quality, images = annotation['task'], annotation['quality'], annotation['images']
    def result(bucket, reason):
        return {'bucket': bucket, 'reason_code': reason, 'policy_version': POLICY_VERSION,
                'training_ready': False, 'readiness_reason': 'PR-only; issue/patch/history and execution not verified'}
    if not images or any(not a['observed'] or a['content_kind'] is None for a in images):
        return result('review', 'unobserved_or_unclassified_images')
    if (quality['problem_clarity'] != 'clear' or quality['evidence_sufficiency'] != 'sufficient'
            or quality['problem_evidence_separable'] != 'yes'):
        return result('review', 'insufficient_or_inseparable_problem_evidence')
    if task['necessity'] == 'unknown' or any(a['relevance'] == 'unknown' for a in images):
        return result('review', 'unknown_necessity_or_relevance')
    if task['necessity'] in ('redundant', 'unrelated'):
        return result('excluded', 'no_additional_task_image_information')
    relevant = [a for a in images if a['relevance'] == 'relevant']
    transcription = task['image_transcription_sufficient']
    if not relevant or transcription == 'unknown' or any(a['ocr_task_sufficient'] == 'unknown' for a in relevant):
        return result('review', 'unknown_text_substitutability')
    all_text = all(a['ocr_task_sufficient'] == 'yes' for a in relevant)
    if (transcription == 'yes') != all_text:
        return result('review', 'conflicting_image_and_task_transcription')
    if transcription == 'yes':
        return result('ocr_auxiliary', 'character_transcription_suffices')
    eligible = {a['asset_id'] for a in relevant if a['temporal_role'] in ('before', 'expected')
                and a['ocr_task_sufficient'] == 'no' and a['faithful_text_representation'] == 'no'}
    if not eligible:
        return result('review', 'no_eligible_pre_repair_nontext_evidence')
    if task['necessity'] == 'necessary':
        selected = set(task['evidence_asset_ids'])
        if not selected or not selected <= eligible or not task['missing_visual_information'].strip():
            return result('review', 'necessity_missing_specific_eligible_evidence')
        return result('visual_necessary', 'necessary_nontext_visual_evidence')
    return result('visual_helpful', 'helpful_but_not_necessary')
