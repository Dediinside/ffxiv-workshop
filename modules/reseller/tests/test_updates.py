import io
import json
import unittest
from urllib.error import URLError
import update_checker


class Response(io.BytesIO):
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()


class UpdatesTests(unittest.TestCase):
    def test_newer_equal_and_numeric_versions(self):
        class Opener:
            def open(self, req, timeout):
                return Response(json.dumps({'tag_name': 'v0.2', 'draft': False, 'prerelease': False}).encode())
        repo = 'Dediinside/ffxiv-reseller-paradise'
        self.assertEqual(update_checker.check_release(repo, '0.1', Opener())['status'], 'available')
        self.assertEqual(update_checker.check_release(repo, '0.2', Opener())['status'], 'current')
        self.assertGreater(update_checker.version('0.10'), update_checker.version('0.2'))

    def test_bad_schema_never_claims_current(self):
        class Opener:
            def open(self, req, timeout):
                return Response(b'{"tag_name":"latest"}')
        self.assertEqual(update_checker.check_release('Dediinside/ffxiv-reseller-paradise', '0.1', Opener())['status'], 'network')

    def test_offline_is_nonfatal_and_repository_is_allowlisted(self):
        class Opener:
            def open(self, req, timeout):
                raise URLError('offline')
        self.assertEqual(update_checker.check_release('Dediinside/ffxiv-reseller-paradise', '0.1', Opener())['status'], 'network')
        with self.assertRaises(ValueError):
            update_checker.check_release('unknown/repo', '0.1', Opener())
