import asyncio
import csv
import io
import logging
import os
from datetime import datetime

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
ORDER_URL = "https://b2b.moysklad.ru/public/9p421RcbdoLa"
WHITE_PRICE_URL = "https://b2b.moysklad.ru/public/NgO26OdrxmZh"
GENERAL_PRICE_URL = "https://b2b.moysklad.ru/public/9p421RcbdoLa"
MANAGER_USERNAME = "vv_vape"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Postgres connection pool, created once in main() before polling starts.
pool: asyncpg.Pool | None = None

# In-memory admin state: which segment the admin is about to broadcast to.
# Reset after each broadcast or with /cancel. Resets to None on service restart.
admin_broadcast_target: str | None = None

# Holds a broadcast waiting on admin confirmation after preview.
# {'message': Message, 'target': str} or None.
pending_broadcast: dict | None = None


# ---------- Database (Supabase Postgres via asyncpg) ----------

async def init_db():
    global pool
    pool = await asyncpg.create_pool(dsn=DATABASE_URL, ssl="require", min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id BIGINT PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                joined_at TIMESTAMPTZ DEFAULT NOW(),
                segment_white BOOLEAN DEFAULT FALSE,
                segment_general BOOLEAN DEFAULT FALSE,
                blocked BOOLEAN DEFAULT FALSE
            )
            """
        )


async def add_subscriber(chat_id: int, username: str | None, full_name: str):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO subscribers (chat_id, username, full_name) VALUES ($1, $2, $3) "
            "ON CONFLICT (chat_id) DO NOTHING",
            chat_id, username, full_name,
        )


async def remove_subscriber(chat_id: int):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM subscribers WHERE chat_id = $1", chat_id)


async def set_segment(chat_id: int, segment: str, value: bool):
    column = "segment_white" if segment == "white" else "segment_general"
    async with pool.acquire() as conn:
        await conn.execute(f"UPDATE subscribers SET {column} = $1 WHERE chat_id = $2", value, chat_id)


async def get_segments(chat_id: int) -> tuple[bool, bool]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT segment_white, segment_general FROM subscribers WHERE chat_id = $1", chat_id
        )
    if not row:
        return False, False
    return bool(row["segment_white"]), bool(row["segment_general"])


async def set_blocked(chat_id: int, blocked: bool) -> bool:
    async with pool.acquire() as conn:
        result = await conn.execute("UPDATE subscribers SET blocked = $1 WHERE chat_id = $2", blocked, chat_id)
    return result.endswith(" 1")  # "UPDATE 1" means one row matched


async def get_subscribers_by_segment(segment: str) -> list[int]:
    column = "segment_white" if segment == "white" else "segment_general"
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT chat_id FROM subscribers WHERE {column} = TRUE AND blocked = FALSE")
    return [row["chat_id"] for row in rows]


async def get_all_subscribers() -> list[int]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT chat_id FROM subscribers WHERE blocked = FALSE")
    return [row["chat_id"] for row in rows]


async def count_subscribers() -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM subscribers WHERE blocked = FALSE")


async def get_all_subscribers_full() -> list[tuple]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT chat_id, username, full_name, joined_at, segment_white, segment_general, blocked "
            "FROM subscribers ORDER BY joined_at"
        )
    return [tuple(row) for row in rows]


# ---------- Client-facing handlers ----------

def segment_keyboard(white: bool, general: bool) -> InlineKeyboardMarkup:
    white_label = ("✅ " if white else "⬜ ") + "Белый прайс"
    general_label = ("✅ " if general else "⬜ ") + "Общий прайс"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=white_label, callback_data="toggle_white")],
            [InlineKeyboardButton(text=general_label, callback_data="toggle_general")],
            [InlineKeyboardButton(text="Готово ✅", callback_data="confirm_segments")],
        ]
    )


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await add_subscriber(
        message.chat.id,
        message.from_user.username,
        message.from_user.full_name,
    )
    await set_blocked(message.chat.id, False)

    welcome_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="ОФОРМИТЬ ЗАКАЗ", url=ORDER_URL)],
            [InlineKeyboardButton(text="Написать менеджеру", url=f"https://t.me/{MANAGER_USERNAME}")],
        ]
    )
    await message.answer(
        "Вы подписались на рассылку о поступлениях.\n"
        "Здесь будут появляться уведомления о новых поступлениях товара.\n\n"
        f"Связаться с менеджером: @{MANAGER_USERNAME}\n\n"
        "Чтобы отписаться в любой момент — отправьте /stop.",
        reply_markup=welcome_keyboard,
    )

    white, general = await get_segments(message.chat.id)
    await message.answer(
        "Выберите, какие прайсы вас интересуют (можно выбрать оба):",
        reply_markup=segment_keyboard(white, general),
    )
    logger.info(f"New subscriber: {message.chat.id} ({message.from_user.full_name})")


@dp.callback_query(F.data.in_(["toggle_white", "toggle_general"]))
async def cb_toggle_segment(callback: CallbackQuery):
    segment = "white" if callback.data == "toggle_white" else "general"
    white, general = await get_segments(callback.message.chat.id)
    current = white if segment == "white" else general
    await set_segment(callback.message.chat.id, segment, not current)
    white, general = await get_segments(callback.message.chat.id)
    await callback.message.edit_reply_markup(reply_markup=segment_keyboard(white, general))
    await callback.answer()


@dp.callback_query(F.data == "confirm_segments")
async def cb_confirm_segments(callback: CallbackQuery):
    white, general = await get_segments(callback.message.chat.id)
    if not white and not general:
        await callback.answer("Выберите хотя бы один прайс", show_alert=True)
        return

    buttons = []
    if white:
        buttons.append([InlineKeyboardButton(text="Открыть белый прайс", url=WHITE_PRICE_URL)])
    if general:
        buttons.append([InlineKeyboardButton(text="Открыть общий прайс", url=GENERAL_PRICE_URL)])

    await callback.message.edit_text(
        "Подписка сохранена. Каталоги:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer("Готово")


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    await remove_subscriber(message.chat.id)
    await message.answer("Вы отписались от рассылки. Чтобы вернуться — отправьте /start.")


# ---------- Admin-facing handlers ----------

def is_admin(message: Message) -> bool:
    return message.from_user.id == ADMIN_ID


@dp.message(Command("help"))
async def cmd_help(message: Message):
    if is_admin(message):
        await message.answer(
            "Команды администратора:\n"
            "/white — рассылка по Белому прайсу (уходит всем подписчикам)\n"
            "/common — рассылка по Общему прайсу (только тем, кто выбрал его)\n"
            "/all — рассылка абсолютно всем подписчикам\n"
            "/stats — статистика подписчиков\n"
            "/export — выгрузить базу подписчиков в CSV\n"
            "/block <chat_id> — заблокировать подписчика\n"
            "/unblock <chat_id> — разблокировать подписчика\n"
            "/cancel — отменить текущее действие (ввод рассылки или ожидание подтверждения)"
        )
    else:
        await message.answer(
            "/start — подписаться и выбрать тип рассылки\n"
            "/stop — отписаться от рассылки"
        )


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message):
        return
    general_count = len(await get_subscribers_by_segment("general"))
    total = await count_subscribers()
    await message.answer(
        f"Всего подписчиков: {total}\n"
        f"— Из них «Общий прайс»: {general_count}\n"
        f"(«Белый прайс» уходит всем подписчикам без исключений)"
    )


@dp.message(Command("export"))
async def cmd_export(message: Message):
    if not is_admin(message):
        return

    rows = await get_all_subscribers_full()
    if not rows:
        await message.answer("Пока нет ни одного подписчика.")
        return

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["chat_id", "username", "full_name", "joined_at", "segment_white", "segment_general", "blocked"]
    )
    writer.writerows(rows)

    file_bytes = buffer.getvalue().encode("utf-8-sig")
    filename = f"subscribers_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"

    await message.answer_document(
        BufferedInputFile(file_bytes, filename=filename),
        caption=f"Бэкап подписчиков: {len(rows)} записей",
    )


@dp.message(Command("block"))
async def cmd_block(message: Message):
    if not is_admin(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Использование: /block <chat_id>\nchat_id можно взять из /export")
        return
    chat_id = int(parts[1])
    if await set_blocked(chat_id, True):
        await message.answer(f"Пользователь {chat_id} исключён из рассылок.")
    else:
        await message.answer("Такого подписчика не найдено.")


@dp.message(Command("unblock"))
async def cmd_unblock(message: Message):
    if not is_admin(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Использование: /unblock <chat_id>")
        return
    chat_id = int(parts[1])
    if await set_blocked(chat_id, False):
        await message.answer(f"Пользователь {chat_id} снова получает рассылки.")
    else:
        await message.answer("Такого подписчика не найдено.")


@dp.message(Command("common"))
async def cmd_broadcast_general(message: Message):
    if not is_admin(message):
        return
    global admin_broadcast_target
    admin_broadcast_target = "general"
    count = len(await get_subscribers_by_segment("general"))
    await message.answer(
        f"Жду сообщение для рассылки по «Общему прайсу» ({count} получателей).\n"
        "Пришлите текст, фото или документ следующим сообщением. Отменить — /cancel"
    )


@dp.message(Command("white"))
async def cmd_broadcast_white(message: Message):
    if not is_admin(message):
        return
    global admin_broadcast_target
    admin_broadcast_target = "white"
    count = await count_subscribers()
    await message.answer(
        f"Жду сообщение для рассылки по «Белому прайсу» — уйдёт ВСЕМ подписчикам ({count} получателей).\n"
        "Пришлите текст, фото или документ следующим сообщением. Отменить — /cancel"
    )


@dp.message(Command("all"))
async def cmd_broadcast_all(message: Message):
    if not is_admin(message):
        return
    global admin_broadcast_target
    admin_broadcast_target = "all"
    count = await count_subscribers()
    await message.answer(
        f"Жду сообщение для рассылки всем подписчикам ({count} получателей).\n"
        "Пришлите текст, фото или документ следующим сообщением. Отменить — /cancel"
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    if not is_admin(message):
        return
    global admin_broadcast_target, pending_broadcast
    admin_broadcast_target = None
    pending_broadcast = None
    await message.answer("Отменено.")


async def broadcast_message(source_message: Message, chat_ids: list[int]):
    sent, failed = 0, 0
    status = await bot.send_message(ADMIN_ID, f"Рассылка запущена: {len(chat_ids)} получателей...")

    for chat_id in chat_ids:
        try:
            await source_message.copy_to(chat_id)
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Failed to send to {chat_id}: {e}")
            if "bot was blocked" in str(e).lower() or "chat not found" in str(e).lower():
                await remove_subscriber(chat_id)
        await asyncio.sleep(0.05)  # ~20 messages/sec, safely under Telegram limits

    await bot.edit_message_text(
        f"Рассылка завершена.\nОтправлено: {sent}\nНе доставлено: {failed}",
        chat_id=ADMIN_ID,
        message_id=status.message_id,
    )


# This bot exists only to broadcast — so, once the admin has picked a target
# with /common, /white, or /all, the very next non-command message they send
# is treated as the content to broadcast. Instead of sending immediately, it's
# shown back to the admin as a preview with Confirm/Cancel buttons.
@dp.message(F.chat.id == ADMIN_ID)
async def admin_message_router(message: Message):
    global admin_broadcast_target, pending_broadcast

    if message.text and message.text.startswith("/"):
        return

    if admin_broadcast_target is None:
        await message.answer(
            "Сначала выберите, кому рассылаем:\n"
            "/common — подписчикам общего прайса\n"
            "/white — всем подписчикам (белый прайс)\n"
            "/all — всем подписчикам"
        )
        return

    target = admin_broadcast_target
    admin_broadcast_target = None
    pending_broadcast = {"message": message, "target": target}

    if target == "all":
        count = await count_subscribers()
        label = "ВСЕМ подписчикам"
    elif target == "white":
        count = await count_subscribers()
        label = "Белому прайсу (всем подписчикам)"
    else:
        count = len(await get_subscribers_by_segment("general"))
        label = "Общему прайсу"

    await message.answer("👀 Превью — так сообщение увидят получатели:")
    await message.copy_to(ADMIN_ID)
    await message.answer(
        f"Разослать по «{label}»? Получателей: {count}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Отправить", callback_data="confirm_broadcast"),
                    InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_broadcast"),
                ]
            ]
        ),
    )


@dp.callback_query(F.data == "confirm_broadcast")
async def cb_confirm_broadcast(callback: CallbackQuery):
    global pending_broadcast
    if callback.from_user.id != ADMIN_ID:
        return
    if pending_broadcast is None:
        await callback.answer("Нечего отправлять — начните заново с /white, /common или /all", show_alert=True)
        return

    target = pending_broadcast["target"]
    source_message = pending_broadcast["message"]
    pending_broadcast = None

    if target in ("all", "white"):
        chat_ids = await get_all_subscribers()
    else:
        chat_ids = await get_subscribers_by_segment("general")

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Рассылка запущена")
    await broadcast_message(source_message, chat_ids)


@dp.callback_query(F.data == "cancel_broadcast")
async def cb_cancel_broadcast(callback: CallbackQuery):
    global pending_broadcast
    if callback.from_user.id != ADMIN_ID:
        return
    pending_broadcast = None
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Отменено")


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
    await init_db()
    logger.info("Bot starting...")
    await start_web_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
