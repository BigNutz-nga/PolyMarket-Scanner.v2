from pathlib import Path

import pytest

from bot import BotSettings, ScannerBot


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


@pytest.mark.asyncio
async def test_chat_allowlist_and_chatid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "123")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = BotSettings.from_env()
    bot = ScannerBot(settings)
    await bot.telegram.close()
    fake = FakeTelegram()
    bot.telegram = fake  # type: ignore[assignment]

    await bot.handle_command(999, "/chatid")
    await bot.handle_command(999, "/latest")
    await bot.handle_command(123, "/start")

    assert fake.messages[0] == (999, "Chat ID: 999")
    assert "Access denied" in fake.messages[1][1]
    assert "Polymarket-Scanner is ready" in fake.messages[2][1]
