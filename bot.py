import asyncio
import csv
import io
import logging
import os
import signal
import sqlite3
from contextlib import closing
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

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "subscribers.db")

# Ссылки на прайсы
WHITE_PRICE_URL = "https://b2b.moysklad.ru/public/NgO26OdrxmZh"
COMMON_PRICE_URL = "https://b2b.moysklad.ru/public/9p421RcbdoLa"
MANAGER_LINK = "https://t.me/vv_vape"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# ---------- FSM States ----------
class BroadcastStates(StatesGroup):
    waiting_white_price = State()      # Ожидание контента для белого прайса
    waiting_common_price = State()     # Ожидание контента для общего прайса
    waiting_all_price = State()        # Ожидание контента для всех подписчиков
    waiting_block_user = State()       # Ожидание ID пользователя для блокировки
    waiting_unblock_user = State()     # Ожидание ID пользователя для разблокировки


# ---------- Database ----------
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
                subscription_type TEXT DEFAULT 'common',
                is_blocked INTEGER DEFAULT 0
            )
            """
        )
        conn.commit()


def add_subscriber(chat_id: int, username: str | None, full_name: str, sub_type: str = 'common'):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO subscribers (chat_id, username, full_name, subscription_type, is_blocked) 
            VALUES (?, ?, ?, ?, 0)
            """,
            (chat_id, username, full_name, sub_type),
        )
        conn.commit()


def update_subscription_type(chat_id: int, sub_type: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE subscribers SET subscription_type = ? WHERE chat_id = ?",
            (sub_type, chat_id)
        )
        conn.commit()


def remove_subscriber(chat_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
        conn.commit()


def get_subscriber_type(chat_id: int) -> str | None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT subscription_type FROM subscribers WHERE chat_id = ?", 
            (chat_id,)
        ).fetchone()
    return row[0] if row else None


def get_all_subscribers(sub_type: str = None) -> list[int]:
    """Получить подписчиков определенного типа или всех"""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        if sub_type and sub_type != 'all':
            rows = conn.execute(
                "SELECT chat_id FROM subscribers WHERE subscription_type = ? AND is_blocked = 0",
                (sub_type,)
            ).fetchall()
        else:
            # Для 'all' или None - получаем всех НЕзаблокированных
            rows = conn.execute(
                "SELECT chat_id FROM subscribers WHERE is_blocked = 0"
            ).fetchall()
    return [row[0] for row in rows]


def count_subscribers(sub_type: str = None) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        if sub_type and sub_type != 'all':
            return conn.execute(
                "SELECT COUNT(*) FROM subscribers WHERE subscription_type = ? AND is_blocked = 0",
                (sub_type,)
            ).fetchone()[0]
        else:
            return conn.execute(
                "SELECT COUNT(*) FROM subscribers WHERE is_blocked = 0"
            ).fetchone()[0]


def get_all_subscribers_full() -> list[tuple]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return conn.execute(
            "SELECT chat_id, username, full_name, joined_at, subscription_type FROM subscribers ORDER BY joined_at"
        ).fetchall()


def block_user(chat_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE subscribers SET is_blocked = 1 WHERE chat_id = ?",
            (chat_id,)
        )
        conn.commit()


def unblock_user(chat_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE subscribers SET is_blocked = 0 WHERE chat_id = ?",
            (chat_id,)
        )
        conn.commit()


def is_user_blocked(chat_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT is_blocked FROM subscribers WHERE chat_id = ?",
            (chat_id,)
        ).fetchone()
    return row[0] == 1 if row else False


# ---------- Client-facing handlers ----------
@dp.message(CommandStart())
async def cmd_start(message: Message):
    # Проверяем, есть ли уже подписчик
    existing_type = get_subscriber_type(message.chat.id)
    
    if existing_type:
        # Если уже подписан, предлагаем сменить тип
        await show_subscription_menu(message, is_change=True)
    else:
        # Новый подписчик - показываем выбор
        await show_subscription_menu(message, is_change=False)


async def show_subscription_menu(message: Message, is_change: bool = False):
    """Показать меню выбора типа подписки"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🤍 Белый прайс (только безнал)", 
                    callback_data="sub_white"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💳 Общий прайс (наличка + безнал)", 
                    callback_data="sub_common"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📞 Связаться с менеджером", 
                    url=MANAGER_LINK
                )
            ]
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
    sub_type = callback.data.split("_")[1]  # 'white' or 'common'
    chat_id = callback.from_user.id
    
    # Сохраняем в базу
    add_subscriber(
        chat_id,
        callback.from_user.username,
        callback.from_user.full_name,
        sub_type
    )
    
    # Отправляем подтверждение с ссылкой на прайс
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
    
    # Подтверждаем callback
    await callback.answer()
    
    logger.info(f"New subscriber: {chat_id} ({callback.from_user.full_name}), type: {sub_type}")


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    remove_subscriber(message.chat.id)
    await message.answer(
        "❌ Вы отписались от рассылки.\n\n"
        "Чтобы вернуться — отправьте /start.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="🔄 Подписаться снова", callback_data="resubscribe")
            ]]
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
    """Список всех команд"""
    if not is_admin(message):
        await message.answer(
            "📋 **Доступные команды:**\n\n"
            "/start — Выбрать тип подписки\n"
            "/stop — Отписаться от рассылки"
        )
        return
    
    await message.answer(
        "📋 **Список всех команд:**\n\n"
        "**Для клиентов:**\n"
        "/start — Выбрать тип подписки\n"
        "/stop — Отписаться от рассылки\n\n"
        "**Для админа:**\n"
        "/help — Показать это сообщение\n"
        "/stats — Статистика подписчиков\n"
        "/white — Рассылка для Белого прайса\n"
        "/common — Рассылка для Общего прайса\n"
        "/all — Рассылка для ВСЕХ подписчиков\n"
        "/block — Заблокировать пользователя\n"
        "/unblock — Разблокировать пользователя\n"
        "/export — Экспорт базы в CSV\n"
        "/cancel — Отменить текущее действие",
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
        f"📊 **Статистика подписчиков**\n\n"
        f"👥 Всего: {total}\n"
        f"🤍 Белый прайс: {white}\n"
        f"💳 Общий прайс: {common}",
        parse_mode="Markdown"
    )


@dp.message(Command("export"))
async def cmd_export(message: Message):
    if not is_admin(message):
        return

    rows = get_all_subscribers_full()
    if not rows:
        await message.answer("ℹ️ Пока нет ни одного подписчика.")
        return

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["chat_id", "username", "full_name", "joined_at", "subscription_type"])
    writer.writerows(rows)

    file_bytes = buffer.getvalue().encode("utf-8-sig")
    filename = f"subscribers_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"

    await message.answer_document(
        BufferedInputFile(file_bytes, filename=filename),
        caption=f"📁 Бэкап подписчиков: {len(rows)} записей"
    )


# ---------- Block/Unblock users ----------
@dp.message(Command("block"))
async def cmd_block_start(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    await message.answer(
        "🚫 **Блокировка пользователя**\n\n"
        "Отправьте ID пользователя, которого нужно заблокировать.\n"
        "Чтобы узнать ID, попросите пользователя написать @userinfobot.\n\n"
        "Для отмены отправьте /cancel",
        parse_mode="Markdown"
    )
    await state.set_state(BroadcastStates.waiting_block_user)


@dp.message(BroadcastStates.waiting_block_user)
async def process_block_user(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Некорректный ID. Отправьте число.")
        return
    
    # Проверяем, есть ли пользователь в базе
    if not get_subscriber_type(user_id):
        await message.answer(f"❌ Пользователь с ID {user_id} не найден в базе.")
        await state.clear()
        return
    
    block_user(user_id)
    await message.answer(f"✅ Пользователь с ID {user_id} заблокирован.")
    logger.info(f"Admin blocked user: {user_id}")
    await state.clear()


@dp.message(Command("unblock"))
async def cmd_unblock_start(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    await message.answer(
        "🔓 **Разблокировка пользователя**\n\n"
        "Отправьте ID пользователя, которого нужно разблокировать.\n\n"
        "Для отмены отправьте /cancel",
        parse_mode="Markdown"
    )
    await state.set_state(BroadcastStates.waiting_unblock_user)


@dp.message(BroadcastStates.waiting_unblock_user)
async def process_unblock_user(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Некорректный ID. Отправьте число.")
        return
    
    if not get_subscriber_type(user_id):
        await message.answer(f"❌ Пользователь с ID {user_id} не найден в базе.")
        await state.clear()
        return
    
    unblock_user(user_id)
    await message.answer(f"✅ Пользователь с ID {user_id} разблокирован.")
    logger.info(f"Admin unblocked user: {user_id}")
    await state.clear()


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    await state.clear()
    await message.answer("❌ Действие отменено.")


# ---------- Broadcast commands ----------
@dp.message(Command("white"))
async def cmd_white_broadcast(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    
    count = count_subscribers('white')
    if count == 0:
        await message.answer("ℹ️ Нет подписчиков на Белый прайс.")
        return
    
    await message.answer(
        f"🤍 **Рассылка для Белого прайса**\n\n"
        f"👥 Получателей: {count}\n\n"
        f"Отправьте сообщение (текст, фото, видео или файл), "
        f"которое получит каждый подписчик.\n"
        f"Для отмены отправьте /cancel",
        parse_mode="Markdown"
    )
    await state.set_state(BroadcastStates.waiting_white_price)


@dp.message(Command("common"))
async def cmd_common_broadcast(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    
    count = count_subscribers('common')
    if count == 0:
        await message.answer("ℹ️ Нет подписчиков на Общий прайс.")
        return
    
    await message.answer(
        f"💳 **Рассылка для Общего прайса**\n\n"
        f"👥 Получателей: {count}\n\n"
        f"Отправьте сообщение (текст, фото, видео или файл), "
        f"которое получит каждый подписчик.\n"
        f"Для отмены отправьте /cancel",
        parse_mode="Markdown"
    )
    await state.set_state(BroadcastStates.waiting_common_price)


@dp.message(Command("all"))
async def cmd_all_broadcast(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    
    total = count_subscribers()
    if total == 0:
        await message.answer("ℹ️ Нет подписчиков для рассылки.")
        return
    
    white_count = count_subscribers('white')
    common_count = count_subscribers('common')
    
    await message.answer(
        f"📢 **Рассылка для ВСЕХ подписчиков**\n\n"
        f"👥 Всего получателей: {total}\n"
        f"🤍 Белый прайс: {white_count}\n"
        f"💳 Общий прайс: {common_count}\n\n"
        f"Отправьте сообщение (текст, фото, видео или файл), "
        f"которое получат ВСЕ подписчики.\n"
        f"Для отмены отправьте /cancel",
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
    """Общая логика для рассылки с подтверждением"""
    # Проверяем содержимое
    if not any([message.text, message.photo, message.video, message.document, message.audio, message.voice]):
        await message.answer("ℹ️ Не могу распознать содержимое. Отправьте текст, фото, видео или файл.")
        return
    
    # Сохраняем сообщение и тип
    await state.update_data(source_message=message, sub_type=sub_type)
    
    # Сначала отправляем превью админу
    try:
        # Отправляем копию сообщения самому админу как превью
        preview_message = await message.copy_to(chat_id=ADMIN_ID)
        
        # Теперь отправляем ответ на это превью с кнопками
        if sub_type == 'all':
            count = count_subscribers()
            type_name = "ВСЕХ подписчиков"
            detail = f"🤍 Белый: {count_subscribers('white')}, 💳 Общий: {count_subscribers('common')}"
        else:
            count = count_subscribers(sub_type)
            type_name = "Белый прайс" if sub_type == "white" else "Общий прайс"
            detail = ""
        
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"✉️ **Превью рассылки**\n\n"
                f"📋 Тип: {type_name}\n"
                f"👥 Получателей: {count}\n"
                f"{detail}\n\n"
                f"⚠️ Отправить это сообщение ВСЕМ подписчикам?"
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Да, отправить всем", callback_data="confirm_broadcast")],
                    [InlineKeyboardButton(text="❌ Нет, отменить", callback_data="cancel_broadcast")],
                ]
            ),
            parse_mode="Markdown",
            reply_to_message_id=preview_message.message_id
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка при создании превью: {e}")
        await state.clear()


@dp.callback_query(F.data == "confirm_broadcast")
async def confirm_broadcast(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    source_message = data.get("source_message")
    sub_type = data.get("sub_type")
    
    if not source_message:
        await callback.message.edit_text("❌ Ошибка: сообщение не найдено.")
        await state.clear()
        return
    
    # Запускаем рассылку
    await callback.message.edit_text("⏳ Запускаю рассылку...")
    await broadcast_message(source_message, callback.message, sub_type)
    await state.clear()


@dp.callback_query(F.data == "cancel_broadcast")
async def cancel_broadcast(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("❌ Рассылка отменена.")
    await state.clear()


# ---------- Broadcast logic ----------
async def broadcast_message(source_message: Message, status_message: Message, sub_type: str = None):
    # Получаем подписчиков в зависимости от типа
    if sub_type == 'all':
        subscribers = get_all_subscribers()  # Все НЕзаблокированные
    else:
        subscribers = get_all_subscribers(sub_type)
    
    if not subscribers:
        await status_message.edit_text("ℹ️ Нет подписчиков для рассылки.")
        return
    
    sent, failed = 0, 0
    total = len(subscribers)
    
    if sub_type == 'all':
        type_name = "ВСЕХ подписчиков"
    elif sub_type == 'white':
        type_name = "Белый прайс"
    else:
        type_name = "Общий прайс"
    
    logger.info(f"Starting broadcast ({type_name}) to {total} subscribers")
    
    # Обновляем статус каждые 50 отправок
    for idx, chat_id in enumerate(subscribers):
        try:
            await source_message.copy_to(chat_id)
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Failed to send to {chat_id}: {e}")
            if "bot was blocked" in str(e).lower() or "chat not found" in str(e).lower():
                remove_subscriber(chat_id)
        
        if idx % 50 == 0 and status_message:
            try:
                await status_message.edit_text(
                    f"⏳ Рассылка ({type_name})... {idx}/{total} (отправлено: {sent}, ошибок: {failed})"
                )
            except Exception as edit_error:
                logger.warning(f"Could not update status: {edit_error}")
                pass
        
        await asyncio.sleep(0.05)
    
    # Финальный отчет
    report = (
        f"✅ **Рассылка завершена**\n\n"
        f"📋 Тип: {type_name}\n"
        f"📤 Отправлено: {sent}\n"
        f"⚠️ Не доставлено: {failed}\n"
        f"📊 Всего получателей: {total}"
    )
    await status_message.edit_text(report, parse_mode="Markdown")
    logger.info(f"Broadcast ({sub_type}) finished: sent={sent}, failed={failed}, total={total}")


# ---------- Health-check web server ----------
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
    logger.info(f"Health-check server running on port {port}")
    return runner, site

async def shutdown_web_server(runner, site):
    """Корректно останавливаем веб-сервер"""
    try:
        await site.stop()
        await runner.cleanup()
        logger.info("Health-check server stopped")
    except Exception as e:
        logger.warning(f"Error stopping web server: {e}")


# ---------- Main ----------
async def main():
    init_db()
    logger.info("🚀 Bot starting...")
    
    # Запускаем веб-сервер
    runner, site = await start_web_server()
    
    # Обработка сигналов для корректного завершения
    loop = asyncio.get_running_loop()
    
    def signal_handler():
        logger.info("Received shutdown signal, stopping...")
        asyncio.create_task(shutdown_web_server(runner, site))
        asyncio.create_task(dp.stop_polling())
    
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # Windows не поддерживает add_signal_handler
            pass
    
    # Небольшая задержка перед стартом поллинга
    await asyncio.sleep(2)
    logger.info("Starting polling...")
    
    try:
        await dp.start_polling(bot)
    finally:
        await shutdown_web_server(runner, site)

if __name__ == "__main__":
    asyncio.run(main())
