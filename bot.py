import asyncio
import json
import logging
import os
import re
import signal
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from downloader import validate_url

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
                    sys.executable, str(Path(__file__).with_name('downloader.py')), url, folder,
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
                    with open(file, 'rb') as video:
                        await message.reply_video(video, supports_streaming=True,
                                                  read_timeout=180, write_timeout=180)
                await status.delete()
    except asyncio.TimeoutError:
        if status:
            await status.edit_text('Не удалось обработать видео за 5 минут. Попробуй короткое видео или другую ссылку.')
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
    app = Application.builder().token(token).concurrent_updates(8).build()
    app.add_handler(CommandHandler(['start', 'help'], start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.add_error_handler(error_handler)
    app.run_polling(drop_pending_updates=False)


if __name__ == '__main__':
    main()
