import os
import tempfile
import unittest
from unittest.mock import patch

import requests

from Main import Manager, USER_AGENT


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class FakeSession:
    def __init__(self, results):
        self.headers = {}
        self.results = list(results)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        pass


def make_manager(session):
    manager = Manager.__new__(Manager)
    manager.session = session
    manager._last_request_time = None
    manager.session.headers.update({"User-Agent": USER_AGENT})
    return manager


class NetworkTests(unittest.TestCase):
    def test_search_and_detail_share_one_session(self):
        session = FakeSession([
            FakeResponse(text="<html><body>search</body></html>"),
            FakeResponse(text="<html><body>detail</body></html>"),
        ])
        manager = make_manager(session)

        with tempfile.TemporaryDirectory() as temp_dir:
            original_cwd = os.getcwd()
            os.chdir(temp_dir)
            try:
                self.assertTrue(manager.loadSearchWeb("example", "example.jar"))
                self.assertEqual(
                    manager.isServerNeeded("https://example.test/mod", "example.jar"),
                    "未知[https://example.test/mod]",
                )
            finally:
                os.chdir(original_cwd)

        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.headers["User-Agent"], USER_AGENT)
        self.assertEqual(session.calls[0][2], (5, 15))
        self.assertIs(manager.session, session)

    def test_connection_error_retries_then_succeeds(self):
        session = FakeSession([
            requests.ConnectionError("connection closed"),
            FakeResponse(),
        ])
        manager = make_manager(session)

        with patch("Main.time.sleep") as sleep, patch("Main.time.monotonic", return_value=0):
            response = manager._request("https://example.test", "搜索", "example.jar")

        self.assertIsNotNone(response)
        self.assertEqual(len(session.calls), 2)
        self.assertIn(1, [call.args[0] for call in sleep.call_args_list])

    def test_retryable_status_stops_after_max_retries(self):
        manager = make_manager(FakeSession([FakeResponse(503) for _ in range(4)]))

        with patch("Main.time.sleep"), patch("Main.time.monotonic", return_value=0):
            response = manager._request("https://example.test", "详情", "example.jar")

        self.assertIsNone(response)
        self.assertEqual(len(manager.session.calls), 4)

    def test_non_retryable_status_is_requested_once(self):
        manager = make_manager(FakeSession([FakeResponse(404)]))

        with patch("Main.time.sleep"), patch("Main.time.monotonic", return_value=0):
            response = manager._request("https://example.test", "详情", "example.jar")

        self.assertIsNone(response)
        self.assertEqual(len(manager.session.calls), 1)

    def test_rate_limit_waits_between_requests(self):
        manager = make_manager(FakeSession([]))
        manager._last_request_time = 0

        with patch("Main.time.sleep") as sleep, patch("Main.time.monotonic", return_value=0.001):
            manager._wait_for_rate_limit()

        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.019, places=6)

    def test_failed_search_does_not_overwrite_old_cache(self):
        manager = make_manager(FakeSession([requests.Timeout("timed out") for _ in range(4)]))

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = os.path.join(temp_dir, "searchWeb.html")
            with open(cache_path, "w", encoding="utf-8") as cache_file:
                cache_file.write("old response")

            original_cwd = os.getcwd()
            os.chdir(temp_dir)
            try:
                with patch("Main.time.sleep"), patch("Main.time.monotonic", return_value=0):
                    self.assertFalse(manager.loadSearchWeb("example", "example.jar"))
                with open(cache_path, "r", encoding="utf-8") as cache_file:
                    self.assertEqual(cache_file.read(), "old response")
            finally:
                os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()
