import unittest

from mogoo_codex_next.context import MAX_CONTEXT_CHARS, snapshot


def turn(ident, user="继续分析", answer="已经完成检查。", status="completed"):
    return {"id": ident, "status": status, "items": [
        {"type": "userMessage", "content": [{"type": "text", "text": user}]},
        {"type": "agentMessage", "phase": "final_answer", "text": answer}]}


class ContextTests(unittest.TestCase):
    def test_only_latest_completed_is_eligible(self):
        for status in ("inProgress", "failed", "interrupted"):
            self.assertIsNone(snapshot({"id": "t", "turns": [turn("a"), turn("b", status=status)]}))
        self.assertIsNone(snapshot({"id": "t", "turns": []}))
        incomplete = turn("a")
        incomplete["itemsView"] = "summary"
        self.assertIsNone(snapshot({"id": "t", "turns": [incomplete]}))

    def test_context_excludes_tools_reasoning_and_host_wrappers(self):
        value = turn("a", "<environment_context>private-host</environment_context>请设计")
        value["items"] += [{"type": "commandExecution", "output": "secret-tool-output"},
                            {"type": "agentMessage", "phase": "commentary", "text": "private-progress"},
                            {"type": "reasoning", "text": "private-reasoning"}]
        snap = snapshot({"id": "t", "turns": [value]})
        self.assertEqual(snap["messages"], [{"role": "user", "content": "请设计"},
                                             {"role": "assistant", "content": "已经完成检查。"}])

    def test_original_goal_and_latest_reply_tail_survive_budget(self):
        turns = [turn(str(i), "目标" + str(i) + "字" * 5000, "回答" * 15000 + "最后需要确认的事项")
                 for i in range(10)]
        snap = snapshot({"id": "t", "name": "项目", "turns": turns})
        self.assertTrue(snap["messages"][0]["content"].startswith("目标0"))
        self.assertTrue(snap["messages"][-1]["content"].endswith("最后需要确认的事项"))
        self.assertLessEqual(sum(len(m["content"]) for m in snap["messages"]), MAX_CONTEXT_CHARS)

    def test_fingerprint_changes_for_new_turn_and_changed_tail(self):
        a = snapshot({"id": "t", "turns": [turn("a")]})
        b = snapshot({"id": "t", "turns": [turn("b")]})
        c = snapshot({"id": "t", "turns": [turn("a", answer="已完成，等待发布确认")]})
        self.assertNotEqual(a["fingerprint"], b["fingerprint"])
        self.assertNotEqual(a["fingerprint"], c["fingerprint"])

    def test_no_final_response_is_not_eligible(self):
        value = turn("a")
        value["items"] = value["items"][:1]
        self.assertIsNone(snapshot({"id": "t", "turns": [value]}))

    def test_active_error_or_archived_thread_rejects_old_completed_turn(self):
        for status in ({"type": "active", "activeFlags": []}, {"type": "systemError"},
                       {"type": "archived"}, "active"):
            with self.subTest(status=status):
                self.assertIsNone(snapshot({"id": "t", "status": status, "turns": [turn("old")]}))
        for flag in ("archived", "isArchived"):
            with self.subTest(flag=flag):
                self.assertIsNone(snapshot({"id": "t", flag: True, "turns": [turn("old")]}))

    def test_idle_not_loaded_and_missing_thread_status_remain_compatible(self):
        for status in (None, {"type": "idle"}, {"type": "notLoaded"}):
            thread = {"id": "t", "turns": [turn("a")]}
            if status is not None:
                thread["status"] = status
            with self.subTest(status=status):
                self.assertIsNotNone(snapshot(thread))
