"""Process-safe plugin state. All files live under the supplied plugin root."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable
import uuid

from .performance import clean_performance


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
        self._next_recovery = 0.0
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
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
            for name, declaration in (("observed_at", "REAL NOT NULL DEFAULT 0"), ("error_source", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE threads ADD COLUMN {name} {declaration}")
            if "observation_after" not in {row[1] for row in db.execute("PRAGMA table_info(panels)")}:
                db.execute("ALTER TABLE panels ADD COLUMN observation_after REAL NOT NULL DEFAULT 0")
            for table, additions in {
                "threads": (("generation", "INTEGER NOT NULL DEFAULT 0"), ("result_job", "TEXT"), ("reason_code", "TEXT")),
                "jobs": (("generation", "INTEGER NOT NULL DEFAULT 0"), ("outcome", "TEXT"),
                         ("performance_json", "TEXT NOT NULL DEFAULT '{}'"),
                         ("preview_json", "TEXT NOT NULL DEFAULT '[]'"), ("preview_ready_at", "REAL"),
                         ("pending_result_json", "TEXT")),
            }.items():
                names = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                for name, declaration in additions:
                    if name not in names:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS refresh_requests (
                    panel_id TEXT NOT NULL, id TEXT NOT NULL, thread_id TEXT NOT NULL,
                    generation INTEGER, requested_at REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', job_id TEXT,
                    PRIMARY KEY(panel_id,id)
                );
                CREATE INDEX IF NOT EXISTS refresh_pending ON refresh_requests(thread_id,state);
            """)
            # Link old valid results, including a successfully generated empty list.
            db.execute("UPDATE threads SET result_job=latest_job WHERE result_job IS NULL "
                       "AND (status IN ('ready','empty') OR error_source='read') AND EXISTS "
                       "(SELECT 1 FROM jobs WHERE id=threads.latest_job AND state='completed')")

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
        now = self.clock()
        with self._connection() as db:
            if self._current_config(db) == config_fp and now < self._next_recovery:
                return False
        with self._transaction() as db:
            if now >= self._next_recovery:
                self._recover_abandoned(db)
                self._next_recovery = now + 10
            if self._current_config(db) == config_fp:
                return False
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('config_fp',?)", (config_fp,))
            db.execute("UPDATE threads SET context_json=NULL,context_key=NULL,config_fp=?,"
                       "status=?,suggestions_json='[]',error=NULL,error_source=NULL,reason_code=NULL,latest_job=NULL,"
                       "result_job=NULL,generation=generation+1,revision=revision+1",
                       (config_fp, "waiting" if configured else "unconfigured"))
            db.execute("UPDATE jobs SET state='cancelled',context_json='{}',finished_at=? WHERE state='queued'", (self.clock(),))
            db.execute("UPDATE jobs SET pending_result_json=NULL WHERE pending_result_json IS NOT NULL")
            db.execute("UPDATE refresh_requests SET state='cancelled' WHERE state='pending'")
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
            db.execute("UPDATE refresh_requests SET state='cancelled' WHERE panel_id=? AND state='pending'", (panel_id,))

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
                    previous = db.execute("SELECT state FROM jobs WHERE id=?", (old["result_job"],)).fetchone()
                    restored = ("ready" if json.loads(old["suggestions_json"]) else "empty") if previous and previous[0] == "completed" else "waiting"
                    db.execute("UPDATE threads SET status=?,error=NULL,error_source=NULL,reason_code=NULL,revision=revision+1 WHERE id=?",
                               (restored, thread_id))
                if actual_title != old["title"]:
                    db.execute("UPDATE threads SET title=?,revision=revision+1 WHERE id=?", (actual_title, thread_id))
                self._reconcile(db, thread_id, fresh=True)
                return True
            db.execute("UPDATE threads SET title=?,context_json=?,context_key=?,config_fp=?,"
                       "status='waiting',suggestions_json='[]',error=NULL,error_source=NULL,reason_code=NULL,latest_job=NULL,"
                       "result_job=NULL,generation=generation+1,"
                       "revision=revision+1,observed_at=? WHERE id=?",
                       (actual_title, _json(context) if context else None, key, config_fp, stamp, thread_id))
            db.execute("UPDATE jobs SET state='cancelled',context_json='{}',finished_at=? WHERE thread_id=? AND state='queued'",
                       (self.clock(), thread_id))
            db.execute("UPDATE jobs SET pending_result_json=NULL WHERE thread_id=? AND pending_result_json IS NOT NULL",
                       (thread_id,))
            db.execute("UPDATE refresh_requests SET generation=? WHERE thread_id=? AND state='pending' AND generation IS NULL",
                       (old["generation"] + 1, thread_id))
            db.execute("UPDATE refresh_requests SET state='cancelled' WHERE thread_id=? AND state='pending' AND generation!=?",
                       (thread_id, old["generation"] + 1))
            self._reconcile(db, thread_id, fresh=True)
            return True

    def _reconcile(self, db, thread_id: str, *, fresh=False) -> None:
        """Restore only this freshly observed context; admission never repairs state."""
        row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if not row or not row["context_key"] or row["error_source"] == "read":
            return
        active = db.execute("SELECT * FROM jobs WHERE context_key=? AND generation=? "
                            "AND state IN ('queued','running','verifying') ORDER BY created_at DESC LIMIT 1",
                            (row["context_key"], row["generation"])).fetchone()
        if active:
            if row["status"] != "generating" or row["latest_job"] != active["id"]:
                db.execute("UPDATE threads SET status='generating',latest_job=?,error=NULL,error_source=NULL,"
                           "reason_code=NULL,revision=revision+1 WHERE id=?", (active["id"], thread_id))
            return
        if row["status"] in ("ready", "empty") and row["result_job"]:
            return
        previous = db.execute("SELECT * FROM jobs WHERE context_key=? AND invoked_at IS NOT NULL "
                              "ORDER BY invoked_at DESC,created_at DESC,id DESC LIMIT 1", (row["context_key"],)).fetchone()
        if not previous:
            return
        same_generation = previous["generation"] == row["generation"]
        # A completed HTTP request may outlive the visible lease. Its body is
        # only a candidate until a read STARTED after completion confirms the
        # same context, configuration and generation. Admission/cancellation
        # paths cannot promote it, and the newest invocation always wins.
        if (fresh and same_generation and previous["state"] == "stale"
                and previous["outcome"] == "succeeded" and previous["pending_result_json"] is not None
                and previous["finished_at"] is not None and row["observed_at"] >= previous["finished_at"]
                and previous["config_fp"] == row["config_fp"]):
            suggestions = json.loads(previous["pending_result_json"])
            db.execute("UPDATE jobs SET state='completed',pending_result_json=NULL WHERE id=?", (previous["id"],))
            db.execute("UPDATE threads SET status=?,suggestions_json=?,error=NULL,error_source=NULL,reason_code=NULL,"
                       "latest_job=?,result_job=?,revision=revision+1 WHERE id=?",
                       ("ready" if suggestions else "empty", _json(suggestions), previous["id"], previous["id"], thread_id))
            return
        message = (previous["error"] if same_generation else None) or "上次建议未能保留，请手动刷新。"
        reason = "generation_failed" if same_generation and previous["error"] else "result_unavailable"
        if row["status"] != "error" or row["error"] != message or row["reason_code"] != reason:
            db.execute("UPDATE threads SET status='error',error=?,error_source='generation',reason_code=?,"
                       "latest_job=NULL,revision=revision+1 WHERE id=?", (message, reason, thread_id))

    def read_error(self, thread_id: str, config_fp: str, message: str) -> None:
        with self._transaction() as db:
            if self._current_config(db) != config_fp or not self._live(db, thread_id):
                return
            # An in-flight generation owns its status; its completion will recheck the thread.
            row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
            if not row:
                return
            if row["status"] == "generating":
                # A failed freshness check must retract an in-flight preview;
                # the model request and its billing evidence continue normally.
                changed = db.execute("UPDATE jobs SET preview_json='[]' WHERE id=? AND preview_json!='[]'",
                                     (row["latest_job"],)).rowcount
                if changed:
                    db.execute("UPDATE threads SET revision=revision+1 WHERE id=?", (thread_id,))
                return
            now = self.clock()
            newly_visible = db.execute("SELECT 1 FROM panels WHERE thread_id=? AND closed=0 "
                                       "AND observation_after>? AND observation_after<=? LIMIT 1",
                                       (thread_id, row["observed_at"], now)).fetchone()
            changed = row["status"] != "error" or row["error"] != message or newly_visible is not None
            db.execute("UPDATE threads SET status='error',error=?,error_source='read',reason_code='read_failed',"
                       "observed_at=?,revision=revision+? WHERE id=?", (message, now, int(changed), thread_id))
            db.execute("UPDATE refresh_requests SET state='read_failed' WHERE thread_id=? AND state='pending'", (thread_id,))

    def prepare_refresh(self, panel_id: str, thread_id: str, request_id: str | None = None) -> str:
        request_id = request_id or uuid.uuid4().hex
        with self._transaction() as db:
            self._panel(db, panel_id, thread_id)
            row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
            inserted = db.execute("INSERT OR IGNORE INTO refresh_requests(panel_id,id,thread_id,generation,requested_at) "
                                 "VALUES(?,?,?,?,?)", (panel_id, request_id, thread_id,
                                 row["generation"] if row["context_key"] else None, self.clock())).rowcount
            if inserted:
                active = db.execute("SELECT id FROM jobs WHERE context_key=? AND state IN ('queued','running','verifying') LIMIT 1",
                                    (row["context_key"],)).fetchone()
                if active:
                    db.execute("UPDATE refresh_requests SET state='attached',job_id=? WHERE panel_id=? AND id=?",
                               (active[0], panel_id, request_id))
                else:
                    # Keep the last valid result until an actual invocation replaces it.
                    db.execute("UPDATE threads SET revision=revision+1 WHERE id=?", (thread_id,))
                    db.execute("UPDATE panels SET observation_after=? WHERE id=?", (self.clock(), panel_id))
        return request_id

    def pending_refreshes(self, thread_id: str) -> list[dict]:
        with self._connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM refresh_requests WHERE thread_id=? AND state='pending' "
                                                   "ORDER BY requested_at,panel_id,id", (thread_id,))]

    def queue_job(self, thread_id: str, config_fp: str, *, manual=False,
                  panel_id: str | None = None, request_id: str | None = None) -> dict | None:
        with self._transaction() as db:
            if self._current_config(db) != config_fp:
                return None
            if panel_id:
                self._panel(db, panel_id, thread_id)
            if not self._live(db, thread_id, auto_only=not manual,
                              panel_id=panel_id if manual else None):
                if request_id:
                    db.execute("UPDATE refresh_requests SET state='cancelled' WHERE panel_id=? AND id=? AND state='pending'",
                               (panel_id, request_id))
                return None
            row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
            if request_id:
                intent = db.execute("SELECT * FROM refresh_requests WHERE panel_id=? AND id=? AND thread_id=?",
                                    (panel_id, request_id, thread_id)).fetchone()
                if not intent or intent["state"] != "pending":
                    return None
                if row["observed_at"] < intent["requested_at"]:
                    return None
                if not row["context_key"] or intent["generation"] != row["generation"]:
                    db.execute("UPDATE refresh_requests SET state='not_eligible' WHERE panel_id=? AND id=?", (panel_id, request_id))
                    return None
            if not row or not row["context_json"] or row["config_fp"] != config_fp:
                return None
            key = row["context_key"]
            active = db.execute("SELECT id FROM jobs WHERE context_key=? AND state IN ('queued','running','verifying') LIMIT 1", (key,)).fetchone()
            if active:
                if request_id:
                    db.execute("UPDATE refresh_requests SET state='attached',job_id=? WHERE panel_id=? AND id=?",
                               (active[0], panel_id, request_id))
                return None
            # Even failed or discarded *invocations* consume the automatic attempt.
            if not manual:
                previous = db.execute("SELECT error FROM jobs WHERE context_key=? AND invoked_at IS NOT NULL "
                                      "ORDER BY invoked_at DESC,created_at DESC,id DESC LIMIT 1", (key,)).fetchone()
                if previous:
                    return None
            context = json.loads(row["context_json"])
            if manual and row["status"] == "ready" and row["result_job"]:
                # The result belongs to this context. Keep it out of the stored
                # snapshot so automatic attempts and context identity stay unchanged.
                previous_prompts = [
                    item["prompt"][:1000]
                    for item in json.loads(row["suggestions_json"])[:3]
                    if isinstance(item, dict) and isinstance(item.get("prompt"), str) and item["prompt"]
                ]
                if previous_prompts:
                    context["previous_suggestions"] = previous_prompts
            job_id = uuid.uuid4().hex
            db.execute("INSERT INTO jobs(id,thread_id,panel_id,context_json,context_key,config_fp,origin,state,created_at,generation)"
                       " VALUES(?,?,?,?,?,?,?,'queued',?,?)",
                       (job_id, thread_id, panel_id, row["context_json"], key, config_fp,
                        "manual" if manual else "auto", self.clock(), row["generation"]))
            if request_id:
                db.execute("UPDATE refresh_requests SET state='created',job_id=? WHERE panel_id=? AND id=?", (job_id, panel_id, request_id))
            db.execute("UPDATE threads SET latest_job=?,status='generating',"
                       "error=NULL,error_source=NULL,reason_code=NULL,revision=revision+1 WHERE id=?", (job_id, thread_id))
            return {"id": job_id, "thread_id": thread_id, "context": context, "config_fp": config_fp}

    def _job_current(self, db, job, *, starting=False, require_live=True) -> bool:
        row = db.execute("SELECT * FROM threads WHERE id=?", (job["thread_id"],)).fetchone()
        if (self._current_config(db) != job["config_fp"] or not row
                or row["context_key"] != job["context_key"] or row["generation"] != job["generation"]
                or row["latest_job"] != job["id"]):
            return False
        return not require_live or self._live(db, job["thread_id"],
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
            db.execute("UPDATE threads SET result_job=NULL,suggestions_json='[]' WHERE id=? AND latest_job=?",
                       (job["thread_id"], job_id))
            return True

    def _cancel(self, db, job) -> None:
        db.execute("UPDATE jobs SET state='cancelled',context_json='{}',finished_at=? WHERE id=?", (self.clock(), job["id"]))
        db.execute("UPDATE threads SET status=CASE WHEN result_job IS NULL THEN 'waiting' "
                   "WHEN suggestions_json='[]' THEN 'empty' ELSE 'ready' END,latest_job=NULL,revision=revision+1 "
                   "WHERE id=? AND latest_job=?", (job["thread_id"], job["id"]))
        self._reconcile(db, job["thread_id"])

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
                          "OR (state IN ('running','verifying') AND started_at<?)", (now - 30, now - 120)).fetchall()
        for job in rows:
            if job["invoked_at"] is None:
                self._cancel(db, job)
                continue
            message = job["error"] or ("无法确认聊天最新状态，请手动刷新。" if job["state"] == "verifying"
                                        else "上次后台请求已中断，请手动刷新。")
            db.execute("UPDATE jobs SET state='error',context_json='{}',preview_json='[]',finished_at=?,error=? WHERE id=?",
                       (now, message, job["id"]))
            db.execute("UPDATE threads SET status='error',error=?,error_source='generation',reason_code='generation_failed',"
                       "suggestions_json='[]',revision=revision+1 WHERE id=? AND latest_job=?",
                       (message, job["thread_id"], job["id"]))

    def recover_abandoned(self) -> None:
        with self._transaction() as db:
            self._recover_abandoned(db)

    def publish_preview(self, job_id: str, suggestions: list, performance=None) -> bool:
        """Publish one validated item only after the caller's fresh thread read."""
        if not isinstance(suggestions, list) or len(suggestions) != 1:
            return False
        with self._transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if (not job or job["state"] != "running" or job["invoked_at"] is None
                    or job["preview_ready_at"] is not None or not self._job_current(db, job)):
                return False
            timings = clean_performance(json.loads(job["performance_json"]))
            timings.update(clean_performance(performance))
            now = self.clock()
            timings.update(clean_performance({"preview_ready_ms": (now - job["invoked_at"]) * 1000}))
            db.execute("UPDATE jobs SET preview_json=?,preview_ready_at=?,performance_json=? WHERE id=?",
                       (_json(suggestions), now, _json(timings), job_id))
            db.execute("UPDATE threads SET revision=revision+1 WHERE id=?", (job["thread_id"],))
            return True

    def record_result(self, job_id: str, usage: dict, error: str | None, *, performance=None, suggestions=None) -> None:
        # Save billing/outcome as soon as the model returns. Verification uses
        # the fair read queue and may need to wait behind other conversations.
        with self._transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["state"] != "running" or job["invoked_at"] is None:
                return
            timings = clean_performance(json.loads(job["performance_json"]))
            timings.update(clean_performance(performance))
            retract = bool(error) or (suggestions is not None and json.loads(job["preview_json"]) != suggestions[:1])
            db.execute("UPDATE jobs SET state='verifying',usage_json=?,error=?,outcome=?,performance_json=?,"
                       "preview_json=CASE WHEN ? THEN '[]' ELSE preview_json END "
                       "WHERE id=? AND state='running' AND invoked_at IS NOT NULL",
                       (_json(usage), error, "failed" if error else "succeeded", _json(timings), retract, job_id))

    def finish_job(self, job_id: str, suggestions: list, usage: dict, *,
                   error: str | None = None, verified=True, performance=None) -> bool:
        with self._transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                return False
            timings = clean_performance(json.loads(job["performance_json"]))
            timings.update(clean_performance(performance))
            if job["state"] not in ("running", "verifying"):
                # A timed-out worker can still return late. Keep its real usage
                # without restoring the result or counting a second invocation.
                if job["invoked_at"] is not None and usage:
                    db.execute("UPDATE jobs SET usage_json=?,performance_json=? WHERE id=?", (_json(usage), _json(timings), job_id))
                return False
            valid = verified and self._job_current(db, job)
            pending = not valid and not error and job["invoked_at"] is not None and self._job_current(db, job, require_live=False)
            state = ("error" if error else "completed") if valid else "stale"
            # Persist billing evidence before deciding whether the result may be displayed.
            db.execute("UPDATE jobs SET state=?,context_json='{}',preview_json='[]',finished_at=?,usage_json=?,error=?,performance_json=?,pending_result_json=? WHERE id=?",
                       (state, self.clock(), _json(usage), error, _json(timings), _json(suggestions) if pending else None, job_id))
            db.execute("UPDATE jobs SET outcome=? WHERE id=?", ("failed" if error else "succeeded", job_id))
            if valid:
                status = "error" if error else ("ready" if suggestions else "empty")
                db.execute("UPDATE threads SET status=?,suggestions_json=?,error=?,error_source=?,reason_code=?,result_job=?,revision=revision+1 WHERE id=?",
                           (status, _json([] if error else suggestions), error, "generation" if error else None,
                            "generation_failed" if error else None, None if error else job_id, job["thread_id"]))
            elif job["id"] == db.execute("SELECT latest_job FROM threads WHERE id=?", (job["thread_id"],)).fetchone()[0]:
                db.execute("UPDATE threads SET status='error',suggestions_json='[]',error=?,error_source='generation',"
                           "reason_code=?,result_job=NULL,latest_job=NULL,"
                           "revision=revision+1 WHERE id=?",
                           (error or ("无法确认聊天最新状态，请手动刷新。" if not verified else "上次建议未能保留，请手动刷新。"),
                            "generation_failed" if error else "result_unavailable", job["thread_id"]))
            return valid

    def record_display(self, panel_id: str, thread_id: str, job_id: str, displayed_at: float, *, stage="final") -> bool:
        """Observe first and complete visible frames; never queue a model call."""
        if type(displayed_at) not in (int, float) or not math.isfinite(displayed_at) or stage not in ("preview", "final"):
            return False
        with self._transaction() as db:
            panel = self._panel(db, panel_id, thread_id)
            job = db.execute("SELECT * FROM jobs WHERE id=? AND thread_id=?", (job_id, thread_id)).fetchone()
            thread = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
            if (not job or not thread or job["created_at"] < panel["observation_after"]
                    or not self._job_current(db, job)):
                return False
            timings = clean_performance(json.loads(job["performance_json"]))
            if stage == "preview":
                ready_at = job["preview_ready_at"]
                if ready_at is None or (job["state"] in ("error", "stale") and
                                         job["finished_at"] is not None and displayed_at > job["finished_at"]):
                    return False
            elif stage == "final" and thread["result_job"] == job_id and thread["status"] == "ready" and job["state"] == "completed":
                ready_at = job["finished_at"]
            else:
                return False
            if ready_at is None or (stage == "final" and "generation_ms" not in timings):
                return False  # Historical/cached results have no new display sample.
            now = self.clock()
            if (type(displayed_at) not in (int, float) or not math.isfinite(displayed_at)
                    or displayed_at < ready_at or not now - 120 <= displayed_at <= now + .1):
                return False
            elapsed = (displayed_at - job["invoked_at"]) * 1000
            if stage == "final" and elapsed < timings.get("all_displayed_ms", float("inf")):
                timings.update(clean_performance({"all_displayed_ms": elapsed,
                                                  "display_after_ready_ms": (displayed_at - ready_at) * 1000}))
            if elapsed < timings.get("displayed_ms", float("inf")):
                timings.update(clean_performance({"displayed_ms": elapsed, "display_stage": stage}))
                if stage == "preview":
                    timings.update(clean_performance({"display_after_preview_ms": (displayed_at - ready_at) * 1000}))
            db.execute("UPDATE jobs SET performance_json=? WHERE id=?", (_json(timings), job_id))
            return True

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
                    "SELECT id,origin,state,invoked_at,finished_at,usage_json,performance_json FROM jobs "
                    "WHERE thread_id=? AND invoked_at IS NOT NULL ORDER BY invoked_at DESC,id DESC LIMIT 20", (thread_id,))):
                usage = json.loads(job["usage_json"])
                safe_usage = {key: value for key, value in usage.items()
                              if key in ("input_tokens", "output_tokens", "cached_input_tokens", "total_tokens", "reasoning_output_tokens")
                              and isinstance(value, int) and not isinstance(value, bool) and value >= 0}
                recent_jobs.append({"ordinal": totals["calls"] - index, "origin": job["origin"],
                                    "state": job["state"], "invoked_at": job["invoked_at"],
                                    "duration_seconds": max(0, job["finished_at"] - job["invoked_at"]) if job["finished_at"] is not None else None,
                                    "usage": safe_usage,
                                    "performance": clean_performance(json.loads(job["performance_json"]))})
            status = row["status"]
            preview = None
            if status == "generating" and row["latest_job"]:
                candidate = db.execute("SELECT * FROM jobs WHERE id=?", (row["latest_job"],)).fetchone()
                if (candidate and candidate["state"] in ("running", "verifying")
                        and candidate["preview_ready_at"] is not None and json.loads(candidate["preview_json"])
                        and self._job_current(db, candidate)):
                    preview = candidate
                    status = "streaming"
            fresh = row["observed_at"] >= panel["observation_after"]
            pending = db.execute("SELECT 1 FROM refresh_requests WHERE panel_id=? AND state='pending' LIMIT 1", (panel_id,)).fetchone()
            if (not fresh or pending) and status != "unconfigured":
                status = "syncing"
            if panel["closed"] or panel["lease_until"] <= self.clock():
                status = "paused"
            elif not panel["auto_enabled"] and status == "waiting":
                status = "paused"
            refresh_notice = None
            if status in ("ready", "empty") and row["result_job"]:
                intent = db.execute("SELECT r.*,j.state AS job_state,j.invoked_at FROM refresh_requests r "
                                    "LEFT JOIN jobs j ON j.id=r.job_id WHERE r.thread_id=? "
                                    "ORDER BY r.requested_at DESC,r.id DESC LIMIT 1", (thread_id,)).fetchone()
                result = db.execute("SELECT invoked_at FROM jobs WHERE id=?", (row["result_job"],)).fetchone()
                if (intent and result and intent["generation"] == row["generation"]
                        and intent["invoked_at"] is None and result[0] <= intent["requested_at"]
                        and (intent["state"] == "cancelled" or intent["job_state"] == "cancelled")):
                    refresh_notice = {"id": intent["panel_id"] + ":" + intent["id"],
                                      "message": "上次刷新未调用模型，当前显示原有建议。"}
            display_sample = None
            if (status == "ready" and row["result_job"]) or status == "streaming":
                result = preview if status == "streaming" else db.execute("SELECT * FROM jobs WHERE id=?", (row["result_job"],)).fetchone()
                if result and result["created_at"] >= panel["observation_after"]:
                    timings = clean_performance(json.loads(result["performance_json"]))
                    missing = "displayed_ms" not in timings if status == "streaming" else "all_displayed_ms" not in timings
                    if ("generation_ms" in timings or status == "streaming") and missing:
                        display_sample = {"job_id": result["id"], "stage": "preview" if status == "streaming" else "final"}
            return {"panel_id": panel_id, "thread_id": thread_id, "title": row["title"],
                    "display_sample": display_sample,
                    "status": status, "auto_generate": bool(panel["auto_enabled"]),
                    "suggestions": [] if status in ("error", "syncing", "generating") or not fresh else
                        json.loads(preview["preview_json"] if status == "streaming" else row["suggestions_json"]), "usage": totals, "recent_jobs": recent_jobs,
                    "error": row["error"] if fresh and status == "error" else None,
                    "reason_code": row["reason_code"] if fresh and status == "error" else None,
                    "refresh_notice": refresh_notice,
                    "revision": row["revision"]}

    def record_stop(self, thread_id: str, turn_id: str | None) -> None:
        with self._transaction() as db:
            db.execute("INSERT OR REPLACE INTO stop_marks(thread_id,turn_id,marked_at) VALUES(?,?,?)",
                       (thread_id, turn_id, self.clock()))
