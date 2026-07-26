from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

log = logging.getLogger(__name__)
DISCLAIMER = "仅供文化研究与娱乐参考。"
FIXED_MINGLI_SHA = "1b93df7f1256d0701f299a882a17052ad37513d8"
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
        with sqlite3.connect(self.path) as db:
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

    def save(self, case: dict[str, Any]) -> None:
        fields = ["case_id", "customer_id", "display_name", "gender", "calendar_type", "birth_datetime", "birth_location", "true_solar_time_policy", "topic", "reality_context", "normalized_input", "mingli_commit_sha", "runtime_version", "result", "confidence", "created_at", "updated_at", "status"]
        values = [json.dumps(case.get(k), ensure_ascii=False) if isinstance(case.get(k), (dict, list)) else case.get(k) for k in fields]
        with sqlite3.connect(self.path) as db:
            db.execute(f"INSERT OR REPLACE INTO cases ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", values)

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT case_id,display_name,topic,confidence,status,updated_at FROM cases ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 50)),)).fetchall()
        return [dict(row) for row in rows]

    def search(self, query: str = "", topic: str = "") -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            if query:
                rows = db.execute("SELECT * FROM cases WHERE case_id LIKE ? OR display_name LIKE ? ORDER BY updated_at DESC", (f"%{query}%", f"%{query}%")).fetchall()
            elif topic:
                rows = db.execute("SELECT * FROM cases WHERE topic LIKE ? ORDER BY updated_at DESC", (f"%{topic}%",)).fetchall()
            else:
                rows = db.execute("SELECT * FROM cases ORDER BY updated_at DESC LIMIT 10").fetchall()
        return [dict(row) for row in rows]

    def get(self, case_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        return dict(row) if row else None

    def save_revision(self, case_id: str, result: str, sha: str, runtime_version: str | None, confidence: str = "low") -> int:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path) as db:
            cur = db.execute("INSERT INTO case_revisions(case_id,mingli_commit_sha,runtime_version,result,confidence,created_at) VALUES (?,?,?,?,?,?)", (case_id, sha, runtime_version, result, confidence, now))
            return int(cur.lastrowid)

    def revisions(self, case_id: str) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
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
    def __init__(self, send: Callable[[str, str], Awaitable[None]], db_path: str | None = None, runtime: MingLiRuntimeAdapter | None = None):
        self.send = send
        self.sessions: dict[str, Session] = {}
        self.completed: dict[str, dict[str, Any]] = {}
        self.repo = CaseRepository(db_path)
        self.runtime = runtime or MingLiRuntimeAdapter()

    async def _reply(self, chat_id: str, text: str) -> None:
        for part in chunks(text):
            await self.send(chat_id, part)

    async def _deny(self, chat_id: str) -> bool:
        await self._reply(chat_id, "当前机器人为内部工作控制台，暂未开放使用。")
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

    async def image_chart(self, user_id: str, chat_id: str, provider_result: object | None) -> bool:
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
        self.sessions.pop(str(user_id), None)
        self.sessions[str(user_id)] = Session(
            "image_chart", step="awaiting_confirmation", data={"candidate": values}, expires_at=time.monotonic() + IMAGE_CONFIRM_TTL_SECONDS
        )
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

    async def _dispatch_confirmed_image(self, user_id: str, chat_id: str, session: Session) -> bool:
        if session.data.get("runtime_dispatch_attempted"):
            await self._reply(chat_id, "runtime_already_dispatched: 本次图片命盘已处理，不会重复调用 Runtime。")
            return True
        session.data["runtime_dispatch_attempted"] = True
        try:
            result = await asyncio.wait_for(asyncio.to_thread(self.runtime.confirmed_pillars, {"image_chart_confirmation": session.data["image_chart_confirmation"]}), timeout=float(os.getenv("MINGLI_RUNTIME_TIMEOUT", "30")))
        except asyncio.TimeoutError:
            session.step = "done"
            await self._reply(chat_id, "MingLi Runtime 超时；为避免重复计算，本次不会自动重试，请重新上传图片。")
            return True
        except Exception as exc:
            session.step = "done"
            log.warning("MingLi confirmed-pillar runtime failed for admin=%s: %s", user_id, type(exc).__name__)
            await self._reply(chat_id, "MingLi Runtime 当前不可用或确认内容不符合接口要求；为避免重复计算，请重新上传图片后再试。")
            return True
        final = str(result.get("final_answer", "")).strip()
        if not final:
            session.step = "done"
            await self._reply(chat_id, "MingLi Runtime 返回结果无效；本次未生成分析，请重新上传图片。")
            return True
        await self._reply(chat_id, final if final.endswith(DISCLAIMER) else final + "\n" + DISCLAIMER)
        session.step = "done"
        session.expires_at = None
        return True

    async def _confirm_image_candidate(self, user_id: str, chat_id: str, session: Session, text: str) -> bool:
        value = text.strip()
        normalized = value.casefold()
        if session.expires_at is not None and time.monotonic() >= session.expires_at:
            self.sessions.pop(str(user_id), None)
            await self._reply(chat_id, "confirmation_expired: 图片候选已超时，请重新上传。")
            return True
        if normalized in {"否", "不确认", "不對", "不对", "取消", "no", "reject", "cancel"}:
            self.sessions.pop(str(user_id), None)
            await self._reply(chat_id, "confirmation_rejected: 已丢弃图片候选，请重新上传。")
            return True
        candidate = session.data.get("candidate")
        if not isinstance(candidate, dict):
            self.sessions.pop(str(user_id), None)
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
                session.expires_at = time.monotonic() + IMAGE_CONFIRM_TTL_SECONDS
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
            await self._reply(chat_id, "gender_required: 四柱与日主已确认。图片未可靠识别性别，现在只需回复男或女；无需补充其他出生资料。")
            return True
        session.data["image_chart_confirmation"] = {"contract": "mingli-image-chart-confirmation@1.2", "confirmation_status": "confirmed", "runtime_dispatch": "confirmed_pillars", "chart_candidate": {"pillars": pillars, "day_master": candidate["day_master"], "gender": candidate["gender"], "requires_confirmation": False, "confidence": "high", "warnings": [], "birth_datetime": None, "birth_place": None, "calendar_type": None}}
        session.step = "runtime_ready"
        session.expires_at = None
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
            self.sessions.pop(str(user_id), None); await self._reply(chat_id, "已取消当前任务。")
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
        await self._reply(chat_id, compressed); return True

    async def text(self, user_id: str, chat_id: str, text: str) -> bool:
        if not is_admin(user_id):
            return await self._deny(chat_id)
        session = self.sessions.get(str(user_id))
        if not session:
            if text.strip() in {"新客户完整测算", "评论区快速回复", "专项问题分析", "历史案例", "取消当前任务"}:
                aliases = {"新客户完整测算": "/new", "评论区快速回复": "/quick", "专项问题分析": "/analyze", "历史案例": "/history", "取消当前任务": "/cancel"}
                return await self.command(user_id, chat_id, aliases[text.strip()])
            return False
        if text.strip().lower() == "/cancel":
            return await self.command(user_id, chat_id, text)
        if session.mode == "image_chart" and session.step in {"awaiting_confirmation", "awaiting_gender"}:
            return await self._confirm_image_candidate(user_id, chat_id, session, text)
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
            await self._reply(chat_id, str(result.get("final_answer", "")) + extra + ("\n" if extra else "") + DISCLAIMER)
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
        if not is_admin(user_id): return await self._deny(chat_id)
        session = self.sessions.get(str(user_id))
        if session and session.mode == "image_chart" and session.step in {"awaiting_confirmation", "awaiting_gender"}:
            return await self._confirm_image_candidate(user_id, chat_id, session, text)
        if session and session.mode == "image_chart" and session.data.get("runtime_dispatch_attempted") and text.strip().casefold() in {"确认", "確認", "confirm", "confirmed"}:
            await self._reply(chat_id, "runtime_already_dispatched: 本次图片命盘已处理，不会重复调用 Runtime。")
            return True
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
