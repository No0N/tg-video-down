import asyncio
import json
import logging
import os
import re
import signal
import sys
import tempfile
import secrets
import time
import shutil
from contextlib import ExitStack
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from downloader import validate_url
from urllib.parse import urlsplit
from stats import Stats

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
log = logging.getLogger(__name__)
active = set()
slots = asyncio.Semaphore(2)
allowed = {int(x.strip()) for x in os.getenv('ALLOWED_USER_IDS', '').split(',') if x.strip()}
admin_id = int(os.getenv('ADMIN_USER_ID', '0'))
stats_store = None


def remember(user):
    if stats_store:
        stats_store.touch(user.id, user.full_name, user.username)


def stats_page(kind='overview', page=1, user_id=None):
    if kind == 'overview':
        text = stats_store.overview()
        page, pages = 1, 1
    else:
        text, page, pages = stats_store.listing(kind, page, user_id)
    target = f'user:{user_id}' if user_id is not None else kind
    buttons = []
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton('← Назад', callback_data=f's:{target}:{page-1}'))
    if page < pages:
        nav.append(InlineKeyboardButton('Далее →', callback_data=f's:{target}:{page+1}'))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton('Обзор', callback_data='s:overview:1'),
                    InlineKeyboardButton('Пользователи', callback_data='s:users:1'),
                    InlineKeyboardButton('История', callback_data='s:recent:1')])
    if kind == 'users':
        text += '\n\nИстория человека: /stats ID (например, /stats 139640926)'
    return text, InlineKeyboardMarkup(buttons)


async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != admin_id or update.effective_chat.type != 'private':
        await update.effective_message.reply_text('Статистика доступна только владельцу в личном чате с ботом.')
        return
    args = context.args
    kind, page, user_id = 'overview', 1, None
    try:
        if args:
            if args[0] in ('users', 'recent'):
                kind = args[0]
            else:
                user_id = int(args[0])
                if not 0 < user_id < 2**63:
                    raise ValueError()
                kind = 'recent'
            if len(args) > 1:
                page = int(args[1])
            if len(args) > 2:
                raise ValueError()
    except ValueError:
        await update.effective_message.reply_text('Команды: /stats, /stats users, /stats recent, /stats ID. Для списков можно дописать номер страницы.')
        return
    text, keyboard = stats_page(kind, page, user_id)
    await update.effective_message.reply_text(text, reply_markup=keyboard)


async def stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != admin_id or query.message.chat.type != 'private':
        await query.answer('Доступ только владельцу в личном чате.', show_alert=True)
        return
    parts = query.data.split(':')
    kind = parts[1]
    user_id = int(parts[2]) if kind == 'user' else None
    page = int(parts[-1])
    text, keyboard = stats_page('recent' if user_id else kind, page, user_id)
    await query.answer()
    # Telegram rejects edits whose text and keyboard have not changed.
    if query.message.text != text or query.message.reply_markup != keyboard:
        await query.edit_message_text(text, reply_markup=keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember(update.effective_user)
    await update.effective_message.reply_text(
        'Пришли ссылку на видео YouTube (включая Shorts), Instagram Reels/публикацию или Threads.\n'
        'Скачаю и пришлю видео сюда. До 30 минут, до 5 видео из публикации. '
        'Большие файлы сжимаю. Закрытые публикации могут быть недоступны.\n'
        'Скачивай материалы, которые тебе разрешено сохранять. Твой ID: ' + str(update.effective_user.id)
        + ('\nСтатистика владельца: /stats' if update.effective_user.id == admin_id else ''))


async def stop_worker(proc):
    if proc.returncode is None:
        try:
            if os.name == 'posix':
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.id
    message = update.effective_message
    if allowed and user not in allowed:
        await message.reply_text('Бот доступен только владельцу.')
        return
    remember(update.effective_user)
    urls = re.findall(r'https?://[^\s<>]+', message.text or '')
    if len(urls) != 1:
        await message.reply_text('Пришли одну ссылку на видео YouTube, Instagram или Threads.')
        return
    try:
        url = validate_url(urls[0].rstrip('.,!);]'))
    except ValueError as exc:
        await message.reply_text(str(exc))
        return
    if urlsplit(url).hostname in {'youtu.be', 'www.youtu.be', 'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'}:
        key = secrets.token_hex(6)
        context.user_data['quality_request'] = (key, url, time.monotonic(), message.chat_id)
        buttons = [[InlineKeyboardButton(f'{height}p', callback_data=f'q:{user}:{key}:{height}')
                    for height in (360, 720, 1080)]]
        await message.reply_text(
            'Выбери качество YouTube (до указанного разрешения). Если его нет, скачаю ближайшее ниже.\n'
            'Если файл превышает лимит Telegram, он будет сжат; качество может снизиться.\n'
            'Кнопки действуют 15 минут, до отправки следующей ссылки YouTube.',
            reply_markup=InlineKeyboardMarkup(buttons))
        return
    await process_download(message, user, url, 720)


async def choose_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, owner, key, height = query.data.split(':')
    if query.from_user.id != int(owner):
        await query.answer('Это выбор другого пользователя.', show_alert=True)
        return
    if allowed and query.from_user.id not in allowed:
        await query.answer('Бот доступен только владельцу.', show_alert=True)
        return
    pending = context.user_data.get('quality_request')
    if (not pending or pending[0] != key or time.monotonic() - pending[2] > 900
            or pending[3] != query.message.chat_id):
        await query.answer('Кнопки устарели. Пришли ссылку заново.', show_alert=True)
        return
    if query.from_user.id in active or len(active) >= 6:
        await query.answer('Подожди завершения текущих загрузок и нажми снова.', show_alert=True)
        return
    context.user_data.pop('quality_request')
    await query.answer()
    await query.edit_message_text(f'Выбрано качество до {height}p.')
    await process_download(query.message, query.from_user.id, pending[1], int(height))


async def process_download(message, user, url, height):
    if user in active:
        await message.reply_text('Твоё предыдущее видео ещё обрабатывается. Подожди немного.')
        return
    if len(active) >= 6:
        await message.reply_text('Сейчас много запросов. Попробуй через минуту.')
        return
    active.add(user)
    status = None
    request_id = None
    outcome = 'error'
    try:
        if stats_store:
            request_id = stats_store.begin(user, url, height)
        status = await message.reply_text('Ссылка принята. Скачиваю видео…')
        async with slots:
            with tempfile.TemporaryDirectory(prefix='tg-video-') as folder:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, str(Path(__file__).with_name('downloader.py')), url, folder, str(height),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=os.name == 'posix')
                try:
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=int(os.getenv('DOWNLOAD_TIMEOUT', '300')))
                finally:
                    await stop_worker(proc)
                result = json.loads(stdout)
                if result.get('error'):
                    await status.edit_text(result['error'])
                    return
                await status.edit_text('Видео готово, отправляю…')
                for file in result['files']:
                    metadata = result['metadata'][file]
                    with ExitStack() as stack:
                        video = stack.enter_context(open(file, 'rb'))
                        thumbnail = stack.enter_context(open(metadata['thumbnail'], 'rb'))
                        await message.reply_video(video, supports_streaming=True, thumbnail=thumbnail,
                                                  width=metadata['width'], height=metadata['height'],
                                                  duration=metadata['duration'],
                                                  read_timeout=180, write_timeout=180)
                    if request_id is not None:
                        stats_store.sent(request_id)
                outcome = 'success'
                await status.delete()
    except asyncio.TimeoutError:
        outcome = 'timeout'
        if status:
            await status.edit_text('Обработка заняла слишком много времени. Попробуй короткое видео или другую ссылку.')
    except Exception as exc:
        log.warning('Request failed: %s', type(exc).__name__)
        if status:
            await status.edit_text('Не удалось обработать или отправить видео. Попробуй позже.')
    finally:
        active.discard(user)
        if request_id is not None:
            stats_store.finish(request_id, outcome)


async def error_handler(update, context):
    log.warning('Telegram update failed: %s', type(context.error).__name__)


def main():
    global stats_store
    token = os.environ.get('BOT_TOKEN')
    if not token:
        raise SystemExit('Set BOT_TOKEN in .env')
    stats_store = Stats(os.getenv('STATS_DB_PATH', 'data/stats.sqlite3'))
    stats_store.recover()
    # A forced restart cannot run TemporaryDirectory's normal cleanup.
    # This service owns these temporary folders and runs as a single instance.
    if os.name == 'posix':
        root = Path(tempfile.gettempdir()).resolve()
        for folder in root.glob('tg-video-*'):
            if folder.is_dir() and not folder.is_symlink() and folder.resolve().parent == root:
                shutil.rmtree(folder)
    app = Application.builder().token(token).concurrent_updates(8).build()
    app.add_handler(CommandHandler(['start', 'help'], start))
    app.add_handler(CommandHandler('stats', show_stats))
    app.add_handler(CallbackQueryHandler(stats_callback, pattern=r'^s:(?:(?:overview|users|recent):\d{1,9}|user:[1-9]\d{0,17}:\d{1,9})$'))
    app.add_handler(CallbackQueryHandler(choose_quality, pattern=r'^q:\d+:[0-9a-f]{12}:(360|720|1080)$'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.add_error_handler(error_handler)
    app.run_polling(drop_pending_updates=False)


if __name__ == '__main__':
    main()
