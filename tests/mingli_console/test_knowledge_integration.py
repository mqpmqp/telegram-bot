from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

from mingli_console.console import MingLiConsole
from mingli_console.knowledge import KnowledgeReferenceClient


def _reference(*, lifecycle: str = "reviewed", source_status: str = "reviewed") -> dict[str, object]:
    return {
        "id": "reviewed-card",
        "title": "已审核卡",
        "text": "仅供参考，不是 Runtime 输入。",
        "lifecycle": lifecycle,
        "source_status": source_status,
        "reference_only": True,
        "runtime_eligible": False,
        "prediction_eligible": False,
    }


def _response(references: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": "mingli-knowledge-search@1.0",
        "review_mode": False,
        "runtime_input": False,
        "prediction_input": False,
        "references": references,
    }


class _Response:
    status = 200

    def __init__(self, payload: object) -> None:
        self.body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int) -> bytes:
        return self.body


class _CapturingOpener:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.request = None
        self.timeout = None

    def __call__(self, request, *, timeout: float):
        self.request = request
        self.timeout = timeout
        return _Response(self.payload)


class _FakeRuntime:
    commit_sha = "test-sha"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def full(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(payload)
        return {"final_answer": "Runtime 原文", "calculation_version": "test"}

    def confirmed_pillars(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(payload)
        return {"final_answer": "图片 Runtime 原文", "calculation_version": "test"}

    def render_intent(
        self, _result: dict[str, object], *, intent: str, question: str
    ) -> dict[str, object]:
        assert intent in {"comment", "focused_question", "follow_up"}
        assert question
        return {"final_answer": "Runtime 原文", "supported": True}


class _FakeKnowledge:
    def __init__(self, references: list[dict[str, object]]) -> None:
        self.references = references
        self.calls: list[str] = []

    def search(self, query: str) -> list[dict[str, object]]:
        self.calls.append(query)
        return self.references


class KnowledgeClientTests(unittest.TestCase):
    def test_default_request_explicitly_disables_review_mode_and_never_sets_authorization(self) -> None:
        opener = _CapturingOpener(_response([_reference()]))
        client = KnowledgeReferenceClient(opener=opener)

        self.assertEqual([_reference()], client.search("事业"))
        self.assertIsNotNone(opener.request)
        self.assertEqual({"query": "事业", "review_mode": False, "limit": 3}, json.loads(opener.request.data))
        headers = {name.lower(): value for name, value in opener.request.header_items()}
        self.assertNotIn("authorization", headers)
        self.assertEqual("application/json", headers["content-type"])

    def test_any_unqualified_card_or_transport_failure_has_empty_reference_context(self) -> None:
        for response in (
            _response([_reference(lifecycle="draft")]),
            _response([_reference(source_status="pending")]),
            {"review_mode": False, "references": []},
        ):
            self.assertEqual([], KnowledgeReferenceClient(opener=_CapturingOpener(response)).search("事业"))

        def forbidden(*_args: object, **_kwargs: object):
            raise HTTPError("http://127.0.0.1:8010/v1/knowledge/search", 403, "forbidden", {}, None)

        self.assertEqual([], KnowledgeReferenceClient(opener=forbidden).search("事业"))


class KnowledgeRoutingContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_admin_ids = os.environ.get("TELEGRAM_ADMIN_IDS")
        os.environ["TELEGRAM_ADMIN_IDS"] = "42"
        self.tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.sent: list[tuple[str, str]] = []

        async def send(chat_id: str, text: str) -> None:
            self.sent.append((chat_id, text))

        self.runtime = _FakeRuntime()
        self.knowledge = _FakeKnowledge([_reference()])
        self.console = MingLiConsole(
            send,
            str(Path(self.tmp.name) / "cases.sqlite3"),
            self.runtime,
            self.knowledge,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()
        if self.old_admin_ids is None:
            os.environ.pop("TELEGRAM_ADMIN_IDS", None)
        else:
            os.environ["TELEGRAM_ADMIN_IDS"] = self.old_admin_ids

    @staticmethod
    def arun(coro):
        return asyncio.run(coro)

    def test_routed_text_answer_appends_only_reviewed_reference_after_runtime(self) -> None:
        self.arun(self.console.command("42", "chat", "/quick"))
        self.arun(self.console.text("42", "chat", "男 公历 1990-01-01 10:30 出生地：福州"))
        self.arun(self.console.text("42", "chat", "事业"))
        self.arun(self.console.text("42", "chat", "300字以内"))

        self.assertEqual(["事业"], self.knowledge.calls)
        self.assertEqual(1, len(self.runtime.calls))
        self.assertNotIn("已审核卡", json.dumps(self.runtime.calls, ensure_ascii=False))
        self.assertIn("已审核参考资料", self.sent[-1][1])
        self.assertIn("已审核卡", self.sent[-1][1])

    def test_empty_references_preserve_original_text_answer(self) -> None:
        self.console.knowledge_client = _FakeKnowledge([])
        self.console.completed["42"] = {
            "chart": {
                "gender": "male", "calendar": "solar", "birth_date": "1990-01-01",
                "birth_time": "10:30", "timezone": "Asia/Shanghai",
                "birth_location": {"city": "福州"}, "true_solar_time": False,
            }
        }
        self.arun(self.console.command("42", "chat", "/analyze"))
        self.arun(self.console.text("42", "chat", "事业"))
        self.arun(self.console.text("42", "chat", "现实背景"))

        self.assertEqual("Runtime 原文仅供文化研究与娱乐参考。", self.sent[-1][1])

    def test_unqualified_injected_result_is_not_rendered(self) -> None:
        self.console.knowledge_client = _FakeKnowledge([_reference(lifecycle="draft")])
        self.arun(self.console.command("42", "chat", "/quick"))
        self.arun(self.console.text("42", "chat", "男 公历 1990-01-01 10:30 出生地：福州"))
        self.arun(self.console.text("42", "chat", "事业"))
        self.arun(self.console.text("42", "chat", "300字以内"))

        self.assertNotIn("已审核卡", self.sent[-1][1])

    def test_image_candidate_confirmation_cancel_and_runtime_handoff_never_search_knowledge(self) -> None:
        provider_result = {
            "success": True,
            "candidates": {
                key: {"value": value, "confidence": "high", "source": "visible", "warning": ""}
                for key, value in {
                    "year_pillar": "甲子", "month_pillar": "乙丑", "day_pillar": "丙寅",
                    "hour_pillar": "丁卯", "day_master": "丙", "gender": "男",
                }.items()
            },
        }
        self.arun(self.console.image_chart("42", "chat", provider_result))
        self.assertEqual([], self.knowledge.calls)
        self.arun(self.console.confirm("42", "chat", "确认"))
        self.assertEqual(1, len(self.runtime.calls))
        self.assertEqual([], self.knowledge.calls)
        self.arun(self.console.command("42", "chat", "/cancel"))
        self.assertEqual([], self.knowledge.calls)
