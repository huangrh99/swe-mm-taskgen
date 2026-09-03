"""Text-only repair-sufficiency contracts and deterministic human routing."""

import copy
from datetime import datetime
import json
import re
from pathlib import Path
from report_pipeline.paths import CODE_ROOT

ROOT = CODE_ROOT
PROMPT = ROOT / 'analysis/prompts/16_01_text_only_repair_sufficiency.system.md'
SCHEMA = ROOT / 'analysis/prompts/16_02_text_only_repair_sufficiency.schema.json'
POLICY_VERSION = 'visual-necessity-human-funnel-v1'
HUMAN_LABELS = ('human_confirmed_visual_candidate', 'human_confirmed_text_sufficient',
                'visual_helpful_only', 'needs_agent_ablation',
                'needs_human_problem_statement', 'invalid_or_leaky')

MARKDOWN_IMAGE = re.compile(r'!\[[^\]]*\]\([^\n)]*\)')
MARKDOWN_REFERENCE_IMAGE = re.compile(r'!\[[^\]]*\]\s*\[[^\]]*\]')
HTML_IMAGE = re.compile(r'<img\b[^>]*>', re.IGNORECASE)


def mask_visuals(text, asset_urls=()):
    """Remove pixels, image alt text and known asset URLs from solver-visible text."""
    count = 0
    def placeholder(_match=None):
        nonlocal count
        count += 1
        return ''
    masked = MARKDOWN_IMAGE.sub(placeholder, text)
    masked = MARKDOWN_REFERENCE_IMAGE.sub(placeholder, masked)
    masked = HTML_IMAGE.sub(placeholder, masked)
    for url in sorted(set(asset_urls), key=len, reverse=True):
        if url and url in masked:
            masked = masked.replace(url, placeholder())
    return masked, count


def bind_schema(packet, schema_path=SCHEMA):
    schema = json.loads(Path(schema_path).read_text())
    schema['properties']['case_id']['const'] = packet['case_id']
    source_ids = [source['source_id'] for source in packet['problem_sources']]
    quote_source = schema['properties']['evidence']['properties']['evidence_quotes']['items']['properties']['source_id']
    if source_ids:
        quote_source['enum'] = source_ids
    paths = packet['baseline_file_index']
    candidates = schema['properties']['localization']['properties']['candidate_paths']
    if paths:
        candidates['items']['enum'] = paths
    else:
        candidates['maxItems'] = 0
    return schema


def validate(annotation, packet, schema_path=SCHEMA):
    import jsonschema
    jsonschema.validate(annotation, json.loads(Path(schema_path).read_text()))
    if annotation['case_id'] != packet['case_id']:
        raise ValueError('Case identity mismatch')
    source_text = {source['source_id']: source['text'] for source in packet['problem_sources']}
    for evidence in annotation['evidence']['evidence_quotes']:
        if evidence['source_id'] not in source_text or evidence['quote'] not in source_text[evidence['source_id']]:
            raise ValueError('Evidence quote is not an exact source substring')
    paths = annotation['localization']['candidate_paths']
    if len(paths) != len(set(paths)) or not set(paths) <= set(packet['baseline_file_index']):
        raise ValueError('Candidate path absent from baseline file index')
    contract, test, counter = annotation['repair_contract'], annotation['test_contract'], annotation['counterfactual']
    if contract['completeness'] == 'complete' and contract['unresolved_variables']:
        raise ValueError('Complete contract cannot retain unresolved variables')
    if contract['completeness'] in ('partial', 'insufficient') and not contract['unresolved_variables']:
        raise ValueError('Incomplete contract must name unresolved variables')
    if test['constructible'] == 'yes' and test['missing_oracles']:
        raise ValueError('Constructible test cannot retain missing oracles')
    if counter['multiple_repairs_fit_text'] == 'yes' and not counter['examples']:
        raise ValueError('Ambiguity requires counterfactual examples')
    if counter['multiple_repairs_fit_text'] == 'no' and counter['examples']:
        raise ValueError('Unambiguous result cannot include counterfactual examples')


def text_decision(annotation):
    """Route conservatively; model confidence never turns a case into an automatic accept."""
    evidence, contract = annotation['evidence'], annotation['repair_contract']
    test, counter = annotation['test_contract'], annotation['counterfactual']
    base = {'policy_version': POLICY_VERSION, 'empirical_visual_dependence': 'not_tested'}
    if evidence['problem_sources_usable'] != 'yes':
        return {**base, 'bucket': 'review', 'reason_code': 'problem_sources_not_usable'}
    if (contract['completeness'] == 'complete' and not contract['unresolved_variables']
            and test['constructible'] == 'yes' and not test['missing_oracles']
            and counter['multiple_repairs_fit_text'] == 'no'):
        return {**base, 'bucket': 'text_sufficient', 'reason_code': 'complete_text_repair_and_test_contract'}
    if (contract['completeness'] in ('partial', 'insufficient') and contract['unresolved_variables']
            and test['constructible'] in ('no', 'unknown') and test['missing_oracles']
            and counter['multiple_repairs_fit_text'] == 'yes'):
        return {**base, 'bucket': 'visual_candidate', 'reason_code': 'text_leaves_testable_contract_ambiguous'}
    return {**base, 'bucket': 'review', 'reason_code': 'mixed_or_unknown_text_sufficiency_evidence'}


def reconcile(visual_decision, text_only_decision):
    """Combine independent judgments into an audit or human queue; never auto-accept."""
    visual = (visual_decision or {}).get('bucket', 'not_run')
    text = (text_only_decision or {}).get('bucket', 'not_run')
    base = {'policy_version': POLICY_VERSION, 'visual_bucket': visual, 'text_bucket': text,
            'human_required_for_acceptance': True, 'agent_ablation_required_now': False}
    if visual in ('excluded', 'ocr_auxiliary') and text == 'text_sufficient':
        return {**base, 'queue': 'automatic_exclusion_audit', 'reason_code': 'both_routes_say_pixels_not_required'}
    if visual == 'visual_necessary' and text == 'visual_candidate':
        return {**base, 'queue': 'high_priority_human', 'reason_code': 'independent_routes_agree_on_missing_visual_contract'}
    if visual in ('visual_necessary', 'visual_helpful') and text in ('visual_candidate', 'review'):
        return {**base, 'queue': 'human_review', 'reason_code': 'plausible_visual_value_requires_human_adjudication'}
    if visual == 'not_run':
        return {**base, 'queue': 'visual_verifier_pending', 'reason_code': 'stage09_visual_judgment_missing'}
    if text == 'not_run':
        return {**base, 'queue': 'text_verifier_pending', 'reason_code': 'stage16_text_judgment_missing'}
    return {**base, 'queue': 'human_review', 'reason_code': 'verifier_disagreement_or_unknown'}


def human_record(case_id, packet_sha256, visual_result_sha256=None, text_result_sha256=None):
    return {'schema_version': 'visual-necessity-human-review-v1', 'case_id': case_id,
            'packet_sha256': packet_sha256, 'visual_result_sha256': visual_result_sha256,
            'text_result_sha256': text_result_sha256, 'reviewer': None, 'reviewed_at': None,
            'text_first_notes': '', 'text_first_recorded_at': None,
            'images_revealed_at': None, 'visual_delta': '',
            'patch_and_test_alignment': '', 'decision': None, 'decision_reason': '',
            'agent_ablation': {'required': False, 'reason': '', 'status': 'not_run'}}


def validate_human_record(record):
    if record['decision'] not in HUMAN_LABELS:
        raise ValueError('Human decision is missing or invalid')
    if not str(record.get('reviewer') or '').strip():
        raise ValueError('Completed human review missing reviewer')
    for key in ('reviewed_at', 'text_first_notes', 'decision_reason'):
        if not record[key]:
            raise ValueError('Completed human review missing ' + key)
    try:
        recorded, revealed, reviewed = (
            datetime.fromisoformat(str(record[key]).replace('Z', '+00:00'))
            for key in ('text_first_recorded_at', 'images_revealed_at', 'reviewed_at'))
    except (KeyError, ValueError) as exc:
        raise ValueError('Completed human review has invalid text-first timestamps') from exc
    if any(value.tzinfo is None for value in (recorded, revealed, reviewed)):
        raise ValueError('Completed human review timestamps require timezones')
    if not recorded <= revealed <= reviewed:
        raise ValueError('Completed human review text-first chronology is invalid')
    if record['decision'] == 'human_confirmed_visual_candidate':
        if not record['visual_delta'] or not record['patch_and_test_alignment']:
            raise ValueError('Visual candidate needs image delta and patch/test alignment')
    if record['decision'] == 'needs_agent_ablation' and not record['agent_ablation']['required']:
        raise ValueError('Ablation label must explicitly request ablation')


def schema_for_packet(packet, schema_path=SCHEMA):
    """Return a deep copy for tests and callers that need a JSON-compatible value."""
    return copy.deepcopy(bind_schema(packet, schema_path))
