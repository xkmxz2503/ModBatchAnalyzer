import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

import requests

from Main import Manager, RATE_LIMITS, USER_AGENT, WindowRateLimiter


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
    manager._limiters = {}
    manager.session.headers.update({"User-Agent": USER_AGENT})
    return manager


class NetworkTests(unittest.TestCase):
    def test_domain_limiter_uses_second_and_minute_windows(self):
        clock = [0.0]

        def monotonic():
            return clock[0]

        def sleep(seconds):
            clock[0] += seconds

        limiter = WindowRateLimiter(per_second=1, per_minute=2)
        with patch("Main.time.monotonic", side_effect=monotonic), patch("Main.time.sleep", side_effect=sleep):
            limiter.acquire()
            clock[0] = 0.1
            limiter.acquire()
            self.assertAlmostEqual(clock[0], 1.0, places=6)
            clock[0] = 1.1
            limiter.acquire()
            self.assertAlmostEqual(clock[0], 60.0, places=6)

    def test_request_selects_limiter_by_target_domain(self):
        manager = make_manager(FakeSession([FakeResponse()]))
        limiter = manager._limiters.setdefault("modrinth.com", WindowRateLimiter(**RATE_LIMITS["modrinth.com"]))
        with patch.object(limiter, "acquire") as acquire:
            self.assertIsNotNone(manager.request("https://api.modrinth.com/v2/search", "搜索", "example.jar"))
        acquire.assert_called_once_with()

    def test_concurrent_first_domain_access_shares_one_limiter(self):
        manager = make_manager(FakeSession([]))
        barrier = threading.Barrier(8)
        limiters = []

        def get_limiter():
            barrier.wait()
            limiters.append(manager._limiter_for_url("https://api.modrinth.com/v2/search"))

        threads = [threading.Thread(target=get_limiter) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len({id(limiter) for limiter in limiters}), 1)

    def test_request_uses_session_and_timeout(self):
        session = FakeSession([
            FakeResponse(text="<html><body>search</body></html>"),
            FakeResponse(text="<html><body>detail</body></html>"),
        ])
        manager = make_manager(session)

        with tempfile.TemporaryDirectory() as temp_dir:
            original_cwd = os.getcwd()
            os.chdir(temp_dir)
            try:
                response = manager.request("https://example.test", "搜索", "example.jar")
                self.assertIsNotNone(response)
            finally:
                os.chdir(original_cwd)

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.headers["User-Agent"], USER_AGENT)
        self.assertEqual(session.calls[0][2], (5, 15))
        self.assertIs(manager.session, session)

    def test_connection_error_retries_then_succeeds(self):
        session = FakeSession([
            requests.ConnectionError("connection closed"),
            FakeResponse(),
        ])
        manager = make_manager(session)

        output = StringIO()
        with patch("Main.time.sleep") as sleep, patch("Main.time.monotonic", return_value=0), redirect_stdout(output):
            response = manager.request("https://example.test", "搜索", "example.jar")

        self.assertIsNotNone(response)
        self.assertEqual(len(session.calls), 2)
        self.assertIn(1, [call.args[0] for call in sleep.call_args_list])
        self.assertIn("[网络重试]", output.getvalue())

    def test_retryable_status_stops_after_max_retries(self):
        manager = make_manager(FakeSession([FakeResponse(503) for _ in range(4)]))

        output = StringIO()
        with patch("Main.time.sleep"), patch("Main.time.monotonic", return_value=0), redirect_stdout(output):
            response = manager.request("https://example.test", "详情", "example.jar")

        self.assertIsNone(response)
        self.assertEqual(len(manager.session.calls), 4)
        self.assertIn("[网络失败]", output.getvalue())

    def test_non_retryable_status_is_requested_once(self):
        manager = make_manager(FakeSession([FakeResponse(404)]))

        with patch("Main.time.sleep"), patch("Main.time.monotonic", return_value=0):
            response = manager.request("https://example.test", "详情", "example.jar")

        self.assertIsNone(response)
        self.assertEqual(len(manager.session.calls), 1)

    def test_rate_limit_waits_between_requests(self):
        manager = make_manager(FakeSession([]))
        manager._last_request_time = 0

        with patch("Main.time.sleep") as sleep, patch("Main.time.monotonic", return_value=0.001):
            manager.wait_for_rate_limit()

        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.019, places=6)

    def test_timeout_returns_none_after_retries(self):
        manager = make_manager(FakeSession([requests.Timeout("timed out") for _ in range(4)]))

        with patch("Main.time.sleep"), patch("Main.time.monotonic", return_value=0):
            self.assertIsNone(manager.request("https://example.test", "搜索", "example.jar"))


if __name__ == "__main__":
    unittest.main()
