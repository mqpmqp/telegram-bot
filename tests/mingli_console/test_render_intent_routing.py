from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from mingli_console.console import MingLiConsole


class IntentRuntime:
    commit_sha = "test-fixed-sha"

    def __init__(self) -> None:
        self.full_calls: list[dict[str, object]] = []
        self.text_confirmed_calls: list[dict[str, object]] = []
        self.render_calls: list[tuple[dict[str, object], str, str]] = []
        self.confirmed_follow_up_calls: list[tuple[dict[str, object], str]] = []

    def full(self, payload: dict[str, object]) -> dict[str, object]:
        self.full_calls.append(payload)
        return {
            "final_answer": "完整八段报告\n五年趋势\n仅供文化研究与娱乐参考。",
            "canonical_hash": "sha256:full",
            "effective_domain_statuses": {"career": "supportive", "wealth": "mixed", "relationship": "challenging"},
            "effective_domain_confidence": {"career": "high", "wealth": "medium", "relationship": "low"},
            "scenario_assessment": None,
            "calculation_version": "test",
        }

    def text_confirmed_pillars(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        self.text_confirmed_calls.append(payload)
        return {
            "final_answer": "手动四柱结果\n仅供文化研究与娱乐参考。",
            "canonical_hash": "sha256:text-confirmed",
            "chart": {"source": "text_confirmed"},
        }

    def render_intent(
        self, result: dict[str, object], *, intent: str, question: str
    ) -> dict[str, object]:
        self.render_calls.append((result, intent, question))
        return {
            "final_answer": f"{intent}:{question}\n仅供文化研究与娱乐参考。",
            "supported": True,
        }

    def confirmed_follow_up(
        self, result: dict[str, object], question: str
    ) -> dict[str, object]:
        self.confirmed_follow_up_calls.append((result, question))
        return {
            "final_answer": "已确认四柱的正式支持范围不含定向续问。\n仅供文化研究与娱乐参考。",
            "supported": False,
        }


class FakeKnowledge:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def search(self, query: str) -> list[dict[str, object]]:
        self.calls.append(query)
        return []


class RenderIntentRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_admin_ids = os.environ.get("TELEGRAM_ADMIN_IDS")
        os.environ["TELEGRAM_ADMIN_IDS"] = "42"
        self.temp = tempfile.TemporaryDirectory(dir=Path.home())
        self.sent: list[tuple[str, str]] = []

        async def send(chat_id: str, text: str) -> None:
            self.sent.append((chat_id, text))

        self.runtime = IntentRuntime()
        self.knowledge = FakeKnowledge()
        self.console = MingLiConsole(
            send,
            str(Path(self.temp.name) / "cases.sqlite3"),
            self.runtime,
            self.knowledge,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()
        if self.old_admin_ids is None:
            os.environ.pop("TELEGRAM_ADMIN_IDS", None)
        else:
            os.environ["TELEGRAM_ADMIN_IDS"] = self.old_admin_ids

    @staticmethod
    def arun(coro):
        return asyncio.run(coro)

    @staticmethod
    def chart() -> dict[str, object]:
        return {
            "gender": "male",
            "calendar": "solar",
            "birth_date": "1990-01-01",
            "birth_time": "10:30",
            "timezone": "Asia/Shanghai",
            "birth_location": {"city": "福州"},
            "true_solar_time": False,
        }

    def test_manual_pillars_require_confirmation_and_reconfirm_after_correction(self) -> None:
        message = "四柱：甲子 乙丑 丙寅 丁卯，日主：丙，性别：男"

        self.assertTrue(self.arun(self.console.text("42", "chat", message)))
        self.assertEqual([], self.runtime.text_confirmed_calls)
        self.assertIn("手动四柱候选", self.sent[-1][1])

        self.assertTrue(self.arun(self.console.text("42", "chat", "时柱：戊辰")))
        self.assertEqual([], self.runtime.text_confirmed_calls)
        self.assertIn("已更新", self.sent[-1][1])

        self.assertTrue(self.arun(self.console.text("42", "chat", "确认并分析")))
        self.assertEqual(1, len(self.runtime.text_confirmed_calls))
        payload = self.runtime.text_confirmed_calls[0]
        self.assertEqual("text_confirmed", payload["source"])
        self.assertNotIn("image_chart_confirmation", payload)
        self.assertNotIn("vision_provider", payload)

        self.assertTrue(self.arun(self.console.text("42", "chat", "确认")))
        self.assertEqual(1, len(self.runtime.text_confirmed_calls))
        self.assertEqual([], self.knowledge.calls)

    def test_manual_pillars_reject_invalid_or_mismatched_values_without_runtime(self) -> None:
        self.assertTrue(
            self.arun(
                self.console.text(
                    "42",
                    "chat",
                    "四柱：甲子 乙丑 丙寅 丁卯，日主：乙，性别：男",
                )
            )
        )
        self.assertEqual([], self.runtime.text_confirmed_calls)
        self.assertIn("日主", self.sent[-1][1])
        self.assertIn("validation_failed", self.sent[-1][1])

        self.assertTrue(
            self.arun(
                self.console.text(
                    "42",
                    "chat",
                    "四柱：甲子 乙丑 丙寅 丁A，日主：丙，性别：男",
                )
            )
        )
        self.assertEqual([], self.runtime.text_confirmed_calls)
        self.assertIn("四柱", self.sent[-1][1])
        self.assertIn("validation_failed", self.sent[-1][1])

    def test_active_full_case_follow_up_uses_saved_runtime_artifacts(self) -> None:
        self.console.completed["42"] = {
            "mode": "full",
            "case_id": "case-full",
            "chart": self.chart(),
            "runtime_result": self.runtime.full({}),
        }
        self.runtime.full_calls.clear()

        self.assertTrue(self.arun(self.console.text("42", "chat", "继续看财运")))

        self.assertEqual([], self.runtime.full_calls)
        self.assertEqual(1, len(self.runtime.render_calls))
        _, intent, question = self.runtime.render_calls[0]
        self.assertEqual("follow_up", intent)
        self.assertEqual("继续看财运", question)
        self.assertIn("follow_up:继续看财运", self.sent[-1][1])

    def test_active_full_case_focused_question_uses_saved_runtime_artifacts(self) -> None:
        self.console.completed["42"] = {
            "mode": "full",
            "case_id": "case-full",
            "chart": self.chart(),
            "runtime_result": self.runtime.full({}),
        }
        self.runtime.full_calls.clear()

        self.assertTrue(self.arun(self.console.text("42", "chat", "只看财运")))

        self.assertEqual([], self.runtime.full_calls)
        self.assertEqual(1, len(self.runtime.render_calls))
        _, intent, question = self.runtime.render_calls[0]
        self.assertEqual("focused_question", intent)
        self.assertEqual("只看财运", question)
        self.assertIn("focused_question:只看财运", self.sent[-1][1])

    def test_non_admin_focused_question_is_denied_without_runtime(self) -> None:
        self.assertTrue(self.arun(self.console.text("43", "chat", "只看财运")))

        self.assertEqual([], self.runtime.full_calls)
        self.assertEqual([], self.runtime.text_confirmed_calls)
        self.assertEqual([], self.runtime.render_calls)
        self.assertIn("暂未开放使用", self.sent[-1][1])

    def test_focused_question_without_active_case_is_consumed_without_runtime(self) -> None:
        self.assertTrue(self.arun(self.console.text("42", "chat", "只看财运")))

        self.assertEqual([], self.runtime.full_calls)
        self.assertEqual([], self.runtime.text_confirmed_calls)
        self.assertEqual([], self.runtime.render_calls)
        self.assertIn("没有已完成的 MingLi 案例", self.sent[-1][1])

    def test_completed_birth_text_routes_follow_up_without_new_runtime(self) -> None:
        message = (
            "请看八字：性别：男，公历，出生日期：1990-01-01，"
            "出生时间：10:30，出生地：福州，时区：Asia/Shanghai，真太阳时：否"
        )

        self.assertTrue(self.arun(self.console.text("42", "chat", message)))
        self.assertEqual(1, len(self.runtime.full_calls))
        self.assertEqual("full", self.console.completed["42"]["mode"])

        self.assertTrue(self.arun(self.console.text("42", "chat", "继续看财运")))
        self.assertEqual(1, len(self.runtime.full_calls))
        self.assertEqual(1, len(self.runtime.render_calls))
        self.assertIn("follow_up:继续看财运", self.sent[-1][1])

    def test_confirmed_pillar_follow_up_is_limited_and_never_generic(self) -> None:
        self.console.completed["42"] = {
            "mode": "confirmed_pillars",
            "case_id": "case-image",
            "confirmed_pillars": {"year": "甲子", "month": "乙丑", "day": "丙寅", "hour": "丁卯"},
            "gender": "male",
            "trace_id": "trace-image",
            "runtime_result": {"canonical_hash": "sha256:image"},
        }

        self.assertTrue(self.arun(self.console.text("42", "chat", "继续看财运")))

        self.assertEqual(1, len(self.runtime.confirmed_follow_up_calls))
        self.assertEqual([], self.runtime.full_calls)
        self.assertIn("正式支持范围", self.sent[-1][1])
        self.assertEqual([], self.knowledge.calls)

    def test_analyze_uses_focused_intent_after_one_runtime_call(self) -> None:
        self.console.completed["42"] = {"mode": "full", "case_id": "case-full", "chart": self.chart()}

        self.assertTrue(self.arun(self.console.command("42", "chat", "/analyze")))
        self.assertTrue(self.arun(self.console.text("42", "chat", "财运")))
        self.assertTrue(self.arun(self.console.text("42", "chat", "只看当前问题")))

        self.assertEqual(1, len(self.runtime.full_calls))
        self.assertEqual(1, len(self.runtime.render_calls))
        _, intent, question = self.runtime.render_calls[0]
        self.assertEqual("focused_question", intent)
        self.assertEqual("只看当前问题", question)
        self.assertNotIn("完整八段报告", self.sent[-1][1])
