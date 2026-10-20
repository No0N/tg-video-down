import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from stats import Stats
import bot


def test_persistence_and_platform_totals(tmp_path):
    path = tmp_path / 'stats.sqlite3'
    store = Stats(path)
    store.touch(123, 'Test Name', 'tester')
    first = store.begin(123, 'https://youtu.be/abc', 1080)
    store.sent(first)
    store.finish(first, 'success')
    other = store.begin(123, 'https://instagram.com/reel/abc', 720)
    store.finish(other, 'error')
    store = Stats(path)
    overview = store.overview()
    assert 'Запросов всего: 2' in overview
    assert 'Пользователей: 1' in overview
    assert 'Успешно: 1' in overview
    assert 'Отправлено видео: 1' in overview
    text, _, _ = store.listing('recent', user_id=123)
    assert 'Test Name @tester' in text
    assert 'до 1080p' in text


def test_partial_and_restart_recovery(tmp_path):
    store = Stats(tmp_path / 'stats.sqlite3')
    first = store.begin(1, 'https://threads.com/@test/post/abc', 720)
    store.sent(first)
    store.finish(first, 'error')
    store.begin(2, 'https://youtu.be/abc', 360)
    store.recover()
    with store.connect() as db:
        assert [r['status'] for r in db.execute('SELECT status FROM requests ORDER BY id')] == ['partial', 'interrupted']


def test_pagination_and_user_filter(tmp_path):
    store = Stats(tmp_path / 'stats.sqlite3')
    for i in range(23):
        store.begin(1 if i % 2 else 2, 'https://youtu.be/abc', 720)
    text, page, pages = store.listing('recent', 999)
    assert (page, pages) == (3, 3)
    assert '#1 ·' in text
    text, _, _ = store.listing('recent', user_id=1)
    assert 'Всего: 11' in text
    assert '\n2 ·' not in text


def test_day_and_week_boundaries(tmp_path):
    from datetime import datetime
    from stats import MOSCOW
    store = Stats(tmp_path / 'stats.sqlite3')
    old = store.begin(1, 'https://youtu.be/old', 720)
    yesterday = store.begin(1, 'https://youtu.be/yesterday', 720)
    store.begin(1, 'https://youtu.be/today', 720)
    midnight = int(datetime.now(MOSCOW).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    with store.connect() as db:
        db.execute('UPDATE requests SET started=? WHERE id=?', (int(time.time())-8*86400, old))
        db.execute('UPDATE requests SET started=? WHERE id=?', (midnight-1, yesterday))
    overview = store.overview()
    assert 'Сегодня: 1' in overview
    assert 'За последние 7 дней: 2' in overview


def test_stats_denied_to_other_users_and_groups(monkeypatch):
    monkeypatch.setattr(bot, 'admin_id', 139640926)
    for user_id, chat_type in [(123, 'private'), (139640926, 'supergroup')]:
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=user_id),
                                 effective_chat=SimpleNamespace(type=chat_type), effective_message=message)
        asyncio.run(bot.show_stats(update, SimpleNamespace(args=[])))
        assert 'только владельцу' in message.reply_text.call_args.args[0]


def test_stats_admin_can_view_and_callback_denies_others(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, 'admin_id', 139640926)
    monkeypatch.setattr(bot, 'stats_store', Stats(tmp_path / 'stats.sqlite3'))
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=139640926),
                             effective_chat=SimpleNamespace(type='private'), effective_message=message)
    asyncio.run(bot.show_stats(update, SimpleNamespace(args=[])))
    assert 'Статистика бота' in message.reply_text.call_args.args[0]
    query = SimpleNamespace(from_user=SimpleNamespace(id=123), answer=AsyncMock(),
                            message=SimpleNamespace(chat=SimpleNamespace(type='private')))
    asyncio.run(bot.stats_callback(SimpleNamespace(callback_query=query), None))
    assert query.answer.call_args.kwargs['show_alert']


def test_download_records_success(monkeypatch, tmp_path):
    # Reuse the upload regression test with a real statistics database.
    from test_bot import test_upload_includes_real_geometry_and_thumbnail
    store = Stats(tmp_path / 'stats.sqlite3')
    monkeypatch.setattr(bot, 'stats_store', store)
    test_upload_includes_real_geometry_and_thumbnail(monkeypatch, tmp_path)
    with store.connect() as db:
        row = db.execute('SELECT * FROM requests').fetchone()
    assert (row['user_id'], row['status'], row['videos']) == (123, 'success', 1)
