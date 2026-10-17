import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot


def query(user=123, owner=123):
    return SimpleNamespace(from_user=SimpleNamespace(id=user), data=f'q:{owner}:abcdef123456:1080',
                           message=SimpleNamespace(chat_id=456), answer=AsyncMock(), edit_message_text=AsyncMock())


def test_other_user_cannot_download(monkeypatch):
    worker = AsyncMock()
    monkeypatch.setattr(bot, 'process_download', worker)
    q = query(user=999)
    asyncio.run(bot.choose_quality(SimpleNamespace(callback_query=q), SimpleNamespace(user_data={})))
    worker.assert_not_awaited()
    assert q.answer.call_args.kwargs['show_alert']


def test_expired_selection_does_not_download(monkeypatch):
    worker = AsyncMock()
    monkeypatch.setattr(bot, 'process_download', worker)
    q = query()
    context = SimpleNamespace(user_data={'quality_request': ('abcdef123456', 'https://youtu.be/abc', time.monotonic()-901, 456)})
    asyncio.run(bot.choose_quality(SimpleNamespace(callback_query=q), context))
    worker.assert_not_awaited()


def test_quality_download_is_consumed_once(monkeypatch):
    worker = AsyncMock()
    monkeypatch.setattr(bot, 'process_download', worker)
    monkeypatch.setattr(bot, 'allowed', set())
    q = query()
    context = SimpleNamespace(user_data={'quality_request': ('abcdef123456', 'https://youtu.be/abc', time.monotonic(), 456)})
    async def scenario():
        await bot.choose_quality(SimpleNamespace(callback_query=q), context)
        await bot.choose_quality(SimpleNamespace(callback_query=q), context)
    asyncio.run(scenario())
    worker.assert_awaited_once_with(q.message, 123, 'https://youtu.be/abc', 1080)
