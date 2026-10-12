"""Public release checks: version comparison, failures and a responsive Tk UI."""
import io
import json
import threading
import time
import tkinter as tk
import unittest
import urllib.error
from unittest.mock import patch

from update_checker import UpdateBar, check_release, version

REPO = 'Dediinside/ffxiv-market-scout'


class FakeOpener:
    def __init__(self, data=None, error=None):
        self.data, self.error, self.request, self.timeout = data, error, None, None

    def open(self, request, timeout):
        self.request, self.timeout = request, timeout
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.data).encode())


class ReleaseTests(unittest.TestCase):
    def test_numeric_versions_and_optional_v(self):
        self.assertEqual(version('v0.1'), version('0.1.0'))
        self.assertGreater(version('0.1.10'), version('0.1.9'))
        for bad in ('1', 'v0.1-beta', '../1.2', None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                version(bad)

    def test_public_request_newer_and_canonical_link(self):
        opener = FakeOpener({'tag_name': 'v0.1.10', 'html_url': 'https://evil.invalid'})
        result = check_release(REPO, '0.1.9', opener)
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['url'], f'https://github.com/{REPO}/releases/tag/v0.1.10')
        self.assertFalse(opener.request.has_header('Authorization'))
        self.assertEqual(opener.timeout, 8)

    def test_equal_and_older_are_current(self):
        for tag in ('v0.1.2', 'v0.1.1'):
            self.assertEqual(check_release(REPO, '0.1.2', FakeOpener({'tag_name': tag}))['status'], 'current')

    def test_bad_response_never_claims_current(self):
        for data in ([], {}, {'tag_name': '../0.2'}, {'tag_name': 'v0.2', 'prerelease': True},
                     {'tag_name': 'v0.2', 'draft': True}):
            self.assertEqual(check_release(REPO, '0.1', FakeOpener(data))['status'], 'network')

    def test_offline_and_http_errors_are_nonfatal(self):
        errors = [OSError('offline')] + [urllib.error.HTTPError('https://api.github.com', n, 'error', {}, None)
                                        for n in (401, 403, 404, 500)]
        for error in errors:
            result = check_release(REPO, '0.1', FakeOpener(error=error))
            self.assertNotIn(result['status'], ('available', 'current'))
            self.assertEqual(result['url'], f'https://github.com/{REPO}/releases')

    def test_unexpected_repository_is_rejected(self):
        with self.assertRaises(ValueError):
            check_release('other/repo', '0.1')


class UpdateUITests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()

    def tearDown(self):
        self.root.destroy()

    def test_demo_does_not_start_network(self):
        with patch('update_checker.check_release') as check:
            bar = UpdateBar(self.root, REPO, '0.1', demo=True)
            self.assertIsNone(bar.start_timer)
            self.root.update()
            check.assert_not_called()

    def test_background_check_keeps_ui_responsive_and_returns_message(self):
        entered, finish = threading.Event(), threading.Event()
        def slow_check(*args):
            entered.set()
            finish.wait(2)
            return dict(message='New release', url='https://github.com/' + REPO + '/releases')
        with patch('update_checker.check_release', slow_check):
            bar = UpdateBar(self.root, REPO, '0.1', demo=True)
            try:
                bar.start()
                self.assertTrue(entered.wait(1))
                self.root.update()
                self.assertTrue(bar.busy)
                self.assertEqual(str(bar.button['state']), 'disabled')
            finally:
                finish.set()
            deadline = time.monotonic() + 2
            while bar.busy and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
            self.assertEqual(bar.text.get(), 'New release')
            self.assertFalse(bar.busy)

    def test_normal_startup_schedules_check_and_close_cancels_timers(self):
        bar = UpdateBar(self.root, REPO, '0.1')
        self.assertIsNotNone(bar.start_timer)
        bar.frame.destroy()
        self.assertTrue(bar.closed)
        bar.start()
        self.assertFalse(bar.busy)
