"""Process-safe plugin state. All files live under the supplied plugin root."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable
import uuid


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time,
                 lease_seconds: float = 10.0):
        if not 0 < lease_seconds <= 12:
            raise ValueError("面板租期必须在 12 秒以内")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "state.sqlite3"
        self.clock = clock
        self.lease_seconds = lease_seconds
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS panels (
                    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
                    lease_until REAL NOT NULL, auto_enabled INTEGER NOT NULL,
                    closed INTEGER NOT NULL DEFAULT 0, observation_after REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS panel_thread ON panels(thread_id, lease_until);
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
                    context_json TEXT, context_key TEXT, config_fp TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'waiting',
                    suggestions_json TEXT NOT NULL DEFAULT '[]', error TEXT, error_source TEXT,
                    latest_job TEXT, revision INTEGER NOT NULL DEFAULT 0,
                    observed_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, panel_id TEXT,
                    context_json TEXT NOT NULL, context_key TEXT NOT NULL,
                    config_fp TEXT NOT NULL, origin TEXT NOT NULL,
                    state TEXT NOT NULL, created_at REAL NOT NULL, started_at REAL,
                    invoked_at REAL, finished_at REAL,
                    usage_json TEXT NOT NULL DEFAULT '{}', error TEXT
                );
                CREATE INDEX IF NOT EXISTS job_context ON jobs(context_key, state);
                CREATE INDEX IF NOT EXISTS job_thread ON jobs(thread_id);
                CREATE TABLE IF NOT EXISTS stop_marks (
                    thread_id TEXT PRIMARY KEY, turn_id TEXT, marked_at REAL NOT NULL
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
            for name, declaration in (("observed_at", "REAL NOT NULL DEFAULT 0"), ("error_source", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE threads ADD COLUMN {name} {declaration}")
            if "observation_after" not in {row[1] for row in db.execute("PRAGMA table_info(panels)")}:
                db.execute("ALTER TABLE panels ADD COLUMN observation_after REAL NOT NULL DEFAULT 0")

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _context_key(thread_id: str, context: dict, config_fp: str) -> str:
        raw = _json([thread_id, context["turn_id"], context["fingerprint"], config_fp])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _current_config(db) -> str | None:
        row = db.execute("SELECT value FROM meta WHERE key='config_fp'").fetchone()
        return row[0] if row else None

    def sync_config(self, config_fp: str, configured: bool) -> bool:
        with self._transaction() as db:
            self._recover_abandoned(db)
            if self._current_config(db) == config_fp:
                return False
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('config_fp',?)", (config_fp,))
            db.execute("UPDATE threads SET context_json=NULL,context_key=NULL,config_fp=?,"
                       "status=?,suggestions_json='[]',error=NULL,error_source=NULL,latest_job=NULL,revision=revision+1",
                       (config_fp, "waiting" if configured else "unconfigured"))
            db.execute("UPDATE jobs SET state='cancelled',context_json='{}',finished_at=? WHERE state='queued'", (self.clock(),))
            return True

    @staticmethod
    def _panel(db, panel_id: str, thread_id: str, *, allow_closed=False):
        row = db.execute("SELECT * FROM panels WHERE id=?", (panel_id,)).fetchone()
        if not row or row["thread_id"] != thread_id:
            raise ValueError("面板与聊天不匹配，请重新打开面板")
        if row["closed"] and not allow_closed:
            raise ValueError("面板已关闭，请重新打开面板")
        return row

    def open_panel(self, thread_id: str, auto_enabled: bool) -> str:
        if not isinstance(thread_id, str) or not thread_id.strip() or len(thread_id) > 512:
            raise ValueError("缺少可信的聊天 ID，无法绑定面板")
        panel_id = uuid.uuid4().hex
        with self._transaction() as db:
            db.execute("INSERT OR IGNORE INTO threads(id) VALUES(?)", (thread_id,))
            now = self.clock()
            db.execute("INSERT INTO panels(id,thread_id,lease_until,auto_enabled,observation_after) VALUES(?,?,?,?,?)",
                       (panel_id, thread_id, now + self.lease_seconds, int(auto_enabled), now))
        return panel_id

    def renew(self, panel_id: str, thread_id: str) -> None:
        with self._transaction() as db:
            panel = self._panel(db, panel_id, thread_id)
            now = self.clock()
            db.execute("UPDATE panels SET lease_until=?,observation_after=? WHERE id=?",
                       (now + self.lease_seconds,
                        now if panel["lease_until"] <= now else panel["observation_after"], panel_id))

    def close_panel(self, panel_id: str, thread_id: str) -> None:
        with self._transaction() as db:
            self._panel(db, panel_id, thread_id, allow_closed=True)
            db.execute("UPDATE panels SET closed=1,lease_until=0 WHERE id=?", (panel_id,))

    def set_auto(self, panel_id: str, thread_id: str, enabled: bool) -> None:
        with self._transaction() as db:
            panel = self._panel(db, panel_id, thread_id)
            now = self.clock()
            db.execute("UPDATE panels SET auto_enabled=?,lease_until=?,observation_after=? WHERE id=?",
                       (int(enabled), now + self.lease_seconds,
                        now if panel["lease_until"] <= now else panel["observation_after"], panel_id))

    def _live(self, db, thread_id: str, *, auto_only=False, panel_id=None) -> bool:
        query = "SELECT 1 FROM panels WHERE thread_id=? AND closed=0 AND lease_until>?"
        args: list[Any] = [thread_id, self.clock()]
        if auto_only:
            query += " AND auto_enabled=1"
        if panel_id:
            query += " AND id=?"
            args.append(panel_id)
        return db.execute(query + " LIMIT 1", args).fetchone() is not None

    def has_live_panel(self, thread_id: str, *, auto_only=False) -> bool:
        with self._connection() as db:
            return self._live(db, thread_id, auto_only=auto_only)

    def observe(self, thread_id: str, context: dict | None, config_fp: str,
                title: str = "", *, observed_at: float | None = None) -> bool:
        """Accept only observations made for the still-current configuration."""
        with self._transaction() as db:
            if self._current_config(db) != config_fp:
                return False
            db.execute("INSERT OR IGNORE INTO threads(id) VALUES(?)", (thread_id,))
            old = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
            stamp = self.clock() if observed_at is None else observed_at
            if stamp < old["observed_at"]:
                return False
            if context and context.get("thread_id") != thread_id:
                raise ValueError("读取结果与当前聊天不匹配")
            key = self._context_key(thread_id, context, config_fp) if context else None
            actual_title = str(context.get("title") or title or old["title"]) if context else str(title or old["title"])
            if old["context_key"] == key and old["config_fp"] == config_fp:
                newly_visible = db.execute("SELECT 1 FROM panels WHERE thread_id=? AND closed=0 "
                                           "AND observation_after>? AND observation_after<=? LIMIT 1",
                                           (thread_id, old["observed_at"], stamp)).fetchone()
                db.execute("UPDATE threads SET observed_at=?,revision=revision+? WHERE id=?",
                           (stamp, int(newly_visible is not None), thread_id))
                if old["error_source"] == "read":
                    previous = db.execute("SELECT state FROM jobs WHERE id=?", (old["latest_job"],)).fetchone()
                    restored = ("ready" if json.loads(old["suggestions_json"]) else "empty") if previous and previous[0] == "completed" else "waiting"
                    db.execute("UPDATE threads SET status=?,error=NULL,error_source=NULL,revision=revision+1 WHERE id=?",
                               (restored, thread_id))
                if actual_title != old["title"]:
                    db.execute("UPDATE threads SET title=?,revision=revision+1 WHERE id=?", (actual_title, thread_id))
                return True
            db.execute("UPDATE threads SET title=?,context_json=?,context_key=?,config_fp=?,"
                       "status='waiting',suggestions_json='[]',error=NULL,error_source=NULL,latest_job=NULL,"
                       "revision=revision+1,observed_at=? WHERE id=?",
                       (actual_title, _json(context) if context else None, key, config_fp, stamp, thread_id))
            db.execute("UPDATE jobs SET state='cancelled',context_json='{}',finished_at=? WHERE thread_id=? AND state='queued'",
                       (self.clock(), thread_id))
            return True

    def read_error(self, thread_id: str, config_fp: str, message: str) -> None:
        with self._transaction() as db:
            if self._current_config(db) != config_fp or not self._live(db, thread_id):
                return
            # An in-flight generation owns its status; its completion will recheck the thread.
            row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
            if not row or row["status"] == "generating" or row["error_source"] == "generation":
                return
            now = self.clock()
            newly_visible = db.execute("SELECT 1 FROM panels WHERE thread_id=? AND closed=0 "
                                       "AND observation_after>? AND observation_after<=? LIMIT 1",
                                       (thread_id, row["observed_at"], now)).fetchone()
            changed = row["status"] != "error" or row["error"] != message or newly_visible is not None
            db.execute("UPDATE threads SET status='error',error=?,error_source='read',"
                       "observed_at=?,revision=revision+? WHERE id=?", (message, now, int(changed), thread_id))

    def prepare_refresh(self, panel_id: str, thread_id: str) -> None:
        with self._transaction() as db:
            self._panel(db, panel_id, thread_id)
            db.execute("UPDATE threads SET status='waiting',error=NULL,error_source=NULL,suggestions_json='[]',"
                       "revision=revision+1 WHERE id=? AND status!='generating'", (thread_id,))

    def queue_job(self, thread_id: str, config_fp: str, *, manual=False,
                  panel_id: str | None = None) -> dict | None:
        with self._transaction() as db:
            if self._current_config(db) != config_fp:
                return None
            if panel_id:
                self._panel(db, panel_id, thread_id)
            if not self._live(db, thread_id, auto_only=not manual,
                              panel_id=panel_id if manual else None):
                return None
            row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
            if not row or not row["context_json"] or row["config_fp"] != config_fp:
                return None
            key = row["context_key"]
            if db.execute("SELECT 1 FROM jobs WHERE context_key=? AND state IN ('queued','running') LIMIT 1", (key,)).fetchone():
                return None
            # Even failed or discarded *invocations* consume the automatic attempt.
            if not manual and db.execute("SELECT 1 FROM jobs WHERE context_key=? AND invoked_at IS NOT NULL LIMIT 1", (key,)).fetchone():
                return None
            job_id = uuid.uuid4().hex
            db.execute("INSERT INTO jobs(id,thread_id,panel_id,context_json,context_key,config_fp,origin,state,created_at)"
                       " VALUES(?,?,?,?,?,?,?,'queued',?)",
                       (job_id, thread_id, panel_id, row["context_json"], key, config_fp,
                        "manual" if manual else "auto", self.clock()))
            db.execute("UPDATE threads SET latest_job=?,status='generating',suggestions_json='[]',"
                       "error=NULL,error_source=NULL,revision=revision+1 WHERE id=?", (job_id, thread_id))
            return {"id": job_id, "thread_id": thread_id, "context": json.loads(row["context_json"]), "config_fp": config_fp}

    def _job_current(self, db, job, *, starting=False) -> bool:
        row = db.execute("SELECT * FROM threads WHERE id=?", (job["thread_id"],)).fetchone()
        if (self._current_config(db) != job["config_fp"] or not row
                or row["context_key"] != job["context_key"] or row["latest_job"] != job["id"]):
            return False
        return self._live(db, job["thread_id"],
                          auto_only=starting and job["origin"] == "auto",
                          panel_id=job["panel_id"] if starting and job["origin"] == "manual" else None)

    def begin_job(self, job_id: str) -> bool:
        with self._transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["state"] != "queued":
                return False
            if not self._job_current(db, job, starting=True):
                self._cancel(db, job)
                return False
            db.execute("UPDATE jobs SET state='running',started_at=? WHERE id=?", (self.clock(), job_id))
            return True

    def mark_invoked(self, job_id: str) -> bool:
        with self._transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["state"] != "running" or job["invoked_at"] is not None:
                return False
            if not self._job_current(db, job, starting=True):
                self._cancel(db, job)
                return False
            db.execute("UPDATE jobs SET invoked_at=? WHERE id=?", (self.clock(), job_id))
            return True

    def _cancel(self, db, job) -> None:
        db.execute("UPDATE jobs SET state='cancelled',context_json='{}',finished_at=? WHERE id=?", (self.clock(), job["id"]))
        db.execute("UPDATE threads SET status='waiting',latest_job=NULL,revision=revision+1 "
                   "WHERE id=? AND latest_job=?", (job["thread_id"], job["id"]))

    def cancel_job(self, job_id: str) -> None:
        with self._transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job and job["invoked_at"] is None and job["state"] in ("queued", "running"):
                self._cancel(db, job)

    def _recover_abandoned(self, db) -> None:
        # The adapter limits inference to 60 seconds. Leave a further minute for
        # post-generation verification before treating a lost process as failed.
        now = self.clock()
        rows = db.execute("SELECT * FROM jobs WHERE (state='queued' AND created_at<?) "
                          "OR (state='running' AND started_at<?)", (now - 30, now - 120)).fetchall()
        for job in rows:
            if job["invoked_at"] is None:
                self._cancel(db, job)
                continue
            message = "上次后台请求已中断，请手动刷新。"
            db.execute("UPDATE jobs SET state='error',context_json='{}',finished_at=?,error=? WHERE id=?",
                       (now, message, job["id"]))
            db.execute("UPDATE threads SET status='error',error=?,error_source='generation',"
                       "suggestions_json='[]',revision=revision+1 WHERE id=? AND latest_job=?",
                       (message, job["thread_id"], job["id"]))

    def recover_abandoned(self) -> None:
        with self._transaction() as db:
            self._recover_abandoned(db)

    def finish_job(self, job_id: str, suggestions: list, usage: dict, *,
                   error: str | None = None, verified=True) -> bool:
        with self._transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                return False
            if job["state"] != "running":
                # A timed-out worker can still return late. Keep its real usage
                # without restoring the result or counting a second invocation.
                if job["invoked_at"] is not None and usage:
                    db.execute("UPDATE jobs SET usage_json=? WHERE id=?", (_json(usage), job_id))
                return False
            valid = verified and self._job_current(db, job)
            state = ("error" if error else "completed") if valid else "stale"
            # Persist billing evidence before deciding whether the result may be displayed.
            db.execute("UPDATE jobs SET state=?,context_json='{}',finished_at=?,usage_json=?,error=? WHERE id=?",
                       (state, self.clock(), _json(usage), error, job_id))
            if valid:
                status = "error" if error else ("ready" if suggestions else "empty")
                db.execute("UPDATE threads SET status=?,suggestions_json=?,error=?,error_source=?,revision=revision+1 WHERE id=?",
                           (status, _json([] if error else suggestions), error, "generation" if error else None, job["thread_id"]))
            elif job["id"] == db.execute("SELECT latest_job FROM threads WHERE id=?", (job["thread_id"],)).fetchone()[0]:
                db.execute("UPDATE threads SET status=?,suggestions_json='[]',error=?,error_source=?,latest_job=NULL,"
                           "revision=revision+1 WHERE id=?",
                           ("error" if not verified else "waiting",
                            "无法确认聊天最新状态，请手动刷新。" if not verified else None,
                            "generation" if not verified else None, job["thread_id"]))
            return valid

    def snapshot(self, panel_id: str, thread_id: str) -> dict:
        with self._connection() as db:
            db.execute("BEGIN")
            panel = self._panel(db, panel_id, thread_id, allow_closed=True)
            row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
            totals: dict[str, int | float] = {"calls": 0, "failed_calls": 0}
            unknown_calls = 0
            for job in db.execute("SELECT usage_json,error FROM jobs WHERE thread_id=? AND invoked_at IS NOT NULL", (thread_id,)):
                totals["calls"] += 1
                totals["failed_calls"] += int(bool(job["error"]))
                usage = json.loads(job["usage_json"])
                if not (type(usage.get("total_tokens")) is int or
                        all(type(usage.get(k)) is int for k in ("input_tokens", "output_tokens"))):
                    unknown_calls += 1
                for key, value in usage.items():
                    if key not in ("calls", "failed_calls") and isinstance(value, (int, float)) and not isinstance(value, bool):
                        totals[key] = totals.get(key, 0) + value
            if unknown_calls:
                totals["unknown_calls"] = unknown_calls
            recent_jobs = []
            for index, job in enumerate(db.execute(
                    "SELECT origin,state,invoked_at,finished_at,usage_json FROM jobs "
                    "WHERE thread_id=? AND invoked_at IS NOT NULL ORDER BY invoked_at DESC,id DESC LIMIT 20", (thread_id,))):
                usage = json.loads(job["usage_json"])
                safe_usage = {key: value for key, value in usage.items()
                              if key in ("input_tokens", "output_tokens", "cached_input_tokens", "total_tokens", "reasoning_output_tokens")
                              and isinstance(value, int) and not isinstance(value, bool) and value >= 0}
                recent_jobs.append({"ordinal": totals["calls"] - index, "origin": job["origin"],
                                    "state": job["state"], "invoked_at": job["invoked_at"],
                                    "duration_seconds": max(0, job["finished_at"] - job["invoked_at"]) if job["finished_at"] is not None else None,
                                    "usage": safe_usage})
            status = row["status"]
            fresh = row["observed_at"] >= panel["observation_after"]
            if not fresh and status != "unconfigured":
                status = "syncing"
            if panel["closed"] or panel["lease_until"] <= self.clock():
                status = "paused"
            elif not panel["auto_enabled"] and status == "waiting":
                status = "paused"
            return {"panel_id": panel_id, "thread_id": thread_id, "title": row["title"],
                    "status": status, "auto_generate": bool(panel["auto_enabled"]),
                    "suggestions": [] if status == "error" or not fresh else json.loads(row["suggestions_json"]), "usage": totals, "recent_jobs": recent_jobs,
                    "error": row["error"] if fresh else None, "revision": row["revision"]}

    def record_stop(self, thread_id: str, turn_id: str | None) -> None:
        with self._transaction() as db:
            db.execute("INSERT OR REPLACE INTO stop_marks(thread_id,turn_id,marked_at) VALUES(?,?,?)",
                       (thread_id, turn_id, self.clock()))
