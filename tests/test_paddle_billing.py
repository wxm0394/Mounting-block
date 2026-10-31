"""Paddle Billing module tests.

Covers, mirroring the LemonSqueezy handler's guarantees:
  - verify_paddle_signature: ts:raw_body HMAC, multi-h1 rotation, stale ts.
  - resolve_paddle_subscription_status: Paddle event -> is_subscribed mapping.
  - /webhook/paddle: signature gate, event_id dedup, occurred_at monotonicity,
    profile update, full-refund period-end cancel (best-effort, after audit).
  - /billing/checkout: plan -> price id, custom_data.user_id binding.
  - /billing/config: plan availability booleans, no price ids leaked.

Same import notes as test_user_profile_resilience.py: dummy Supabase env vars,
sandbox proxy env vars removed before import, auth bypassed via
dependency_overrides on get_current_user.
"""
import hashlib
import hmac
import os
import time

for _v in ("http_proxy", "https_proxy", "all_proxy",
           "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
           "no_proxy", "NO_PROXY"):
    os.environ.pop(_v, None)
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "dummy-key-for-test")

from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

import backend
from backend import (app, get_current_user, verify_paddle_signature,
                     resolve_paddle_subscription_status)

SECRET = "pdl_ntfset_test_secret"


class MockUser:
    id = "00000000-0000-0000-0000-000000000001"
    email = "test@example.com"


app.dependency_overrides[get_current_user] = lambda: MockUser()
client = TestClient(app, raise_server_exceptions=False)


def _sign(body: bytes, secret: str = SECRET, ts: int | None = None) -> str:
    ts = ts if ts is not None else int(time.time())
    digest = hmac.new(secret.encode(), f"{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return f"ts={ts};h1={digest}"


def _body(event_id, event_type, occurred_at, data):
    import json
    return json.dumps({
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "data": data,
    }).encode()


class FakeTable:
    """Minimal supabase table double: select/eq/insert/update/execute chain."""
    def __init__(self, rows):
        self._rows = rows
        self.inserted = []
        self.updated = None

    def select(self, *args):
        return self

    def insert(self, row):
        self.inserted.append(row)
        return self

    def update(self, payload):
        self.updated = payload
        return self

    def eq(self, *args):
        return self

    def execute(self):
        return SimpleNamespace(data=list(self._rows))


def _mock_supabase(profile=None, log_rows=None):
    tables = {
        "user_profiles": FakeTable([profile] if profile else []),
        "webhook_logs": FakeTable(log_rows or []),
    }
    mock = MagicMock()
    mock.table.side_effect = lambda name: tables[name]
    return mock, tables


def _profile(sub_updated_at="2026-10-30T00:00:00Z"):
    return {
        "user_id": MockUser.id,
        "is_subscribed": False,
        "subscription_updated_at": sub_updated_at,
        "subscription_id": None,
        "customer_id": None,
    }


# ── Signature ────────────────────────────────────────────────────────────────

def test_signature_valid():
    body = b'{"event_id":"evt_1"}'
    assert verify_paddle_signature(body, _sign(body), SECRET) is True


def test_signature_wrong_secret():
    body = b'{"event_id":"evt_1"}'
    assert verify_paddle_signature(body, _sign(body, secret="other"), SECRET) is False


def test_signature_tampered_body():
    body = b'{"event_id":"evt_1"}'
    assert verify_paddle_signature(b'{"event_id":"evt_2"}', _sign(body), SECRET) is False


def test_signature_stale_timestamp():
    body = b'{"event_id":"evt_1"}'
    assert verify_paddle_signature(body, _sign(body, ts=int(time.time()) - 1000), SECRET) is False


def test_signature_missing_parts():
    body = b'{"event_id":"evt_1"}'
    assert verify_paddle_signature(body, "h1=abc", SECRET) is False
    assert verify_paddle_signature(body, "", SECRET) is False


def test_signature_rotation_second_h1_accepted():
    body = b'{"event_id":"evt_1"}'
    good = _sign(body)
    header = good + ";h1=" + "0" * 64
    assert verify_paddle_signature(body, header, SECRET) is True


# ── Status mapping ───────────────────────────────────────────────────────────

def test_status_mapping():
    cases = [
        ("subscription.activated", {"status": "active"}, True),
        ("subscription.resumed", {"status": "active"}, True),
        ("subscription.created", {"status": "active"}, True),
        ("subscription.created", {"status": "trialing"}, True),
        ("subscription.created", {"status": "past_due"}, None),
        ("subscription.updated", {"status": "active"}, True),
        ("subscription.updated", {"status": "past_due"}, None),
        ("subscription.updated", {"status": "paused"}, False),
        ("subscription.updated", {"status": "canceled"}, False),
        ("subscription.canceled", {"status": "canceled"}, False),
        ("subscription.paused", {"status": "paused"}, False),
        ("subscription.past_due", {"status": "past_due"}, None),
        ("transaction.completed", {"subscription_id": "sub_1"}, True),
        ("transaction.completed", {}, None),
        ("transaction.updated", {"status": "completed", "subscription_id": "sub_1"}, True),
        ("transaction.updated", {"status": "past_due"}, None),
        ("adjustment.created", {"action": "refund"}, None),
        ("customer.updated", {}, None),
    ]
    for event_type, data, expected in cases:
        assert resolve_paddle_subscription_status(event_type, data) is expected, event_type


# ── Webhook endpoint ─────────────────────────────────────────────────────────

def test_webhook_bad_signature_rejected(monkeypatch):
    monkeypatch.setattr(backend.settings, "paddle_webhook_secret", SECRET)
    body = _body("evt_1", "subscription.activated", "2026-10-31T00:00:00Z",
                 {"id": "sub_1", "customer_id": "ctm_1", "status": "active",
                  "custom_data": {"user_id": MockUser.id}})
    res = client.post("/webhook/paddle", content=body,
                      headers={"Paddle-Signature": _sign(body, secret="wrong")})
    assert res.status_code == 400


def test_webhook_missing_secret_500(monkeypatch):
    monkeypatch.setattr(backend.settings, "paddle_webhook_secret", "")
    body = _body("evt_1", "subscription.activated", "2026-10-31T00:00:00Z", {})
    res = client.post("/webhook/paddle", content=body,
                      headers={"Paddle-Signature": _sign(body)})
    assert res.status_code == 500


def test_webhook_activated_grants_access(monkeypatch):
    monkeypatch.setattr(backend.settings, "paddle_webhook_secret", SECRET)
    mock, tables = _mock_supabase(profile=_profile())
    body = _body("evt_10", "subscription.activated", "2026-10-31T00:00:00Z",
                 {"id": "sub_1", "customer_id": "ctm_1", "status": "active",
                  "custom_data": {"user_id": MockUser.id}})
    with monkeypatch.context() as m:
        m.setattr(backend, "supabase", mock)
        res = client.post("/webhook/paddle", content=body,
                          headers={"Paddle-Signature": _sign(body)})
    assert res.status_code == 200, res.text
    assert tables["user_profiles"].updated["is_subscribed"] is True
    assert tables["user_profiles"].updated["subscription_id"] == "sub_1"
    assert tables["user_profiles"].updated["customer_id"] == "ctm_1"
    assert tables["webhook_logs"].inserted[0]["status"] == "processed"
    assert tables["webhook_logs"].inserted[0]["event_id"] == "evt_10"


def test_webhook_duplicate_event_is_noop(monkeypatch):
    monkeypatch.setattr(backend.settings, "paddle_webhook_secret", SECRET)
    mock, tables = _mock_supabase(profile=_profile(),
                                  log_rows=[{"status": "processed"}])
    body = _body("evt_10", "subscription.activated", "2026-10-31T00:00:00Z",
                 {"id": "sub_1", "customer_id": "ctm_1", "status": "active",
                  "custom_data": {"user_id": MockUser.id}})
    with monkeypatch.context() as m:
        m.setattr(backend, "supabase", mock)
        res = client.post("/webhook/paddle", content=body,
                          headers={"Paddle-Signature": _sign(body)})
    assert res.status_code == 200
    assert res.json()["duplicate"] is True
    assert tables["user_profiles"].updated is None


def test_webhook_stale_event_ignored(monkeypatch):
    monkeypatch.setattr(backend.settings, "paddle_webhook_secret", SECRET)
    mock, tables = _mock_supabase(profile=_profile(sub_updated_at="2026-10-31T12:00:00Z"))
    body = _body("evt_11", "subscription.activated", "2026-10-31T00:00:00Z",
                 {"id": "sub_1", "customer_id": "ctm_1", "status": "active",
                  "custom_data": {"user_id": MockUser.id}})
    with monkeypatch.context() as m:
        m.setattr(backend, "supabase", mock)
        res = client.post("/webhook/paddle", content=body,
                          headers={"Paddle-Signature": _sign(body)})
    assert res.status_code == 200
    assert res.json()["reason"] == "stale_event"
    assert tables["user_profiles"].updated is None
    assert tables["webhook_logs"].inserted[0]["status"] == "ignored_stale"


def test_webhook_full_refund_cancels_at_period_end(monkeypatch):
    monkeypatch.setattr(backend.settings, "paddle_webhook_secret", SECRET)
    mock, tables = _mock_supabase(profile=_profile())
    cancel_calls = []

    def fake_api(method, path, body=None):
        cancel_calls.append((method, path, body))
        return {"data": {}}

    body = _body("evt_20", "adjustment.created", "2026-10-31T00:00:00Z",
                 {"id": "adj_1", "action": "refund", "status": "approved", "type": "full",
                  "transaction_id": "txn_1", "subscription_id": "sub_1"})
    with monkeypatch.context() as m:
        m.setattr(backend, "supabase", mock)
        m.setattr(backend, "paddle_api_request", fake_api)
        res = client.post("/webhook/paddle", content=body,
                          headers={"Paddle-Signature": _sign(body)})
    assert res.status_code == 200, res.text
    # Grace: access untouched by the refund itself; the cancel is scheduled.
    assert "is_subscribed" not in (tables["user_profiles"].updated or {})
    assert cancel_calls == [("POST", "/subscriptions/sub_1/cancel",
                             {"effective_from": "next_billing_period"})]


def test_webhook_partial_refund_records_only(monkeypatch):
    monkeypatch.setattr(backend.settings, "paddle_webhook_secret", SECRET)
    mock, tables = _mock_supabase(profile=_profile())
    cancel_calls = []
    body = _body("evt_21", "adjustment.created", "2026-10-31T00:00:00Z",
                 {"id": "adj_2", "action": "refund", "status": "approved", "type": "partial",
                  "transaction_id": "txn_1", "subscription_id": "sub_1"})
    with monkeypatch.context() as m:
        m.setattr(backend, "supabase", mock)
        m.setattr(backend, "paddle_api_request",
                  lambda *a, **k: cancel_calls.append(a) or {"data": {}})
        res = client.post("/webhook/paddle", content=body,
                          headers={"Paddle-Signature": _sign(body)})
    assert res.status_code == 200
    assert cancel_calls == []
    assert tables["webhook_logs"].inserted[0]["status"] == "processed"


# ── Checkout + config ────────────────────────────────────────────────────────

def test_checkout_creates_transaction_with_user_binding(monkeypatch):
    monkeypatch.setattr(backend.settings, "paddle_api_key", "pdl_test_key")
    monkeypatch.setattr(backend.settings, "paddle_price_id_monthly", "pri_monthly")
    mock, _ = _mock_supabase(profile=None)
    calls = []

    def fake_api(method, path, body=None):
        calls.append((method, path, body))
        return {"data": {"id": "txn_99",
                         "checkout": {"url": "https://checkout.paddle.com/txn_99"}}}

    with monkeypatch.context() as m:
        m.setattr(backend, "supabase", mock)
        m.setattr(backend, "paddle_api_request", fake_api)
        res = client.post("/billing/checkout", json={"plan": "monthly"})
    assert res.status_code == 200, res.text
    assert res.json() == {"transaction_id": "txn_99",
                          "checkout_url": "https://checkout.paddle.com/txn_99"}
    method, path, body = calls[0]
    assert (method, path) == ("POST", "/transactions")
    assert body["items"] == [{"price_id": "pri_monthly", "quantity": 1}]
    assert body["custom_data"] == {"user_id": MockUser.id}
    assert body["customer"] == {"email": MockUser.email}


def test_checkout_missing_price_503(monkeypatch):
    monkeypatch.setattr(backend.settings, "paddle_api_key", "pdl_test_key")
    monkeypatch.setattr(backend.settings, "paddle_price_id_monthly", "")
    res = client.post("/billing/checkout", json={"plan": "monthly"})
    assert res.status_code == 503


def test_billing_config_reports_plan_availability(monkeypatch):
    monkeypatch.setattr(backend.settings, "paddle_price_id_monthly", "pri_monthly")
    monkeypatch.setattr(backend.settings, "paddle_price_id_yearly", "")
    monkeypatch.setattr(backend.settings, "paddle_client_token", "")
    res = client.get("/billing/config")
    assert res.status_code == 200
    body = res.json()
    assert body["plans"] == {"monthly": True, "yearly": False}
    assert body["client_token"] is None
    assert "pri_monthly" not in res.text  # price ids stay server-side
