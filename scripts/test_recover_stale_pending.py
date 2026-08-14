"""Smoke-test: Phase C.3 hotfix — recover_stale_pending_tasks.

Tests 3 recovery scenarios:
  1. Ghost task without Redis snapshot and without files on disk → marked FAILED
  2. Ghost task with Redis snapshot status=done → marked DONE (sync from snapshot)
  3. Ghost task without Redis snapshot but WITH files on disk → marked DONE
     (files recovered from disk)

Plus:
  4. _find_task_files_on_disk() — verifies disk scanning logic
  5. save_task_final_state_from_snapshot_sync() — verifies sync DB update
     (with mocked psycopg.connect)
  6. _maybe_recover_stale_pending_tasks() TTL — second call within 60s is no-op
"""
import asyncio
import sys
import types
import shutil
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path("/home/z/my-project/gibdd-bot")
sys.path.insert(0, str(PROJECT_ROOT))

# === Mock config ===
config = types.ModuleType("config")
config.USE_CELERY = True
config.REDIS_URL = "redis://127.0.0.1:6379/0"
config.REDIS_PUBSUB_PREFIX = "gibdd"
config.REDIS_TASK_STATE_TTL = 86400
config.DATABASE_URL = "postgresql://fake:fake@localhost/fake"
config.DB_ENABLED = True
config.DB_POOL_MIN = 1
config.DB_POOL_MAX = 5
config.DB_CONNECT_TIMEOUT = 10
sys.modules["config"] = config

# miniapp.backend.config — load the real one and override
# Set DATABASE_URL env var BEFORE importing (pydantic-settings reads env)
import os
os.environ["DATABASE_URL"] = "postgresql://fake:fake@localhost/fake"
os.environ["DB_POOL_MIN"] = "1"
os.environ["DB_POOL_MAX"] = "5"
os.environ["DB_CONNECT_TIMEOUT"] = "10"

# Clear cached settings to re-read env
from miniapp.backend import config as miniapp_config
miniapp_config.get_settings.cache_clear()
miniapp_config.settings = miniapp_config.get_settings()

# === Mock celery (needed for worker.tasks.gibdd_tasks imports) ===
celery_mod = types.ModuleType("celery")


class _FakeTask:
    pass


class _FakeApp:
    def task(self, *args, **kwargs):
        def decorator(fn):
            fn.delay = lambda *a, **k: None
            return fn
        if args and callable(args[0]):
            return decorator(args[0])
        return decorator


celery_mod.Task = _FakeTask
celery_mod.app = _FakeApp()
celery_mod.Celery = lambda *a, **k: celery_mod.app
sys.modules["celery"] = celery_mod

celery_app_mod = types.ModuleType("worker.celery_app")
celery_app_mod.app = celery_mod.app
sys.modules["worker.celery_app"] = celery_app_mod

# === Mock redis with in-memory store ===
class FakeRedis:
    def __init__(self):
        self._store = {}

    def from_url(self, url, **kwargs):
        return self

    def ping(self):
        return True

    def setex(self, key, ttl, value):
        self._store[key] = value
        return True

    def get(self, key):
        return self._store.get(key)

    def delete(self, key):
        return 1 if self._store.pop(key, None) is not None else 0

    def scan_iter(self, match=None, count=None):
        import fnmatch
        for k in self._store:
            if match is None or fnmatch.fnmatch(k, match):
                yield k


_fake_redis = FakeRedis()
redis_mod = types.ModuleType("redis")
redis_mod.from_url = _fake_redis.from_url
redis_mod.Redis = FakeRedis
sys.modules["redis"] = redis_mod

# Force fake redis into task_state
import worker.task_state as ts
ts._redis_client = _fake_redis
ts._redis_client_checked = True

# === Mock psycopg for save_task_final_state_from_snapshot_sync ===
_psycopg_calls: list = []


class _FakePsycopgCursor:
    def execute(self, sql, params=None):
        _psycopg_calls.append(("execute", sql, params))

    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakePsycopgConn:
    def cursor(self):
        return _FakePsycopgCursor()

    def commit(self):
        _psycopg_calls.append(("commit",))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakePsycopg:
    def connect(self, url, **kwargs):
        _psycopg_calls.append(("connect", url))
        return _FakePsycopgConn()

    # expose Json helper (real psycopg has it)
    @staticmethod
    def Json(data):
        import json
        return json.dumps(data) if data is not None else None


# Replace real psycopg with fake BEFORE importing repository
real_psycopg = types.ModuleType("psycopg")
real_psycopg.connect = _FakePsycopg().connect
real_psycopg.OperationalError = type("OperationalError", (Exception,), {})

# Provide Json from real psycopg.types.json — but we mocked psycopg,
# so we need a fake types.json module too
psycopg_types = types.ModuleType("psycopg.types")
psycopg_types_json = types.ModuleType("psycopg.types.json")


class _FakeJson:
    """Mimics psycopg.types.json.Json — wraps a Python value for SQL serialization."""

    def __init__(self, obj):
        self.obj = obj

    def get_grammar(self):
        return None

    def dump(self, obj):
        import json
        return json.dumps(obj)


class _FakeJsonb(_FakeJson):
    pass


psycopg_types_json.Json = _FakeJson
psycopg_types_json.Jsonb = _FakeJsonb
psycopg_types.json = psycopg_types_json
sys.modules["psycopg.types"] = psycopg_types
sys.modules["psycopg.types.json"] = psycopg_types_json

real_psycopg.types = psycopg_types
sys.modules["psycopg"] = real_psycopg

# Mock psycopg.rows
psycopg_rows = types.ModuleType("psycopg.rows")
psycopg_rows.dict_row = None
sys.modules["psycopg.rows"] = psycopg_rows

# Mock psycopg_pool
psycopg_pool = types.ModuleType("psycopg_pool")


class _FakeAsyncConnectionPool:
    """Fake async pool — never used because is_db_ready() returns False,
    but we need the import to succeed."""

    def __init__(self, *a, **k):
        pass

    async def open(self, *a, **k):
        pass

    async def close(self):
        pass

    def connection(self):
        raise RuntimeError("FakeAsyncConnectionPool.connection() should not be called")


psycopg_pool.AsyncConnectionPool = _FakeAsyncConnectionPool
sys.modules["psycopg_pool"] = psycopg_pool

# === Now we can import repository ===
# We'll override is_db_ready / get_pool for each test


def _make_fake_pool(rows_per_query):
    """Returns a fake async pool where each .connection() returns a fake conn.
    rows_per_query: list of lists of dict-rows. Each .execute() returns a fake cursor
    whose .fetchall() returns the next list of rows. Multiple UPDATEs in sequence
    don't need rows (they're not fetched).
    """
    state = {"row_index": 0, "rows_per_query": rows_per_query, "calls": []}

    class _FakeCursor:
        def __init__(self):
            self.last_query = None

        async def execute(self, sql, params=None, **kwargs):
            state["calls"].append(("execute", sql, params))
            self.last_query = sql

        async def fetchall(self):
            if state["row_index"] < len(state["rows_per_query"]):
                rows = state["rows_per_query"][state["row_index"]]
                state["row_index"] += 1
                return rows
            return []

        async def fetchone(self):
            if state["row_index"] < len(state["rows_per_query"]):
                row = state["rows_per_query"][state["row_index"]]
                state["row_index"] += 1
                return row[0] if row else None
            return None

    class _FakeConn:
        async def execute(self, sql, params=None, **kwargs):
            state["calls"].append(("conn.execute", sql, params))
            return _FakeCursor()

        async def commit(self):
            state["calls"].append(("commit",))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakePool:
        def connection(self):
            return _FakeConn()

    return _FakePool(), state


def _override_db_ready(repo_module, ready: bool, pool=None):
    """Monkey-patches is_db_ready() and get_pool() in repository."""
    repo_module.is_db_ready = lambda: ready
    repo_module.get_pool = lambda: pool


# Set up _PROJECT_ROOT override for _find_task_files_on_disk
import miniapp.backend.services._imports as _imports


# ============================================================
# TEST 4: _find_task_files_on_disk (no DB needed)
# ============================================================
print("=" * 70)
print("TEST 4: _find_task_files_on_disk scans data/tasks/{task_id}/")
print("=" * 70)

# Create a temp dir simulating data/tasks/{task_id}/
tmp_root = tempfile.mkdtemp(prefix="gibdd_test_")
tmp_data_tasks = Path(tmp_root) / "data" / "tasks"
tmp_data_tasks.mkdir(parents=True, exist_ok=True)

# Save original _PROJECT_ROOT and override
orig_project_root = _imports._PROJECT_ROOT
_imports._PROJECT_ROOT = Path(tmp_root)

# Sub-test 4a: empty dir → []
empty_task_dir = tmp_data_tasks / "empty-task"
empty_task_dir.mkdir(parents=True, exist_ok=True)

from miniapp.backend.db import repository as repo
files = repo._find_task_files_on_disk("empty-task")
print(f"  empty-task → {len(files)} files (expected 0)")
assert len(files) == 0, f"Expected 0 files for empty dir, got {files}"

# Sub-test 4b: dir with files → list of file metadata
filled_task_dir = tmp_data_tasks / "filled-task"
filled_task_dir.mkdir(parents=True, exist_ok=True)
(filled_task_dir / "dtp_cards_Nizhegorodskaya_7_mes_2026.xlsx").write_bytes(b"x" * 1024)
(filled_task_dir / "dtp_uch_Nizhegorodskaya_7_mes_2026.xlsx").write_bytes(b"y" * 2048)
(filled_task_dir / "dtp_map_Nizhegorodskaya_7_mes_2026.html").write_text("<html></html>", encoding="utf-8")

files = repo._find_task_files_on_disk("filled-task")
print(f"  filled-task → {len(files)} files (expected 3)")
file_types = sorted(f["type"] for f in files)
print(f"  file types: {file_types}")
assert len(files) == 3
assert "dtp_cards" in file_types
assert "dtp_participants" in file_types
assert "map_html" in file_types
# Check size
for f in files:
    if f["type"] == "dtp_cards":
        assert f["size_bytes"] == 1024
    if f["type"] == "dtp_participants":
        assert f["size_bytes"] == 2048
print("  PASS")

# Sub-test 4c: non-existent dir → []
files = repo._find_task_files_on_disk("nonexistent-task")
print(f"  nonexistent-task → {len(files)} files (expected 0)")
assert len(files) == 0
print("  PASS")

# Sub-test 4d: empty file (size 0) is ignored
empty_file_task = tmp_data_tasks / "empty-file-task"
empty_file_task.mkdir(parents=True, exist_ok=True)
(empty_file_task / "dtp_cards_test.xlsx").write_bytes(b"")  # 0 bytes
files = repo._find_task_files_on_disk("empty-file-task")
print(f"  empty-file-task → {len(files)} files (expected 0 — empty file ignored)")
assert len(files) == 0
print("  PASS")

# Cleanup tmp
shutil.rmtree(tmp_root)
_imports._PROJECT_ROOT = orig_project_root
print()


# ============================================================
# TEST 1: ghost task without snapshot and without files → FAILED
# ============================================================
print("=" * 70)
print("TEST 1: ghost task without Redis snapshot, without disk files → FAILED")
print("=" * 70)

# Reset repository._last_stale_recovery_at to allow re-run
repo._last_stale_recovery_at = None
# Force _STALE_PENDING_MINUTES to 0 so any task is considered stale
repo._STALE_PENDING_MINUTES = 0

# Mock: DB ready, pool returns 1 stale pending row
stale_task_row = [{
    "id": "ghost-1",
    "status": "pending",
    "progress": 0,
    "created_at": datetime.now(timezone.utc) - timedelta(hours=1),
}]
fake_pool, pool_state = _make_fake_pool([stale_task_row])
_override_db_ready(repo, ready=True, pool=fake_pool)

# Mock _find_task_files_on_disk to return [] (no files)
repo._find_task_files_on_disk = lambda tid: []

# Mock load_task_state to return None (no Redis snapshot)
_orig_load = ts.load_task_state
ts.load_task_state = lambda tid: None
# Also patch the import inside recover_stale_pending_tasks
import worker.task_state as ts_module
_orig_imported = None

count = asyncio.run(repo.recover_stale_pending_tasks())
print(f"  recover_stale_pending_tasks returned: {count}")
print(f"  pool calls: {len(pool_state['calls'])}")

# Check that UPDATE ... SET status='failed' was called
failed_updates = [
    c for c in pool_state["calls"]
    if c[0] == "conn.execute" and "status = 'failed'" in c[1]
]
print(f"  failed UPDATEs: {len(failed_updates)}")
assert count == 1, f"Expected 1 recovery, got {count}"
assert len(failed_updates) >= 1, "Expected at least one failed UPDATE"
print("  PASS — ghost marked as FAILED")

# Restore
ts.load_task_state = _orig_load
print()


# ============================================================
# TEST 2: ghost task WITH Redis snapshot status=done → DONE
# ============================================================
print("=" * 70)
print("TEST 2: ghost task WITH Redis snapshot (status=done) → DONE")
print("=" * 70)

repo._last_stale_recovery_at = None  # reset TTL

stale_task_row = [{
    "id": "ghost-2",
    "status": "pending",
    "progress": 0,
    "created_at": datetime.now(timezone.utc) - timedelta(hours=2),
}]
fake_pool, pool_state = _make_fake_pool([stale_task_row])
_override_db_ready(repo, ready=True, pool=fake_pool)
repo._find_task_files_on_disk = lambda tid: []

# Mock load_task_state to return a done snapshot
done_snapshot = {
    "id": "ghost-2",
    "status": "done",
    "progress": 100,
    "total_dtp": 745,
    "total_dead": 41,
    "total_injured": 970,
    "files": [{"type": "dtp_cards", "filename": "test.xlsx", "size_bytes": 1024}],
    "analytics": {"total_dtp": 745},
}
ts.load_task_state = lambda tid: done_snapshot if tid == "ghost-2" else None

count = asyncio.run(repo.recover_stale_pending_tasks())
print(f"  recover_stale_pending_tasks returned: {count}")

# Check that UPDATE ... SET status='done' was called with total_dtp=745
done_updates = [
    c for c in pool_state["calls"]
    if c[0] == "conn.execute" and "status = 'done'" in c[1]
]
print(f"  done UPDATEs: {len(done_updates)}")
assert count == 1
assert len(done_updates) >= 1
# Check params — the 3rd positional arg is params tuple
# The done-update uses positional params (not named)
done_call = done_updates[0]
print(f"  done UPDATE params: total_dtp={done_call[2][0]}, total_dead={done_call[2][1]}")
assert done_call[2][0] == 745  # total_dtp
assert done_call[2][1] == 41   # total_dead
assert done_call[2][2] == 970  # total_injured
print("  PASS — ghost marked as DONE (synced from Redis snapshot)")

ts.load_task_state = _orig_load
print()


# ============================================================
# TEST 3: ghost task WITHOUT snapshot BUT WITH disk files → DONE
# ============================================================
print("=" * 70)
print("TEST 3: ghost task WITHOUT snapshot, WITH disk files → DONE")
print("=" * 70)

repo._last_stale_recovery_at = None

stale_task_row = [{
    "id": "ghost-3",
    "status": "pending",
    "progress": 0,
    "created_at": datetime.now(timezone.utc) - timedelta(hours=3),
}]
fake_pool, pool_state = _make_fake_pool([stale_task_row])
_override_db_ready(repo, ready=True, pool=fake_pool)

# Mock _find_task_files_on_disk to return files for ghost-3
disk_files_for_ghost3 = [
    {"type": "dtp_cards", "filename": "dtp_cards_test.xlsx", "path": "/tmp/test.xlsx",
     "size_bytes": 5120, "mime": "application/vnd.ms-excel"},
    {"type": "dtp_participants", "filename": "dtp_uch_test.xlsx", "path": "/tmp/test2.xlsx",
     "size_bytes": 8192, "mime": "application/vnd.ms-excel"},
]
repo._find_task_files_on_disk = lambda tid: disk_files_for_ghost3 if tid == "ghost-3" else []

# No Redis snapshot
ts.load_task_state = lambda tid: None

count = asyncio.run(repo.recover_stale_pending_tasks())
print(f"  recover_stale_pending_tasks returned: {count}")

# Check that UPDATE ... SET status='done' with files=... was called
done_updates = [
    c for c in pool_state["calls"]
    if c[0] == "conn.execute" and "status = 'done'" in c[1]
]
print(f"  done UPDATEs: {len(done_updates)}")
assert count == 1
assert len(done_updates) >= 1
print("  PASS — ghost marked as DONE (files recovered from disk)")

ts.load_task_state = _orig_load
print()


# ============================================================
# TEST 5: save_task_final_state_from_snapshot_sync (mocked psycopg)
# ============================================================
print("=" * 70)
print("TEST 5: save_task_final_state_from_snapshot_sync")
print("=" * 70)

_psycopg_calls.clear()

# Force fake psycopg
sys.modules["psycopg"] = real_psycopg

result = repo.save_task_final_state_from_snapshot_sync(
    "task-sync-test",
    status="done",
    progress=100,
    total_dtp=745,
    total_dead=41,
    total_injured=970,
    files=[{"type": "dtp_cards", "filename": "test.xlsx", "size_bytes": 1024}],
    analytics={"total_dtp": 745},
)

print(f"  returned: {result}")
print(f"  psycopg calls: {len(_psycopg_calls)}")
for c in _psycopg_calls:
    if len(c) >= 2:
        print(f"    {c[0]}: {str(c[1])[:80]}...")
    else:
        print(f"    {c[0]}")

assert result is True
# Should have: 1 connect, 1 execute, 1 commit
call_types = [c[0] for c in _psycopg_calls]
assert "connect" in call_types
assert "execute" in call_types
assert "commit" in call_types
print("  PASS — sync DB update works")
print()


# ============================================================
# TEST 6: _maybe_recover_stale_pending_tasks TTL
# ============================================================
print("=" * 70)
print("TEST 6: _maybe_recover_stale_pending_tasks TTL (60 sec)")
print("=" * 70)

# Run once — should call recover_stale_pending_tasks
repo._last_stale_recovery_at = None
call_count_before = len(pool_state["calls"]) if 'pool_state' in dir() else 0

# Set up a fresh pool for this test
stale_task_row = [{
    "id": "ghost-ttl",
    "status": "pending",
    "progress": 0,
    "created_at": datetime.now(timezone.utc) - timedelta(hours=4),
}]
fake_pool2, pool_state2 = _make_fake_pool([stale_task_row])
_override_db_ready(repo, ready=True, pool=fake_pool2)
repo._find_task_files_on_disk = lambda tid: []
ts.load_task_state = lambda tid: None

async def _run_maybe():
    await repo._maybe_recover_stale_pending_tasks()
asyncio.run(_run_maybe())
calls_after_first = len(pool_state2["calls"])
print(f"  After 1st call: pool calls = {calls_after_first}")
assert calls_after_first >= 1, "Expected at least 1 pool call on 1st invocation"

# Run again immediately — should be no-op due to TTL
async def _run_maybe2():
    await repo._maybe_recover_stale_pending_tasks()
asyncio.run(_run_maybe2())
calls_after_second = len(pool_state2["calls"])
print(f"  After 2nd call (within 60s): pool calls = {calls_after_second}")
assert calls_after_second == calls_after_first, \
    "Expected NO additional pool calls (TTL should block)"
print("  PASS — TTL blocks second call within 60 sec")

ts.load_task_state = _orig_load
print()


# ============================================================
# ALL TESTS PASSED
# ============================================================
print("=" * 70)
print("ALL TESTS PASSED — Phase C.3 hotfix (stale pending recovery) verified")
print("=" * 70)
print()
print("Summary:")
print("  - TEST 1: ghost without snapshot/files → FAILED ✓")
print("  - TEST 2: ghost with Redis snapshot status=done → DONE (from snapshot) ✓")
print("  - TEST 3: ghost without snapshot but WITH files → DONE (from disk) ✓")
print("  - TEST 4: _find_task_files_on_disk scans correctly ✓")
print("  - TEST 5: save_task_final_state_from_snapshot_sync works ✓")
print("  - TEST 6: _maybe_recover_stale_pending_tasks TTL (60s) ✓")
