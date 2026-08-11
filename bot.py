import asyncio
import csv
import io
import logging
import os
import signal
from datetime import datetime
from urllib.parse import quote_plus

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, 
    Message, CallbackQuery
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
import asyncpg
from aiohttp import web

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Параметры базы данных PostgreSQL (Supabase)
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "postgres")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

# Собираем URL с экранированием специальных символов
DATABASE_URL = f"postgresql://{DB_USER}:{quote_plus(DB_PASSWORD)}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# Ссылки на прайсы
WHITE_PRICE_URL = "https://b2b.moysklad.ru/public/NgO26OdrxmZh"
COMMON_PRICE_URL = "https://b2b.moysklad.ru/public/9p421RcbdoLa"
MANAGER_LINK = "https://t.me/vv_vape"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

db_pool = None


# ---------- Database functions ----------
async def init_db():
    """Инициализация базы данных"""
    global db_pool
    try:
        logger.info(f"Connecting to database at {DB_HOST}:{DB_PORT}/{DB_NAME}")
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        
        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id BIGINT PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    subscription_type TEXT DEFAULT 'common',
                    is_blocked INTEGER DEFAULT 0
                )
            """)
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization error: {e}")
        raise


async def add_subscriber(chat_id: int, username: str | None, full_name: str, sub_type: str = 'common'):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO subscribers (chat_id, username, full_name, subscription_type, is_blocked) 
            VALUES ($1, $2, $3, $4, 0)
            ON CONFLICT (chat_id) DO UPDATE SET 
                username = $2, 
                full_name = $3, 
                subscription_type = $4
        """, chat_id, username, full_name, sub_type)


async def update_subscription_type(chat_id: int, sub_type: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE subscribers SET subscription_type = $1 WHERE chat_id = $2",
            sub_type, chat_id
        )


async def remove_subscriber(chat_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM subscribers WHERE chat_id = $1",
            chat_id
        )


async def get_subscriber_type(chat_id: int) -> str | None:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT subscription_type FROM subscribers WHERE chat_id = $1",
            chat_id
        )
    return row['subscription_type'] if row else None


async def get_all_subscribers(sub_type: str = None) -> list[int]:
    async with db_pool.acquire() as conn:
        if sub_type and sub_type != 'all':
            rows = await conn.fetch(
                "SELECT chat_id FROM subscribers WHERE subscription_type = $1 AND is_blocked = 0",
                sub_type
            )
        else:
            rows = await conn.fetch(
                "SELECT chat_id FROM subscribers WHERE is_blocked = 0"
            )
    return [row['chat_id'] for row in rows]


async def count_subscribers(sub_type: str = None) -> int:
    async with db_pool.acquire() as conn:
        if sub_type and sub_type != 'all':
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM subscribers WHERE subscription_type = $1 AND is_blocked = 0",
                sub_type
            )
        else:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM subscribers WHERE is_blocked = 0"
            )
    return count


async def get_all_subscribers_full() -> list[dict]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT chat_id, username, full_name, joined_at, subscription_type FROM subscribers ORDER BY joined_at"
        )
    return [dict(row) for row in rows]


async def block_user(chat_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE subscribers SET is_blocked = 1 WHERE chat_id = $1",
            chat_id
        )


async def unblock_user(chat_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE subscribers SET is_blocked = 0 WHERE chat_id = $1",
            chat_id
        )


async def is_user_blocked(chat_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_blocked FROM subscribers WHERE chat_id = $1",
            chat_id
        )
    return row['is_blocked'] == 1 if row else False


# ---------- FSM States ----------
class BroadcastStates(StatesGroup):
    waiting_white_price = State()
    waiting_common_price = State()
    waiting_all_price = State()
    waiting_block_user = State()
    waiting_unblock_user = State()


# ---------- Client-facing handlers ----------
@dp.message(CommandStart())
async def cmd_start(message: Message):
    existing_type = await get_subscriber_type(message.chat.id)
    
    if existing_type:
        await show_subscription_menu(message, is_change=True)
    else:
        await show_subscription_menu(message, is_change=False)


async def show_subscription_menu(message: Message, is_change: bool = False):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤍 Белый прайс (только безнал)", callback_data="sub_white")],
            [InlineKeyboardButton(text="💳 Общий прайс (наличка + безнал)", callback_data="sub_common")],
            [InlineKeyboardButton(text="📞 Связаться с менеджером", url=MANAGER_LINK)]
        ]
    )
    
    text = (
        "👋 Добро пожаловать!\n\n"
        "Выберите тип подписки:\n\n"
        "🤍 **Белый прайс** — только для безналичных расчетов\n"
        "💳 **Общий прайс** — наличные + безналичные\n\n"
        "Вы всегда можете изменить тип подписки, отправив /start повторно."
    )
    
    if is_change:
        text = "🔄 **Изменить тип подписки**\n\n" + text
    
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("sub_"))
async def handle_subscription_choice(callback: CallbackQuery):
    sub_type = callback.data.split("_")[1]
    chat_id = callback.from_user.id
    
    await add_subscriber(
        chat_id,
        callback.from_user.username,
        callback.from_user.full_name,
        sub_type
    )
    
    if sub_type == "white":
        price_url = WHITE_PRICE_URL
        price_name = "Белый прайс"
    else:
        price_url = COMMON_PRICE_URL
        price_name = "Общий прайс"
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📄 Открыть прайс", url=price_url)],
            [InlineKeyboardButton(text="📞 Связаться с менеджером", url=MANAGER_LINK)]
        ]
    )
    
    await callback.message.edit_text(
        f"✅ Вы подписались на рассылку **{price_name}**!\n\n"
        f"📄 Скачать актуальный прайс можно по кнопке ниже.\n\n"
        f"Уведомления о новых поступлениях будут приходить сюда.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    
    await callback.answer()
    logger.info(f"New subscriber: {chat_id} ({callback.from_user.full_name}), type: {sub_type}")


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    await remove_subscriber(message.chat.id)
    await message.answer(
        "❌ Вы отписались от рассылки.\n\n"
        "Чтобы вернуться — отправьте /start.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔄 Подписаться снова", callback_data="resubscribe")]]
        )
    )


@dp.callback_query(F.data == "resubscribe")
async def resubscribe(callback: CallbackQuery):
    await callback.answer()
    await show_subscription_menu(callback.message, is_change=False)


# ---------- Admin handlers ----------
def is_admin(message: Message) -> bool:
    return message.from_user.id == ADMIN_ID


@dp.message(Command("help"))
async def cmd_help(message: Message):
    if not is_admin(message):
        await message.answer("📋 /start — Выбрать тип подписки\n/stop — Отписаться")
        return
    
    await message.answer(
        "📋 **Список всех команд:**\n\n"
        "**Для клиентов:**\n"
        "/start — Выбрать тип подписки\n"
        "/stop — Отписаться\n\n"
        "**Для админа:**\n"
        "/help — Помощь\n"
        "/stats — Статистика\n"
        "/white — Рассылка для Белого прайса\n"
        "/common — Рассылка для Общего прайса\n"
        "/all — Рассылка для ВСЕХ\n"
        "/block — Заблокировать\n"
        "/unblock — Разблокировать\n"
        "/export — Экспорт базы\n"
        "/cancel — Отменить действие",
        parse_mode="Markdown"
    )


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message):
        return
    
    total = await count_subscribers()
    white = await count_subscribers('white')
    common = await count_subscribers('common')
    
    await message.answer(
        f"📊 **Статистика**\n\n"
        f"👥 Всего: {total}\n"
        f"🤍 Белый: {white}\n"
        f"💳 Общий: {common}",
        parse_mode="Markdown"
    )


@dp.message(Command("export"))
async def cmd_export(message: Message):
    if not is_admin(message):
        return

    rows = await get_all_subscribers_full()
    if not rows:
        await message.answer("ℹ️ Нет подписчиков.")
        return

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["chat_id", "username", "full_name", "joined_at", "subscription_type"])
    for row in rows:
        writer.writerow([row['chat_id'], row['username'], row['full_name'], row['joined_at'], row['subscription_type']])

    file_bytes = buffer.getvalue().encode("utf-8-sig")
    filename = f"subscribers_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"

    await message.answer_document(
        BufferedInputFile(file_bytes, filename=filename),
        caption=f"📁 Бэкап: {len(rows)} записей"
    )


# ---------- Block/Unblock ----------
@dp.message(Command("block"))
async def cmd_block_start(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    await message.answer("🚫 Отправьте ID пользователя для блокировки.\nДля отмены /cancel")
    await state.set_state(BroadcastStates.waiting_block_user)


@dp.message(BroadcastStates.waiting_block_user)
async def process_block_user(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Некорректный ID.")
        return
    
    if not await get_subscriber_type(user_id):
        await message.answer(f"❌ Пользователь {user_id} не найден.")
        await state.clear()
        return
    
    await block_user(user_id)
    await message.answer(f"✅ Пользователь {user_id} заблокирован.")
    await state.clear()


@dp.message(Command("unblock"))
async def cmd_unblock_start(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    await message.answer("🔓 Отправьте ID пользователя для разблокировки.\nДля отмены /cancel")
    await state.set_state(BroadcastStates.waiting_unblock_user)


@dp.message(BroadcastStates.waiting_unblock_user)
async def process_unblock_user(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Некорректный ID.")
        return
    
    if not await get_subscriber_type(user_id):
        await message.answer(f"❌ Пользователь {user_id} не найден.")
        await state.clear()
        return
    
    await unblock_user(user_id)
    await message.answer(f"✅ Пользователь {user_id} разблокирован.")
    await state.clear()


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    await state.clear()
    await message.answer("❌ Отменено.")


# ---------- Broadcast commands ----------
@dp.message(Command("white"))
async def cmd_white_broadcast(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    
    count = await count_subscribers('white')
    if count == 0:
        await message.answer("ℹ️ Нет подписчиков на Белый прайс.")
        return
    
    await message.answer(f"🤍 **Белый прайс**\n👥 Получателей: {count}\n\nОтправьте сообщение для рассылки.\nДля отмены /cancel", parse_mode="Markdown")
    await state.set_state(BroadcastStates.waiting_white_price)


@dp.message(Command("common"))
async def cmd_common_broadcast(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    
    count = await count_subscribers('common')
    if count == 0:
        await message.answer("ℹ️ Нет подписчиков на Общий прайс.")
        return
    
    await message.answer(f"💳 **Общий прайс**\n👥 Получателей: {count}\n\nОтправьте сообщение для рассылки.\nДля отмены /cancel", parse_mode="Markdown")
    await state.set_state(BroadcastStates.waiting_common_price)


@dp.message(Command("all"))
async def cmd_all_broadcast(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    
    total = await count_subscribers()
    if total == 0:
        await message.answer("ℹ️ Нет подписчиков.")
        return
    
    white = await count_subscribers('white')
    common = await count_subscribers('common')
    
    await message.answer(
        f"📢 **ВСЕМ подписчикам**\n\n"
        f"👥 Всего: {total}\n"
        f"🤍 Белый: {white}\n"
        f"💳 Общий: {common}\n\n"
        f"Отправьте сообщение для рассылки.\nДля отмены /cancel",
        parse_mode="Markdown"
    )
    await state.set_state(BroadcastStates.waiting_all_price)


# ---------- Broadcast with confirmation ----------
@dp.message(BroadcastStates.waiting_white_price)
async def process_white_broadcast(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    await process_broadcast_with_confirmation(message, state, 'white')


@dp.message(BroadcastStates.waiting_common_price)
async def process_common_broadcast(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    await process_broadcast_with_confirmation(message, state, 'common')


@dp.message(BroadcastStates.waiting_all_price)
async def process_all_broadcast(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    await process_broadcast_with_confirmation(message, state, 'all')


async def process_broadcast_with_confirmation(message: Message, state: FSMContext, sub_type: str):
    if not any([message.text, message.photo, message.video, message.document, message.audio, message.voice]):
        await message.answer("ℹ️ Отправьте текст, фото, видео или файл.")
        return
    
    await state.update_data(source_message=message, sub_type=sub_type)
    
    try:
        preview_message = await message.copy_to(chat_id=ADMIN_ID)
        
        if sub_type == 'all':
            count = await count_subscribers()
            type_name = "ВСЕХ"
            detail = f"🤍 Белый: {await count_subscribers('white')}, 💳 Общий: {await count_subscribers('common')}"
        else:
            count = await count_subscribers(sub_type)
            type_name = "Белый прайс" if sub_type == "white" else "Общий прайс"
            detail = ""
        
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"✉️ **Превью**\n\n"
                f"📋 Тип: {type_name}\n"
                f"👥 Получателей: {count}\n"
                f"{detail}\n\n"
                f"⚠️ Отправить?"
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Да", callback_data="confirm_broadcast")],
                    [InlineKeyboardButton(text="❌ Нет", callback_data="cancel_broadcast")],
                ]
            ),
            parse_mode="Markdown",
            reply_to_message_id=preview_message.message_id
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        await state.clear()


@dp.callback_query(F.data == "confirm_broadcast")
async def confirm_broadcast(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    source_message = data.get("source_message")
    sub_type = data.get("sub_type")
    
    if not source_message:
        await callback.message.edit_text("❌ Ошибка.")
        await state.clear()
        return
    
    await callback.message.edit_text("⏳ Запускаю...")
    await broadcast_message(source_message, callback.message, sub_type)
    await state.clear()


@dp.callback_query(F.data == "cancel_broadcast")
async def cancel_broadcast(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("❌ Отменено.")
    await state.clear()


# ---------- Broadcast logic ----------
async def broadcast_message(source_message: Message, status_message: Message, sub_type: str = None):
    if sub_type == 'all':
        subscribers = await get_all_subscribers()
    else:
        subscribers = await get_all_subscribers(sub_type)
    
    if not subscribers:
        await status_message.edit_text("ℹ️ Нет подписчиков.")
        return
    
    sent, failed = 0, 0
    total = len(subscribers)
    
    type_name = "ВСЕХ" if sub_type == 'all' else ("Белый" if sub_type == 'white' else "Общий")
    
    for idx, chat_id in enumerate(subscribers):
        try:
            await source_message.copy_to(chat_id)
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Failed to send to {chat_id}: {e}")
            if "bot was blocked" in str(e).lower() or "chat not found" in str(e).lower():
                await remove_subscriber(chat_id)
        
        if idx % 50 == 0:
            try:
                await status_message.edit_text(f"⏳ {idx}/{total} (отправлено: {sent}, ошибок: {failed})")
            except:
                pass
        
        await asyncio.sleep(0.05)
    
    await status_message.edit_text(
        f"✅ **Готово**\n\n"
        f"📋 Тип: {type_name}\n"
        f"📤 Отправлено: {sent}\n"
        f"⚠️ Ошибок: {failed}\n"
        f"📊 Всего: {total}",
        parse_mode="Markdown"
    )
    logger.info(f"Broadcast ({sub_type}) finished: sent={sent}, failed={failed}, total={total}")


# ---------- Health-check ----------
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
    logger.info(f"Health-check running on port {port}")
    return runner, site

async def shutdown_web_server(runner, site):
    try:
        await site.stop()
        await runner.cleanup()
        logger.info("Health-check stopped")
    except Exception as e:
        logger.warning(f"Error stopping web server: {e}")


# ---------- Main ----------
async def main():
    await init_db()
    logger.info("🚀 Bot starting...")
    
    runner, site = await start_web_server()
    
    loop = asyncio.get_running_loop()
    
    def signal_handler():
        logger.info("Received shutdown signal...")
        asyncio.create_task(shutdown_web_server(runner, site))
        asyncio.create_task(dp.stop_polling())
    
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            pass
    
    await asyncio.sleep(2)
    logger.info("Starting polling...")
    
    try:
        await dp.start_polling(bot)
    finally:
        await shutdown_web_server(runner, site)
        if db_pool:
            await db_pool.close()

if __name__ == "__main__":
    asyncio.run(main())
