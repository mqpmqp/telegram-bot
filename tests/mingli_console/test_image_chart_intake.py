from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mingli_console.console import MingLiConsole


class FakeRuntime:
    commit_sha = "test-sha"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def full(self, payload: dict) -> dict:
        self.calls.append(payload)
        return {"final_answer": "测试结果\n仅供文化研究与娱乐参考。", "calculation_version": "test"}

    def confirmed_pillars(self, payload: dict) -> dict:
        self.calls.append(payload)
        return {"final_answer": "图片命盘结果\n仅供文化研究与娱乐参考。"}


def _field(value: str, *, confidence: str = "high", source: str = "visible", warning: str = "") -> dict[str, str]:
    return {"value": value, "confidence": confidence, "source": source, "warning": warning}


def _provider_result() -> dict[str, object]:
    return {
        "success": True,
        "candidates": {
            "year_pillar": _field("甲子"),
            "month_pillar": _field("乙丑"),
            "day_pillar": _field("丙寅"),
            "hour_pillar": _field("丁卯"),
            "day_master": _field("丙"),
        },
    }


class ImageChartConsoleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_admin_ids = os.environ.get("TELEGRAM_ADMIN_IDS")
        self.old_repo = os.environ.get("MINGLI_REPO")
        os.environ["TELEGRAM_ADMIN_IDS"] = "42"
        self.assertTrue(self.old_repo, "MINGLI_REPO must point to the isolated MingLi worktree")
        self.tmp = tempfile.TemporaryDirectory(dir=Path.home(), ignore_cleanup_errors=True)
        self.sent: list[tuple[str, str]] = []

        async def send(chat_id: str, text: str) -> None:
            self.sent.append((chat_id, text))

        self.runtime = FakeRuntime()
        self.console = MingLiConsole(send, str(Path(self.tmp.name) / "cases.sqlite3"), self.runtime)

    def tearDown(self) -> None:
        self.tmp.cleanup()
        if self.old_admin_ids is None:
            os.environ.pop("TELEGRAM_ADMIN_IDS", None)
        else:
            os.environ["TELEGRAM_ADMIN_IDS"] = self.old_admin_ids
        if self.old_repo is None:
            os.environ.pop("MINGLI_REPO", None)
        else:
            os.environ["MINGLI_REPO"] = self.old_repo

    @staticmethod
    def arun(coro):
        return asyncio.run(coro)

    def _complete_independent_birth_data(self) -> None:
        for value in ["案例A", "男", "公历", "否", "1990-01-01", "10:30", "福州", "否", "事业", "现实背景", "标准版"]:
            self.assertTrue(self.arun(self.console.text("42", "c", value)))

    def test_candidate_is_displayed_and_runtime_is_not_called_before_confirmation(self) -> None:
        self.assertTrue(self.arun(self.console.image_chart("42", "c", _provider_result())))

        self.assertEqual("image_chart", self.console.sessions[("c", "42")].mode)
        self.assertEqual("awaiting_confirmation", self.console.sessions[("c", "42")].step)
        self.assertEqual([], self.runtime.calls)
        reply = self.sent[-1][1]
        self.assertIn("年柱：甲子", reply)
        self.assertIn("日主：丙", reply)

    def test_field_correction_requires_a_second_confirmation(self) -> None:
        self.arun(self.console.image_chart("42", "c", _provider_result()))
        self.assertTrue(self.arun(self.console.confirm("42", "c", "年柱=乙丑")))

        self.assertEqual("乙丑", self.console.sessions[("c", "42")].data["candidate"]["year_pillar"])
        self.assertEqual("awaiting_confirmation", self.console.sessions[("c", "42")].step)
        self.assertEqual([], self.runtime.calls)
        self.assertIn("尚未确认", self.sent[-1][1])

    def test_confirmed_candidate_without_gender_asks_only_for_gender(self) -> None:
        self.arun(self.console.image_chart("42", "c", _provider_result()))
        self.assertTrue(self.arun(self.console.confirm("42", "c", "确认图片候选")))

        self.assertEqual("awaiting_gender", self.console.sessions[("c", "42")].step)
        self.assertEqual([], self.runtime.calls)
        self.assertIn("只需回复男或女", self.sent[-1][1])

    def test_gender_reply_dispatches_runtime_once_only(self) -> None:
        self.arun(self.console.image_chart("42", "c", _provider_result()))
        self.arun(self.console.confirm("42", "c", "确认图片候选"))
        self.assertEqual([], self.runtime.calls)
        self.assertTrue(self.arun(self.console.confirm("42", "c", "女")))
        self.assertEqual(1, len(self.runtime.calls))
        self.assertTrue(self.arun(self.console.confirm("42", "c", "确认")))
        self.assertEqual(1, len(self.runtime.calls))

    def test_invalid_or_incomplete_provider_response_never_creates_candidate(self) -> None:
        response = _provider_result()
        del response["candidates"]["hour_pillar"]  # type: ignore[index]
        self.assertTrue(self.arun(self.console.image_chart("42", "c", response)))
        self.assertNotIn("42", self.console.sessions)
        self.assertEqual([], self.runtime.calls)
        self.assertIn("不完整", self.sent[-1][1])

    def test_non_admin_is_denied_without_creating_image_state(self) -> None:
        self.assertTrue(self.arun(self.console.image_chart("7", "c", _provider_result())))

        self.assertNotIn("7", self.console.sessions)
        self.assertEqual([], self.runtime.calls)
        self.assertIn("暂未开放", self.sent[-1][1])

    def test_image_fallback_does_not_echo_sensitive_provider_content(self) -> None:
        sensitive = '{"success": false, "birth_date": "1990-01-01", "api_key": "not-a-key"}'
        self.assertTrue(self.arun(self.console.image_chart("42", "c", sensitive)))

        self.assertNotIn("1990-01-01", self.sent[-1][1])
        self.assertNotIn("not-a-key", self.sent[-1][1])
        self.assertEqual([], self.runtime.calls)

    def test_existing_console_commands_remain_available(self) -> None:
        for command in ("/new", "/quick", "/analyze", "/history", "/cancel"):
            self.assertTrue(self.arun(self.console.command("42", "c", command)))

        self.assertIn("图片会先识别候选四柱", "\n".join(text for _, text in self.sent))
        self.assertEqual([], self.runtime.calls)

    def test_same_user_in_different_chats_keeps_distinct_image_sessions(self) -> None:
        self.arun(self.console.image_chart("42", "chat-a", _provider_result()))
        self.arun(self.console.image_chart("42", "chat-b", _provider_result()))

        self.assertIn(("chat-a", "42"), self.console.sessions)
        self.assertIn(("chat-b", "42"), self.console.sessions)

    def test_confirmed_image_dispatches_runtime_once_without_generic_intake(self) -> None:
        response = _provider_result()
        response["candidates"]["gender"] = _field("\u5143\u5973")  # type: ignore[index]
        self.arun(self.console.image_chart("42", "c", response))

        self.assertTrue(self.arun(self.console.confirm("42", "c", "确认")))
        self.assertEqual(1, len(self.runtime.calls))
        self.assertIn("image_chart_confirmation", self.runtime.calls[0])
        self.assertTrue(self.arun(self.console.confirm("42", "c", "确认")))
        self.assertEqual(1, len(self.runtime.calls))

    def test_cancel_clears_the_active_image_session(self) -> None:
        self.arun(self.console.image_chart("42", "c", _provider_result()))

        self.assertTrue(self.arun(self.console.text("42", "c", "/cancel")))
        self.assertNotIn(("c", "42"), self.console.sessions)

    def test_restart_restores_confirmation_and_runtime_idempotency(self) -> None:
        database = str(Path(self.tmp.name) / "restart.sqlite3")
        first = MingLiConsole(self.console.send, database, FakeRuntime())
        self.arun(
            first.image_chart(
                "42",
                "c",
                _provider_result(),
                bot_id="bot-a",
                update_id="100",
                message_id="10",
                image_hash="sha256:image-a",
                vision_provider="vision-a",
                vision_request_id="request-a",
            )
        )

        second_runtime = FakeRuntime()
        second = MingLiConsole(self.console.send, database, second_runtime)
        self.assertIn(("c", "42"), second.sessions)
        self.assertTrue(self.arun(second.confirm("42", "c", "确认")))
        self.assertTrue(self.arun(second.confirm("42", "c", "女")))
        self.assertEqual(1, len(second_runtime.calls))

        third_runtime = FakeRuntime()
        third = MingLiConsole(self.console.send, database, third_runtime)
        self.assertTrue(self.arun(third.confirm("42", "c", "确认")))
        self.assertEqual([], third_runtime.calls)

    def test_restart_resumes_confirmed_session_before_runtime_claim(self) -> None:
        database = str(Path(self.tmp.name) / "confirmed-restart.sqlite3")
        first = MingLiConsole(self.console.send, database, FakeRuntime())
        self.arun(first.image_chart("42", "c", _provider_result()))
        pending = first.sessions[("c", "42")]
        pending.data["candidate"]["gender"] = "female"
        first._persist_image_session(pending, state="CONFIRMED")

        runtime = FakeRuntime()
        restarted = MingLiConsole(self.console.send, database, runtime)
        self.assertTrue(self.arun(restarted.confirm("42", "c", "确认")))
        self.assertEqual(1, len(runtime.calls))
        self.assertEqual(
            restarted.sessions[("c", "42")].data["trace_id"],
            runtime.calls[0]["trace_id"],
        )
        self.assertEqual(
            restarted.sessions[("c", "42")].data["runtime_idempotency_key"],
            runtime.calls[0]["idempotency_key"],
        )

    def test_runtime_failure_remains_idempotent_after_restart(self) -> None:
        class FailingRuntime(FakeRuntime):
            def confirmed_pillars(self, payload: dict) -> dict:
                self.calls.append(payload)
                raise RuntimeError("synthetic runtime failure")

        database = str(Path(self.tmp.name) / "failed-runtime.sqlite3")
        failing_runtime = FailingRuntime()
        first = MingLiConsole(self.console.send, database, failing_runtime)
        response = _provider_result()
        response["candidates"]["gender"] = _field("元女")  # type: ignore[index]
        self.arun(first.image_chart("42", "c", response))
        self.assertTrue(self.arun(first.confirm("42", "c", "确认")))
        self.assertEqual(1, len(failing_runtime.calls))

        restarted_runtime = FakeRuntime()
        restarted = MingLiConsole(self.console.send, database, restarted_runtime)
        self.assertTrue(self.arun(restarted.confirm("42", "c", "确认")))
        self.assertEqual([], restarted_runtime.calls)
        audit = restarted.image_store.get_audit(
            restarted.sessions[("c", "42")].data["trace_id"]
        )
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual("FAILED", audit["status"])

    def test_completed_audit_is_not_overwritten_by_cancel_or_replacement(self) -> None:
        response = _provider_result()
        response["candidates"]["gender"] = _field("元女")  # type: ignore[index]

        self.arun(self.console.image_chart("42", "c", response))
        first_trace = self.console.sessions[("c", "42")].data["trace_id"]
        self.arun(self.console.confirm("42", "c", "确认"))
        self.arun(self.console.command("42", "c", "/cancel"))
        first_audit = self.console.image_store.get_audit(first_trace)
        assert first_audit is not None
        self.assertEqual("COMPLETED", first_audit["status"])

        self.arun(self.console.image_chart("42", "c", response))
        second_trace = self.console.sessions[("c", "42")].data["trace_id"]
        self.arun(self.console.confirm("42", "c", "确认"))
        self.arun(self.console.image_chart("42", "c", response))
        second_audit = self.console.image_store.get_audit(second_trace)
        assert second_audit is not None
        self.assertEqual("COMPLETED", second_audit["status"])

    def test_runtime_result_is_sent_when_audit_completion_write_fails(self) -> None:
        database = str(Path(self.tmp.name) / "completion-write-failure.sqlite3")
        runtime = FakeRuntime()
        first = MingLiConsole(self.console.send, database, runtime)
        response = _provider_result()
        response["candidates"]["gender"] = _field("元女")  # type: ignore[index]
        self.arun(first.image_chart("42", "c", response))

        def fail_completion(*args, **kwargs):
            raise sqlite3.OperationalError("synthetic completion failure")

        first.image_store.complete_runtime = fail_completion  # type: ignore[method-assign]
        self.assertTrue(self.arun(first.confirm("42", "c", "确认")))
        self.assertEqual(1, len(runtime.calls))
        self.assertIn("图片命盘结果", self.sent[-1][1])

        restarted_runtime = FakeRuntime()
        restarted = MingLiConsole(self.console.send, database, restarted_runtime)
        self.assertTrue(self.arun(restarted.confirm("42", "c", "确认")))
        self.assertEqual([], restarted_runtime.calls)
