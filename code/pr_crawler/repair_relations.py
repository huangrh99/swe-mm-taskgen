"""Multi-signal recall, not a causal repair-failure classifier. Never drops PRs."""
from collections import defaultdict
import hashlib
import itertools
import re

from .core import references, timestamp

SIGNALS = re.compile(r'\b(missed|still broken|incomplete fix|not fixed|follow[- ]up|regression|revert|backport|cherry[- ]pick|release)\b', re.I)
NEGATION = re.compile(r'\b(not (?:a )?regression|no longer (?:broken|failing)|not caused by)\b', re.I)
STOP = {'fix', 'fixes', 'bug', 'the', 'with', 'from', 'that', 'when', 'this', 'update', 'support', 'add', 'test', 'tests'}


def clean(text):
    text = re.sub(r'<!--[\s\S]*?-->', '', text or '')
    return re.sub(r'(?m)^\s*(```|~~~)[\s\S]*?^\s*\1[^\n]*$', '', text)


def identifier(row):
    return f'{row["repo"].lower()}#{row["number"]}'


def node(row):
    return {'pr_id': identifier(row), 'repo': row['repo'].lower(), 'number': row['number'],
        'title': row.get('title', ''), 'created_at': row.get('created_at'), 'merged_at': row.get('merged_at'),
        'merge_commit_sha': row.get('merge_commit_sha'), 'base_sha': (row.get('base') or {}).get('sha'),
        'base_ref': (row.get('base') or {}).get('ref'), 'state': row.get('state'), 'external': False}


def build(rows, archives=(), max_group=60, max_edges=20000):
    nodes, texts, issues, files, commits, events = {}, {}, defaultdict(set), defaultdict(set), defaultdict(set), []
    for row in rows:
        key = identifier(row)
        if key in nodes:
            raise ValueError('Duplicate PR in relation input: ' + key)
        nodes[key] = node(row)
        texts[key] = [{'source_id': 'index:title', 'text': row.get('title', ''), 'url': row.get('html_url')},
                      {'source_id': 'index:body', 'text': row.get('body') or '', 'url': row.get('html_url')}]
    for record in archives:
        s = record['sections']
        pr = s['pull_request'].get('data') or {}
        if not pr:
            continue
        pr = dict(pr, repo=record['repo'])
        key = identifier(pr)
        nodes[key] = node(pr)
        nodes[key]['merge_commit_sha'] = record['archival_view'].get('git_anchors', {}).get('merge_sha') or pr.get('merge_commit_sha')
        # A referenced PR/Issue's words are not assertions made by this PR.
        # They remain in the source archive, but are not attributed to this node.
        texts[key] = [d for d in record['archival_view']['documents']
                      if d.get('kind') not in ('issue', 'issue_comment', 'referenced_pr', 'referenced_pr_comment')]
        for event in s.get('timeline', {}).get('items', []):
            source = event.get('source', {}).get('issue', {})
            url = source.get('html_url', '')
            if '/pull/' in url:
                texts[key].append({'source_id': f'pr_timeline:{event.get("id", "anonymous")}',
                    'text': url, 'url': event.get('url') or url,
                    'signal_kind': 'pr_timeline_cross_reference', 'event_created_at': event.get('created_at')})
        for f in s.get('files', {}).get('items', []):
            files[(record['repo'], f['filename'])].add(key)
        for c in s.get('commits', {}).get('items', []):
            commits[(record['repo'], c['sha'])].add(key)
        for linked in s.get('linked_issues', {}).get('items', []):
            if linked['kind'] == 'issue':
                issues[(linked['repo'], linked['number'])].add(key)
                closed = None
                for event in sorted(linked.get('timeline', {}).get('items', []), key=lambda e: e.get('created_at') or ''):
                    if event.get('event') == 'closed':
                        closed = event
                    if event.get('event') == 'reopened' and closed:
                        events.append({'pr_id': key, 'signal': 'issue_closed_then_reopened',
                            'issue': f'{linked["repo"]}#{linked["number"]}', 'closed_event': closed, 'reopened_event': event,
                            'after_pr_merge': bool(pr.get('merged_at') and event.get('created_at') and timestamp(event['created_at']) > timestamp(pr['merged_at']))})
            for event in linked.get('timeline', {}).get('items', []):
                source = event.get('source', {}).get('issue', {})
                url = source.get('html_url', '')
                if '/pull/' in url:
                    texts[key].append({'source_id': f'timeline:{linked["repo"]}#{linked["number"]}:{event.get("id", "anonymous")}',
                        'text': url, 'url': event.get('url') or url,
                        'signal_kind': 'linked_issue_timeline_co_reference' if linked['kind'] == 'issue' else 'referenced_pr_timeline_co_reference',
                        'event_created_at': event.get('created_at')})
    edges, node_signals, omissions = {}, [], []
    def add(a, b, signal):
        if a == b:
            return
        a, b = sorted((a, b))
        if (a, b) not in edges:
            if len(edges) >= max_edges:
                omissions.append({'reason': 'edge_budget', 'a': a, 'b': b})
                return
            na, nb = nodes[a], nodes[b]
            dates = [na.get('merged_at'), nb.get('merged_at')]
            older, newer = (a, b) if not all(dates) or timestamp(dates[0]) <= timestamp(dates[1]) else (b, a)
            edges[(a, b)] = {'edge_id': hashlib.sha256((a + '|' + b).encode()).hexdigest()[:24],
                'a': older, 'b': newer, 'order_verified_by_merge_time': bool(all(dates) and timestamp(dates[0]) != timestamp(dates[1])),
                'signals': [], 'relation_type': 'unknown', 'review_status': 'pending',
                'action': 'retain_both_pending_review', 'runtime_validation': 'not_executed'}
        if signal not in edges[(a, b)]['signals']:
            edges[(a, b)]['signals'].append(signal)
    for key, documents in list(texts.items()):
        n = nodes[key]
        for doc in documents:
            body = clean(doc['text'])
            # Blockquotes are retained as qualified signals but never treated as current assertions.
            for line in body.splitlines():
                if SIGNALS.search(line):
                    node_signals.append({'pr_id': key, 'source_id': doc['source_id'], 'quote': line,
                        'url': doc.get('url'), 'signal': 'repair_language',
                        'qualification': 'quoted_context' if line.lstrip().startswith('>') else
                            'negated_context' if NEGATION.search(line) else
                            'template_like' if re.search(r'minimize regression|\[ \]|ensure.*regression', line, re.I) else 'unverified_assertion'})
            reference_text = '\n'.join(line for line in body.splitlines() if not line.lstrip().startswith('>'))
            for (repo, number), ref in references(doc.get('reference_repo', n['repo']), n['number'], [(doc['source_id'], reference_text)]).items():
                target = f'{repo}#{number}'
                if target in nodes or re.search(r'github\.com/' + re.escape(repo) + r'/pull/' + str(number) + r'\b', reference_text, re.I):
                    if target not in nodes:
                        nodes[target] = {'pr_id': target, 'repo': repo, 'number': number, 'title': '',
                            'merged_at': None, 'external': True, 'metadata_status': 'not_collected'}
                    # Keep exact source text; no claim that a reference implies a failed fix.
                    add(key, target, {'kind': doc.get('signal_kind', 'explicit_pr_reference'), 'from_pr': key,
                        'source_id': doc['source_id'], 'url': doc.get('url'), 'quote': reference_text,
                        'event_created_at': doc.get('event_created_at')})
                elif re.search(r'\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(?:https://github.com/[\w.-]+/[\w.-]+/issues/|#)' + str(number) + r'\b', reference_text, re.I):
                    issues[(repo, number)].add(key)
    for kind, groups in [('shared_issue_reference', issues), ('shared_changed_file', files), ('shared_commit', commits)]:
        for subject, members in sorted(groups.items()):
            if len(members) > max_group:
                omissions.append({'reason': 'large_posting_group', 'kind': kind, 'subject': str(subject), 'members': len(members)})
                continue
            for a, b in itertools.combinations(sorted(members), 2):
                add(a, b, {'kind': kind, 'subject': list(subject), 'meaning': 'retrieval_signal_not_causality'})
    # Lexical title similarity is a separate recall path; shared files are never required.
    tokens, postings = {}, defaultdict(list)
    for key, n in nodes.items():
        words = set(re.findall(r'[a-z][a-z0-9_]{3,}', n['title'].lower())) - STOP
        tokens[key] = words
        for word in words:
            postings[(n['repo'], word)].append(key)
    seen_pairs = set()
    for subject, members in sorted(postings.items()):
        if len(members) > max_group:
            omissions.append({'reason': 'large_posting_group', 'kind': 'title_token',
                              'subject': list(subject), 'members': len(members)})
            continue
        for a, b in itertools.combinations(sorted(members), 2):
            if (a, b) in seen_pairs:
                continue
            seen_pairs.add((a, b))
            common = tokens[a] & tokens[b]
            score = len(common) / max(1, len(tokens[a] | tokens[b]))
            if len(common) >= 3 and score >= 0.5:
                add(a, b, {'kind': 'title_lexical_similarity', 'score': round(score, 4), 'shared_terms': sorted(common)})
    return {'nodes': list(nodes.values()), 'edges': list(edges.values()), 'node_signals': node_signals,
            'reopen_events': events, 'omissions': omissions,
            'limitations': ['No signal is a failure verdict; no PR is removed',
                'Current mutable source snapshots, not historical text reconstruction',
                'Lexical title recall is not semantic all-pairs retrieval',
                'Linked Issue and referenced PR text is archived but not attributed to the current PR',
                'Missing later evidence is not proof of successful repair; observation is right-censored',
                'No ancestry, patch equivalence, revert survival, F2P or P2P execution check']}
