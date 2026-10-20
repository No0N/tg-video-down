"""Persistent usage metadata. Media and source URLs are never stored."""
import math
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager
from urllib.parse import urlsplit

MOSCOW = timezone(timedelta(hours=3))
PAGE_SIZE = 10
LABELS = {'pending': 'в обработке', 'success': 'успешно', 'error': 'ошибка',
          'timeout': 'таймаут', 'partial': 'частично отправлено', 'interrupted': 'прервано перезапуском'}


def stamp(value):
    return datetime.fromtimestamp(value, MOSCOW).strftime('%d.%m.%Y %H:%M')


def identity(row):
    name = ' '.join((row['name'] or '').split())[:60]
    username = f" @{row['username']}" if row['username'] else ''
    return f"{row['user_id']} · {name}{username}"


class Stats:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '', username TEXT,
                    first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(user_id),
                    platform TEXT NOT NULL, quality INTEGER NOT NULL, started INTEGER NOT NULL,
                    finished INTEGER, status TEXT NOT NULL DEFAULT 'pending', videos INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS requests_user ON requests(user_id, id DESC);
                CREATE INDEX IF NOT EXISTS requests_started ON requests(started);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def touch(self, user_id, name='', username=None):
        now = int(time.time())
        with self.connect() as db:
            db.execute('''INSERT INTO users VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, username=excluded.username,
                last_seen=excluded.last_seen''', (user_id, name, username, now, now))

    def begin(self, user_id, url, quality):
        host = urlsplit(url).hostname
        platform = 'YouTube' if 'youtu' in host else 'Instagram' if 'instagram' in host else 'Threads'
        now = int(time.time())
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO users(user_id,first_seen,last_seen) VALUES (?,?,?)', (user_id, now, now))
            return db.execute('INSERT INTO requests(user_id,platform,quality,started) VALUES (?,?,?,?)',
                              (user_id, platform, quality, now)).lastrowid

    def sent(self, request_id):
        with self.connect() as db:
            db.execute('UPDATE requests SET videos=videos+1 WHERE id=?', (request_id,))

    def finish(self, request_id, status):
        with self.connect() as db:
            db.execute('''UPDATE requests SET status=CASE WHEN videos>0 AND ?!='success' THEN 'partial' ELSE ? END,
                       finished=? WHERE id=?''', (status, status, int(time.time()), request_id))

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE requests SET status=CASE WHEN videos>0 THEN 'partial' ELSE 'interrupted' END, finished=? WHERE status='pending'",
                       (int(time.time()),))

    def overview(self):
        now = datetime.now(MOSCOW)
        midnight = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        week = int(now.timestamp()) - 7 * 86400
        with self.connect() as db:
            users = db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
            row = db.execute('''SELECT COUNT(*) total, COUNT(DISTINCT user_id) downloaders,
                COALESCE(SUM(started>=?),0) today, COALESCE(SUM(started>=?),0) week,
                COALESCE(SUM(status='success'),0) success, COALESCE(SUM(status='pending'),0) pending,
                COALESCE(SUM(status NOT IN ('success','pending')),0) errors,
                COALESCE(SUM(videos),0) videos, MIN(started) first FROM requests''', (midnight, week)).fetchone()
            platforms = db.execute('SELECT platform,COUNT(*) n FROM requests GROUP BY platform ORDER BY platform').fetchall()
        return (f"Статистика бота · время МСК\n\nПользователей: {users}\nЗапрашивали скачивание: {row['downloaders']}\n"
                f"Запросов всего: {row['total']}\nСегодня: {row['today']}\nЗа последние 7 дней: {row['week']}\n"
                f"Успешно: {row['success']}\nОшибок/прерванных/частичных: {row['errors']}\n"
                f"В обработке: {row['pending']}\nОтправлено видео: {row['videos']}\n\n"
                + '\n'.join(f"{p['platform']}: {p['n']}" for p in platforms)
                + (f"\nПервый запрос: {stamp(row['first'])}" if row['first'] else '\nЗапросов пока нет.')
                + '\n\nУчитываются принятые загрузки; YouTube — после выбора качества. История до обновления недоступна.')

    def listing(self, kind, page=1, user_id=None):
        page = max(1, page)
        with self.connect() as db:
            if kind == 'users':
                total = db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
                pages = max(1, math.ceil(total / PAGE_SIZE))
                page = min(page, pages)
                rows = db.execute('''SELECT u.*,COUNT(r.id) n,COALESCE(SUM(r.videos),0) videos,
                    SUM(r.status='success') success FROM users u LEFT JOIN requests r USING(user_id)
                    GROUP BY u.user_id ORDER BY u.last_seen DESC,u.user_id LIMIT ? OFFSET ?''',
                                  (PAGE_SIZE, (page-1)*PAGE_SIZE)).fetchall()
                lines = ['Пользователи · время МСК']
                for row in rows:
                    lines.append(f"{identity(row)}\nЗапросов: {row['n']}, успешно: {row['success'] or 0}, видео: {row['videos']}\n"
                                 f"Первый визит: {stamp(row['first_seen'])}; последний: {stamp(row['last_seen'])}")
            else:
                where = 'WHERE r.user_id=?' if user_id is not None else ''
                args = (user_id,) if user_id is not None else ()
                total = db.execute(f'SELECT COUNT(*) FROM requests r {where}', args).fetchone()[0]
                pages = max(1, math.ceil(total / PAGE_SIZE))
                page = min(page, pages)
                rows = db.execute(f'''SELECT r.*,u.name,u.username FROM requests r JOIN users u USING(user_id)
                    {where} ORDER BY r.id DESC LIMIT ? OFFSET ?''', (*args, PAGE_SIZE, (page-1)*PAGE_SIZE)).fetchall()
                lines = [f"История {'пользователя ' + str(user_id) if user_id is not None else 'запросов'} · время МСК"]
                if user_id is not None:
                    summary = db.execute('''SELECT COUNT(*) n,COALESCE(SUM(status='success'),0) success,
                        COALESCE(SUM(videos),0) videos FROM requests WHERE user_id=?''', (user_id,)).fetchone()
                    lines.append(f"Всего: {summary['n']}, успешно: {summary['success']}, видео: {summary['videos']}")
                for row in rows:
                    quality = f" · до {row['quality']}p" if row['platform'] == 'YouTube' else ''
                    lines.append(f"#{row['id']} · {stamp(row['started'])}\n{identity(row)}\n"
                                 f"{row['platform']}{quality} · {LABELS[row['status']]} · видео: {row['videos']}")
            if not rows:
                lines.append('Данных пока нет.')
            lines.append(f'Страница {page}/{pages}')
        return '\n\n'.join(lines), page, pages
