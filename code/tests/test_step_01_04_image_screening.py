import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from pr_crawler.image_screening import classify, discover_body
from analysis.scripts.step_01_screen_pr_body_images import ALL_IMAGES, ALL_EVIDENCE, PARTITIONS, screen
from analysis.scripts.step_04_01_type_attachments import export_final, load_evidence
from analysis.scripts.step_04_02_audit_image_screening import audit


class ImageScreeningTests(unittest.TestCase):
    def test_markdown_reference_html_relative_and_nested_destination(self):
        body = '![a][ref]\n\n[ref]: https://example.com/a_(b).png\n\n<img src="/relative/a.png" alt="before">'
        rows = discover_body(body)
        self.assertEqual(2, len(rows))
        self.assertTrue(all(a['media_kind'] == 'image' for a in rows))
        self.assertEqual('relative_unresolved', rows[1]['url_resolution'])

    def test_ignore_code_comments_and_non_media_links(self):
        body = '<!-- ![placeholder](https://example.com/a.png) -->\n\n`![x](https://example.com/b.png)`\n\n```html\n<img src="https://example.com/c.png">\n```\n\n[code](https://github.com/o/r/pull/1)'
        self.assertEqual([], discover_body(body))

    def test_details_and_inline_html_images(self):
        body = '<details>\n![x](https://example.com/a.png)\n</details>\n\nText <img src="https://example.com/b.webp"> more'
        self.assertEqual(2, len(discover_body(body)))

    def test_inline_html_code_context_is_preserved_across_tokens(self):
        self.assertEqual([], discover_body('Text <code>https://example.com/a.png</code> end'))

    def test_badges_preserved_separately_not_all_bots_dropped(self):
        badge = '![age](https://developer.mend.io/api/mc/badges/age/npm/x/1)'
        self.assertEqual('only_badge_or_decoration_image_evidence', classify(discover_body(badge)))
        self.assertEqual('non_badge_image_evidence', classify(discover_body(badge + '\n![real](https://example.com/real.png)')))

    def test_github_untyped_attachment_is_not_an_image_without_evidence(self):
        url = 'https://github.com/user-attachments/assets/uuid'
        self.assertEqual('untyped_attachment_without_image_evidence', classify(discover_body(url)))
        self.assertEqual('non_badge_image_evidence', classify(discover_body(f'![x]({url})')))

    def test_videos_gifs_and_conflicting_media(self):
        self.assertEqual('video_without_image_evidence', classify(discover_body('https://example.com/a.mp4')))
        self.assertEqual('image', discover_body('https://example.com/a.gif?raw=true')[0]['media_kind'])
        self.assertEqual('conflicting', discover_body('![x](https://example.com/a.mp4)')[0]['media_kind'])

    def test_duplicate_assets_and_bad_urls(self):
        self.assertEqual(1, len(discover_body('![x](https://example.com/a.png)\nhttps://example.com/a.png')))
        self.assertEqual([], discover_body('<img src="https://user:password@example.com/a.png">'))

    def test_partition_full_record_fidelity_and_tmp_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'input.jsonl'
            bodies = ['![x](https://example.com/a.png)', '![badge](https://img.shields.io/x)',
                      'https://github.com/user-attachments/assets/uuid', 'https://example.com/a.mp4', 'no media']
            original = [{"repo": "o/r", "number": n, "id": n, "created_at": "2025-01-01T00:00:00Z", "body": b, "custom": {"keep": n}}
                        for n, b in enumerate(bodies)]
            source.write_text(''.join(json.dumps(r) + '\n' for r in original))
            result = screen(source, root / 'result', root / 'tmp')
            self.assertEqual(5, result['counts']['input_prs'])
            self.assertEqual(2, result['counts']['all_image_evidence_including_badges'])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), result['input_sha256'])
            self.assertEqual([], list((root / 'tmp').iterdir()))
            identities = []
            for category, name in PARTITIONS.items():
                rows = [json.loads(s) for s in (root / 'result' / name).read_text().splitlines()]
                self.assertEqual(1, len(rows))
                identities += [r['id'] for r in rows]
                if category != 'no_detected_media_in_pr_body':
                    self.assertEqual(original[rows[0]['id']], {k:v for k,v in rows[0].items() if k != 'image_screening'})
            self.assertEqual(list(range(5)), sorted(identities))
            self.assertEqual(5, len((root / 'result' / ALL_EVIDENCE).read_text().splitlines()))
            evidence = load_evidence(root / 'result')
            asset = evidence[('o/r',2)]['image_screening']['assets'][0]
            checks = {asset['asset_id']:{'asset_id':asset['asset_id'],'url':asset['url'],'status':'typed','media_kind':'image'}}
            typed = export_final(root / 'result', root / 'tmp', evidence, checks)
            self.assertEqual(2,typed['counts']['non_badge_image_evidence'])
            self.assertEqual(1,typed['counts']['additional_non_badge_image_prs_from_attachment_typing'])
            self.assertTrue(audit(root / 'result')['passed'])
            target=root/'result/04_pr_body_images_after_attachment_typing/04_prs_with_non_badge_images.jsonl'
            records=[json.loads(line) for line in target.read_text().splitlines()]
            records[0]['body']='altered'
            target.write_text(''.join(json.dumps(r)+'\n' for r in records))
            self.assertFalse(audit(root / 'result')['passed'])


if __name__ == '__main__':
    unittest.main()
