from __future__ import annotations

import sqlite3

from mingli_console.image_reliability_store import ImageReliabilityStore


def test_telegram_event_and_image_hash_dedupe_survive_reopen(tmp_path) -> None:
    database = tmp_path / "cases.sqlite3"
    store = ImageReliabilityStore(database)

    assert store.claim_event(
        bot_id="bot-a",
        update_id="100",
        chat_id="chat-a",
        message_id="10",
        user_id="user-a",
        event_type="image",
    )
    assert not store.claim_event(
        bot_id="bot-a",
        update_id="100",
        chat_id="chat-a",
        message_id="11",
        user_id="user-a",
        event_type="image",
    )
    assert not store.claim_event(
        bot_id="bot-a",
        update_id="101",
        chat_id="chat-a",
        message_id="10",
        user_id="user-a",
        event_type="image",
    )
    assert store.claim_image_hash(
        bot_id="bot-a",
        chat_id="chat-a",
        user_id="user-a",
        image_hash="sha256:image-a",
        event_id=1,
    )

    reopened = ImageReliabilityStore(database)
    assert not reopened.claim_image_hash(
        bot_id="bot-a",
        chat_id="chat-a",
        user_id="user-a",
        image_hash="sha256:image-a",
        event_id=2,
    )


def test_session_runtime_claim_and_audit_are_persistent(tmp_path) -> None:
    database = tmp_path / "cases.sqlite3"
    store = ImageReliabilityStore(database)
    record = {
        "session_id": "session-a",
        "trace_id": "trace-a",
        "platform": "telegram",
        "bot_id": "bot-a",
        "chat_id": "chat-a",
        "user_id": "user-a",
        "update_id": "100",
        "image_message_id": "10",
        "image_hash": "sha256:image-a",
        "vision_provider": "vision-a",
        "vision_request_id": "request-a",
        "candidate": {
            "year_pillar": "甲子",
            "month_pillar": "乙丑",
            "day_pillar": "丙寅",
            "hour_pillar": "丁卯",
            "day_master": "丙",
            "gender": "female",
        },
        "state": "AWAITING_CONFIRMATION",
        "runtime_idempotency_key": "runtime-a",
        "candidate_reply_count": 1,
        "runtime_invocation_count": 0,
        "created_at": "2026-07-26T00:00:00+00:00",
        "updated_at": "2026-07-26T00:00:00+00:00",
        "expires_at": 4_000_000_000.0,
    }
    store.save_session(record)

    reopened = ImageReliabilityStore(database)
    restored = reopened.load_sessions(now=1_000_000_000.0)
    assert restored == [record]
    assert reopened.claim_runtime("session-a", "runtime-a")
    assert not reopened.claim_runtime("session-a", "runtime-a")
    reopened.complete_runtime(
        "session-a",
        confirmed_pillars=record["candidate"],
        runtime_result_hash="sha256:result-a",
        status="COMPLETED",
    )

    audit = reopened.get_audit("trace-a")
    assert audit is not None
    for field in (
        "case_id",
        "telegram_user_id",
        "telegram_chat_id",
        "image_hash",
        "vision_provider",
        "vision_request_id",
        "candidate_pillars",
        "confirmed_pillars",
        "runtime_called_at",
        "runtime_result_hash",
        "status",
    ):
        assert audit[field] not in (None, "")

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {
        "telegram_image_events",
        "telegram_image_hashes",
        "image_sessions",
        "image_audits",
    } <= tables
