"""Audit stage-17 identity coverage, full-source embedding, links, and HTML structure."""

import argparse
import hashlib
from html.parser import HTMLParser
import html
import json
from pathlib import Path
import re


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Parser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []
        self.rows = 0

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'a' and values.get('href'):
            self.hrefs.append(values['href'])
        if tag == 'tr' and values.get('data-repo'):
            self.rows += 1


def audit(run, index):
    run, index = Path(run).resolve(), Path(index).resolve()
    manifest_path = run / '16_03_run_manifest.json'
    manifest = json.loads(manifest_path.read_text())
    index_text = index.read_text()
    parser = Parser()
    parser.feed(index_text)
    detail_links = [href for href in parser.hrefs if href.startswith('17_02_pr_details/')]
    if parser.rows != len(manifest['pr_ids']) or len(detail_links) != len(manifest['pr_ids']):
        raise ValueError('Index row/detail-link coverage mismatch')
    if len(detail_links) != len(set(detail_links)):
        raise ValueError('Duplicate detail link')
    archive_characters = pr_body_characters = embedded_images = 0
    details = {}
    for position, (pr_id, relative) in enumerate(zip(manifest['pr_ids'], detail_links), 1):
        detail = index.parent / relative
        if not detail.is_file():
            raise FileNotFoundError(detail)
        text = detail.read_text()
        parsed = Parser()
        parsed.feed(text)
        result = json.loads((run / f'16_03_result_{position:04d}.json').read_text())
        if result['pr_id'] != pr_id:
            raise ValueError('Result identity mismatch: ' + pr_id)
        packet = json.loads(Path(result['packet']).read_text())
        archive_path = Path(packet['provenance']['source_archive'])
        archive_text = archive_path.read_text()
        archive = json.loads(archive_text)
        pull = archive['sections']['pull_request']['data']
        body = pull.get('body') or ''
        full_json = json.dumps(archive, ensure_ascii=False, indent=2)
        display_body = body.replace('\r\n', '\n').replace('\r', '\n')
        required = [html.escape(pr_id, quote=True), html.escape(pull.get('title') or '', quote=True),
                    html.escape(display_body, quote=True), html.escape(full_json, quote=True),
                    html.escape(str(archive_path), quote=True)]
        if any(value not in text for value in required):
            raise ValueError('Detail omits identity, source text, or complete archive: ' + pr_id)
        archive_characters += len(full_json)
        pr_body_characters += len(body)
        embedded_images += len(re.findall(r'<img\b[^>]*\bsrc="data:image/', text))
        details[detail.name] = digest(detail)
    status = 'passed'
    report = {
        'schema_version': 'cross-repo-funnel-static-audit-v1', 'status': status,
        'source_run': str(run), 'source_manifest_sha256': digest(manifest_path),
        'index': str(index), 'index_sha256': digest(index),
        'index_rows': parser.rows, 'detail_pages': len(details), 'detail_sha256': details,
        'repositories': len({value.rsplit('#', 1)[0] for value in manifest['pr_ids']}),
        'complete_archive_json_characters_embedded': archive_characters,
        'complete_pr_body_characters_embedded': pr_body_characters,
        'embedded_local_images': embedded_images,
        'full_source_embedding_verified': True, 'browser_interaction_verified': False,
        'browser_limit': 'Local file URL navigation blocked by browser security policy',
    }
    output = index.parent / '17_03_static_audit.json'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--index', type=Path, required=True)
    args = parser.parse_args()
    print(audit(args.run, args.index))


if __name__ == '__main__':
    main()
