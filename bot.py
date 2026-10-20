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

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
log = logging.getLogger(__name__)
active = set()
slots = asyncio.Semaphore(2)
allowed = {int(x.strip()) for x in os.getenv('ALLOWED_USER_IDS', '').split(',') if x.strip()}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        'Пришли ссылку на видео YouTube (включая Shorts), Instagram Reels/публикацию или Threads.\n'
        'Скачаю и пришлю видео сюда. До 30 минут, до 5 видео из публикации. '
        'Большие файлы сжимаю. Закрытые публикации могут быть недоступны.\n'
        'Скачивай материалы, которые тебе разрешено сохранять. Твой ID: ' + str(update.effective_user.id))


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
    try:
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
                await status.delete()
    except asyncio.TimeoutError:
        if status:
            await status.edit_text('Обработка заняла слишком много времени. Попробуй короткое видео или другую ссылку.')
    except Exception as exc:
        log.warning('Request failed: %s', type(exc).__name__)
        if status:
            await status.edit_text('Не удалось обработать или отправить видео. Попробуй позже.')
    finally:
        active.discard(user)


async def error_handler(update, context):
    log.warning('Telegram update failed: %s', type(context.error).__name__)


def main():
    token = os.environ.get('BOT_TOKEN')
    if not token:
        raise SystemExit('Set BOT_TOKEN in .env')
    # A forced restart cannot run TemporaryDirectory's normal cleanup.
    # This service owns these temporary folders and runs as a single instance.
    if os.name == 'posix':
        root = Path(tempfile.gettempdir()).resolve()
        for folder in root.glob('tg-video-*'):
            if folder.is_dir() and not folder.is_symlink() and folder.resolve().parent == root:
                shutil.rmtree(folder)
    app = Application.builder().token(token).concurrent_updates(8).build()
    app.add_handler(CommandHandler(['start', 'help'], start))
    app.add_handler(CallbackQueryHandler(choose_quality, pattern=r'^q:\d+:[0-9a-f]{12}:(360|720|1080)$'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.add_error_handler(error_handler)
    app.run_polling(drop_pending_updates=False)


if __name__ == '__main__':
    main()
