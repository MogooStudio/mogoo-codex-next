import tempfile
from pathlib import Path
import unittest

from mogoo_codex_next.store import Store


def context(thread="thread-a", turn="turn-1", fingerprint="content-1"):
    return {"thread_id": thread, "turn_id": turn, "fingerprint": fingerprint,
            "messages": [{"role": "user", "content": "继续处理"},
                         {"role": "assistant", "content": "本轮已完成"}], "title": "测试聊天"}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.store = Store(Path(self.temp.name), clock=lambda: self.now)
        self.store.sync_config("config-a", True)
        self.panel = self.store.open_panel("thread-a", True)
        self.store.observe("thread-a", context(), "config-a")

    def job(self, *, manual=False):
        return self.store.queue_job("thread-a", "config-a", manual=manual,
                                    panel_id=self.panel if manual else None)

    def invoke(self, job):
        self.assertTrue(self.store.begin_job(job["id"]))
        self.assertTrue(self.store.mark_invoked(job["id"]))

    def test_panel_cannot_be_rebound_to_another_chat(self):
        for action in (self.store.renew, self.store.close_panel):
            with self.assertRaises(ValueError):
                action(self.panel, "thread-b")
        with self.assertRaises(ValueError):
            self.store.set_auto(self.panel, "thread-b", True)
        self.assertTrue(self.store.has_live_panel("thread-a"))

    def test_close_one_panel_keeps_other_panel_lease(self):
        second = self.store.open_panel("thread-a", True)
        self.store.close_panel(self.panel, "thread-a")
        job = self.job()
        self.assertIsNotNone(job)
        self.invoke(job)
        self.assertTrue(self.store.finish_job(job["id"], [{"prompt": "继续"}], {}))
        self.assertEqual("ready", self.store.snapshot(second, "thread-a")["status"])
        with self.assertRaises(ValueError):
            self.store.renew(self.panel, "thread-a")

    def test_two_store_instances_deduplicate_same_context(self):
        other = Store(Path(self.temp.name), clock=lambda: self.now)
        job = self.job()
        self.assertIsNone(other.queue_job("thread-a", "config-a"))
        self.invoke(job)
        self.store.finish_job(job["id"], [], {})
        self.assertIsNone(other.queue_job("thread-a", "config-a"))
        manual = other.queue_job("thread-a", "config-a", manual=True, panel_id=self.panel)
        self.assertIsNotNone(manual)

    def test_error_does_not_automatically_retry_but_refresh_can(self):
        job = self.job()
        self.invoke(job)
        self.store.finish_job(job["id"], [], {"input_tokens": 7}, error="失败")
        self.assertEqual("error", self.store.snapshot(self.panel, "thread-a")["status"])
        self.assertIsNone(self.job())
        self.assertIsNotNone(self.job(manual=True))

    def test_expired_or_disabled_panel_cannot_start_queued_call(self):
        job = self.job()
        self.now += 11
        self.assertFalse(self.store.begin_job(job["id"]))
        self.assertEqual(0, self.store.snapshot(self.panel, "thread-a")["usage"]["calls"])
        self.store.renew(self.panel, "thread-a")
        second = self.job()
        self.assertTrue(self.store.begin_job(second["id"]))
        self.store.set_auto(self.panel, "thread-a", False)
        self.assertFalse(self.store.mark_invoked(second["id"]))
        self.assertIsNone(self.job())
        self.assertIsNotNone(self.job(manual=True))

    def test_stale_result_discarded_but_usage_is_preserved(self):
        job = self.job()
        self.invoke(job)
        self.now += 11
        self.assertFalse(self.store.finish_job(job["id"], [{"prompt": "过时建议"}],
                                             {"input_tokens": 23, "output_tokens": 9}))
        snap = self.store.snapshot(self.panel, "thread-a")
        self.assertEqual([], snap["suggestions"])
        self.assertEqual({"calls": 1, "failed_calls": 0, "input_tokens": 23, "output_tokens": 9}, snap["usage"])

    def test_configuration_change_invalidates_queued_and_completed_results(self):
        job = self.job()
        self.store.sync_config("config-b", True)
        self.assertFalse(self.store.begin_job(job["id"]))
        self.assertFalse(self.store.observe("thread-a", context(), "config-a"))
        snap = self.store.snapshot(self.panel, "thread-a")
        self.assertEqual([], snap["suggestions"])
        self.assertEqual("waiting", snap["status"])

    def test_new_turn_and_out_of_order_process_observations(self):
        job = self.job()
        self.invoke(job)
        self.store.observe("thread-a", context(turn="turn-2"), "config-a", observed_at=1002)
        self.assertFalse(self.store.observe("thread-a", context(), "config-a", observed_at=1001))
        self.assertFalse(self.store.finish_job(job["id"], [{"prompt": "旧轮"}], {"output_tokens": 3}))
        next_job = self.job()
        self.assertEqual("turn-2", next_job["context"]["turn_id"])

    def test_active_turn_clears_suggestions_and_blocks_generation(self):
        job = self.job()
        self.invoke(job)
        self.store.finish_job(job["id"], [{"prompt": "上轮建议"}], {})
        self.store.observe("thread-a", None, "config-a")
        self.assertEqual([], self.store.snapshot(self.panel, "thread-a")["suggestions"])
        self.assertIsNone(self.job())

    def test_stop_marks_do_not_create_panels_or_jobs(self):
        self.store.record_stop("thread-b", "turn-b")
        self.assertFalse(self.store.has_live_panel("thread-b"))
        self.assertIsNone(self.store.queue_job("thread-b", "config-a"))

    def test_transient_read_error_restores_cached_result_without_call(self):
        job = self.job()
        self.invoke(job)
        self.store.finish_job(job["id"], [{"prompt": "缓存建议"}], {})
        self.store.read_error("thread-a", "config-a", "读取失败")
        self.assertEqual([], self.store.snapshot(self.panel, "thread-a")["suggestions"])
        self.store.observe("thread-a", context(), "config-a")
        snap = self.store.snapshot(self.panel, "thread-a")
        self.assertEqual("ready", snap["status"])
        self.assertEqual("缓存建议", snap["suggestions"][0]["prompt"])
        self.assertIsNone(self.job())

    def test_reopening_waits_for_fresh_read_before_showing_cache(self):
        job = self.job()
        self.invoke(job)
        self.store.finish_job(job["id"], [{"prompt": "缓存建议"}], {})
        self.now += 1
        second = self.store.open_panel("thread-a", True)
        snap = self.store.snapshot(second, "thread-a")
        self.assertEqual("syncing", snap["status"])
        self.assertEqual([], snap["suggestions"])
        self.store.observe("thread-a", context(), "config-a")
        self.assertEqual("ready", self.store.snapshot(second, "thread-a")["status"])
        self.assertIsNone(self.job())

    def test_expired_lease_is_syncing_until_rechecked_without_new_inference(self):
        job = self.job(); self.invoke(job)
        self.store.finish_job(job["id"], [{"prompt": "缓存建议"}], {})
        self.now += 11
        self.store.renew(self.panel, "thread-a")
        snap = self.store.snapshot(self.panel, "thread-a")
        self.assertEqual("syncing", snap["status"])
        self.assertEqual([], snap["suggestions"])
        self.store.observe("thread-a", context(), "config-a")
        self.assertEqual("ready", self.store.snapshot(self.panel, "thread-a")["status"])
        self.assertIsNone(self.job())

    def test_disabled_auto_stays_disabled_while_reconnecting(self):
        self.store.close_panel(self.panel, "thread-a")
        self.now += 1
        second = self.store.open_panel("thread-a", False)
        snap = self.store.snapshot(second, "thread-a")
        self.assertEqual("syncing", snap["status"])
        self.assertFalse(snap["auto_generate"])
        self.store.observe("thread-a", context(), "config-a")
        self.assertEqual("paused", self.store.snapshot(second, "thread-a")["status"])
        self.assertIsNone(self.job())

    def test_crashed_invocation_requires_manual_retry_and_keeps_unknown_usage(self):
        job = self.job()
        self.invoke(job)
        self.now += 121
        self.store.renew(self.panel, "thread-a")
        self.store.recover_abandoned()
        self.store.observe("thread-a", context(), "config-a")
        snap = self.store.snapshot(self.panel, "thread-a")
        self.assertEqual("error", snap["status"])
        self.assertEqual({"calls": 1, "failed_calls": 1, "unknown_calls": 1}, snap["usage"])
        self.assertIsNone(self.job())
        # A late response still contributes real usage and cannot restore output.
        self.assertFalse(self.store.finish_job(job["id"], [{"prompt": "迟到"}], {"input_tokens": 6}))
        self.assertEqual(6, self.store.snapshot(self.panel, "thread-a")["usage"]["input_tokens"])
        self.assertIsNotNone(self.job(manual=True))

    def test_terminal_jobs_discard_context_but_keep_usage(self):
        job = self.job()
        self.invoke(job)
        self.store.finish_job(job["id"], [], {"output_tokens": 2})
        with self.store._connection() as db:
            row = db.execute("SELECT context_json,usage_json FROM jobs WHERE id=?", (job["id"],)).fetchone()
        self.assertEqual("{}", row["context_json"])
        self.assertIn("2", row["usage_json"])

    def test_recent_records_are_thread_scoped_redacted_and_keep_unknown_usage(self):
        job = self.job(); self.invoke(job); self.now += 2
        self.store.finish_job(job["id"], [], {}, error="fixed-error")
        other_panel = self.store.open_panel("thread-b", True)
        self.store.observe("thread-b", context("thread-b"), "config-a")
        other = self.store.queue_job("thread-b", "config-a")
        self.invoke(other); self.store.finish_job(other["id"], [], {"input_tokens": 999, "output_tokens": 1})
        snap = self.store.snapshot(self.panel, "thread-a")
        self.assertEqual(snap["usage"]["unknown_calls"], 1)
        self.assertEqual(len(snap["recent_jobs"]), 1)
        record = snap["recent_jobs"][0]
        self.assertEqual((record["ordinal"], record["state"], record["duration_seconds"], record["usage"]), (1, "error", 2, {}))
        self.assertEqual(set(record), {"ordinal", "origin", "state", "invoked_at", "duration_seconds", "usage"})
        self.assertNotIn("fixed-error", str(record))
        self.assertEqual(self.store.snapshot(other_panel, "thread-b")["recent_jobs"][0]["usage"]["input_tokens"], 999)

    def test_uninvoked_crashed_queue_can_be_requeued(self):
        old = self.job()
        self.now += 31
        self.store.renew(self.panel, "thread-a")
        self.store.recover_abandoned()
        self.store.observe("thread-a", context(), "config-a")
        self.assertFalse(self.store.begin_job(old["id"]))
        self.assertIsNotNone(self.job())
        self.assertEqual(0, self.store.snapshot(self.panel, "thread-a")["usage"]["calls"])


if __name__ == "__main__":
    unittest.main()
