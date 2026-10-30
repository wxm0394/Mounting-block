"""get_user_profile resilience tests (Plan Item 4).

Strict split between "no profile row" and "DB unreachable":
  Case A: Supabase query succeeds with res.data == [] -> free defaults,
          /annotate returns 200 with the default 5000-char limit.
  Case B: Supabase raises httpx.ConnectTimeout on all retries ->
          /annotate returns 503, NOT a silent downgrade to free tier.

Auth is bypassed the same way as test_target_lang_and_difficulty.py
(dependency_overrides on get_current_user). Same import notes: dummy
Supabase env vars, sandbox proxy env vars removed before import.
"""
import os

for _v in ("http_proxy", "https_proxy", "all_proxy",
           "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
           "no_proxy", "NO_PROXY"):
    os.environ.pop(_v, None)
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "dummy-key-for-test")

from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import httpx
from fastapi.testclient import TestClient

import backend
from backend import app, get_current_user


class MockUser:
    id = "00000000-0000-0000-0000-000000000001"
    email = "test@example.com"


app.dependency_overrides[get_current_user] = lambda: MockUser()
client = TestClient(app, raise_server_exceptions=False)


def _mock_supabase_with_data(data=None, side_effect=None):
    """Mock backend.supabase whose user_profiles query returns data / raises."""
    mock = MagicMock()
    query = mock.table.return_value.select.return_value.eq.return_value
    if side_effect is not None:
        query.execute.side_effect = side_effect
    else:
        query.execute.return_value = SimpleNamespace(data=data)
    return mock


def test_get_user_profile_not_found_returns_defaults(capsys):
    with patch("backend.supabase", _mock_supabase_with_data(data=[])), \
            patch("backend.try_consume_quota", return_value=5), \
            patch("backend.batch_translate",
                  side_effect=lambda ann, *a, **k: ann):
        res = client.post("/annotate", json={"text": "hello world",
                                             "target_lang": "zh-Hans"})
    assert res.status_code == 200, res.text
    body = res.json()
    # Defaults applied: free tier 5000-char daily limit, not a crash.
    assert body["usage"]["chars_limit"] == 5000


def test_get_user_profile_db_error_raises_503(capsys):
    with patch("backend.supabase",
               _mock_supabase_with_data(
                   side_effect=httpx.ConnectTimeout("timed out"))), \
            patch("time.sleep"):
        res = client.post("/annotate", json={"text": "hello world",
                                             "target_lang": "zh-Hans"})
    assert res.status_code == 503, res.text
    assert res.json() == {"detail": "用户配置服务暂时不可用，请稍后重试"}
    print('[503 RESPONSE] {"detail": "用户配置服务暂时不可用，请稍后重试"}')
