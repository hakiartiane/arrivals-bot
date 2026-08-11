import asyncio
import csv
import io
import logging
import os
import signal
from datetime import datetime

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
from supabase import create_client, Client
from aiohttp import web

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Supabase API настройки
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Ссылки на прайсы
WHITE_PRICE_URL = "https://b2b.moysklad.ru/public/NgO26OdrxmZh"
COMMON_PRICE_URL = "https://b2b.moysklad.ru/public/9p421RcbdoLa"
MANAGER_LINK = "https://t.me/vv_vape"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Supabase клиент
supabase: Client = None


# ---------- Database functions ----------
def init_db():
    """Инициализация Supabase"""
    global supabase
    try:
        if not SUPABASE_URL or not SUPABASE_KEY:
            logger.error("SUPABASE_URL or SUPABASE_KEY not set!")
            raise ValueError("Supabase credentials required")
        
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        
        # Проверяем подключение
        response = supabase.table('subscribers').select('*').limit(1).execute()
        logger.info("Connected to Supabase successfully, table exists")
        
    except Exception as e:
        logger.error(f"Database initialization error: {e}")
        logger.error("Please make sure the 'subscribers' table exists in Supabase")
        logger.error("Run this SQL in Supabase SQL Editor:")
        logger.error("""
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id BIGINT PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            subscription_type TEXT DEFAULT 'common',
            is_blocked INTEGER DEFAULT 0
        );
        """)
        raise


def get_subscriber_type(chat_id: int) -> str | None:
    try:
        response = supabase.table('subscribers').select('subscription_type').eq('chat_id', chat_id).execute()
        if response.data and len(response.data) > 0:
            return response.data[0]['subscription_type']
        return None
    except Exception as e:
        logger.error(f"Error getting subscriber: {e}")
        return None


def add_subscriber(chat_id: int, username: str | None, full_name: str, sub_type: str = 'common'):
    try:
        data = {
            'chat_id': chat_id,
            'username': username,
            'full_name': full_name,
            'subscription_type': sub_type,
            'is_blocked': 0
        }
        response = supabase.table('subscribers').upsert(data).execute()
        logger.info(f"Added subscriber {chat_id}")
        return True
    except Exception as e:
        logger.error(f"Error adding subscriber: {e}")
        return False


def remove_subscriber(chat_id: int):
    try:
        response = supabase.table('subscribers').delete().eq('chat_id', chat_id).execute()
        logger.info(f"Removed subscriber {chat_id}")
        return True
    except Exception as e:
        logger.error(f"Error removing subscriber: {e}")
        return False


def get_all_subscribers(sub_type: str = None) -> list[int]:
    try:
        if sub_type and sub_type != 'all':
            response = supabase.table('subscribers').select('chat_id').eq('subscription_type', sub_type).eq('is_blocked', 0).execute()
        else:
            response = supabase.table('subscribers').select('chat_id').eq('is_blocked', 0).execute()
        
        return [row['chat_id'] for row in response.data]
    except Exception as e:
        logger.error(f"Error getting subscribers: {e}")
        return []


def count_subscribers(sub_type: str = None) -> int:
    try:
        if sub_type and sub_type != 'all':
            response = supabase.table('subscribers').select('*', count='exact').eq('subscription_type', sub_type).eq('is_blocked', 0).execute()
        else:
            response = supabase.table('subscribers').select('*', count='exact').eq('is_blocked', 0).execute()
        return response.count
    except Exception as e:
        logger.error(f"Error counting subscribers: {e}")
        return 0


def get_all_subscribers_full() -> list[dict]:
    try:
        response = supabase.table('subscribers').select('*').order('joined_at').execute()
        return response.data
    except Exception as e:
        logger.error(f"Error getting all subscribers: {e}")
        return []


def block_user(chat_id: int):
    try:
        supabase.table('subscribers').update({'is_blocked': 1}).eq('chat_id', chat_id).execute()
        logger.info(f"Blocked user {chat_id}")
        return True
    except Exception as e:
        logger.error(f"Error blocking user: {e}")
        return False


def unblock_user(chat_id: int):
    try:
        supabase.table('subscribers').update({'is_blocked': 0}).eq('chat_id', chat_id).execute()
        logger.info(f"Unblocked user {chat_id}")
        return True
    except Exception as e:
        logger.error(f"Error unblocking user: {e}")
        return False


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
    existing_type = get_subscriber_type(message.chat.id)
    
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
    
    add_subscriber(
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
    remove_subscriber(message.chat.id)
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
    
    total = count_subscribers()
    white = count_subscribers('white')
    common = count_subscribers('common')
    
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

    rows = get_all_subscribers_full()
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
    
    if not get_subscriber_type(user_id):
        await message.answer(f"❌ Пользователь {user_id} не найден.")
        await state.clear()
        return
    
    block_user(user_id)
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
    
    if not get_subscriber_type(user_id):
        await message.answer(f"❌ Пользователь {user_id} не найден.")
        await state.clear()
        return
    
    unblock_user(user_id)
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
    
    count = count_subscribers('white')
    if count == 0:
        await message.answer("ℹ️ Нет подписчиков на Белый прайс.")
        return
    
    await message.answer(f"🤍 **Белый прайс**\n👥 Получателей: {count}\n\nОтправьте сообщение для рассылки.\nДля отмены /cancel", parse_mode="Markdown")
    await state.set_state(BroadcastStates.waiting_white_price)


@dp.message(Command("common"))
async def cmd_common_broadcast(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    
    count = count_subscribers('common')
    if count == 0:
        await message.answer("ℹ️ Нет подписчиков на Общий прайс.")
        return
    
    await message.answer(f"💳 **Общий прайс**\n👥 Получателей: {count}\n\nОтправьте сообщение для рассылки.\nДля отмены /cancel", parse_mode="Markdown")
    await state.set_state(BroadcastStates.waiting_common_price)


@dp.message(Command("all"))
async def cmd_all_broadcast(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    
    total = count_subscribers()
    if total == 0:
        await message.answer("ℹ️ Нет подписчиков.")
        return
    
    white = count_subscribers('white')
    common = count_subscribers('common')
    
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
            count = count_subscribers()
            type_name = "ВСЕХ"
            detail = f"🤍 Белый: {count_subscribers('white')}, 💳 Общий: {count_subscribers('common')}"
        else:
            count = count_subscribers(sub_type)
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
        subscribers = get_all_subscribers()
    else:
        subscribers = get_all_subscribers(sub_type)
    
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
                remove_subscriber(chat_id)
        
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
    init_db()
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

if __name__ == "__main__":
    asyncio.run(main())
