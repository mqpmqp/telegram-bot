from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mingli_console.console import MingLiConsole
from plugins.platforms.telegram import adapter as telegram_adapter


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


class FakeKnowledge:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def search(self, query: str) -> list[dict]:
        self.calls.append(query)
        return []


class FakeFile:
    _sequence = 0

    def __init__(
        self,
        *,
        fail: Exception | None = None,
        content: bytes | None = None,
    ) -> None:
        type(self)._sequence += 1
        self.fail = fail
        self.content = content or f"synthetic-image-{self._sequence}".encode()
        self.download_paths: list[str] = []

    async def download_to_drive(self, custom_path: str) -> None:
        self.download_paths.append(custom_path)
        if self.fail is not None:
            raise self.fail
        Path(custom_path).write_bytes(self.content)


class FakeMedia:
    def __init__(self, width: int, height: int, file_obj: FakeFile, *, filename: str = "chart.jpg") -> None:
        self.width = width
        self.height = height
        self.file_size = width * height
        self.file_name = filename
        self._file_obj = file_obj
        self.get_file_calls = 0

    async def get_file(self) -> FakeFile:
        self.get_file_calls += 1
        return self._file_obj


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


def _analysis_wrapper(
    analysis: object, *, success: bool = True
) -> dict[str, object]:
    return {"success": success, "analysis": analysis}


class TelegramImageChartHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_admin_ids = os.environ.get("TELEGRAM_ADMIN_IDS")
        self.old_repo = os.environ.get("MINGLI_REPO")
        self.old_timeout = os.environ.get("MINGLI_IMAGE_VISION_TIMEOUT")
        os.environ["TELEGRAM_ADMIN_IDS"] = "42"
        self.assertTrue(self.old_repo, "MINGLI_REPO must point to the isolated MingLi worktree")
        self.tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.sent: list[tuple[str, str]] = []

        async def send(chat_id: str, text: str) -> None:
            self.sent.append((chat_id, text))

        self.runtime = FakeRuntime()
        self.knowledge = FakeKnowledge()
        self.console = MingLiConsole(
            send,
            str(Path(self.tmp.name) / "cases.sqlite3"),
            self.runtime,
            self.knowledge,
        )
        self.adapter = object.__new__(telegram_adapter.TelegramAdapter)
        self.adapter._mingli_console = self.console
        self.adapter._bot = SimpleNamespace(id=999)
        self._next_update_id = 1000
        self._next_message_id = 2000

    def tearDown(self) -> None:
        self.tmp.cleanup()
        for name, value in (("TELEGRAM_ADMIN_IDS", self.old_admin_ids), ("MINGLI_REPO", self.old_repo), ("MINGLI_IMAGE_VISION_TIMEOUT", self.old_timeout)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    @staticmethod
    def arun(coro):
        return asyncio.run(coro)

    def update_for(
        self,
        *,
        user_id: str = "42",
        chat_id: str = "chat",
        photos: list[FakeMedia] | None = None,
        document: FakeMedia | None = None,
        voice: object | None = None,
        text: str | None = None,
        update_id: int | None = None,
        message_id: int | None = None,
    ):
        if update_id is None:
            update_id = self._next_update_id
            self._next_update_id += 1
        if message_id is None:
            message_id = self._next_message_id
            self._next_message_id += 1
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            chat=SimpleNamespace(id=chat_id, type="private"),
            photo=photos or [],
            document=document,
            voice=voice,
            text=text,
            message_id=message_id,
        )
        return SimpleNamespace(
            effective_message=message,
            message=message,
            update_id=update_id,
        )

    def _run_media_with_vision(self, update, vision):
        module = types.ModuleType("tools.vision_tools")
        module.vision_analyze_tool = vision
        with mock.patch.dict(sys.modules, {"tools.vision_tools": module}):
            with self.assertRaises(telegram_adapter.ApplicationHandlerStop):
                self.arun(self.adapter._handle_mingli_media(update, None))

    def _run_text(self, text: str, **update_kwargs) -> None:
        with self.assertRaises(telegram_adapter.ApplicationHandlerStop):
            self.arun(
                self.adapter._handle_mingli_text(
                    self.update_for(text=text, **update_kwargs),
                    None,
                )
            )

    def _run_provider_payload(self, payload: object) -> FakeMedia:
        media = FakeMedia(500, 700, FakeFile())

        async def vision(**kwargs):
            return json.dumps(payload, ensure_ascii=False)

        self._run_media_with_vision(self.update_for(photos=[media]), vision)
        return media

    def test_selects_largest_photo_and_cleans_up_after_success(self) -> None:
        small_file, large_file = FakeFile(), FakeFile()
        small = FakeMedia(100, 100, small_file)
        large = FakeMedia(1600, 900, large_file)
        seen_paths: list[str] = []

        async def vision(**kwargs):
            seen_paths.append(kwargs["image_url"])
            return json.dumps(_provider_result())

        self._run_media_with_vision(self.update_for(photos=[small, large]), vision)

        self.assertEqual(0, small.get_file_calls)
        self.assertEqual(1, large.get_file_calls)
        self.assertEqual(1, len(seen_paths))
        self.assertFalse(Path(seen_paths[0]).exists())
        self.assertEqual("image_chart", self.console.sessions[("chat", "42")].mode)
        self.assertEqual([], self.runtime.calls)

    def test_image_document_uses_the_same_isolated_intake_path(self) -> None:
        file_obj = FakeFile()
        document = FakeMedia(500, 700, file_obj, filename="chart.png")

        async def vision(**kwargs):
            return json.dumps(_provider_result())

        self._run_media_with_vision(self.update_for(document=document), vision)

        self.assertEqual(1, document.get_file_calls)
        self.assertFalse(Path(file_obj.download_paths[0]).exists())
        self.assertEqual([], self.knowledge.calls)
        self.assertEqual([], self.runtime.calls)

    def test_voice_media_never_searches_knowledge(self) -> None:
        with self.assertRaises(telegram_adapter.ApplicationHandlerStop):
            self.arun(
                self.adapter._handle_mingli_media(
                    self.update_for(voice=object()), None
                )
            )
        self.assertEqual([], self.knowledge.calls)
        self.assertEqual([], self.runtime.calls)

    def test_download_failure_returns_safe_fallback_without_provider_call(self) -> None:
        broken = FakeMedia(500, 700, FakeFile(fail=RuntimeError("telegram-token-should-not-log")))
        calls = 0

        async def vision(**kwargs):
            nonlocal calls
            calls += 1
            return json.dumps(_provider_result())

        self._run_media_with_vision(self.update_for(photos=[broken]), vision)

        self.assertEqual(0, calls)
        self.assertIn("图片下载失败", self.sent[-1][1])
        self.assertFalse(Path(broken._file_obj.download_paths[0]).exists())
        self.assertEqual([], self.runtime.calls)

    def test_provider_missing_returns_manual_confirmation_fallback(self) -> None:
        media = FakeMedia(500, 700, FakeFile())

        async def vision(**kwargs):
            return None

        self._run_media_with_vision(self.update_for(photos=[media]), vision)

        self.assertIn("请手动输入四柱或完整出生资料", self.sent[-1][1])
        self.assertNotIn(("chat", "42"), self.console.sessions)
        self.assertFalse(Path(media._file_obj.download_paths[0]).exists())
        self.assertEqual([], self.runtime.calls)

    def test_provider_timeout_is_caught_and_temp_file_is_removed(self) -> None:
        media = FakeMedia(500, 700, FakeFile())
        os.environ["MINGLI_IMAGE_VISION_TIMEOUT"] = "0.001"

        async def vision(**kwargs):
            await asyncio.sleep(1)
            return json.dumps(_provider_result())

        self._run_media_with_vision(self.update_for(photos=[media]), vision)

        self.assertIn("请手动输入四柱或完整出生资料", self.sent[-1][1])
        self.assertFalse(Path(media._file_obj.download_paths[0]).exists())
        self.assertEqual([], self.runtime.calls)

    def test_provider_exception_is_redacted_and_does_not_reach_runtime(self) -> None:
        media = FakeMedia(500, 700, FakeFile())

        async def vision(**kwargs):
            raise RuntimeError(
                "telegram-token-should-not-log api-key-should-not-log "
                "1990-01-01 10:30 Fuzhou c3ludGhldGljLWltYWdl provider-raw-should-not-log"
            )

        with self.assertLogs("plugins.platforms.telegram.adapter", level="WARNING") as captured:
            self._run_media_with_vision(self.update_for(photos=[media]), vision)

        logs = "\n".join(captured.output)
        for sensitive in ("telegram-token-should-not-log", "api-key-should-not-log", "1990-01-01", "10:30", "Fuzhou", "c3ludGhldGljLWltYWdl", "provider-raw-should-not-log"):
            self.assertNotIn(sensitive, logs)
        self.assertIn("RuntimeError", logs)
        self.assertFalse(Path(media._file_obj.download_paths[0]).exists())
        self.assertEqual([], self.runtime.calls)

    def test_untrusted_incomplete_and_invalid_provider_results_are_rejected(self) -> None:
        cases: list[dict[str, object]] = []
        incomplete = _provider_result()
        del incomplete["candidates"]["hour_pillar"]  # type: ignore[index]
        cases.append(incomplete)
        low_confidence = _provider_result()
        low_confidence["candidates"]["year_pillar"]["confidence"] = "low"  # type: ignore[index]
        cases.append(low_confidence)
        warned = _provider_result()
        warned["candidates"]["year_pillar"]["warning"] = "blurred"  # type: ignore[index]
        cases.append(warned)
        inferred = _provider_result()
        inferred["candidates"]["year_pillar"]["source"] = "inferred"  # type: ignore[index]
        cases.append(inferred)
        invalid = _provider_result()
        invalid["candidates"]["year_pillar"]["value"] = "甲丑"  # type: ignore[index]
        cases.append(invalid)

        for response in cases:
            media = FakeMedia(500, 700, FakeFile())

            async def vision(**kwargs):
                return json.dumps(response)

            with self.subTest(response=response):
                self._run_media_with_vision(self.update_for(photos=[media]), vision)
                self.assertNotIn(("chat", "42"), self.console.sessions)
                self.assertFalse(Path(media._file_obj.download_paths[0]).exists())
                self.assertEqual([], self.runtime.calls)

    def test_analysis_json_string_and_object_are_unwrapped_once(self) -> None:
        for analysis in (
            json.dumps(_provider_result(), ensure_ascii=False),
            _provider_result(),
        ):
            with self.subTest(analysis_type=type(analysis).__name__):
                media = self._run_provider_payload(_analysis_wrapper(analysis))
                self.assertEqual("image_chart", self.console.sessions[("chat", "42")].mode)
                self.assertEqual("awaiting_confirmation", self.console.sessions[("chat", "42")].step)
                self.assertIn("年柱：甲子", self.sent[-1][1])
                self.assertIn("日主：丙", self.sent[-1][1])
                self.assertEqual([], self.runtime.calls)
                self.assertFalse(Path(media._file_obj.download_paths[0]).exists())
                self.console.sessions.clear()

    def test_malformed_or_nested_analysis_is_rejected(self) -> None:
        nested = _analysis_wrapper(json.dumps(_provider_result(), ensure_ascii=False))
        for analysis in (
            "not-json",
            "",
            [],
            ["unexpected"],
            7,
            None,
            nested,
        ):
            with self.subTest(analysis=analysis):
                media = self._run_provider_payload(_analysis_wrapper(analysis))
                self.assertNotIn(("chat", "42"), self.console.sessions)
                self.assertIn("不完整", self.sent[-1][1])
                self.assertEqual([], self.runtime.calls)
                self.assertFalse(Path(media._file_obj.download_paths[0]).exists())

    def test_unwrapped_analysis_still_uses_existing_candidate_validation(self) -> None:
        cases: list[dict[str, object]] = []
        incomplete = _provider_result()
        del incomplete["candidates"]["hour_pillar"]  # type: ignore[index]
        cases.append(incomplete)
        low_confidence = _provider_result()
        low_confidence["candidates"]["year_pillar"]["confidence"] = "low"  # type: ignore[index]
        cases.append(low_confidence)
        invisible = _provider_result()
        invisible["candidates"]["year_pillar"]["source"] = "inferred"  # type: ignore[index]
        cases.append(invisible)
        warned = _provider_result()
        warned["candidates"]["year_pillar"]["warning"] = "blurred"  # type: ignore[index]
        cases.append(warned)
        conflicting = _provider_result()
        conflicting["candidates"]["day_master"]["value"] = "丁"  # type: ignore[index]
        cases.append(conflicting)

        for analysis in cases:
            with self.subTest(analysis=analysis):
                media = self._run_provider_payload(
                    _analysis_wrapper(json.dumps(analysis, ensure_ascii=False))
                )
                self.assertNotIn(("chat", "42"), self.console.sessions)
                self.assertEqual([], self.runtime.calls)
                self.assertFalse(Path(media._file_obj.download_paths[0]).exists())

    def test_top_level_candidates_take_precedence_over_analysis(self) -> None:
        payload = _provider_result()
        conflicting_analysis = _provider_result()
        conflicting_analysis["candidates"]["year_pillar"]["value"] = "戊辰"  # type: ignore[index]
        payload["analysis"] = json.dumps(conflicting_analysis, ensure_ascii=False)

        self._run_provider_payload(payload)

        candidate = self.console.sessions[("chat", "42")].data["candidate"]
        self.assertEqual("甲子", candidate["year_pillar"])
        self.assertEqual([], self.runtime.calls)

    def test_wrapped_candidate_confirmation_calls_runtime_once(self) -> None:
        self._run_provider_payload(
            _analysis_wrapper(json.dumps(_provider_result(), ensure_ascii=False))
        )
        self.assertEqual([], self.runtime.calls)

        self._run_text("确认图片候选")
        self.assertEqual([], self.runtime.calls)
        self._run_text("女")
        self.assertEqual(1, len(self.runtime.calls))
        self._run_text("确认")
        self.assertEqual(1, len(self.runtime.calls))

    def test_non_admin_is_denied_before_download_or_provider_invocation(self) -> None:
        media = FakeMedia(500, 700, FakeFile())
        calls = 0

        async def vision(**kwargs):
            nonlocal calls
            calls += 1
            return json.dumps(_provider_result())

        self._run_media_with_vision(self.update_for(user_id="7", photos=[media]), vision)

        self.assertEqual(0, media.get_file_calls)
        self.assertEqual(0, calls)
        self.assertNotIn(("chat", "7"), self.console.sessions)
        self.assertIn("暂未开放", self.sent[-1][1])

    def test_console_messages_are_consumed_but_non_admin_ordinary_text_propagates(self) -> None:
        command_update = self.update_for(text="/new")
        with self.assertRaises(telegram_adapter.ApplicationHandlerStop):
            self.arun(self.adapter._handle_mingli_command(command_update, None))
        self.assertEqual("new", self.console.sessions["42"].mode)

        self.console.sessions.clear()
        sent_before = list(self.sent)
        self.assertIsNone(
            self.arun(
                self.adapter._handle_mingli_text(
                    self.update_for(user_id="7", text="ordinary chat"), None
                )
            )
        )
        self.assertEqual(sent_before, self.sent)

    def test_non_admin_explicit_mingli_text_is_rejected(self) -> None:
        with self.assertRaises(telegram_adapter.ApplicationHandlerStop):
            self.arun(
                self.adapter._handle_mingli_text(
                    self.update_for(user_id="7", text="请帮我看四柱"), None
                )
            )
        self.assertIn("内部工作控制台", self.sent[-1][1])

    def test_admin_birth_text_without_new_is_consumed_before_generic_route(self) -> None:
        text = (
            "请看八字：性别：男，公历，出生日期：1990-01-01，"
            "出生时间：10:30，出生地：福州，时区：Asia/Shanghai，真太阳时：否"
        )

        with self.assertRaises(telegram_adapter.ApplicationHandlerStop):
            self.arun(self.adapter._handle_mingli_text(self.update_for(text=text), None))

        self.assertEqual(1, len(self.runtime.calls))
        self.assertIn("仅供文化研究与娱乐参考。", self.sent[-1][1])

    def test_admin_incomplete_birth_text_without_new_is_consumed_with_no_runtime(self) -> None:
        with self.assertRaises(telegram_adapter.ApplicationHandlerStop):
            self.arun(
                self.adapter._handle_mingli_text(
                    self.update_for(text="请排盘：性别：女，公历，出生日期：1990-01-01"),
                    None,
                )
            )

        self.assertEqual([], self.runtime.calls)
        self.assertIn("出生时间", self.sent[-1][1])

    def test_existing_commands_and_image_confirmation_route_through_early_handlers(self) -> None:
        for command in ("/new", "/quick", "/analyze", "/history", "/cancel"):
            with self.assertRaises(telegram_adapter.ApplicationHandlerStop):
                self.arun(self.adapter._handle_mingli_command(self.update_for(text=command), None))

        media = FakeMedia(500, 700, FakeFile())

        async def vision(**kwargs):
            return json.dumps(_provider_result())

        self._run_media_with_vision(self.update_for(photos=[media]), vision)
        with self.assertRaises(telegram_adapter.ApplicationHandlerStop):
            self.arun(self.adapter._handle_mingli_text(self.update_for(text="确认图片候选"), None))
        with self.assertRaises(telegram_adapter.ApplicationHandlerStop):
            self.arun(self.adapter._handle_mingli_text(self.update_for(text="女"), None))
        self.assertEqual("done", self.console.sessions[("chat", "42")].step)
        self.assertNotIn("42", self.console.sessions)
        self.assertEqual(1, len(self.runtime.calls))

    def test_same_update_id_is_consumed_before_second_download(self) -> None:
        first = FakeMedia(500, 700, FakeFile())
        second = FakeMedia(500, 700, FakeFile())
        vision_calls = 0

        async def vision(**kwargs):
            nonlocal vision_calls
            vision_calls += 1
            return json.dumps(_provider_result())

        self._run_media_with_vision(
            self.update_for(photos=[first], update_id=700, message_id=701),
            vision,
        )
        self._run_media_with_vision(
            self.update_for(photos=[second], update_id=700, message_id=702),
            vision,
        )

        self.assertEqual(1, vision_calls)
        self.assertEqual(1, first.get_file_calls)
        self.assertEqual(0, second.get_file_calls)

    def test_same_message_id_is_consumed_before_second_download(self) -> None:
        first = FakeMedia(500, 700, FakeFile())
        second = FakeMedia(500, 700, FakeFile())
        vision_calls = 0

        async def vision(**kwargs):
            nonlocal vision_calls
            vision_calls += 1
            return json.dumps(_provider_result())

        self._run_media_with_vision(
            self.update_for(photos=[first], update_id=710, message_id=711),
            vision,
        )
        self._run_media_with_vision(
            self.update_for(photos=[second], update_id=712, message_id=711),
            vision,
        )

        self.assertEqual(1, vision_calls)
        self.assertEqual(1, first.get_file_calls)
        self.assertEqual(0, second.get_file_calls)

    def test_same_image_hash_skips_second_vision_and_candidate_reply(self) -> None:
        image = b"same-chart-image"
        first = FakeMedia(500, 700, FakeFile(content=image))
        second = FakeMedia(500, 700, FakeFile(content=image))
        vision_calls = 0

        async def vision(**kwargs):
            nonlocal vision_calls
            vision_calls += 1
            return json.dumps(_provider_result())

        self._run_media_with_vision(
            self.update_for(photos=[first], update_id=720, message_id=721),
            vision,
        )
        self._run_media_with_vision(
            self.update_for(photos=[second], update_id=722, message_id=723),
            vision,
        )

        session = self.console.sessions[("chat", "42")]
        self.assertEqual(1, vision_calls)
        self.assertEqual(1, session.data["candidate_reply_count"])
        self.assertEqual(1, len(self.sent))

    def test_same_image_can_retry_after_provider_failure(self) -> None:
        image = b"retryable-chart-image"
        first = FakeMedia(500, 700, FakeFile(content=image))
        second = FakeMedia(500, 700, FakeFile(content=image))
        vision_calls = 0

        async def failing_vision(**kwargs):
            nonlocal vision_calls
            vision_calls += 1
            raise RuntimeError("synthetic provider failure")

        async def successful_vision(**kwargs):
            nonlocal vision_calls
            vision_calls += 1
            return json.dumps(_provider_result())

        self._run_media_with_vision(
            self.update_for(photos=[first], update_id=724, message_id=725),
            failing_vision,
        )
        self._run_media_with_vision(
            self.update_for(photos=[second], update_id=726, message_id=727),
            successful_vision,
        )

        self.assertEqual(2, vision_calls)
        self.assertIn(("chat", "42"), self.console.sessions)

    def test_duplicate_confirmation_event_is_consumed_once(self) -> None:
        payload = _provider_result()
        payload["candidates"]["gender"] = _field("元女")  # type: ignore[index]
        self._run_provider_payload(payload)

        self._run_text("确认", update_id=730, message_id=731)
        sent_after_first = len(self.sent)
        self._run_text("确认", update_id=730, message_id=731)

        self.assertEqual(1, len(self.runtime.calls))
        self.assertEqual(sent_after_first, len(self.sent))

    def test_successful_chain_persists_adapter_audit_metadata(self) -> None:
        image = b"audited-chart-image"
        media = FakeMedia(500, 700, FakeFile(content=image))
        payload = _provider_result()
        payload["candidates"]["gender"] = _field("元女")  # type: ignore[index]

        async def vision(**kwargs):
            return json.dumps(payload, ensure_ascii=False)

        self._run_media_with_vision(
            self.update_for(photos=[media], update_id=740, message_id=741),
            vision,
        )
        session = self.console.sessions[("chat", "42")]
        self._run_text("确认", update_id=742, message_id=743)

        audit = self.console.image_store.get_audit(session.data["trace_id"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual("42", audit["telegram_user_id"])
        self.assertEqual("chat", audit["telegram_chat_id"])
        self.assertEqual(
            "sha256:" + hashlib.sha256(image).hexdigest(),
            audit["image_hash"],
        )
        self.assertEqual("hermes.vision_analyze_tool", audit["vision_provider"])
        self.assertTrue(audit["vision_request_id"])
        self.assertTrue(audit["candidate_pillars"])
        self.assertTrue(audit["confirmed_pillars"])
        self.assertTrue(audit["runtime_called_at"])
        self.assertTrue(audit["runtime_result_hash"])
        self.assertEqual("COMPLETED", audit["status"])
