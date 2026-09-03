import base64
import unittest

from pr_crawler.media_probe import probe, signature_kind
from analysis.scripts.step_04_01_type_attachments import apply_types
from pr_crawler.image_screening import discover_body


class ProbeTests(unittest.TestCase):
    def test_binary_signatures_distinguish_images_and_videos(self):
        for prefix in (b'\x89PNG\r\n\x1a\n', b'GIF89a', b'\xff\xd8\xff', b'RIFFxxxxWEBP', b'<svg xmlns="x">'):
            self.assertEqual('image', signature_kind(prefix))
        self.assertEqual('video', signature_kind(b'xxxxftypisom'))
        self.assertEqual('image', signature_kind(b'xxxxftypavif'))
        self.assertIsNone(signature_kind(b'<html>not an image</html>'))

    def fake_probe(self, kind, body, status=206):
        calls = []
        class Connection:
            def __init__(self, *args): pass
            def request(self, method, path, headers): calls.append((method, path, headers))
            def getresponse(self): return self
            def getheader(self, key): return kind if key == 'Content-Type' else None
            def read(self, maximum): return body[:maximum]
            def close(self): pass
        Connection.status = status
        result = probe({'url':'https://example.com/uuid','asset_id':'id'}, Connection, lambda url: ('example.com','8.8.8.8','/uuid'))
        return result, calls

    def test_prefix_only_no_auth_and_http_status(self):
        result,calls=self.fake_probe('image/png', b'\x89PNG\r\n\x1a\n' + b'x'*1000)
        self.assertEqual('typed',result['status'])
        self.assertEqual(512,len(base64.b64decode(result['prefix_base64'])))
        self.assertFalse(result['full_download'])
        self.assertNotIn('Authorization',calls[0][2])
        self.assertEqual('bytes=0-511',calls[0][2]['Range'])
        self.assertEqual('unavailable',self.fake_probe('text/html',b'no',404)[0]['status'])
        self.assertEqual('unresolved',self.fake_probe('text/html',b'<html>login</html>')[0]['status'])

    def test_conflicting_type_and_empty_response_remain_unknown(self):
        self.assertEqual('unresolved',self.fake_probe('video/mp4',b'GIF89a')[0]['status'])
        self.assertEqual('unresolved',self.fake_probe('image/png',b'')[0]['status'])

    def test_typing_does_not_mutate_original_evidence(self):
        assets=discover_body('https://github.com/user-attachments/assets/uuid')
        old={'assets':assets,'category':'untyped_attachment_without_image_evidence'}
        result=apply_types(old,{assets[0]['asset_id']:{'status':'typed','media_kind':'image'}})
        self.assertEqual('non_badge_image_evidence',result['category'])
        self.assertEqual('untyped_attachment',old['assets'][0]['media_kind'])


if __name__=='__main__':
    unittest.main()
