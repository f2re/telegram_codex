#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram AI CLI Bot.

A minimal, reliable Telegram long-polling service that starts local AI CLI
commands, tracks job state in SQLite and sends results back to Telegram.

The service intentionally does not modify codex-plan/codex-run. It only starts
configured commands as external processes.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

# Configure logging to stdout for systemd/journalctl
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("bot")

TELEGRAM_MESSAGE_LIMIT = 3900


def mask_token(token: str) -> str:
    """Mask Telegram token for safe logging."""
    if ":" in token:
        prefix, suffix = token.split(":", 1)
        return f"{prefix}:{'*' * (len(suffix) - 4)}{suffix[-4:]}"
    return "****"


def utc_now() -> str:
    """Return current UTC time as ISO string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def require_env(name: str) -> str:
    """Read required environment variable."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is not set: {name}")
    return value


def parse_user_ids(raw: str) -> set[int]:
    """Parse comma-separated Telegram user IDs."""
    users: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            users.add(int(part))
        except ValueError as exc:
            raise RuntimeError(f"Invalid Telegram user id: {part}") from exc
    return users


def read_text_limited(path: Path, max_chars: int) -> str:
    """Read UTF-8 text file with size limit for Telegram messages."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"Cannot read {path}: {exc}"

    if len(text) > max_chars:
        return text[:max_chars] + "\n\n...[truncated for Telegram]..."
    return text


@dataclass(frozen=True)
class Config:
    """Runtime configuration loaded from environment."""

    telegram_bot_token: str
    telegram_allowed_users: set[int]
    project_dir: Path
    data_dir: Path
    logs_dir: Path
    results_dir: Path
    db_path: Path
    codex_plan_cmd: str
    codex_run_cmd: str
    codex_cmd: str
    gemini_cmd: str
    service_path: str
    home: str
    telegram_poll_timeout: int
    http_timeout: int

    @classmethod
    def from_env(cls) -> "Config":
        token = require_env("TELEGRAM_BOT_TOKEN")
        users = parse_user_ids(require_env("TELEGRAM_ALLOWED_USERS"))
        if not users:
            raise RuntimeError("TELEGRAM_ALLOWED_USERS is empty")

        data_dir = Path(os.environ.get("DATA_DIR", "/var/lib/codex-telegram-bot")).resolve()
        logs_dir = data_dir / "logs"
        results_dir = data_dir / "results"

        cfg = cls(
            telegram_bot_token=token,
            telegram_allowed_users=users,
            project_dir=Path(os.environ.get("PROJECT_DIR", "/srv/projects/my-project")).resolve(),
            data_dir=data_dir,
            logs_dir=logs_dir,
            results_dir=results_dir,
            db_path=data_dir / "bot.sqlite3",
            codex_plan_cmd=os.environ.get("CODEX_PLAN_CMD", "codex-plan"),
            codex_run_cmd=os.environ.get("CODEX_RUN_CMD", "codex-run"),
            codex_cmd=os.environ.get("CODEX_CMD", "codex"),
            gemini_cmd=os.environ.get("GEMINI_CMD", "gemini"),
            service_path=os.environ.get("SERVICE_PATH", os.environ.get("PATH", "")),
            home=os.environ.get("HOME", str(Path.home())),
            telegram_poll_timeout=int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "50")),
            http_timeout=int(os.environ.get("HTTP_TIMEOUT", "70")),
        )

        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        cfg.results_dir.mkdir(parents=True, exist_ok=True)
        return cfg

    @property
    def telegram_api_base(self) -> str:
        return f"https://api.telegram.org/bot{self.telegram_bot_token}"


class TelegramClient:
    """Small Telegram Bot API client."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.session = requests.Session()

    def call(
        self,
        method: str,
        payload: dict[str, Any],
        files: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Call Telegram Bot API method."""
        url = f"{self.cfg.telegram_api_base}/{method}"
        masked_url = f"https://api.telegram.org/bot{mask_token(self.cfg.telegram_bot_token)}/{method}"
        
        # Don't log full getUpdates payload to keep logs clean
        log_payload = {k: v for k, v in payload.items() if k != "offset"} if method == "getUpdates" else payload
        logger.debug("Telegram API Request: %s %s", method, log_payload)

        try:
            if files:
                response = self.session.post(url, data=payload, files=files, timeout=self.cfg.http_timeout)
            else:
                response = self.session.post(url, json=payload, timeout=self.cfg.http_timeout)
        except requests.RequestException as exc:
            logger.error("Telegram request failed: %s %s", masked_url, exc)
            raise RuntimeError(f"Telegram request failed: {exc}") from exc

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            logger.error("Telegram returned non-JSON HTTP %d: %s", response.status_code, response.text)
            raise RuntimeError(f"Telegram returned non-JSON HTTP {response.status_code}") from exc

        if not data.get("ok"):
            logger.error("Telegram API error: %s", data)
            raise RuntimeError(f"Telegram API error: {data}")
        
        if method != "getUpdates" or data.get("result"):
             logger.debug("Telegram API Response: %s", data)
             
        return data

    def get_updates(self, offset: Optional[int]) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": self.cfg.telegram_poll_timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload).get("result", [])

    def send_message(self, chat_id: int, text: str, reply_markup: Optional[dict[str, Any]] = None) -> None:
        if not text:
            text = "📭 Пустой ответ."
        for idx in range(0, len(text), TELEGRAM_MESSAGE_LIMIT):
            chunk = text[idx : idx + TELEGRAM_MESSAGE_LIMIT]
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
                "parse_mode": "Markdown",
            }
            if reply_markup and idx + TELEGRAM_MESSAGE_LIMIT >= len(text):
                payload["reply_markup"] = reply_markup

            try:
                self.call("sendMessage", payload)
            except RuntimeError as exc:
                if "can't parse entities" in str(exc).lower() or "bad request" in str(exc).lower():
                    # Fallback to plain text if Markdown is broken
                    payload.pop("parse_mode", None)
                    # Also escape any unintentional Markdown-like characters is not needed in plain text
                    # but we might want to tell the user it was a fallback
                    if idx == 0:
                        payload["text"] = "⚠️ [Markdown Fallback]\n" + chunk
                    self.call("sendMessage", payload)
                else:
                    raise

    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None) -> None:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self.call("answerCallbackQuery", payload)

    def send_document(self, chat_id: int, path: Path, caption: str = "") -> None:
        if not path.exists():
            self.send_message(chat_id, f"❌ Файл не найден: {path}")
            return
        with path.open("rb") as fh:
            self.call(
                "sendDocument",
                {"chat_id": chat_id, "caption": caption[:1024]},
                files={"document": (path.name, fh, "text/markdown")},
            )


class Storage:
    """SQLite-backed job storage."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.lock = threading.Lock()
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self.lock, self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL,
                    task TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_by INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    return_code INTEGER,
                    output_file TEXT,
                    log_file TEXT,
                    error TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at)")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS issue_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL,
                    draft_text TEXT NOT NULL,
                    confirmed INTEGER DEFAULT 0,
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                )
                """
            )
            conn.commit()

    def create_issue_draft(self, job_id: int, draft_text: str) -> None:
        with self.lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO issue_drafts(job_id, draft_text) VALUES (?, ?)",
                (job_id, draft_text),
            )
            conn.commit()

    def get_issue_draft(self, job_id: int) -> Optional[sqlite3.Row]:
        with self.lock, self.connect() as conn:
            return conn.execute("SELECT * FROM issue_drafts WHERE job_id=?", (job_id,)).fetchone()

    def create_job(self, mode: str, task: str, user_id: int, chat_id: int, log_file: Path) -> int:
        with self.lock, self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO jobs(mode, task, status, requested_by, chat_id, created_at, log_file)
                VALUES (?, ?, 'queued', ?, ?, ?, ?)
                """,
                (mode, task, user_id, chat_id, utc_now(), str(log_file)),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"status", "started_at", "finished_at", "return_code", "output_file", "log_file", "error"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown job fields: {unknown}")
        keys = list(fields)
        sql = "UPDATE jobs SET " + ", ".join(f"{key}=?" for key in keys) + " WHERE id=?"
        values = [fields[key] for key in keys] + [job_id]
        with self.lock, self.connect() as conn:
            conn.execute(sql, values)
            conn.commit()

    def get_job(self, job_id: int) -> Optional[sqlite3.Row]:
        with self.lock, self.connect() as conn:
            return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def active_job(self) -> Optional[sqlite3.Row]:
        with self.lock, self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('queued', 'running')
                ORDER BY id ASC
                LIMIT 1
                """
            ).fetchone()

    def last_jobs(self, limit: int = 10) -> list[sqlite3.Row]:
        with self.lock, self.connect() as conn:
            return list(conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall())

    def last_finished_job(self) -> Optional[sqlite3.Row]:
        with self.lock, self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('success', 'failed', 'cancelled')
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()


class JobRunner:
    """Single-worker job queue for local AI CLIs."""

    def __init__(self, cfg: Config, tg: TelegramClient, storage: Storage) -> None:
        self.cfg = cfg
        self.tg = tg
        self.storage = storage
        self.jobs: queue.Queue[int] = queue.Queue()
        self.proc_lock = threading.Lock()
        self.current_job_id: Optional[int] = None
        self.current_proc: Optional[subprocess.Popen[Any]] = None
        self.cancel_requested_for: Optional[int] = None
        self.worker = threading.Thread(target=self.worker_loop, daemon=True)
        self.worker.start()

    def enqueue(self, mode: str, task: str, user_id: int, chat_id: int) -> int:
        if mode not in {"plan", "run", "gemini", "codex", "issue", "stats"}:
            raise ValueError("mode must be one of: plan, run, gemini, codex, issue, stats")

        if self.storage.active_job() is not None:
            logger.warning("Enqueue failed: active job exists (User %d, Mode %s)", user_id, mode)
            raise RuntimeError("There is already an active job. Use /status or /cancel.")
        
        log_file = self.cfg.logs_dir / f"{mode}-{int(time.time())}.log"
        job_id = self.storage.create_job(mode, task, user_id, chat_id, log_file)
        logger.info("Job #%d enqueued (User %d, Mode %s)", job_id, user_id, mode)
        self.jobs.put(job_id)
        return job_id

    def request_cancel(self) -> bool:
        with self.proc_lock:
            if self.current_job_id is None:
                return False
            self.cancel_requested_for = self.current_job_id
            proc = self.current_proc
            job_id = self.current_job_id

        logger.info("Cancellation requested for Job #%d", job_id)
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except OSError:
                try:
                    proc.terminate()
                except OSError:
                    pass
        return True

    def worker_loop(self) -> None:
        while True:
            job_id = self.jobs.get()
            logger.info("Worker picked Job #%d", job_id)
            try:
                self.run_job(job_id)
            except Exception as exc:  # noqa: BLE001 - final safety net for daemon worker
                logger.exception("Critical error in Job #%d:", job_id)
                row = self.storage.get_job(job_id)
                chat_id = int(row["chat_id"]) if row else 0
                self.storage.update_job(job_id, status="failed", finished_at=utc_now(), error=str(exc))
                if chat_id:
                    self.tg.send_message(chat_id, f"💥 Задача #{job_id} завершилась с критической ошибкой:\n\n```\n{exc}\n```")
            finally:
                logger.info("Worker finished Job #%d", job_id)
                self.jobs.task_done()

    def split_configured_command(self, command: str) -> list[str]:
        parts = shlex.split(command)
        if not parts:
            raise RuntimeError("Configured command is empty")
        parts[0] = self.resolve_executable(parts[0])
        return parts

    def resolve_executable(self, executable: str) -> str:
        if "/" in executable:
            path = Path(executable)
            if not path.exists():
                raise RuntimeError(f"Команда не найдена по пути: {executable}")
            return str(path)
        found = shutil.which(executable, path=self.cfg.service_path)
        if not found:
            raise RuntimeError(
                f"Исполняемый файл '{executable}' не найден в SERVICE_PATH.\n"
                f"Проверьте настройки в файле конфигурации.\n"
                f"Текущий PATH поиска: {self.cfg.service_path}"
            )
        return found

    def command_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PROJECT_DIR"] = str(self.cfg.project_dir)
        env["OUT_DIR"] = str(self.cfg.results_dir)
        env["HOME"] = self.cfg.home
        env["PATH"] = self.cfg.service_path
        return env

    def default_output_file(self, mode: str, job_id: int) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return self.cfg.results_dir / f"{mode}-{stamp}-job{job_id}.md"

    def build_command(self, mode: str, task: str, output_file: Path) -> list[str]:
        if mode == "plan":
            return [*self.split_configured_command(self.cfg.codex_plan_cmd), task]
        if mode == "run":
            return [*self.split_configured_command(self.cfg.codex_run_cmd), task]
        if mode == "gemini":
            return [
                *self.split_configured_command(self.cfg.gemini_cmd),
                "--skip-trust",
                "--yolo",
                "--prompt",
                task,
                "--output-format",
                "text",
            ]
        if mode == "codex":
            return [
                *self.split_configured_command(self.cfg.codex_cmd),
                "exec",
                "--cd",
                str(self.cfg.project_dir),
                "--skip-git-repo-check",
                "--sandbox",
                "none",
                "--output-last-message",
                str(output_file),
                task,
            ]
        if mode == "issue":
            prompt = (
                "You are an expert system analyst. Your goal is to draft a GitLab issue for the task below. "
                "You have GitLab MCP tools available; do NOT use them yet. Just create a clear Markdown draft "
                "including a Title, Description, and any Labels or Milestones you'd suggest. "
                "Make it professional and concise. "
                f"Task: {task}"
            )
            return [
                *self.split_configured_command(self.cfg.gemini_cmd),
                "--skip-trust",
                "--yolo",
                "--prompt",
                prompt,
                "--output-format",
                "text",
            ]
        if mode == "stats":
            # Собираем детальную статистику по лимитам
            gemini_parts = self.split_configured_command(self.cfg.gemini_cmd)
            codex_parts = self.split_configured_command(self.cfg.codex_cmd)
            
            # Скрипт запрашивает статус у обоих CLI
            # Для Gemini используем скрытую команду /stats через prompt
            script = (
                f"echo '♊️ **Gemini Quota & Usage:**'; "
                f"{shlex.quote(gemini_parts[0])} --prompt '/stats model' --output-format json 2>&1 || echo '❌ Не удалось получить JSON от Gemini'; "
                f"echo; echo '💻 **Codex Status:**'; "
                f"{shlex.quote(codex_parts[0])} status 2>&1 || echo '❌ Не удалось получить статус Codex'"
            )
            return ["bash", "-c", script]
        raise RuntimeError(f"Unsupported mode: {mode}")

    def find_output_file(self, mode: str, log_file: Path, started_ts: float) -> Optional[Path]:
        log_text = read_text_limited(log_file, max_chars=250_000)
        matches = re.findall(r"(/[^\s\"']*(?:plan|run)-\d{8}-\d{6}\.md)", log_text)
        for match in reversed(matches):
            path = Path(match)
            if path.exists():
                return path

        prefix = "plan-" if mode == "plan" else "run-"
        candidates: list[Path] = []
        for path in self.cfg.results_dir.glob(f"{prefix}*.md"):
            try:
                if path.stat().st_mtime >= started_ts - 5:
                    candidates.append(path)
            except OSError:
                continue
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.stat().st_mtime)

    def run_job(self, job_id: int) -> None:
        row = self.storage.get_job(job_id)
        if row is None:
            logger.error("Job #%d not found in database", job_id)
            return

        mode = str(row["mode"])
        task = str(row["task"])
        chat_id = int(row["chat_id"])
        log_file = Path(str(row["log_file"]))
        output_file = self.default_output_file(mode, job_id)
        argv = self.build_command(mode, task, output_file)
        display_command = " ".join(shlex.quote(part) for part in argv[:-1]) + " <task>"

        logger.info("Starting Job #%d (Mode: %s)", job_id, mode)
        logger.debug("Command for Job #%d: %s", job_id, " ".join(shlex.quote(p) for p in argv))

        self.storage.update_job(job_id, status="running", started_at=utc_now())
        self.tg.send_message(
            chat_id,
            f"🚀 **Задача #{job_id} запущена**\n"
            f"📂 Режим: `{mode}`\n"
            f"🏗 Проект: `{self.cfg.project_dir}`\n"
            f"💻 Команда: `{display_command}`",
        )

        started_ts = time.time()
        return_code: Optional[int] = None
        cancelled = False

        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("ab") as log:
            log.write(f"\n=== job #{job_id} started {utc_now()} ===\n".encode("utf-8"))
            log.write(f"mode={mode}\nproject_dir={self.cfg.project_dir}\ncommand={display_command}\n\n".encode("utf-8"))

            proc = subprocess.Popen(
                argv,
                cwd=str(self.cfg.project_dir),
                env=self.command_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

            with self.proc_lock:
                self.current_job_id = job_id
                self.current_proc = proc

            output_chunks: list[bytes] = []
            captured_output_bytes = 0

            def read_output() -> None:
                nonlocal captured_output_bytes
                if proc.stdout is None:
                    return
                for chunk in iter(lambda: proc.stdout.read(8192), b""):
                    log.write(chunk)
                    log.flush()
                    if captured_output_bytes < 1_000_000:
                        remaining = 1_000_000 - captured_output_bytes
                        output_chunks.append(chunk[:remaining])
                        captured_output_bytes += min(len(chunk), remaining)

            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()

            try:
                while True:
                    return_code = proc.poll()
                    if return_code is not None:
                        break

                    with self.proc_lock:
                        should_cancel = self.cancel_requested_for == job_id

                    if should_cancel:
                        cancelled = True
                        logger.warning("Terminating Job #%d process (PID %d)", job_id, proc.pid)
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                        except OSError:
                            pass

                        for _ in range(10):
                            if proc.poll() is not None:
                                break
                            time.sleep(0.5)

                        if proc.poll() is None:
                            logger.error("Force killing Job #%d process (PID %d)", job_id, proc.pid)
                            try:
                                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                            except OSError:
                                pass

                        return_code = proc.wait()
                        break

                    time.sleep(1)
            finally:
                reader.join(timeout=10)
                with self.proc_lock:
                    self.current_job_id = None
                    self.current_proc = None
                    if self.cancel_requested_for == job_id:
                        self.cancel_requested_for = None

            log.write(
                f"\n=== job #{job_id} finished {utc_now()} rc={return_code} cancelled={cancelled} ===\n".encode(
                    "utf-8"
                )
            )

        if mode in {"gemini", "stats", "issue"} and output_chunks:
            full_output = b"".join(output_chunks)
            output_file.write_bytes(full_output)
            try:
                decoded_output = full_output.decode("utf-8", errors="replace")
                logger.info("Job #%d output captured:\n%s", job_id, decoded_output.strip())
            except Exception:
                logger.info("Job #%d output captured (binary or undecodable)", job_id)

        if not output_file.exists():
            output_file = self.find_output_file(mode, log_file, started_ts)
        
        status = "cancelled" if cancelled else "success" if return_code == 0 else "failed"
        logger.info("Job #%d finished with status: %s (RC: %s)", job_id, status, return_code)

        if output_file and output_file.exists():
            logger.info("Job #%d result file: %s", job_id, output_file)
        elif status != "cancelled":
            logger.warning("Job #%d finished but no output file found", job_id)

        self.storage.update_job(
            job_id,
            status=status,
            finished_at=utc_now(),
            return_code=return_code,
            output_file=str(output_file) if output_file else None,
        )
        self.report_job(chat_id, job_id, status, return_code, output_file, log_file)

    def report_job(
        self,
        chat_id: int,
        job_id: int,
        status: str,
        return_code: Optional[int],
        output_file: Optional[Path],
        log_file: Path,
    ) -> None:
        row = self.storage.get_job(job_id)
        mode = row["mode"] if row else "unknown"

        if mode == "issue" and status == "success" and output_file and output_file.exists():
            self.handle_issue_report(chat_id, job_id, output_file)
            return

        if mode == "stats":
            log_text = read_text_limited(log_file, max_chars=12_000)
            # Убираем системные маркеры из лога для чистого вывода
            clean_output = []
            for line in log_text.splitlines():
                if line.startswith("=== job #") or line.startswith("mode=") or line.startswith("project_dir=") or line.startswith("command="):
                    continue
                clean_output.append(line)
            
            self.tg.send_message(chat_id, "📈 **Статистика использования**\n\n" + "\n".join(clean_output).strip())
            return

        status_icon = "✅" if status == "success" else "❌" if status == "failed" else "🚫"
        status_text = "Успешно" if status == "success" else "Ошибка" if status == "failed" else "Отменено"
        
        header = f"{status_icon} **Задача #{job_id} завершена**\nСтатус: {status_text}\nКод возврата: `{return_code}`"
        
        if output_file and output_file.exists():
            result = read_text_limited(output_file, max_chars=12_000)
            logger.info("Job #%d reporting success. Result snippet:\n%s", job_id, result[:1000] + "..." if len(result) > 1000 else result)
            self.tg.send_message(chat_id, f"{header}\n\n📝 **Результат:**\n\n{result}")
            self.tg.send_document(chat_id, output_file, caption=f"📄 Результат задачи #{job_id}")
            return

        log_tail = read_text_limited(log_file, max_chars=12_000)
        logger.warning("Job #%d reporting failure/no-output. Log tail:\n%s", job_id, log_tail[:1000] + "..." if len(log_tail) > 1000 else log_tail)
        self.tg.send_message(chat_id, f"{header}\n\n⚠️ Файл результата не найден. Последние строки лога:\n\n{log_tail}")
        self.tg.send_document(chat_id, log_file, caption=f"📜 Лог задачи #{job_id}")

    def handle_issue_report(self, chat_id: int, job_id: int, output_file: Path) -> None:
        try:
            draft_text = output_file.read_text(encoding="utf-8").strip()
            self.storage.create_issue_draft(job_id, draft_text)

            msg = (
                f"📋 **Черновик Issue для GitLab (MCP)**\n\n"
                f"{draft_text}\n\n"
                f"❓ Создать этот тикет в GitLab, используя MCP инструменты?"
            )
            
            keyboard = {
                "inline_keyboard": [[
                    {"text": "✅ Создать через MCP", "callback_data": f"issue_confirm:{job_id}"},
                    {"text": "❌ Отмена", "callback_data": f"issue_cancel:{job_id}"}
                ]]
            }
            self.tg.send_message(chat_id, msg, reply_markup=keyboard)

        except Exception as exc:
            self.tg.send_message(chat_id, f"⚠️ Ошибка подготовки черновика: {exc}")

    def create_gitlab_issue(self, chat_id: int, job_id: int) -> None:
        draft = self.storage.get_issue_draft(job_id)
        if not draft:
            self.tg.send_message(chat_id, "❌ Черновик не найден.")
            return

        # Для создания мы запускаем новую задачу через Codex, который умеет пользоваться MCP GitLab
        prompt = (
            "Using your GitLab MCP tools, create a new issue exactly as described in this draft. "
            "Report the final issue URL. "
            f"Draft:\n{draft['draft_text']}"
        )
        
        try:
            # Мы ставим задачу в очередь бота как новый Job режима 'codex' (чтобы были видны логи и процесс)
            # Но для пользователя это выглядит как продолжение
            new_job_id = self.enqueue("codex", prompt, 0, chat_id) # 0 as system/bot user id
            self.tg.send_message(chat_id, f"⚙️ **Запущено создание Issue (MCP)...** (Задача #{new_job_id})")
        except Exception as exc:
            self.tg.send_message(chat_id, f"💥 Не удалось запустить процесс создания: {exc}")


class BotApp:
    """Telegram long-polling application."""

    def __init__(self) -> None:
        self.cfg = Config.from_env()
        self.tg = TelegramClient(self.cfg)
        self.storage = Storage(self.cfg.db_path)
        self.runner = JobRunner(self.cfg, self.tg, self.storage)
        self.offset: Optional[int] = None
        self.stop_event = threading.Event()

        signal.signal(signal.SIGTERM, self.handle_signal)
        signal.signal(signal.SIGINT, self.handle_signal)

    def handle_signal(self, signum: int, _frame: object) -> None:
        logger.info("Received signal %d, stopping bot", signum)
        self.stop_event.set()
        self.runner.request_cancel()

    def is_allowed(self, user_id: int) -> bool:
        return user_id in self.cfg.telegram_allowed_users

    def run(self) -> None:
        logger.info("codex-telegram-bot started")
        while not self.stop_event.is_set():
            try:
                updates = self.tg.get_updates(self.offset)
                for update in updates:
                    self.offset = int(update["update_id"]) + 1
                    self.handle_update(update)
            except Exception as exc:  # noqa: BLE001 - keep daemon alive
                logger.error("Polling error: %s", exc)
                time.sleep(5)

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
            return

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        text = str(message.get("text") or "").strip()
        if not text:
            return

        chat_id = int(chat.get("id"))
        user_id = int(user.get("id"))
        username = user.get("username", "unknown")

        if not self.is_allowed(user_id):
            logger.warning("Access denied for User %d (@%s) in Chat %d", user_id, username, chat_id)
            self.tg.send_message(chat_id, "⛔️ **Доступ запрещен.** Ваш ID не в белом списке.")
            return

        logger.info("Incoming message from User %d (@%s): %s", user_id, username, text[:50] + "..." if len(text) > 50 else text)
        try:
            self.handle_text(chat_id, user_id, text)
        except Exception as exc:  # noqa: BLE001 - report command errors to chat
            logger.exception("Error handling command from User %d:", user_id)
            self.tg.send_message(chat_id, f"⚠️ **Ошибка выполнения команды:**\n\n`{exc}`")

    def handle_callback(self, query: dict[str, Any]) -> None:
        chat_id = query["message"]["chat"]["id"]
        user_id = query["from"]["id"]
        data = query.get("data", "")

        if not self.is_allowed(user_id):
            self.tg.answer_callback_query(query["id"], "Доступ запрещен")
            return

        if data.startswith("issue_confirm:"):
            job_id = int(data.split(":")[1])
            self.tg.answer_callback_query(query["id"], "Создаю Issue...")
            self.runner.create_gitlab_issue(chat_id, job_id)
        elif data.startswith("issue_cancel:"):
            self.tg.answer_callback_query(query["id"], "Отменено")
            self.tg.send_message(chat_id, "🚫 Создание Issue отменено.")

    @staticmethod
    def parse_command(text: str) -> tuple[str, str]:
        if not text.startswith("/"):
            return "", text
        parts = text.split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""
        return command, argument

    def handle_text(self, chat_id: int, user_id: int, text: str) -> None:
        command, argument = self.parse_command(text)
        if command in {"/start", "/help"}:
            self.cmd_help(chat_id)
        elif command == "/plan":
            self.cmd_job(chat_id, user_id, "plan", argument)
        elif command == "/run":
            self.cmd_job(chat_id, user_id, "run", argument)
        elif command == "/gemini":
            self.cmd_job(chat_id, user_id, "gemini", argument)
        elif command == "/codex":
            self.cmd_job(chat_id, user_id, "codex", argument)
        elif command == "/issue":
            self.cmd_job(chat_id, user_id, "issue", argument)
        elif command == "/status":
            self.cmd_status(chat_id)
        elif command == "/jobs":
            self.cmd_jobs(chat_id)
        elif command == "/last":
            self.cmd_last(chat_id)
        elif command == "/stats":
            self.cmd_stats(chat_id, user_id)
        elif command == "/cancel":
            self.cmd_cancel(chat_id)
        else:
            # Safe default: plain text means analysis, not code modification.
            self.cmd_job(chat_id, user_id, "plan", text)

    def cmd_help(self, chat_id: int) -> None:
        self.tg.send_message(
            chat_id,
            "🤖 **Codex Telegram Bot** — Ваш AI-ассистент для работы с кодом.\n\n"
            "**Команды:**\n"
            "🔍 /plan <задача> — Анализ и планирование (только чтение)\n"
            "🛠 /run <задача> — Выполнение и модификация кода (YOLO)\n"
            "♊️ /gemini <задача> — Запуск Gemini CLI (YOLO)\n"
            "💻 /codex <задача> — Запуск Codex CLI (без песочницы)\n"
            "🎫 /issue <задание> — Создать Issue в GitLab (с подтверждением)\n"
            "📊 /stats [model] — Просмотр лимитов и статистики моделей\n\n"
            "📊 **Статус:**\n"
            "🕒 /status — Состояние текущей задачи\n"
            "📜 /jobs — Последние 10 задач\n"
            "📈 /stats — Лимиты и статистика использования\n"
            "🔄 /last — Переотправить последний результат\n"
            "🛑 /cancel — Остановить выполнение задачи\n\n"
            "💡 _Текст без команды автоматически запускает_ `/plan`.\n"
            "⚠️ _Одновременно может выполняться только одна задача._",
        )

    def cmd_stats(self, chat_id: int, user_id: int) -> None:
        job_id = self.runner.enqueue("stats", "Fetch usage statistics and quotas", user_id, chat_id)
        self.tg.send_message(chat_id, f"📊 **Задача #{job_id} на получение статистики добавлена в очередь**")

    def cmd_job(self, chat_id: int, user_id: int, mode: str, argument: str) -> None:
        if not argument:
            self.tg.send_message(chat_id, f"📝 Пожалуйста, укажите описание задачи.\nИспользование: `/{mode} <текст задачи>`")
            return
        try:
            job_id = self.runner.enqueue(mode, argument, user_id, chat_id)
            self.tg.send_message(chat_id, f"📥 **Задача #{job_id} добавлена в очередь** (режим: {mode})")
        except RuntimeError as exc:
            self.tg.send_message(chat_id, f"⏳ **Очередь занята:** {exc}")

    def cmd_status(self, chat_id: int) -> None:
        row = self.storage.active_job()
        if not row:
            self.tg.send_message(chat_id, "💤 Сейчас нет активных задач.")
            return
        self.tg.send_message(chat_id, "🔎 **Текущая задача:**\n\n" + self.format_job(row, include_task=True))

    def cmd_jobs(self, chat_id: int) -> None:
        rows = self.storage.last_jobs(limit=10)
        if not rows:
            self.tg.send_message(chat_id, "📭 История задач пуста.")
            return
        self.tg.send_message(chat_id, "📜 **Последние задачи:**\n\n" + "\n\n" + "\n\n".join(self.format_job(row, include_task=False) for row in rows))

    def cmd_last(self, chat_id: int) -> None:
        row = self.storage.last_finished_job()
        if not row:
            self.tg.send_message(chat_id, "🤷‍♂️ Еще нет завершенных задач.")
            return

        output_file = row["output_file"]
        log_file = row["log_file"]

        if output_file and Path(output_file).exists():
            path = Path(output_file)
            self.tg.send_message(chat_id, f"🔄 **Результат задачи #{row['id']}:**\n\n" + read_text_limited(path, max_chars=12_000))
            self.tg.send_document(chat_id, path, caption=f"📄 Результат задачи #{row['id']}")
            return

        if log_file and Path(log_file).exists():
            path = Path(log_file)
            self.tg.send_message(chat_id, f"🔄 **Лог задачи #{row['id']}:**\n\n" + read_text_limited(path, max_chars=12_000))
            self.tg.send_document(chat_id, path, caption=f"📜 Лог задачи #{row['id']}")
            return

        self.tg.send_message(chat_id, f"❌ Файлы для задачи #{row['id']} не найдены.")

    def cmd_cancel(self, chat_id: int) -> None:
        if self.runner.request_cancel():
            self.tg.send_message(chat_id, "🛑 **Запрос на отмену отправлен.** Прерываю процесс...")
        else:
            self.tg.send_message(chat_id, "🤷‍♂️ Нет активных задач для отмены.")

    @staticmethod
    def format_job(row: sqlite3.Row, include_task: bool) -> str:
        task = str(row["task"])
        if len(task) > 500:
            task = task[:500] + "..."
            
        status_map = {
            "queued": "⏳ В очереди",
            "running": "⚙️ Выполняется",
            "success": "✅ Завершено",
            "failed": "❌ Ошибка",
            "cancelled": "🚫 Отменено"
        }
        status_display = status_map.get(row["status"], row["status"])
        
        text = (
            f"🆔 **Задача #{row['id']}**\n"
            f"🏷 Режим: `{row['mode']}`\n"
            f"📊 Статус: {status_display}\n"
            f"📅 Создана: `{row['created_at']}`\n"
            f"🚀 Старт: `{row['started_at'] or '-'}`\n"
            f"🏁 Конец: `{row['finished_at'] or '-'}`\n"
            f"🔢 Код: `{row['return_code'] if row['return_code'] is not None else '-'}`"
        )
        if include_task:
            text += f"\n\n📝 **Задание:**\n_{task}_"
        return text


def main() -> None:
    BotApp().run()


if __name__ == "__main__":
    main()
