"""Worker fault tolerance tests (Plan Item 2).

Covers epub_worker.supabase_retry + rebuild_supabase_client:
  Case A: RemoteProtocolError("Server disconnected") on attempts 1-2,
          success on attempt 3 -> 2 rebuilds, [SUCCESS] on attempt 3.
  Case B: APIError code 555 on all attempts -> [ERROR] after 3 retries, raised.

Notes:
  - epub_worker import creates a real Supabase client (Settings requires env
    vars); dummy values suffice because create_client does not connect.
  - The sandbox's proxy env vars (notably no_proxy with IPv6 literals) break
    httpx URL parsing inside supabase-py, so they are removed before import.
    This is a test-environment quirk, not production behavior.
"""
import os

for _v in ("http_proxy", "https_proxy", "all_proxy",
           "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
           "no_proxy", "NO_PROXY"):
    os.environ.pop(_v, None)
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "dummy-key-for-test")

import httpx
import pytest
from unittest.mock import patch

import epub_worker


class FakeAPIError(Exception):
    """Stand-in for postgrest.APIError (has .code like the real one)."""

    def __init__(self, code):
        self.code = code
        super().__init__(f"API error with code {code}")


def test_supabase_retry_and_rebuild_success(capsys):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.RemoteProtocolError("Server disconnected")
        return "ok"

    with patch("epub_worker.rebuild_supabase_client") as mock_rebuild, \
            patch("time.sleep"):
        assert epub_worker.supabase_retry(flaky)() == "ok"

    assert calls["n"] == 3
    assert mock_rebuild.call_count == 2
    out = capsys.readouterr().out
    assert "[REBUILD] Triggered client rebuild due to: Server disconnected (attempt 1/3)" in out
    assert "[REBUILD] Triggered client rebuild due to: Server disconnected (attempt 2/3)" in out
    assert "[SUCCESS] Operation completed on attempt 3." in out


def test_supabase_retry_exhausted_logs_error(capsys):
    def always_fail():
        raise FakeAPIError("555")

    with patch("epub_worker.rebuild_supabase_client") as mock_rebuild, \
            patch("time.sleep"):
        with pytest.raises(FakeAPIError):
            epub_worker.supabase_retry(always_fail)()

    assert mock_rebuild.call_count == 2
    out = capsys.readouterr().out
    assert "[ERROR] Supabase operation failed after 3 retries: code 555" in out
