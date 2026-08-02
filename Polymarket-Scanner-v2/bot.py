"""Telegram long-polling access point for Polymarket-Scanner.

This implementation uses Telegram's HTTPS Bot API directly through httpx, so
there is no web server, public domain, webhook, or Telegram framework required.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import httpx

from fetcher import SQLiteStore
from reporter import telegram_clusters, telegram_summary, telegram_wallets
from scanner import ScanOptions, execute_scan

LOGGER = logging.getLogger(__name__)


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value not in {None, ""} else default


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value not in {None, ""} else default


def _allowed_chat_ids() -> set[int]:
    raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
    result: set[int] = set()
    for token in raw.replace(";", ",").split(","):
        token = token.strip()
        if token:
            result.add(int(token))
    return result


@dataclass(slots=True)
class BotSettings:
    token: str
    allowed_chat_ids: set[int]
    database_path: Path
    output_dir: Path
    scan_interval_minutes: int
    scan_on_start: bool
    options: ScanOptions

    @classmethod
    def from_env(cls) -> "BotSettings":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
        data_dir = Path(os.getenv("DATA_DIR", "data"))
        database_path = Path(os.getenv("DATABASE_PATH", str(data_dir / "polymarket_data.db")))
        output_dir = Path(os.getenv("OUTPUT_DIR", str(data_dir / "reports")))
        options = ScanOptions(
            timeframe=os.getenv("SCAN_TIMEFRAME", "WEEK"),
            metric=os.getenv("SCAN_METRIC", "PNL"),
            limit=_int_env("SCAN_LIMIT", 25),
            category=os.getenv("SCAN_CATEGORY", "OVERALL"),
            trade_limit=_int_env("SCAN_TRADE_LIMIT", 200),
            trade_lookback_days=_int_env("SCAN_TRADE_LOOKBACK_DAYS", 30),
            closed_limit=_int_env("SCAN_CLOSED_LIMIT", 100),
            max_positions=_int_env("SCAN_MAX_POSITIONS", 1000),
            window_minutes=_int_env("SCAN_WINDOW_MINUTES", 15),
            min_overlap=_float_env("SCAN_MIN_OVERLAP", 0.20),
            min_cosine=_float_env("SCAN_MIN_COSINE", 0.80),
            min_shared_positions=_int_env("SCAN_MIN_SHARED_POSITIONS", 2),
            min_cooccurrences=_int_env("SCAN_MIN_COOCCURRENCES", 2),
            min_sync_rate=_float_env("SCAN_MIN_SYNC_RATE", 0.05),
            min_shared_trade_markets=_int_env("SCAN_MIN_SHARED_TRADE_MARKETS", 1),
            cache_ttl_minutes=_int_env("SCAN_CACHE_TTL_MINUTES", 10),
            timeout_seconds=_float_env("HTTP_TIMEOUT_SECONDS", 30.0),
            retries=_int_env("HTTP_RETRIES", 4),
            request_delay_seconds=_float_env("HTTP_REQUEST_DELAY_SECONDS", 0.08),
            concurrency=_int_env("HTTP_CONCURRENCY", 6),
            failure_policy=os.getenv("WALLET_FAILURE_POLICY", "partial"),
            max_failed_wallet_ratio=_float_env("MAX_FAILED_WALLET_RATIO", 1.0),
            allow_stale_fallback=_bool_env("ALLOW_STALE_FALLBACK", True),
        )
        options.validate()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            token=token,
            allowed_chat_ids=_allowed_chat_ids(),
            database_path=database_path,
            output_dir=output_dir,
            scan_interval_minutes=max(0, _int_env("SCAN_INTERVAL_MINUTES", 360)),
            scan_on_start=_bool_env("SCAN_ON_START", False),
            options=options,
        )


class TelegramAPIError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(65.0, connect=15.0),
            headers={"User-Agent": "Polymarket-Scanner-Telegram/2.0"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def call(self, method: str, data: Mapping[str, Any] | None = None) -> Any:
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = await self.client.post(f"/{method}", data=dict(data or {}))
                if response.status_code == 429 or response.status_code >= 500:
                    retry_after = 0.0
                    try:
                        retry_after = float(response.headers.get("Retry-After", "0"))
                    except ValueError:
                        pass
                    if attempt < 3:
                        await asyncio.sleep(max(retry_after, 1.0 * (2**attempt)))
                        continue
                response.raise_for_status()
                payload = response.json()
                if not payload.get("ok"):
                    parameters = payload.get("parameters") or {}
                    retry_after = float(parameters.get("retry_after") or 0.0)
                    if retry_after > 0 and attempt < 3:
                        await asyncio.sleep(retry_after)
                        continue
                    raise TelegramAPIError(f"Telegram {method} failed: {payload}")
                return payload.get("result")
            except (httpx.HTTPError, ValueError, TelegramAPIError) as exc:
                last_error = exc
                if attempt >= 3:
                    break
                await asyncio.sleep(1.0 * (2**attempt))
        raise TelegramAPIError(f"Telegram {method} failed after retries: {last_error}")

    async def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict[str, Any]]:
        data: dict[str, Any] = {"timeout": timeout, "allowed_updates": json.dumps(["message"])}
        if offset is not None:
            data["offset"] = offset
        result = await self.call("getUpdates", data)
        return result if isinstance(result, list) else []

    async def send_message(self, chat_id: int, text: str) -> None:
        await self.call("sendMessage", {"chat_id": chat_id, "text": text})

    async def send_document(self, chat_id: int, path: Path, caption: str) -> None:
        with path.open("rb") as handle:
            response = await self.client.post(
                "/sendDocument",
                data={"chat_id": str(chat_id), "caption": caption},
                files={"document": (path.name, handle, "application/octet-stream")},
                timeout=httpx.Timeout(120.0, connect=15.0),
            )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise TelegramAPIError(f"Telegram sendDocument failed: {payload}")

    async def set_commands(self) -> None:
        commands = [
            {"command": "scan", "description": "Run a fresh scan"},
            {"command": "latest", "description": "Show latest summary"},
            {"command": "wallets", "description": "Show top trader scores"},
            {"command": "clusters", "description": "Show wallet clusters"},
            {"command": "report", "description": "Download JSON and Markdown"},
            {"command": "status", "description": "Show scheduler and scan status"},
            {"command": "chatid", "description": "Show this chat ID"},
        ]
        await self.call("setMyCommands", {"commands": json.dumps(commands)})


class ScanManager:
    def __init__(self, settings: BotSettings) -> None:
        self.settings = settings
        self.lock = asyncio.Lock()
        self.last_error: str | None = None
        self.last_source: str | None = None

    async def run(self, *, refresh: bool) -> dict[str, Any]:
        async with self.lock:
            try:
                analysis, source, _paths = await execute_scan(
                    self.settings.options,
                    database_path=self.settings.database_path,
                    output_dir=self.settings.output_dir,
                    refresh=refresh,
                )
                self.last_error = None
                self.last_source = source
                return analysis
            except Exception as exc:
                self.last_error = str(exc)
                raise

    def latest(self) -> dict[str, Any] | None:
        return SQLiteStore(self.settings.database_path).get_latest_analysis()


class ScannerBot:
    def __init__(self, settings: BotSettings) -> None:
        self.settings = settings
        self.telegram = TelegramClient(settings.token)
        self.manager = ScanManager(settings)
        self.stop_event = asyncio.Event()
        self.update_tasks: set[asyncio.Task[Any]] = set()

    def authorized(self, chat_id: int) -> bool:
        return chat_id in self.settings.allowed_chat_ids

    async def send_text(self, chat_id: int, text: str) -> None:
        remaining = text
        while len(remaining) > 3900:
            split_at = remaining.rfind("\n", 0, 3900)
            if split_at < 1000:
                split_at = 3900
            await self.telegram.send_message(chat_id, remaining[:split_at])
            remaining = remaining[split_at:].lstrip("\n")
        await self.telegram.send_message(chat_id, remaining)

    async def deny(self, chat_id: int) -> None:
        await self.telegram.send_message(
            chat_id,
            "Access denied. Add this chat ID to TELEGRAM_ALLOWED_CHAT_IDS and redeploy:\n"
            f"{chat_id}",
        )

    async def handle_command(self, chat_id: int, command: str) -> None:
        command = command.split("@", 1)[0].lower()
        if command in {"/start", "/help"}:
            if not self.authorized(chat_id):
                await self.telegram.send_message(
                    chat_id,
                    "Polymarket-Scanner is locked. Your chat ID is:\n"
                    f"{chat_id}\n\n"
                    "Add it to TELEGRAM_ALLOWED_CHAT_IDS in Railway, then redeploy. "
                    "Only /start and /chatid reveal this ID; scan data remains blocked.",
                )
                return
            await self.telegram.send_message(
                chat_id,
                "Polymarket-Scanner is ready.\n\n"
                "/scan — fresh on-demand scan\n"
                "/latest — latest summary\n"
                "/wallets — top trader scores\n"
                "/clusters — correlated wallet groups\n"
                "/report — JSON and Markdown files\n"
                "/status — scheduler and last scan status",
            )
            return
        if command == "/chatid":
            await self.telegram.send_message(chat_id, f"Chat ID: {chat_id}")
            return
        if not self.authorized(chat_id):
            await self.deny(chat_id)
            return

        if command == "/scan":
            if self.manager.lock.locked():
                await self.telegram.send_message(chat_id, "A scan is already running.")
                return
            await self.telegram.send_message(chat_id, "Starting a fresh scan…")
            try:
                analysis = await self.manager.run(refresh=True)
            except Exception as exc:
                LOGGER.exception("On-demand scan failed")
                await self.telegram.send_message(chat_id, f"Scan failed: {exc}")
                return
            await self.send_text(chat_id, telegram_summary(analysis))
            return

        analysis = self.manager.latest()
        if command in {"/latest", "/wallets", "/clusters", "/report"} and analysis is None:
            await self.telegram.send_message(chat_id, "No saved scan yet. Run /scan.")
            return
        if command == "/latest":
            await self.send_text(chat_id, telegram_summary(analysis or {}))
        elif command == "/wallets":
            await self.send_text(chat_id, telegram_wallets(analysis or {}))
        elif command == "/clusters":
            await self.send_text(chat_id, telegram_clusters(analysis or {}))
        elif command == "/report":
            json_path = self.settings.output_dir / "analysis.json"
            markdown_path = self.settings.output_dir / "summary_report.md"
            if not json_path.exists() or not markdown_path.exists():
                await self.telegram.send_message(chat_id, "No report files yet. Run /scan.")
                return
            await self.telegram.send_document(chat_id, json_path, "Latest structured analysis")
            await self.telegram.send_document(chat_id, markdown_path, "Latest Markdown summary")
        elif command == "/status":
            latest = self.manager.latest()
            lines = [
                f"Scheduler: every {self.settings.scan_interval_minutes} minute(s)"
                if self.settings.scan_interval_minutes > 0
                else "Scheduler: disabled",
                f"Scan running: {self.manager.lock.locked()}",
                f"Last source: {self.manager.last_source or 'unknown'}",
                f"Last process error: {self.manager.last_error or 'none'}",
            ]
            if latest:
                lines.extend(
                    [
                        f"Last scan ID: {latest.get('scan_id')}",
                        f"Last fetched: {latest.get('scan_fetched_at')}",
                        f"Last status: {latest.get('scan_status')}",
                        f"Data source: {latest.get('data_source')}",
                    ]
                )
            else:
                lines.append("Last scan: none")
            await self.telegram.send_message(chat_id, "\n".join(lines))
        else:
            await self.telegram.send_message(chat_id, "Unknown command. Use /help.")

    async def handle_update(self, update: Mapping[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, Mapping):
            return
        chat = message.get("chat")
        if not isinstance(chat, Mapping) or "id" not in chat:
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.startswith("/"):
            return
        command = text.strip().split(maxsplit=1)[0]
        await self.handle_command(int(chat["id"]), command)

    async def poll_loop(self) -> None:
        offset: int | None = None
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                updates = await self.telegram.get_updates(offset, timeout=50)
                backoff = 1.0
                for update in updates:
                    update_id = int(update.get("update_id", 0))
                    offset = max(offset or 0, update_id + 1)
                    task = asyncio.create_task(self.handle_update(update))
                    self.update_tasks.add(task)
                    task.add_done_callback(self.update_tasks.discard)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Telegram polling failed")
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)

    async def scheduled_scan_once(self) -> None:
        if self.manager.lock.locked():
            LOGGER.warning("Skipping scheduled scan because another scan is running")
            return
        try:
            analysis = await self.manager.run(refresh=True)
            text = telegram_summary(analysis)
        except Exception as exc:
            LOGGER.exception("Scheduled scan failed")
            text = f"Scheduled Polymarket scan failed: {exc}"
        for chat_id in sorted(self.settings.allowed_chat_ids):
            try:
                await self.send_text(chat_id, text)
            except Exception:
                LOGGER.exception("Could not send scheduled result to chat %s", chat_id)

    async def scheduler_loop(self) -> None:
        if self.settings.scan_interval_minutes <= 0:
            LOGGER.info("Scheduled scans disabled")
            await self.stop_event.wait()
            return
        interval_seconds = self.settings.scan_interval_minutes * 60
        initial_delay = 5 if self.settings.scan_on_start else interval_seconds
        LOGGER.info(
            "Scheduled scans every %s minutes; first run in %s seconds",
            self.settings.scan_interval_minutes,
            initial_delay,
        )
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=initial_delay)
            return
        except asyncio.TimeoutError:
            pass
        while not self.stop_event.is_set():
            await self.scheduled_scan_once()
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def run(self) -> None:
        if not self.settings.allowed_chat_ids:
            LOGGER.warning(
                "TELEGRAM_ALLOWED_CHAT_IDS is empty. Only /start and /chatid reveal chat IDs; scan commands are denied."
            )
        await self.telegram.call("deleteWebhook", {"drop_pending_updates": "false"})
        await self.telegram.set_commands()
        poll_task = asyncio.create_task(self.poll_loop(), name="telegram-poll-loop")
        scheduler_task = asyncio.create_task(self.scheduler_loop(), name="scan-scheduler-loop")
        try:
            await self.stop_event.wait()
        finally:
            for task in (poll_task, scheduler_task):
                task.cancel()
            await asyncio.gather(poll_task, scheduler_task, return_exceptions=True)
            if self.update_tasks:
                await asyncio.gather(*self.update_tasks, return_exceptions=True)
            await self.telegram.close()

    def stop(self) -> None:
        self.stop_event.set()


async def async_main() -> None:
    settings = BotSettings.from_env()
    bot = ScannerBot(settings)
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, bot.stop)
        except NotImplementedError:
            pass
    await bot.run()


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
