"""Source-complete observational archives and conservative problem/curator partitions."""
import hashlib
import json
import re

from .core import collect_pr, one, rest_pages, section
from .image_screening import discover_body
from .store import now


def source_documents(record):
    sections, documents = record['sections'], []
    pr = sections['pull_request'].get('data') or {}
    def add(identifier, kind, item, body=None, field='body', relation=None):
        text = item.get(field) if body is None else body
        if text is None:
            return
        documents.append({'source_id': identifier, 'kind': kind, 'field': field, 'text': text,
            'reference_repo': (re.search(r'github\.com/([^/]+/[^/]+)/(?:issues|pull)/', item.get('html_url') or item.get('url') or '') or [None, record['repo']])[1],
            'url': item.get('html_url') or item.get('url'), 'created_at': item.get('created_at', item.get('createdAt')),
            'updated_at': item.get('updated_at', item.get('updatedAt')), 'relation': relation,
            'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'historical_version_verified': False})
    for field in ('title', 'body'):
        add('pr:' + field, 'pr', pr, field=field)
    for kind in ('comments', 'reviews', 'review_comments'):
        for item in sections.get(kind, {}).get('items', []):
            add(f'{kind}:{item["id"]}', kind, item)
    for thread in sections.get('review_threads', {}).get('items', []):
        for item in thread['comments']['items']:
            add('thread:' + item['id'], 'review_comments', item)
    for issue in sections.get('linked_issues', {}).get('items', []):
        prefix = f'{issue["repo"]}#{issue["number"]}'
        item = issue['detail'].get('data') or {}
        kind = 'issue' if issue['kind'] == 'issue' else 'referenced_pr'
        for field in ('title', 'body'):
            add(prefix + ':' + field, kind, item, field=field, relation=issue['relationship'])
        for comment in issue['comments']['items']:
            add(prefix + ':comment:' + str(comment['id']), kind + '_comment', comment)
    for item in sections.get('commits', {}).get('items', []):
        add('commit:' + item['sha'], 'commit', item, body=item.get('commit', {}).get('message', ''))
    for kind in ('diff', 'patch'):
        value = sections.get(kind, {}).get('data')
        if isinstance(value, str):
            add(kind, kind, {'html_url': pr.get('html_url')}, body=value)
    return documents


def partition(record):
    documents = source_documents(record)
    assets, reproductions = {}, []
    problem_ids = []
    for document in documents:
        # Issue text is a candidate, never automatically certified leakage-free.
        # A bare hyperlink or template/documentation reference is useful
        # curator context, but is not evidence that the Issue defines the PR's
        # problem.  Only GitHub's explicit closing relationship is safe to seed
        # as an Issue-derived problem source without a separate relation review.
        if document['kind'] == 'issue' and document.get('relation') == 'closes':
            problem_ids.append(document['source_id'])
        for asset in discover_body(document['text']):
            entry = assets.setdefault(asset['asset_id'], dict(asset, occurrences=[],
                historical_first_seen=None, temporal_role='unknown'))
            entry['occurrences'].append({k: document[k] for k in
                ('source_id', 'url', 'created_at', 'updated_at', 'text_sha256')})
        for match in re.finditer(r'https://(?:codesandbox\.io|codepen\.io|jsfiddle\.net|stackblitz\.com)/[^\s<>"\x60)]+', document['text']):
            reproductions.append({'source_id': document['source_id'], 'url': match[0], 'status': 'reference_only_not_mirrored'})
    return {'purpose': 'data_archival_and_screening', 'documents': documents,
        'problem_packet': {'status': 'needs_source_and_leakage_review', 'candidate_source_ids': problem_ids,
            'safe_for_problem_input': False, 'reason': 'Current issue text may have been edited after the fix; PR-only material is not silently promoted'},
        'curator_packet': {'source_ids': [d['source_id'] for d in documents], 'includes_solutions': True},
        'media': list(assets.values()), 'reproduction_links': reproductions,
        'execution_validation': 'not_requested', 'f2p': None, 'p2p': None}


def enrich_with_history(api, repo, number, download_media=False, asset_workers=1):
    record = collect_pr(api, repo, number, download_assets=download_media,
                        asset_workers=asset_workers)
    sections = record['sections']
    scope = f'11_history:{repo}:{number}'
    sections['timeline'] = rest_pages(api, f'/repos/{repo}/issues/{number}/timeline', scope, anonymous=True)
    for linked in sections['linked_issues']['items']:
        root = f'/repos/{linked["repo"]}'
        relationship_verified = (
            linked.get('relationship') == 'closes'
            and linked.get('confidence') == 'github_reported'
        )
        if relationship_verified:
            linked['timeline'] = rest_pages(
                api, root + f'/issues/{linked["number"]}/timeline', scope,
                anonymous=True)
            if linked['kind'] == 'pull_request':
                linked['pull_metadata'] = one(
                    api, root + f'/pulls/{linked["number"]}', scope)
            if (linked['timeline']['status'] != 'complete'
                    or linked.get('pull_metadata', {}).get('status', 'complete') != 'complete'):
                sections['linked_issues']['status'] = 'partial'
        else:
            # Keep the already archived detail/comments/labels as curator-only
            # provenance.  Do not let an unrelated template or documentation
            # hyperlink pull in an unbounded, concurrently mutating timeline.
            linked['timeline'] = section(
                status='not_required', reason='unverified_text_reference',
                pages=0, observed_count=0)
            if linked['kind'] == 'pull_request':
                linked['pull_metadata'] = {
                    'status': 'not_required',
                    'reason': 'unverified_text_reference',
                }
    pr = sections['pull_request'].get('data') or {}
    anchors = merge_anchors(pr, sections['timeline']['items'])
    sha = anchors['resolved_sha']
    sections['merge_commit'] = one(api, f'/repos/{repo}/commits/{sha}', scope) if sha else {'status': anchors['status']}
    sections['merge_anchor_evidence'] = anchors
    record['archival_view'] = partition(record)
    record['archival_view']['git_anchors'] = {
        'base_sha_observed': (pr.get('base') or {}).get('sha'), 'head_sha_observed': (pr.get('head') or {}).get('sha'),
        'merge_sha': sha, 'merge_parents': [p['sha'] for p in sections['merge_commit'].get('data', {}).get('parents', [])],
        'merge_sha_sources': anchors['sources'],
        'meaning': 'Observed GitHub metadata; merge parents are not an automatically validated benchmark base'}
    record['archive_schema'] = 'source-history-v1'
    record['history_observed_at'] = now()
    record['provenance']['response_ids'] = sorted(api.response_ids)
    record['status'] = 'complete' if record['status'] == 'complete' and all(
        sections[k]['status'] == 'complete' for k in ('timeline', 'linked_issues', 'merge_commit')) else 'partial'
    api.store.put(api.run_id, f'11_record/{repo}/{number}', record)
    return record


def merge_anchors(pr, events):
    sources = []
    if pr.get('merged_at') and pr.get('merge_commit_sha'):
        sources.append({'sha': pr['merge_commit_sha'], 'source': 'pull_request.merge_commit_sha'})
    for event in events:
        if event.get('event') == 'merged' and event.get('commit_id'):
            sources.append({'sha': event['commit_id'], 'source': 'pr_timeline.merged',
                            'url': event.get('url'), 'created_at': event.get('created_at')})
    unique = {s['sha'] for s in sources}
    return {'status': 'complete' if len(unique) == 1 else 'ambiguous' if unique else 'not_available',
            'resolved_sha': next(iter(unique)) if len(unique) == 1 else None, 'sources': sources}
