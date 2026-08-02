from __future__ import annotations

"""Fail-closed client for optional, reviewed MingLi reference cards.

The client is deliberately separate from the fixed chart Runtime.  It only
queries the loopback knowledge service after a text answer has been produced,
and every transport or schema failure means no reference context.
"""

import json
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


KNOWLEDGE_SEARCH_URL = "http://127.0.0.1:8010/v1/knowledge/search"
KNOWLEDGE_SEARCH_SCHEMA = "mingli-knowledge-search@1.0"
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_REFERENCES = 3
_REVIEWED_LIFECYCLES = frozenset({"reviewed", "verified"})


class KnowledgeReferenceClient:
    """Read reviewed reference cards without credentials or Runtime coupling."""

    def __init__(
        self,
        *,
        url: str = KNOWLEDGE_SEARCH_URL,
        timeout_seconds: float = 0.5,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self._opener = opener

    @staticmethod
    def _valid_reference(value: object) -> bool:
        if not isinstance(value, dict):
            return False
        return (
            isinstance(value.get("id"), str)
            and bool(value["id"].strip())
            and isinstance(value.get("title"), str)
            and bool(value["title"].strip())
            and isinstance(value.get("text"), str)
            and bool(value["text"].strip())
            and value.get("lifecycle") in _REVIEWED_LIFECYCLES
            and value.get("source_status") == "reviewed"
            and value.get("reference_only") is True
            and value.get("runtime_eligible") is False
            and value.get("prediction_eligible") is False
        )

    @classmethod
    def _validated_references(cls, payload: object) -> list[dict[str, object]]:
        if not isinstance(payload, dict):
            return []
        references = payload.get("references")
        if (
            payload.get("schema_version") != KNOWLEDGE_SEARCH_SCHEMA
            or payload.get("review_mode") is not False
            or payload.get("runtime_input") is not False
            or payload.get("prediction_input") is not False
            or not isinstance(references, list)
            or not reviewed_references_only(references)
        ):
            return []
        return reviewed_references_only(references)

    def search(self, query: str) -> list[dict[str, object]]:
        """Return only a fully conforming reviewed response, otherwise []."""

        body = json.dumps(
            {"query": query, "review_mode": False, "limit": _MAX_REFERENCES},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = Request(
            self.url,
            data=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                if status != 200:
                    return []
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            return []
        if len(raw) > _MAX_RESPONSE_BYTES:
            return []
        try:
            return self._validated_references(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return []


def reviewed_references_only(values: object) -> list[dict[str, object]]:
    """Accept a complete, bounded set of reviewed reference cards only."""

    if (
        not isinstance(values, list)
        or len(values) > _MAX_REFERENCES
        or not all(KnowledgeReferenceClient._valid_reference(value) for value in values)
    ):
        return []
    return list(values)


def render_reviewed_references(references: list[dict[str, object]]) -> str:
    """Render approved cards as presentation-only reference context."""

    if not references:
        return ""
    return "\n\n已审核参考资料：\n" + "\n".join(
        f"- {item['title']}：{item['text']}" for item in references
    )
