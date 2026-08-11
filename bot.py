import asyncio
import csv
import io
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, Message
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "subscribers.db")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ---------- Database ----------

def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                joined_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def add_subscriber(chat_id: int, username: str | None, full_name: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO subscribers (chat_id, username, full_name) VALUES (?, ?, ?)",
            (chat_id, username, full_name),
        )
        conn.commit()


def remove_subscriber(chat_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
        conn.commit()


def get_all_subscribers() -> list[int]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        rows = conn.execute("SELECT chat_id FROM subscribers").fetchall()
    return [row[0] for row in rows]


def count_subscribers() -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]


def get_all_subscribers_full() -> list[tuple]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return conn.execute(
            "SELECT chat_id, username, full_name, joined_at FROM subscribers ORDER BY joined_at"
        ).fetchall()


# ---------- Client-facing handlers ----------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    add_subscriber(
        message.chat.id,
        message.from_user.username,
        message.from_user.full_name,
    )
    await message.answer(
        "Вы подписались на рассылку о поступлениях.\n"
        "Здесь будут появляться уведомления о новых поступлениях товара.\n\n"
        "Чтобы отписаться в любой момент — отправьте /stop."
    )
    logger.info(f"New subscriber: {message.chat.id} ({message.from_user.full_name})")


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    remove_subscriber(message.chat.id)
    await message.answer("Вы отписались от рассылки. Чтобы вернуться — отправьте /start.")


# ---------- Admin-facing handlers ----------

def is_admin(message: Message) -> bool:
    return message.from_user.id == ADMIN_ID


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message):
        return
    await message.answer(f"Подписчиков: {count_subscribers()}")


@dp.message(Command("export"))
async def cmd_export(message: Message):
    if not is_admin(message):
        return

    rows = get_all_subscribers_full()
    if not rows:
        await message.answer("Пока нет ни одного подписчика.")
        return

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["chat_id", "username", "full_name", "joined_at"])
    writer.writerows(rows)

    file_bytes = buffer.getvalue().encode("utf-8-sig")  # BOM for correct Excel display
    filename = f"subscribers_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"

    await message.answer_document(
        BufferedInputFile(file_bytes, filename=filename),
        caption=f"Бэкап подписчиков: {len(rows)} записей",
    )


async def broadcast_message(source_message: Message):
    subscribers = get_all_subscribers()
    sent, failed = 0, 0

    status = await bot.send_message(ADMIN_ID, f"Рассылка запущена: {len(subscribers)} получателей...")

    for chat_id in subscribers:
        try:
            await source_message.copy_to(chat_id)
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Failed to send to {chat_id}: {e}")
            # Client blocked the bot or deleted account — clean up
            if "bot was blocked" in str(e).lower() or "chat not found" in str(e).lower():
                remove_subscriber(chat_id)
        await asyncio.sleep(0.05)  # ~20 messages/sec, safely under Telegram limits

    await bot.edit_message_text(
        f"Рассылка завершена.\nОтправлено: {sent}\nНе доставлено: {failed}",
        chat_id=ADMIN_ID,
        message_id=status.message_id,
    )


# This bot exists only to broadcast — so any message the admin sends to it
# (that isn't a command) is treated as content to send to all subscribers.
# Works for text, photos with captions, documents, etc.
@dp.message(F.chat.id == ADMIN_ID)
async def admin_message_as_broadcast(message: Message):
    if message.text and message.text.startswith("/"):
        return
    await broadcast_message(message)


# ---------- HTTP health-check (for Render + UptimeRobot) ----------
# The bot itself talks to Telegram via polling and has no URL of its own.
# This tiny web server exists only so Render can bind a port (required for
# its free Web Service tier) and so UptimeRobot has something to ping to
# keep the service from spinning down.

from aiohttp import web


async def health_check(request):
    return web.Response(text="OK")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health-check server listening on port {port}")


async def main():
    init_db()
    logger.info("Bot starting...")
    await start_web_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
