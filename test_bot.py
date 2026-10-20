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


def test_upload_includes_real_geometry_and_thumbnail(monkeypatch, tmp_path):
    import json
    video = tmp_path / 'clip.mp4'
    thumb = tmp_path / 'thumb.jpg'
    video.write_bytes(b'video')
    thumb.write_bytes(b'thumb')
    payload = {'files': [str(video)], 'metadata': {str(video): {
        'width': 360, 'height': 640, 'duration': 43, 'thumbnail': str(thumb)}}}
    proc = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(json.dumps(payload).encode(), None)))
    monkeypatch.setattr(bot.asyncio, 'create_subprocess_exec', AsyncMock(return_value=proc))
    status = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
    message = SimpleNamespace(reply_text=AsyncMock(return_value=status), reply_video=AsyncMock())
    async def scenario():
        monkeypatch.setattr(bot, 'slots', asyncio.Semaphore(2))
        await bot.process_download(message, 123, 'https://youtu.be/abc', 720)
    asyncio.run(scenario())
    params = message.reply_video.call_args.kwargs
    assert (params['width'], params['height'], params['duration']) == (360, 640, 43)
    assert params['thumbnail'].name == str(thumb)
    assert 123 not in bot.active
