import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from mingli_console.console import MingLiConsole, chunks


class FakeRuntime:
    commit_sha = "test-sha"

    def __init__(self):
        self.calls = []

    def full(self, payload):
        self.calls.append(payload)
        layers = []
        if payload.get("scenario") == "career_exam":
            layers = [{"layer": x, "label": "conditional", "confidence": "medium"} for x in ["system_fit", "admission_outlook", "exam_outlook", "position_direction", "preparation_strategy"]]
        if payload.get("scenario") == "relationship_reunion":
            layers = [{"layer": x, "label": "conditional", "confidence": "medium"} for x in ["attraction", "recontact", "reunion", "stability"]]
        return {"final_answer": "1. 资料确认\n2. 称骨歌诀\n3. 结论\n4. 事业\n5. 财运\n6. 感情\n7. 五年断事\n8. 建议\n仅供文化研究与娱乐参考。", "calculation_version": "test", "scenario_assessment": {"layers": layers}}


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        os.environ["TELEGRAM_ADMIN_IDS"] = "42"
        self.tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.sent = []

        async def send(chat_id, text):
            self.sent.append((chat_id, text))

        self.runtime = FakeRuntime()
        self.console = MingLiConsole(send, str(Path(self.tmp.name) / "cases.sqlite3"), self.runtime)

    def tearDown(self):
        self.tmp.cleanup()

    def arun(self, coro):
        return asyncio.run(coro)

    def test_admin_and_non_admin(self):
        self.assertTrue(self.arun(self.console.command("7", "c", "/start")))
        self.assertIn("内部工作控制台", self.sent[-1][1])
        self.assertTrue(self.arun(self.console.command("42", "c", "/start")))
        self.assertIn("MingLi 命理师控制台", self.sent[-1][1])

    def test_new_state_missing_and_cancel(self):
        self.assertTrue(self.arun(self.console.command("42", "c", "/new")))
        self.assertTrue(self.arun(self.console.text("42", "c", "案例A")))
        self.assertTrue(self.arun(self.console.text("42", "c", "男")))
        self.assertTrue(self.arun(self.console.command("42", "c", "/cancel")))
        self.assertIn("已取消", self.sent[-1][1])

    def test_confirm_and_save(self):
        self.arun(self.console.command("42", "c", "/new"))
        for value in ["案例A", "男", "公历", "否", "1990-01-01", "10:30", "福州", "否", "事业", "已工作", "标准版"]:
            self.assertTrue(self.arun(self.console.text("42", "c", value)))
        self.assertIn("资料确认", self.sent[-1][1])
        self.assertTrue(self.arun(self.console.confirm("42", "c", "确认并分析")))
        self.assertTrue(self.sent[-1][1].endswith("仅供文化研究与娱乐参考。"))
        self.assertEqual("completed", self.console.repo.recent(1)[0]["status"])

    def test_chunking(self):
        text = "a" * 5000
        parts = chunks(text)
        self.assertEqual(2, len(parts))
        self.assertEqual(text, "".join(parts))

    def test_whoami_pre_auth_and_group_id_separation(self):
        self.assertTrue(self.arun(self.console.whoami("777", "-100999", "group")))
        self.assertIn("777", self.sent[-1][1])
        self.assertIn("group", self.sent[-1][1])
        self.assertTrue(self.arun(self.console.command("777", "-100999", "/start")))
        self.assertIn("内部工作控制台", self.sent[-1][1])

    def test_quick_complete_calls_runtime_and_incomplete_does_not(self):
        self.arun(self.console.command("42", "c", "/quick"))
        self.arun(self.console.text("42", "c", "男 公历 1990-01-01 10:30 出生地：福州"))
        self.arun(self.console.text("42", "c", "事业"))
        self.arun(self.console.text("42", "c", "80字以内"))
        self.assertTrue(self.runtime.calls)
        self.assertLessEqual(len(self.sent[-1][1]), 80)
        before = len(self.runtime.calls)
        self.arun(self.console.command("42", "c", "/quick"))
        self.arun(self.console.text("42", "c", "只有一句留言"))
        self.arun(self.console.text("42", "c", "事业"))
        self.arun(self.console.text("42", "c", "150字以内"))
        self.assertEqual(before, len(self.runtime.calls))
        self.assertIn("资料不足，只能看问题趋势", self.sent[-1][1])

    def test_analyze_scenarios_and_reality(self):
        self.console.completed["42"] = {"chart": {"gender": "male", "calendar": "solar", "birth_date": "1990-01-01", "birth_time": "10:30", "timezone": "Asia/Shanghai", "birth_location": {"city": "福州"}, "true_solar_time": False}, "case_id": "case-x"}
        for topic, scenario, required in [("考公考编", "career_exam", "适合体制内与否"), ("复合", "relationship_reunion", "缘分牵引")]:
            self.arun(self.console.command("42", "c", "/analyze"))
            self.arun(self.console.text("42", "c", topic))
            self.arun(self.console.text("42", "c", "已进入面试；现实证据"))
            self.assertEqual(scenario, self.runtime.calls[-1]["scenario"])
            self.assertIn(required, self.sent[-1][1])
            self.assertEqual("interview", self.runtime.calls[-1]["reality"]["exam_stage"])
            self.assertEqual("已进入面试；现实证据", self.runtime.calls[-1]["reality_context_raw"])

    def test_runtime_timeout_and_unsupported_topic(self):
        class Slow:
            commit_sha = "slow"
            def full(self, payload):
                import time; time.sleep(0.1); return {}
        old = os.environ.get("MINGLI_RUNTIME_TIMEOUT")
        original_runtime = self.console.runtime
        self.console.runtime = Slow()
        os.environ["MINGLI_RUNTIME_TIMEOUT"] = "0.01"
        try:
            self.assertIsNone(self.arun(self.console._run({})))
        finally:
            self.console.runtime = original_runtime
            if old is None: os.environ.pop("MINGLI_RUNTIME_TIMEOUT", None)
            else: os.environ["MINGLI_RUNTIME_TIMEOUT"] = old
        self.console.completed["42"] = {"chart": {"gender": "male", "calendar": "solar", "birth_date": "1990-01-01", "birth_time": "10:30", "timezone": "Asia/Shanghai", "birth_location": {"city": "福州"}, "true_solar_time": False}}
        self.arun(self.console.command("42", "c", "/analyze")); self.arun(self.console.text("42", "c", "学业")); self.arun(self.console.text("42", "c", "现实"))
        self.assertIn("unsupported", self.sent[-1][1])

    def test_runtime_schema_error_and_lunar_flag(self):
        from mingli_console.console import MingLiRuntimeAdapter
        adapter = MingLiRuntimeAdapter()
        with self.assertRaises(ValueError): adapter._validate({"chart_input": {"gender": "male"}, "anchor_year": 2026})
        with self.assertRaises(ValueError): adapter._validate({"chart_input": {"gender": "male", "calendar": "lunar", "birth_date": "1990-01-01", "birth_time": "10:30", "timezone": "Asia/Shanghai", "birth_location": {}, "true_solar_time": False}, "anchor_year": 2026})

    def test_history_queries_export_and_revision(self):
        payload = {"chart_input": {"gender": "male", "calendar": "solar", "birth_date": "1990-01-01", "birth_time": "10:30", "timezone": "Asia/Shanghai", "birth_location": {"city": "福州"}, "true_solar_time": False}, "anchor_year": 2026, "scenario": None, "reality": {}, "fusion_evidence": [], "annual_evidence": [], "advice_codes": []}
        self.console.repo.save({"case_id": "case-x", "customer_id": "42", "display_name": "A", "gender": "男", "calendar_type": "公历", "birth_datetime": "1990-01-01 10:30", "birth_location": {"city": "福州"}, "true_solar_time_policy": "否", "topic": "事业", "reality_context": "x", "normalized_input": payload, "mingli_commit_sha": "old", "runtime_version": "old", "result": "old result", "confidence": "low", "created_at": "old-time", "updated_at": "old-time", "status": "completed"})
        self.arun(self.console.command("42", "c", "/history case-x")); self.assertIn("case-x", self.sent[-1][1])
        self.arun(self.console.command("42", "c", "/history topic 事业")); self.assertIn("case-x", self.sent[-1][1])
        self.arun(self.console.command("42", "c", "/history reanalyze case-x")); self.assertIn("旧 SHA：old", self.sent[-1][1])
        self.assertEqual("old-time", self.console.repo.get("case-x")["created_at"])
        self.assertEqual(1, len(self.console.repo.revisions("case-x")))
