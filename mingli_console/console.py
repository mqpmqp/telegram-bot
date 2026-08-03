from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Mapping

from mingli_console.image_reliability_store import ImageReliabilityStore
from mingli_console.knowledge import (
    KnowledgeReferenceClient,
    render_reviewed_references,
    reviewed_references_only,
)

log = logging.getLogger(__name__)
DISCLAIMER = "仅供文化研究与娱乐参考。"
FIXED_MINGLI_SHA = "129ebd09df5c924cc4466e58271938f9b9a19875"
MAX_TELEGRAM_TEXT = 4096
IMAGE_CONFIRM_TTL_SECONDS = 15 * 60
PILLAR_ORDER = ("year", "month", "day", "hour")
SEXAGENARY = frozenset("甲乙丙丁戊己庚辛壬癸"[index % 10] + "子丑寅卯辰巳午未申酉戌亥"[index % 12] for index in range(60))


def chunks(text: str, limit: int = MAX_TELEGRAM_TEXT) -> list[str]:
    """Split without losing order; prefer paragraph/line boundaries."""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < max(1, limit // 2):
            cut = rest.rfind(" ", 0, limit)
        if cut < 1:
            cut = limit
        out.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n ")
    if rest:
        out.append(rest)
    return out


def _admin_ids() -> set[str]:
    return {x.strip() for x in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if x.strip()}


def is_admin(user_id: Any) -> bool:
    return str(user_id or "").strip() in _admin_ids()


@dataclass
class Session:
    mode: str
    step: str = "input"
    data: dict[str, Any] = field(default_factory=dict)
    expires_at: float | None = None


class CaseRepository:
    def __init__(self, path: str | None = None):
        raw = path or os.getenv("MINGLI_CASES_DB", "~/.hermes/mingli/cases.sqlite3")
        self.path = Path(raw).expanduser().resolve()
        if not str(self.path).startswith(str(Path.home().resolve())):
            raise ValueError("MINGLI_CASES_DB must remain under the user home directory")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS cases (
              case_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, display_name TEXT,
              gender TEXT, calendar_type TEXT, birth_datetime TEXT, birth_location TEXT,
              true_solar_time_policy TEXT, topic TEXT, reality_context TEXT,
              normalized_input TEXT, mingli_commit_sha TEXT, runtime_version TEXT,
              result TEXT, confidence TEXT, created_at TEXT, updated_at TEXT, status TEXT
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS case_revisions (
              revision_id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,
              mingli_commit_sha TEXT, runtime_version TEXT, result TEXT NOT NULL,
              confidence TEXT, created_at TEXT NOT NULL
            )""")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save(self, case: dict[str, Any]) -> None:
        fields = ["case_id", "customer_id", "display_name", "gender", "calendar_type", "birth_datetime", "birth_location", "true_solar_time_policy", "topic", "reality_context", "normalized_input", "mingli_commit_sha", "runtime_version", "result", "confidence", "created_at", "updated_at", "status"]
        values = [json.dumps(case.get(k), ensure_ascii=False) if isinstance(case.get(k), (dict, list)) else case.get(k) for k in fields]
        with self._connect() as db:
            db.execute(f"INSERT OR REPLACE INTO cases ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", values)

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT case_id,display_name,topic,confidence,status,updated_at FROM cases ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 50)),)).fetchall()
        return [dict(row) for row in rows]

    def search(self, query: str = "", topic: str = "") -> list[dict[str, Any]]:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            if query:
                rows = db.execute("SELECT * FROM cases WHERE case_id LIKE ? OR display_name LIKE ? ORDER BY updated_at DESC", (f"%{query}%", f"%{query}%")).fetchall()
            elif topic:
                rows = db.execute("SELECT * FROM cases WHERE topic LIKE ? ORDER BY updated_at DESC", (f"%{topic}%",)).fetchall()
            else:
                rows = db.execute("SELECT * FROM cases ORDER BY updated_at DESC LIMIT 10").fetchall()
        return [dict(row) for row in rows]

    def get(self, case_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        return dict(row) if row else None

    def save_revision(self, case_id: str, result: str, sha: str, runtime_version: str | None, confidence: str = "low") -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            cur = db.execute("INSERT INTO case_revisions(case_id,mingli_commit_sha,runtime_version,result,confidence,created_at) VALUES (?,?,?,?,?,?)", (case_id, sha, runtime_version, result, confidence, now))
            return int(cur.lastrowid)

    def revisions(self, case_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM case_revisions WHERE case_id = ? ORDER BY revision_id", (case_id,)).fetchall()
        return [dict(row) for row in rows]
class RuntimeErrorBase(RuntimeError):
    pass


class MingLiRuntimeAdapter:
    """Validated adapter; Hermes owns no calculation logic."""
    def __init__(self, repo: str | None = None, expected_sha: str = FIXED_MINGLI_SHA):
        self.repo = Path(repo or os.getenv("MINGLI_REPO", "/root/mingli-agent")).resolve()
        self.expected_sha = os.getenv("MINGLI_COMMIT_SHA", expected_sha)
        self._ready = False
        self._mingli = None
        self._mingli_confirmed = None
        self._engine = None

    def _prepare(self) -> None:
        if self._ready:
            return
        if not (self.repo / ".git").exists() or not (self.repo / "src").is_dir():
            raise RuntimeErrorBase("mingli-agent checkout unavailable")
        try:
            actual = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeErrorBase("mingli-agent commit could not be verified") from exc
        if actual != self.expected_sha:
            raise RuntimeErrorBase("mingli-agent checkout is not the configured fixed SHA")
        src = str(self.repo / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        try:
            from mingli.bazi import DeterministicBaziEngine
            from mingli.confirmed_pillar_runtime import run_confirmed_pillar_agent
            from mingli.phase23 import run_mingli_agent
        except Exception as exc:
            raise RuntimeErrorBase("mingli-agent public runtime import failed") from exc
        self._engine, self._mingli = DeterministicBaziEngine, run_mingli_agent
        self._mingli_confirmed = run_confirmed_pillar_agent
        self._ready = True

    def _validate(self, payload: dict[str, Any], full: bool = True) -> None:
        chart = payload.get("chart_input")
        if not isinstance(chart, dict):
            raise ValueError("chart_input is required")
        required = ("gender", "calendar", "birth_date", "birth_time", "timezone", "birth_location", "true_solar_time")
        missing = [key for key in required if key not in chart or chart[key] in (None, "")]
        if missing:
            raise ValueError("missing chart_input: " + ",".join(missing))
        if chart["gender"] not in {"male", "female"} or chart["calendar"] not in {"solar", "lunar"}:
            raise ValueError("invalid gender/calendar")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(chart["birth_date"])) or not re.fullmatch(r"\d{2}:\d{2}", str(chart["birth_time"])):
            raise ValueError("birth date/time format invalid")
        if chart["calendar"] == "lunar" and "is_leap_month" not in chart:
            raise ValueError("lunar leap-month flag is incomplete")
        loc = chart["birth_location"]
        if not isinstance(loc, dict) or (chart["true_solar_time"] and not {"longitude", "latitude"} <= set(loc)):
            raise ValueError("birth location is incomplete")
        if full and not isinstance(payload.get("anchor_year"), int):
            raise ValueError("anchor_year is required")

    def chart(self, chart_input: dict[str, Any]) -> dict[str, Any]:
        self._prepare()
        payload = {"chart_input": chart_input, "anchor_year": datetime.now().year}
        self._validate(payload, full=False)
        return dict(self._engine().calculate(chart_input))

    def full(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._prepare()
        if "image_chart_confirmation" in payload:
            raise ValueError("confirmed image charts must use confirmed_pillars")
        self._validate(payload)
        result = self._mingli(payload).to_dict()
        if not isinstance(result.get("final_answer"), str) or not result["final_answer"].strip():
            raise RuntimeErrorBase("runtime output schema invalid")
        return result

    def confirmed_pillars(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._prepare()
        handoff = payload.get("image_chart_confirmation")
        if not isinstance(handoff, Mapping) or handoff.get("contract") != "mingli-image-chart-confirmation@1.2":
            raise ValueError("image chart confirmation contract invalid")
        candidate = handoff.get("chart_candidate")
        if not isinstance(candidate, Mapping) or handoff.get("confirmation_status") != "confirmed" or handoff.get("runtime_dispatch") != "confirmed_pillars":
            raise ValueError("image chart confirmation is incomplete")
        pillars = candidate.get("pillars")
        if not isinstance(pillars, Mapping) or tuple(pillars) != PILLAR_ORDER:
            raise ValueError("confirmed image chart pillar order invalid")
        normalized = {name: str(pillars[name]) for name in PILLAR_ORDER}
        if any(value not in SEXAGENARY for value in normalized.values()):
            raise ValueError("confirmed image chart contains invalid pillars")
        if candidate.get("day_master") != normalized["day"][0] or candidate.get("gender") not in {"male", "female"}:
            raise ValueError("confirmed image chart metadata invalid")
        if any(candidate.get(name) is not None for name in ("birth_datetime", "birth_place", "calendar_type")):
            raise ValueError("image chart confirmation contains inferred birth data")
        result = self._mingli_confirmed({"pillars": normalized, "day_master": normalized["day"][0], "gender": candidate["gender"], "source": "image_confirmed", "confirmation_status": "confirmed"}).to_dict()
        chart = result.get("chart") if isinstance(result, Mapping) else None
        if not isinstance(result.get("final_answer"), str) or not result["final_answer"].strip() or not isinstance(chart, Mapping):
            raise RuntimeErrorBase("runtime output schema invalid")
        if chart.get("pillars") != normalized or chart.get("day_master") != normalized["day"][0] or chart.get("gender") != candidate["gender"]:
            raise ValueError("confirmed image chart does not match Runtime output")
        return dict(result)

    @property
    def commit_sha(self) -> str:
        return self.expected_sha


class MingLiConsole:
    """Telegram-facing state machine. Transport is injected for testability."""
    def __init__(
        self,
        send: Callable[[str, str], Awaitable[None]],
        db_path: str | None = None,
        runtime: MingLiRuntimeAdapter | None = None,
        knowledge_client: KnowledgeReferenceClient | None = None,
    ):
        self.send = send
        self.sessions: dict[object, Session] = {}
        self.completed: dict[str, dict[str, Any]] = {}
        self.repo = CaseRepository(db_path)
        self.image_store = ImageReliabilityStore(self.repo.path)
        self.runtime = runtime or MingLiRuntimeAdapter()
        self.knowledge_client = knowledge_client or KnowledgeReferenceClient()
        self._restore_image_sessions()

    @staticmethod
    def _step_for_state(state: str) -> str:
        return {
            "AWAITING_CONFIRMATION": "awaiting_confirmation",
            "AWAITING_GENDER": "awaiting_gender",
            "CONFIRMED": "runtime_ready",
            "RUNTIME_PENDING": "runtime_ready",
            "COMPLETED": "done",
        }.get(state, "done")

    @staticmethod
    def _is_image_confirmation_reply(value: str) -> bool:
        return value.strip().casefold() in {
            "确认",
            "確認",
            "confirm",
            "confirmed",
            "确认图片候选",
            "确认并分析",
            "男",
            "男命",
            "male",
            "女",
            "女命",
            "female",
        }

    @staticmethod
    def _image_confirmation_handoff(data: Mapping[str, Any]) -> dict[str, Any]:
        candidate = data["candidate"]
        return {
            "contract": "mingli-image-chart-confirmation@1.2",
            "confirmation_status": "confirmed",
            "runtime_dispatch": "confirmed_pillars",
            "trace_id": data["trace_id"],
            "idempotency_key": data["runtime_idempotency_key"],
            "chart_candidate": {
                "pillars": {
                    "year": candidate["year_pillar"],
                    "month": candidate["month_pillar"],
                    "day": candidate["day_pillar"],
                    "hour": candidate["hour_pillar"],
                },
                "day_master": candidate["day_master"],
                "gender": candidate["gender"],
                "requires_confirmation": False,
                "confidence": "high",
                "warnings": [],
                "birth_datetime": None,
                "birth_place": None,
                "calendar_type": None,
            },
        }

    def _restore_image_sessions(self) -> None:
        for record in self.image_store.load_sessions(now=time.time()):
            data = dict(record)
            candidate = data.pop("candidate")
            data["candidate"] = candidate
            data["runtime_dispatch_attempted"] = (
                int(data.get("runtime_invocation_count", 0)) > 0
            )
            if (
                data.get("state") == "CONFIRMED"
                and not data["runtime_dispatch_attempted"]
            ):
                data["image_chart_confirmation"] = self._image_confirmation_handoff(
                    data
                )
            self.sessions[
                self._image_session_key(record["user_id"], record["chat_id"])
            ] = Session(
                "image_chart",
                step=self._step_for_state(str(record["state"])),
                data=data,
                expires_at=float(record["expires_at"]),
            )

    def _persist_image_session(self, session: Session, *, state: str) -> None:
        session.data["state"] = state
        session.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        session.data["expires_at"] = float(session.expires_at or time.time())
        self.image_store.save_session(session.data)

    def _finish_image_session(self, session: Session, *, state: str) -> None:
        session.data["state"] = state
        session.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.image_store.delete_session(str(session.data["session_id"]), status=state)

    def has_image_session(self, user_id: str, chat_id: str) -> bool:
        return self._image_session(user_id, chat_id) is not None

    def claim_telegram_event(
        self,
        *,
        bot_id: str,
        update_id: str | None,
        chat_id: str,
        message_id: str | None,
        user_id: str,
        event_type: str,
    ) -> int | None:
        return self.image_store.claim_event(
            bot_id=bot_id,
            update_id=update_id,
            chat_id=chat_id,
            message_id=message_id,
            user_id=user_id,
            event_type=event_type,
        )

    def complete_telegram_event(self, event_id: int, status: str) -> None:
        self.image_store.complete_event(event_id, status)

    def claim_image_hash(
        self,
        *,
        bot_id: str,
        chat_id: str,
        user_id: str,
        image_hash: str,
        event_id: int | None,
    ) -> bool:
        return self.image_store.claim_image_hash(
            bot_id=bot_id,
            chat_id=chat_id,
            user_id=user_id,
            image_hash=image_hash,
            event_id=event_id,
        )

    def release_image_hash(
        self,
        *,
        bot_id: str,
        chat_id: str,
        user_id: str,
        image_hash: str,
        event_id: int,
    ) -> bool:
        return self.image_store.release_image_hash(
            bot_id=bot_id,
            chat_id=chat_id,
            user_id=user_id,
            image_hash=image_hash,
            event_id=event_id,
        )

    async def _reply(self, chat_id: str, text: str) -> None:
        for part in chunks(text):
            await self.send(chat_id, part)

    async def _deny(self, chat_id: str) -> bool:
        await self._reply(chat_id, "当前机器人为内部工作控制台，暂未开放使用。")
        return True

    @staticmethod
    def _is_explicit_mingli_text(text: str) -> bool:
        """Return whether free text is explicitly addressed to the MingLi console.

        The Telegram text handler runs before Hermes's general text route.  It
        must therefore leave ordinary non-admin conversation unhandled while
        still refusing an unmistakable attempt to use the internal MingLi
        console.
        """
        normalized = text.strip()
        if normalized in {
            "新客户完整测算",
            "评论区快速回复",
            "专项问题分析",
            "历史案例",
            "取消当前任务",
        }:
            return True
        if any(
            keyword in normalized
            for keyword in (
                "八字",
                "四柱",
                "命盘",
                "排盘",
                "命理",
                "出生资料",
                "出生日期",
                "出生时间",
                "生辰",
                "日主",
            )
        ):
            return True
        return bool(
            re.search(r"\b\d{4}-\d{1,2}-\d{1,2}\b", normalized)
            and re.search(r"(?<!\d)\d{1,2}:\d{2}(?!\d)", normalized)
        )

    @staticmethod
    def _text_intake_updates(text: str) -> tuple[dict[str, Any], tuple[str, ...]]:
        """Parse only explicit, user-provided birth fields; never infer them."""

        chart: dict[str, Any] = {}
        invalid: list[str] = []
        lower = text.casefold()

        gender = re.search(r"性别\s*[:：]?\s*(男|女|male|female)\b", text, re.I)
        if gender is None:
            gender = re.search(
                r"(?:^|[：:\s，,；;])\s*(男|女|male|female)(?=$|[\s，,；;])",
                text,
                re.I,
            )
        if gender:
            chart["gender"] = "female" if gender.group(1).casefold() in {"女", "female"} else "male"

        if "农历" in text:
            chart["calendar"] = "lunar"
        elif "公历" in text or "阳历" in text:
            chart["calendar"] = "solar"

        leap = re.search(r"(?:是否)?闰月\s*[:：]?\s*(是|否|true|false|闰|非闰)", text, re.I)
        if leap:
            chart["is_leap_month"] = leap.group(1).casefold() in {"是", "true", "闰"}

        date_match = re.search(
            r"(?:出生日期|生日|日期)\s*[:：]?\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
            text,
        ) or re.search(r"\b(\d{4}-\d{1,2}-\d{1,2})\b", text)
        if date_match:
            raw_date = date_match.group(1).replace("/", "-")
            try:
                chart["birth_date"] = datetime.strptime(raw_date, "%Y-%m-%d").date().isoformat()
            except ValueError:
                invalid.append("出生日期")

        time_match = re.search(r"(?:出生时间|时间)\s*[:：]?\s*(\d{1,2}:\d{2})", text) or re.search(
            r"(?<!\d)(\d{1,2}:\d{2})(?!\d)", text
        )
        if time_match:
            raw_time = time_match.group(1)
            try:
                chart["birth_time"] = datetime.strptime(raw_time, "%H:%M").strftime("%H:%M")
            except ValueError:
                invalid.append("出生时间")

        location = re.search(r"(?:出生地|出生地点|地点)\s*[:：]?\s*([^，,；;\n]+)", text)
        if location:
            value = location.group(1).strip()
            if value:
                chart["birth_location"] = value
            else:
                invalid.append("出生地")

        timezone_match = re.search(r"(?:时区|timezone)\s*[:：]?\s*([A-Za-z_]+/[A-Za-z_+\-]+)", text, re.I)
        if timezone_match:
            chart["timezone"] = timezone_match.group(1)
        elif "时区" in text or "timezone" in lower:
            invalid.append("时区")

        solar = re.search(r"真太阳时\s*[:：]?\s*(是|否|true|false)", text, re.I)
        if solar:
            chart["true_solar_time"] = solar.group(1).casefold() in {"是", "true"}

        return chart, tuple(dict.fromkeys(invalid))

    @staticmethod
    def _text_intake_missing(chart: Mapping[str, Any]) -> list[str]:
        labels = {
            "gender": "性别",
            "calendar": "公历/农历",
            "birth_date": "出生日期",
            "birth_time": "出生时间",
            "birth_location": "出生地",
            "timezone": "时区",
            "true_solar_time": "是否采用真太阳时",
        }
        missing = [label for key, label in labels.items() if chart.get(key) in (None, "")]
        if chart.get("calendar") == "lunar" and chart.get("is_leap_month") is None:
            missing.append("农历是否闰月")
        if chart.get("true_solar_time") is True:
            location = str(chart.get("birth_location", ""))
            parts = [part.strip() for part in location.split(",")]
            try:
                valid_coordinates = len(parts) >= 3 and all(
                    isinstance(float(value), float) for value in parts[:2]
                )
            except ValueError:
                valid_coordinates = False
            if not valid_coordinates:
                missing.append("出生地经纬度")
        return missing

    @staticmethod
    def _text_intake_location(chart: Mapping[str, Any]) -> dict[str, Any]:
        location = str(chart["birth_location"]).strip()
        if not chart["true_solar_time"]:
            return {"city": location}
        longitude, latitude, *city = [part.strip() for part in location.split(",")]
        return {
            "longitude": float(longitude),
            "latitude": float(latitude),
            "city": ",".join(city),
        }

    async def _complete_text_intake(
        self, user_id: str, chat_id: str, session: Session, text: str
    ) -> bool:
        updates, invalid = self._text_intake_updates(text)
        chart = session.data.setdefault("chart", {})
        chart.update(updates)
        if chart.get("calendar") == "solar":
            chart["is_leap_month"] = False

        if invalid:
            await self._reply(
                chat_id,
                "资料校验失败：" + "、".join(invalid) + "无效，请仅更正这些字段。",
            )
            return True

        missing = self._text_intake_missing(chart)
        if missing:
            await self._reply(
                chat_id,
                "资料尚不完整，缺少：" + "、".join(missing) + "。请只补充缺少字段。",
            )
            return True

        normalized_chart = {
            "gender": chart["gender"],
            "calendar": chart["calendar"],
            "is_leap_month": bool(chart["is_leap_month"]),
            "birth_date": chart["birth_date"],
            "birth_time": chart["birth_time"],
            "timezone": chart["timezone"],
            "birth_location": self._text_intake_location(chart),
            "true_solar_time": bool(chart["true_solar_time"]),
        }
        payload = self._payload_from_chart(normalized_chart, "")
        result = await self._run(payload)
        if result is None:
            await self._reply(chat_id, "MingLi Runtime 当前不可用或资料不符合接口要求；请检查出生资料后重试。")
            return True

        final = str(result["final_answer"])
        if not final.endswith(DISCLAIMER):
            final += "\n" + DISCLAIMER
        await self._reply(chat_id, final)
        now = datetime.now(timezone.utc).isoformat()
        case_id = "case-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        case = {
            "case_id": case_id,
            "customer_id": str(user_id),
            "display_name": str(user_id),
            "gender": normalized_chart["gender"],
            "calendar_type": normalized_chart["calendar"],
            "birth_datetime": normalized_chart["birth_date"] + " " + normalized_chart["birth_time"],
            "birth_location": normalized_chart["birth_location"],
            "true_solar_time_policy": normalized_chart["true_solar_time"],
            "topic": "综合",
            "reality_context": "",
            "normalized_input": payload,
            "mingli_commit_sha": self.runtime.commit_sha,
            "runtime_version": result.get("calculation_version"),
            "result": final,
            "confidence": "low",
            "created_at": now,
            "updated_at": now,
            "status": "completed",
        }
        try:
            self.repo.save(case)
        except Exception:
            log.warning("case save failed: %s", type(sys.exc_info()[1]).__name__)
        self.completed[str(user_id)] = {
            "chart": normalized_chart,
            "case_id": case_id,
            "topic": "综合",
            "reality_context": "",
        }
        session.data["active_case_id"] = case_id
        session.step = "done"
        return True

    @staticmethod
    def _menu() -> str:
        return "🔮 MingLi 命理师控制台\n\n请选择：\n\n【新客户完整测算】 /new\n【评论区快速回复】 /quick\n【专项问题分析】 /analyze\n【历史案例】 /history\n【取消当前任务】 /cancel\n\n/help 查看帮助"

    @staticmethod
    def _image_intake_dependencies():
        """Import the deterministic parser from the configured MingLi checkout."""
        repo = Path(os.getenv("MINGLI_REPO", "/root/mingli-agent")).expanduser().resolve()
        source = repo / "src"
        if source.is_dir() and str(source) not in sys.path:
            sys.path.insert(0, str(source))
        from mingli.intake.image_chart import (
            HEAVENLY_STEMS,
            VALID_GANZHI,
            ImageChartIntakeRequest,
            intake_image_chart,
        )

        return HEAVENLY_STEMS, VALID_GANZHI, ImageChartIntakeRequest, intake_image_chart

    @staticmethod
    def _image_candidate_prompt() -> str:
        return "请回复“确认”直接分析；如需更正，请发送：年柱=甲子、月柱=乙丑、日柱=丙寅、时柱=丁卯、日主=丙 或 性别=男。更正后必须再次确认。"

    @staticmethod
    def _image_session_key(user_id: str, chat_id: str) -> tuple[str, str]:
        return (str(chat_id), str(user_id))

    def _image_session(self, user_id: str, chat_id: str) -> Session | None:
        session = self.sessions.get(self._image_session_key(user_id, chat_id))
        return session if isinstance(session, Session) and session.mode == "image_chart" else None

    async def image_chart(
        self,
        user_id: str,
        chat_id: str,
        provider_result: object | None,
        *,
        bot_id: str = "",
        update_id: str | None = None,
        message_id: str | None = None,
        image_hash: str | None = None,
        vision_provider: str | None = None,
        vision_request_id: str | None = None,
    ) -> bool:
        """Store a validated image candidate only until explicit user confirmation."""
        if not is_admin(user_id):
            return await self._deny(chat_id)
        try:
            _, _, request_type, intake = self._image_intake_dependencies()
            result = intake(request_type(source="telegram", provider_result=provider_result))
        except Exception as exc:
            log.warning("MingLi image intake unavailable: %s", type(exc).__name__)
            await self._reply(chat_id, "图片命盘识别暂不可用。请手动输入四柱或完整出生资料；如果是从图片读出的四柱，请先确认：请确认我读的四柱和日主是否正确？")
            return True
        if not result.accepted:
            await self._reply(chat_id, result.user_message)
            return True

        candidate = result.candidate
        assert candidate is not None
        values = {**candidate.pillars, "day_master": candidate.day_master, "gender": candidate.gender}
        now = datetime.now(timezone.utc).isoformat()
        session_id = uuid.uuid4().hex
        trace_id = uuid.uuid4().hex
        session_key = self._image_session_key(user_id, chat_id)
        replaced = self.sessions.pop(session_key, None)
        if isinstance(replaced, Session) and replaced.mode == "image_chart":
            self._finish_image_session(replaced, state="REPLACED")
        session = Session(
            "image_chart",
            step="awaiting_confirmation",
            data={
                "session_id": session_id,
                "trace_id": trace_id,
                "platform": "telegram",
                "bot_id": str(bot_id),
                "chat_id": str(chat_id),
                "user_id": str(user_id),
                "update_id": str(update_id) if update_id not in (None, "") else None,
                "image_message_id": (
                    str(message_id) if message_id not in (None, "") else None
                ),
                "image_hash": image_hash,
                "vision_provider": vision_provider,
                "vision_request_id": vision_request_id,
                "candidate": values,
                "runtime_idempotency_key": f"image-runtime:{trace_id}",
                "candidate_reply_count": 1,
                "runtime_invocation_count": 0,
                "created_at": now,
                "updated_at": now,
            },
            expires_at=time.time() + IMAGE_CONFIRM_TTL_SECONDS,
        )
        self.sessions[session_key] = session
        self._persist_image_session(session, state="AWAITING_CONFIRMATION")
        await self._reply(
            chat_id,
            "【图片候选四柱】\n"
            + "\n".join(candidate.display_lines())
            + "\n\n"
            + self._image_candidate_prompt(),
        )
        return True

    async def image_chart_failure(self, user_id: str, chat_id: str, status: str) -> bool:
        """Return a privacy-safe fallback for a transport or provider failure."""
        if not is_admin(user_id):
            return await self._deny(chat_id)
        if status == "image_download_failed":
            await self._reply(chat_id, "图片下载失败，请重新发送图片；也可以手动输入四柱或完整出生资料，并先确认四柱和日主。")
        else:
            await self._reply(chat_id, "图片命盘识别暂不可用。请手动输入四柱或完整出生资料；如果是从图片读出的四柱，请先确认：请确认我读的四柱和日主是否正确？")
        return True

    def _record_image_runtime_completion(
        self,
        session: Session,
        *,
        confirmed_pillars: object,
        runtime_result_hash: str | None,
        status: str,
    ) -> bool:
        try:
            self.image_store.complete_runtime(
                str(session.data["session_id"]),
                confirmed_pillars=confirmed_pillars,
                runtime_result_hash=runtime_result_hash,
                status=status,
            )
            return True
        except sqlite3.Error as exc:
            session.data["audit_persistence_failed"] = True
            log.warning(
                "MingLi image Runtime completion audit failed for trace=%s: %s",
                session.data.get("trace_id"),
                type(exc).__name__,
            )
            return False

    async def _dispatch_confirmed_image(self, user_id: str, chat_id: str, session: Session) -> bool:
        if session.data.get("runtime_dispatch_attempted"):
            await self._reply(chat_id, "runtime_already_dispatched: 本次图片命盘已处理，不会重复调用 Runtime。")
            return True
        session_id = str(session.data["session_id"])
        idempotency_key = str(session.data["runtime_idempotency_key"])
        try:
            claimed = self.image_store.claim_runtime(session_id, idempotency_key)
        except sqlite3.Error as exc:
            log.warning(
                "MingLi image Runtime claim failed for admin=%s: %s",
                user_id,
                type(exc).__name__,
            )
            await self._reply(
                chat_id,
                "MingLi Runtime 暂不可用；幂等状态未能安全保存，本次没有调用 Runtime，请稍后重试。",
            )
            return True
        if not claimed:
            session.data["runtime_dispatch_attempted"] = True
            session.data["runtime_invocation_count"] = 1
            session.step = "done"
            await self._reply(chat_id, "runtime_already_dispatched: 本次图片命盘已处理，不会重复调用 Runtime。")
            return True

        session.data["runtime_dispatch_attempted"] = True
        session.data["runtime_invocation_count"] = 1
        session.expires_at = time.time() + IMAGE_CONFIRM_TTL_SECONDS
        self._persist_image_session(session, state="RUNTIME_PENDING")
        candidate = session.data["candidate"]
        runtime_payload = {
            "image_chart_confirmation": session.data["image_chart_confirmation"],
            "trace_id": session.data["trace_id"],
            "idempotency_key": idempotency_key,
            "source": "image_confirmed",
        }
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self.runtime.confirmed_pillars, runtime_payload),
                timeout=float(os.getenv("MINGLI_RUNTIME_TIMEOUT", "30")),
            )
        except asyncio.TimeoutError:
            session.step = "done"
            self._record_image_runtime_completion(
                session,
                confirmed_pillars=candidate,
                runtime_result_hash=None,
                status="FAILED",
            )
            session.data["state"] = "FAILED"
            await self._reply(chat_id, "MingLi Runtime 超时；为避免重复计算，本次不会自动重试，请重新上传图片。")
            return True
        except Exception as exc:
            session.step = "done"
            self._record_image_runtime_completion(
                session,
                confirmed_pillars=candidate,
                runtime_result_hash=None,
                status="FAILED",
            )
            session.data["state"] = "FAILED"
            log.warning("MingLi confirmed-pillar runtime failed for admin=%s: %s", user_id, type(exc).__name__)
            await self._reply(chat_id, "MingLi Runtime 当前不可用或确认内容不符合接口要求；为避免重复计算，请重新上传图片后再试。")
            return True
        final = str(result.get("final_answer", "")).strip()
        if not final:
            session.step = "done"
            self._record_image_runtime_completion(
                session,
                confirmed_pillars=candidate,
                runtime_result_hash=None,
                status="FAILED",
            )
            session.data["state"] = "FAILED"
            await self._reply(chat_id, "MingLi Runtime 返回结果无效；本次未生成分析，请重新上传图片。")
            return True
        result_json = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        result_hash = "sha256:" + hashlib.sha256(result_json.encode("utf-8")).hexdigest()
        self._record_image_runtime_completion(
            session,
            confirmed_pillars=candidate,
            runtime_result_hash=result_hash,
            status="COMPLETED",
        )
        session.data["runtime_result_hash"] = result_hash
        session.data["state"] = "COMPLETED"
        await self._reply(chat_id, final if final.endswith(DISCLAIMER) else final + "\n" + DISCLAIMER)
        session.step = "done"
        return True

    async def _confirm_image_candidate(self, user_id: str, chat_id: str, session: Session, text: str) -> bool:
        value = text.strip()
        normalized = value.casefold()
        if session.expires_at is not None and time.time() >= session.expires_at:
            self._finish_image_session(session, state="EXPIRED")
            self.sessions.pop(self._image_session_key(user_id, chat_id), None)
            await self._reply(chat_id, "confirmation_expired: 图片候选已超时，请重新上传。")
            return True
        if normalized in {"否", "不确认", "不對", "不对", "取消", "no", "reject", "cancel"}:
            self._finish_image_session(session, state="REJECTED")
            self.sessions.pop(self._image_session_key(user_id, chat_id), None)
            await self._reply(chat_id, "confirmation_rejected: 已丢弃图片候选，请重新上传。")
            return True
        candidate = session.data.get("candidate")
        if not isinstance(candidate, dict):
            self._finish_image_session(session, state="FAILED")
            self.sessions.pop(self._image_session_key(user_id, chat_id), None)
            await self._reply(chat_id, "confirmation_state_missing: 图片候选已失效，请重新上传。")
            return True
        if session.step == "awaiting_gender":
            gender = {"男": "male", "男命": "male", "male": "male", "女": "female", "女命": "female", "female": "female"}.get(normalized)
            if gender is None:
                await self._reply(chat_id, "gender_required: 四柱已经确认，现在只需回复“男”或“女”。")
                return True
            candidate["gender"] = gender
        else:
            match = re.fullmatch(r"(?:修改\s*)?(年柱|月柱|日柱|时柱|日主|性别)\s*[=:：]\s*([^\s，,；;]+)", value)
            if match:
                field = {"年柱": "year_pillar", "月柱": "month_pillar", "日柱": "day_pillar", "时柱": "hour_pillar", "日主": "day_master", "性别": "gender"}[match.group(1)]
                corrected = match.group(2)
                genders = {"男": "male", "男命": "male", "male": "male", "女": "female", "女命": "female", "female": "female"}
                if field == "gender":
                    corrected = genders.get(corrected.casefold())
                    valid = corrected is not None
                else:
                    stems, valid_ganzhi, _, _ = self._image_intake_dependencies()
                    valid = corrected in stems if field == "day_master" else corrected in valid_ganzhi
                if not valid:
                    await self._reply(chat_id, "validation_failed: 更正值非法，图片候选尚未确认。")
                    return True
                candidate[field] = corrected
                session.expires_at = time.time() + IMAGE_CONFIRM_TTL_SECONDS
                self._persist_image_session(session, state="AWAITING_CONFIRMATION")
                await self._reply(chat_id, "corrected_awaiting_confirmation: 图片候选已更新，尚未确认。\n" + self._image_candidate_prompt())
                return True
            if normalized not in {"确认", "確認", "confirm", "confirmed", "确认图片候选"}:
                await self._reply(chat_id, "图片候选尚未确认。" + self._image_candidate_prompt())
                return True
            if candidate.get("day_master") != str(candidate.get("day_pillar", ""))[:1]:
                await self._reply(chat_id, "validation_failed: 日柱与日主不一致，本次不会进入测算。")
                return True
        pillars = {"year": candidate.get("year_pillar"), "month": candidate.get("month_pillar"), "day": candidate.get("day_pillar"), "hour": candidate.get("hour_pillar")}
        if any(not isinstance(value, str) or value not in SEXAGENARY for value in pillars.values()):
            await self._reply(chat_id, "validation_failed: 图片候选包含非法干支，本次不会进入测算。")
            return True
        if candidate.get("gender") not in {"male", "female"}:
            session.step = "awaiting_gender"
            session.expires_at = time.time() + IMAGE_CONFIRM_TTL_SECONDS
            self._persist_image_session(session, state="AWAITING_GENDER")
            await self._reply(chat_id, "gender_required: 四柱与日主已确认。图片未可靠识别性别，现在只需回复男或女；无需补充其他出生资料。")
            return True
        session.data["image_chart_confirmation"] = self._image_confirmation_handoff(
            session.data
        )
        session.step = "runtime_ready"
        session.expires_at = time.time() + IMAGE_CONFIRM_TTL_SECONDS
        self._persist_image_session(session, state="CONFIRMED")
        return await self._dispatch_confirmed_image(user_id, chat_id, session)

    async def whoami(self, user_id: str, chat_id: str, chat_type: str) -> bool:
        await self._reply(chat_id, f"Telegram user ID：{user_id}\nchat type：{chat_type}")
        return True

    async def command(self, user_id: str, chat_id: str, text: str) -> bool:
        if not is_admin(user_id):
            return await self._deny(chat_id)
        name = text.split()[0].split("@", 1)[0].lower()
        if name in {"/start", "/help"}:
            await self._reply(chat_id, self._menu() if name == "/start" else "命令：/new 完整测算｜/quick 评论回复｜/analyze 专项分析｜/history 案例｜/cancel 取消")
        elif name == "/cancel":
            image_session = self._image_session(user_id, chat_id)
            if image_session is not None:
                self._finish_image_session(image_session, state="CANCELLED")
            self.sessions.pop(str(user_id), None)
            self.sessions.pop(self._image_session_key(user_id, chat_id), None)
            await self._reply(chat_id, "已取消当前任务。")
        elif name == "/new":
            self.sessions[str(user_id)] = Session("new", data={"fields": []}); await self._reply(chat_id, "新客户完整测算。请依次发送：称呼/案例代号、性别（男/女）、历法（公历/农历）、农历是否闰月（是/否；公历填否）、出生日期（YYYY-MM-DD）、出生时间（HH:MM；未知请明确写未知）、出生地（城市；真太阳时需经纬度）、是否真太阳时（是/否）、主要问题、现实背景、输出模式（简洁版/标准版/详细版）。每次一项。")
        elif name == "/quick":
            self.sessions[str(user_id)] = Session("quick", step="message"); await self._reply(chat_id, "请粘贴客户留言或已确认的命盘资料。图片会先识别候选四柱，再要求确认四柱和日主；如识别不可用，请手动输入四柱或完整出生资料。")
        elif name == "/analyze":
            self.sessions[str(user_id)] = Session("analyze", step="topic"); await self._reply(chat_id, "专项主题：事业、财运、感情、复合、考公考编、学业、婚姻、迁移、合作、其他。")
        elif name == "/history":
            args = text.split()[1:]
            if args and args[0] == "export":
                rows = self.repo.search(query=args[1] if len(args) > 1 else "")
                body = "\n\n".join(f"案例：{r['case_id']}\n称呼：{r['display_name']}\n主题：{r['topic']}\nSHA：{r['mingli_commit_sha']}\n结果：{r['result']}" for r in rows) or "暂无案例。"
                await self._reply(chat_id, body)
            elif args and args[0] == "reanalyze" and len(args) > 1:
                row = self.repo.get(args[1])
                if not row:
                    await self._reply(chat_id, "案例不存在。"); return True
                try: payload = json.loads(row["normalized_input"])
                except (TypeError, json.JSONDecodeError):
                    await self._reply(chat_id, "案例原始输入不可读取，无法重新分析。"); return True
                result = await self._run(payload)
                if result is None:
                    await self._reply(chat_id, "MingLi Runtime 超时或不可用，未生成新版本。"); return True
                final = str(result.get("final_answer", ""))
                revision = self.repo.save_revision(row["case_id"], final, self.runtime.commit_sha, result.get("calculation_version"))
                await self._reply(chat_id, f"重新分析完成。revision：{revision}\n旧 SHA：{row['mingli_commit_sha']}\n新 SHA：{self.runtime.commit_sha}\n原 created_at：{row['created_at']}\n\n{final}")
            elif args and args[0] in {"view", "摘要"} and len(args) > 1:
                row = self.repo.get(args[1]); await self._reply(chat_id, (f"案例：{row['case_id']}\n称呼：{row['display_name']}\n主题：{row['topic']}\n置信度：{row['confidence']}\nSHA：{row['mingli_commit_sha']}\n状态：{row['status']}" if row else "案例不存在。"))
            elif args and args[0] == "topic":
                rows = self.repo.search(topic=" ".join(args[1:]))
                await self._reply(chat_id, "\n".join(f"{r['case_id']}｜{r['display_name']}｜{r['topic']}｜{r['confidence']}" for r in rows) or "暂无匹配案例。")
            elif args:
                rows = self.repo.search(query=args[0])
                await self._reply(chat_id, "\n".join(f"{r['case_id']}｜{r['display_name']}｜{r['topic']}｜{r['confidence']}" for r in rows) or "暂无匹配案例。")
            else:
                rows = self.repo.recent(); await self._reply(chat_id, "最近案例：\n" + ("\n".join(f"{r['case_id']}｜{r['display_name']}｜{r['topic']}｜{r['confidence']}" for r in rows) if rows else "暂无案例。"))
        else:
            return False
        return True

    def _payload_from_chart(self, chart: dict[str, Any], reality: str, scenario: str | None = None) -> dict[str, Any]:
        facts: dict[str, Any] = {}
        if "对方已婚" in reality or "有新人" in reality: facts["other_party_status"] = "married"
        if "已婚" in reality and "对方" not in reality: facts["relationship_status"] = "married"
        if "失联" in reality:
            facts["contact_status"] = "no_contact"
            match = re.search(r"失联\s*(\d+)\s*个?月", reality)
            if match: facts["no_contact_months"] = int(match.group(1))
        if "明确拒绝" in reality or "拒绝" in reality: facts["explicit_rejection"] = True
        if "专业限制" in reality or "专业不符" in reality: facts["major_eligible"] = False
        if "已进入面试" in reality: facts["exam_stage"] = "interview"
        if "失业" in reality: facts["career_status"] = "unemployed"
        if "双方愿意" in reality or "都有意愿" in reality: facts["both_willing"] = True
        return {"chart_input": chart, "anchor_year": datetime.now().year, "scenario": scenario, "reality": facts, "reality_context_raw": reality, "fusion_evidence": [], "annual_evidence": [], "advice_codes": []}

    async def _run(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(asyncio.to_thread(self.runtime.full, payload), timeout=float(os.getenv("MINGLI_RUNTIME_TIMEOUT", "30")))
        except asyncio.TimeoutError:
            return None
        except Exception as exc:
            log.warning("MingLi runtime failed: %s", type(exc).__name__)
            return None

    async def _append_reviewed_references(
        self, answer: str, topic: object, *, limit: int | None = None
    ) -> str:
        """Query only after a routed text answer exists; failures leave it unchanged."""

        query = str(topic).strip()
        if not query:
            return answer
        references = await asyncio.to_thread(self.knowledge_client.search, query)
        rendered = answer + render_reviewed_references(reviewed_references_only(references))
        return rendered if limit is None or len(rendered) <= limit else answer

    @staticmethod
    def _scenario_for(topic: str) -> str | None:
        if topic in {"考公考编", "考公", "考编"}: return "career_exam"
        if topic == "复合": return "relationship_reunion"
        return None

    @staticmethod
    def _scenario_text(result: dict[str, Any], topic: str) -> str:
        assessment = result.get("scenario_assessment") or {}
        layers = assessment.get("layers") if isinstance(assessment, dict) else None
        labels = {"system_fit":"适合体制内与否", "admission_outlook":"能否上岸", "exam_outlook":"考试运", "position_direction":"岗位方向", "preparation_strategy":"备考策略", "attraction":"缘分牵引", "recontact":"复联可能", "reunion":"复合可能", "stability":"稳定可能"}
        if not isinstance(layers, list): return ""
        return "\n专项 Runtime 结果（固定 SHA）：\n" + "\n".join(f"{labels.get(str(x.get('layer')), str(x.get('layer')))}：{x.get('label')}｜置信度：{x.get('confidence')}" for x in layers if isinstance(x, dict))

    async def _quick_complete(self, user_id: str, chat_id: str, session: Session, limit: int) -> bool:
        message = str(session.data.get("message", ""))
        date = re.search(r"(\d{4}-\d{2}-\d{2})", message)
        time = re.search(r"(?<!\d)(\d{1,2}:\d{2})(?!\d)", message)
        location = re.search(r"(?:出生地|地点)\s*[:：]\s*([^，,\n]+)", message)
        if not date or not time or not location or not any(token in message.lower() for token in ("男", "女", "male", "female")):
            result = f"低置信：资料不足，只能看问题趋势，不能据此完成精确排盘。\n主题：{session.data.get('topic')}。\n限制：未提供可验证的完整出生资料。\n{DISCLAIMER}"
            await self._reply(chat_id, result[:limit]); return True
        chart = {"gender": "female" if "女" in message or "female" in message.lower() else "male", "calendar": "lunar" if "农历" in message else "solar", "birth_date": date.group(1), "birth_time": time.group(1).zfill(5), "timezone": "Asia/Shanghai", "birth_location": {"city": location.group(1).strip()}, "true_solar_time": False, "is_leap_month": False}
        result = await self._run(self._payload_from_chart(chart, message))
        if result is None:
            await self._reply(chat_id, "MingLi Runtime 超时或不可用，未生成评论回复。"); return True
        rendered = str(result.get("final_answer", ""))
        compressed = "\n".join(line for line in rendered.splitlines() if line.strip())
        compressed = compressed[:max(0, limit - len(DISCLAIMER) - 1)].rstrip() + "\n" + DISCLAIMER
        compressed = await self._append_reviewed_references(
            compressed, session.data.get("topic"), limit=limit
        )
        await self._reply(chat_id, compressed); return True

    async def text(self, user_id: str, chat_id: str, text: str) -> bool:
        if not is_admin(user_id):
            if self._is_explicit_mingli_text(text):
                return await self._deny(chat_id)
            return False
        image_session = self._image_session(user_id, chat_id)
        session = image_session or self.sessions.get(str(user_id))
        if not session:
            if text.strip() in {"新客户完整测算", "评论区快速回复", "专项问题分析", "历史案例", "取消当前任务"}:
                aliases = {"新客户完整测算": "/new", "评论区快速回复": "/quick", "专项问题分析": "/analyze", "历史案例": "/history", "取消当前任务": "/cancel"}
                return await self.command(user_id, chat_id, aliases[text.strip()])
            if self._is_explicit_mingli_text(text):
                session = Session("text_intake", step="collecting", data={"chart": {}})
                self.sessions[str(user_id)] = session
                return await self._complete_text_intake(user_id, chat_id, session, text)
            return False
        if text.strip().lower() == "/cancel":
            return await self.command(user_id, chat_id, text)
        if image_session is not None and image_session.data.get("runtime_dispatch_attempted"):
            if self._is_image_confirmation_reply(text):
                await self._reply(chat_id, "runtime_already_dispatched: 本次图片命盘已处理，不会重复调用 Runtime。")
                return True
            session = self.sessions.get(str(user_id))
            if session is None:
                return False
        if image_session is not None and image_session.step in {"awaiting_confirmation", "awaiting_gender"}:
            return await self._confirm_image_candidate(user_id, chat_id, image_session, text)
        if session.mode == "text_intake":
            if session.step == "done":
                return False
            return await self._complete_text_intake(user_id, chat_id, session, text)
        if session.mode == "new":
            return await self._new_step(user_id, chat_id, session, text.strip())
        if session.mode == "quick":
            if session.step == "message":
                session.data["message"] = text; session.step = "topic"; await self._reply(chat_id, "主题：事业｜财运｜感情｜考公考编｜综合趋势"); return True
            if session.step == "topic":
                session.data["topic"] = text; session.step = "length"; await self._reply(chat_id, "字数：80字以内｜150字以内｜300字以内"); return True
            limit = 80 if "80" in text else 150 if "150" in text else 300 if "300" in text else 0
            if not limit: await self._reply(chat_id, "请从 80字以内、150字以内、300字以内中选择。"); return True
            session.step = "done"
            return await self._quick_complete(user_id, chat_id, session, limit)
        if session.mode == "analyze":
            if session.step == "topic":
                session.data["topic"] = text.strip(); session.step = "question"; await self._reply(chat_id, "请发送专项问题与现实背景。将使用最近一次已确认出生资料；如没有，请先 /new。现实现状优先。"); return True
            data = self.completed.get(str(user_id))
            if not data:
                await self._reply(chat_id, "没有已确认出生资料，请先使用 /new 完整收集后再进行专项分析。"); session.step = "done"; return True
            topic = session.data.get("topic", "其他")
            scenario = self._scenario_for(topic)
            payload = self._payload_from_chart(data["chart"], text, scenario)
            result = await self._run(payload)
            if result is None:
                await self._reply(chat_id, "MingLi Runtime 超时或不可用，未生成专项结果。"); return True
            extra = self._scenario_text(result, topic)
            if topic not in {"事业", "财运", "感情", "考公考编", "复合"}:
                extra = f"\n专项状态：unsupported。固定 SHA 当前专项场景仅支持 career_exam、relationship_reunion；本主题仅返回基础 Runtime 结果。"
            answer = str(result.get("final_answer", "")) + extra + ("\n" if extra else "") + DISCLAIMER
            await self._reply(
                chat_id,
                await self._append_reviewed_references(answer, topic),
            )
            session.step = "done"; return True
        return False

    async def _new_step(self, user_id: str, chat_id: str, session: Session, value: str) -> bool:
        fields = session.data.setdefault("fields", [])
        fields.append(value)
        prompts = ["性别（男/女）：", "历法（公历/农历）：", "农历是否闰月（是/否；公历填否）：", "出生日期（YYYY-MM-DD）：", "出生时间（HH:MM；未知请明确写未知）：", "出生地（城市；真太阳时需经纬度）：", "是否采用真太阳时（是/否）：", "主要问题：", "现实背景：", "输出模式（简洁版/标准版/详细版）："]
        if len(fields) < 11:
            await self._reply(chat_id, prompts[len(fields)-1]); return True
        keys = ["display_name", "gender", "calendar", "is_leap_month", "birth_date", "birth_time", "birth_location", "true_solar_time", "topic", "reality_context", "output_mode"]
        session.data.update(dict(zip(keys, fields)))
        if session.data["birth_time"] == "未知" or not re.fullmatch(r"\d{2}:\d{2}", session.data["birth_time"]):
            await self._reply(chat_id, "出生时间缺失或格式错误，不能进行精确分析；资料不足，只能低置信看趋势。请 /cancel 或重新 /new。"); return True
        await self._reply(chat_id, "【资料确认】\n" + "\n".join(f"{k}：{session.data[k]}" for k in keys) + "\n\n请回复：确认并分析，或修改资料，或取消。")
        session.step = "confirm"; return True

    async def confirm(self, user_id: str, chat_id: str, text: str) -> bool:
        if not is_admin(user_id):
            if self._is_explicit_mingli_text(text):
                return await self._deny(chat_id)
            return False
        image_session = self._image_session(user_id, chat_id)
        if image_session and image_session.step in {"awaiting_confirmation", "awaiting_gender"}:
            return await self._confirm_image_candidate(user_id, chat_id, image_session, text)
        if (
            image_session
            and image_session.step == "runtime_ready"
            and not image_session.data.get("runtime_dispatch_attempted")
            and self._is_image_confirmation_reply(text)
        ):
            return await self._dispatch_confirmed_image(
                user_id, chat_id, image_session
            )
        if image_session and image_session.data.get("runtime_dispatch_attempted") and self._is_image_confirmation_reply(text):
            await self._reply(chat_id, "runtime_already_dispatched: 本次图片命盘已处理，不会重复调用 Runtime。")
            return True
        session = self.sessions.get(str(user_id))
        if not session or session.mode != "new" or session.step != "confirm": return False
        if text.strip() not in {"确认并分析", "确认"}:
            await self._reply(chat_id, "未确认。请回复“确认并分析”、 “修改资料”或“取消”。"); return True
        loc_text = session.data["birth_location"].strip()
        loc_parts = [part.strip() for part in loc_text.split(",")]
        solar = session.data["true_solar_time"] in {"是", "true", "True"}
        if solar:
            if len(loc_parts) < 3:
                await self._reply(chat_id, "真太阳时需要出生地经纬度；请重新 /new，并在出生地项写入 longitude,latitude,city。"); return True
            try:
                loc = {"longitude": float(loc_parts[0]), "latitude": float(loc_parts[1]), "city": ",".join(loc_parts[2:])}
            except ValueError:
                await self._reply(chat_id, "出生地经纬度格式错误，请使用 longitude,latitude,city。"); return True
        else:
            loc = {"city": loc_text}
        chart = {"gender": "male" if session.data["gender"] in {"男", "male"} else "female", "calendar": "lunar" if "农" in session.data["calendar"] else "solar", "birth_date": session.data["birth_date"], "birth_time": session.data["birth_time"], "timezone": "Asia/Shanghai", "birth_location": loc, "true_solar_time": solar, "is_leap_month": session.data["is_leap_month"] in {"是", "true", "True"} }
        payload = {"chart_input": chart, "anchor_year": datetime.now().year, "scenario": None, "reality": {"text": session.data["reality_context"]}, "fusion_evidence": [], "annual_evidence": [], "advice_codes": []}
        try:
            result = await asyncio.wait_for(asyncio.to_thread(self.runtime.full, payload), timeout=float(os.getenv("MINGLI_RUNTIME_TIMEOUT", "30")))
        except asyncio.TimeoutError:
            await self._reply(chat_id, "MingLi Runtime 超时，请稍后重试；本次未生成案例。"); return True
        except Exception as exc:
            log.warning("MingLi runtime failed for admin=%s: %s", user_id, type(exc).__name__)
            await self._reply(chat_id, "MingLi Runtime 当前不可用或资料不符合接口要求；请检查出生资料后重试。")
            return True
        final = str(result["final_answer"])
        if not final.endswith(DISCLAIMER): final += "\n" + DISCLAIMER
        await self._reply(chat_id, final)
        now = datetime.now(timezone.utc).isoformat()
        case_id = "case-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        case = {"case_id": case_id, "customer_id": str(user_id), "display_name": session.data["display_name"], "gender": session.data["gender"], "calendar_type": session.data["calendar"], "birth_datetime": session.data["birth_date"] + " " + session.data["birth_time"], "birth_location": loc, "true_solar_time_policy": session.data["true_solar_time"], "topic": session.data["topic"], "reality_context": session.data["reality_context"], "normalized_input": payload, "mingli_commit_sha": self.runtime.commit_sha, "runtime_version": result.get("calculation_version"), "result": final, "confidence": "low", "created_at": now, "updated_at": now, "status": "completed"}
        try: self.repo.save(case)
        except Exception: log.warning("case save failed: %s", type(sys.exc_info()[1]).__name__)
        self.completed[str(user_id)] = {"chart": chart, "case_id": case_id, "topic": session.data["topic"], "reality_context": session.data["reality_context"]}
        session.step = "done"; return True
