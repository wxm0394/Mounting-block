"""Zombie reclamation + per-batch translation heartbeat tests (Plan Item 3).

  Test 1 (zombie): fake epub_tasks with one row updated 3.5h ago and one
          1h ago -> only the stale row is reclaimed as failed.
  Test 2 (heartbeat): a 3-batch translation via run_phase15_translate ->
          progress_callback fires at 51% / 63% / 75% (40 + 35*batch/total).

Same import notes as test_worker_fault_tolerance.py: dummy Supabase env
vars, sandbox proxy env vars removed before import (httpx IPv6 quirk).
"""
import os

for _v in ("http_proxy", "https_proxy", "all_proxy",
           "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
           "no_proxy", "NO_PROXY"):
    os.environ.pop(_v, None)
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "dummy-key-for-test")

from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import epub_worker
import epub_pipeline


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    """Minimal Supabase-py query-chain stand-in."""

    def __init__(self, fake):
        self.fake = fake
        self.op = None
        self.payload = None
        self.in_filter = None

    def select(self, *cols):
        self.op = "select"
        return self

    def update(self, payload):
        self.op = "update"
        self.payload = payload
        return self

    def in_(self, col, vals):
        self.in_filter = (col, list(vals))
        return self

    def lt(self, col, val):
        return self

    def eq(self, col, val):
        return self

    def execute(self):
        if self.op == "select":
            return FakeResult(self.fake.rows)
        self.fake.updates.append((self.payload, self.in_filter))
        return FakeResult(None)


class FakeSupabase:
    def __init__(self, rows):
        self.rows = rows
        self.updates = []

    def table(self, name):
        return FakeQuery(self)


def test_zombie_tasks_reclaimed(capsys):
    now = datetime.now(timezone.utc)
    rows = [
        {"id": "task-timeout-id",
         "updated_at": (now - timedelta(hours=3.5)).isoformat()},
        {"id": "task-recent-id",
         "updated_at": (now - timedelta(hours=1)).isoformat()},
    ]
    fake = FakeSupabase(rows)
    with patch("epub_worker.supabase", fake):
        ids = epub_worker.reclaim_zombie_tasks()

    assert ids == ["task-timeout-id"]
    assert len(fake.updates) == 1
    payload, in_filter = fake.updates[0]
    assert payload["status"] == "failed"
    assert payload["error_message"] == "服务中断，请重新上传"
    assert payload["current_step_desc"] == "任务超时已终止"
    assert in_filter == ("id", ["task-timeout-id"])
    out = capsys.readouterr().out
    assert "[ZOMBIE] Reclaimed 1 zombie task(s): ['task-timeout-id'] -> marked as failed." in out


def test_batch_translation_heartbeat(capsys):
    progress = []

    def fake_progress(pct, desc):
        progress.append((pct, desc))
        print(f"[PROGRESS] {pct}% - {desc} -> updated_at bumped")

    def fake_batch_translate(annotations, target_lang, source_text="",
                             doc_hash=None, skip_source_check=False,
                             on_batch_complete=None):
        for i in range(3):
            if on_batch_complete:
                on_batch_complete(i, 3)

    with patch("epub_pipeline.backend.batch_translate",
               side_effect=fake_batch_translate):
        epub_pipeline.run_phase15_translate(
            {"word1": {}, "word2": {}}, "zh-Hans", "bookhash123",
            progress_callback=fake_progress)

    assert [pct for pct, _ in progress] == [51, 63, 75]
    out = capsys.readouterr().out
    assert "[PROGRESS] 51% - 正在批量翻译全书生词 (1/3)... -> updated_at bumped" in out
    assert "[PROGRESS] 63% - 正在批量翻译全书生词 (2/3)... -> updated_at bumped" in out
    assert "[PROGRESS] 75% - 正在批量翻译全书生词 (3/3)... -> updated_at bumped" in out
