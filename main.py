import os
import json
import asyncio
import gspread
from aiohttp import web
from google.oauth2.service_account import Credentials

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# =========================
# Настройки окружения
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
CREDENTIALS_JSON = os.getenv("GOOGLE_SHEETS_CREDENTIALS")

# Для webhook:
# BASE_URL = https://<твой-сервис>.onrender.com
BASE_URL = os.getenv("BASE_URL") or os.getenv("RENDER_EXTERNAL_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change_me_please")
PORT = int(os.getenv("PORT", "10000"))

if not all([BOT_TOKEN, GOOGLE_SHEET_ID, CREDENTIALS_JSON]):
    raise ValueError("Не заданы переменные окружения: BOT_TOKEN / GOOGLE_SHEET_ID / GOOGLE_SHEETS_CREDENTIALS")

if not BASE_URL:
    raise ValueError("Не задан BASE_URL (или RENDER_EXTERNAL_URL). Пример: https://your-service.onrender.com")

BASE_URL = BASE_URL.rstrip("/")
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"

# =========================
# Google Sheets
# =========================
def get_sheet():
    creds_dict = json.loads(CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    client = gspread.authorize(creds)
    return client.open_by_key(GOOGLE_SHEET_ID).sheet1

# =========================
# FSM состояния
# =========================
class BookingStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()

# =========================
# Инициализация бота
# =========================
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# =========================
# Слоты
# =========================
SLOTS = {
    "2026-02-12": {f"{h:02d}:{m:02d}": False for h in range(10, 20) for m in (0, 30)},
    "2026-02-13": {f"{h:02d}:{m:02d}": False for h in range(10, 20) for m in (0, 30)},
}

EVENT_INFO = (
    "🎉 Добро пожаловать на наше мероприятие!\n\n"
    "📅 Доступные дни:\n"
    "• Четверг, 12 февраля 2026\n"
    "• Пятница, 13 февраля 2026\n\n"
    "🕗 Время: с 10:00 до 20:00\n"
    "⏳ Слоты по 30 минут\n"
    "👥 Один человек на слот\n\n"
    "👉 Выберите день ниже:"
)

def load_bookings_from_sheet():
    """Подтягивает confirmed слоты из таблицы и помечает их занятыми."""
    try:
        sheet = get_sheet()
        records = sheet.get_all_records()
        for row in records:
            if str(row.get("status", "")).strip().lower() == "confirmed":
                date = str(row.get("date", "")).strip()
                time = str(row.get("time", "")).strip()
                if date in SLOTS and time in SLOTS[date]:
                    SLOTS[date][time] = True
    except Exception as e:
        print(f"[load_bookings_from_sheet] Ошибка: {e}")

def is_slot_free_sheet(date_str: str, time_str: str) -> bool:
    """
    Защита от "гонки": перед финальным подтверждением проверяем в таблице,
    не появился ли уже confirmed на тот же слот.
    """
    try:
        sheet = get_sheet()
        records = sheet.get_all_records()
        for row in records:
            if (str(row.get("status", "")).strip().lower() == "confirmed"
                and str(row.get("date", "")).strip() == date_str
                and str(row.get("time", "")).strip() == time_str):
                return False
        return True
    except Exception as e:
        print(f"[is_slot_free_sheet] Ошибка: {e}")
        # в сомнительной ситуации лучше считать занятым, чем овербукинг
        return False

# =========================
# Хендлеры
# =========================
@dp.message(Command("start"))
async def send_welcome(message: types.Message):
    load_bookings_from_sheet()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Четверг, 12 февраля", callback_data="day_2026-02-12")],
            [InlineKeyboardButton(text="Пятница, 13 февраля", callback_data="day_2026-02-13")],
        ]
    )
    await message.answer(EVENT_INFO, reply_markup=keyboard)

@dp.callback_query(lambda c: c.data.startswith("day_"))
async def choose_time(callback: types.CallbackQuery, state: FSMContext):
    date_str = callback.data.split("_", 1)[1]
    if date_str not in SLOTS:
        await callback.answer("Неверная дата", show_alert=True)
        return

    # обновляем занятость перед показом (чтобы люди видели актуальные слоты)
    load_bookings_from_sheet()

    free_slots = [t for t, booked in SLOTS[date_str].items() if not booked]
    if not free_slots:
        await callback.message.edit_text("❌ Все слоты заняты!")
        return

    buttons = [[InlineKeyboardButton(text=t, callback_data=f"slot_{date_str}_{t}")]
               for t in free_slots[:40]]  # можно увеличить, если нужно
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons + [[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]])
    await callback.message.edit_text(f"Выберите время на {date_str}:", reply_markup=keyboard)

@dp.callback_query(lambda c: c.data == "back")
async def go_back(callback: types.CallbackQuery):
    await send_welcome(callback.message)

@dp.callback_query(lambda c: c.data.startswith("slot_"))
async def start_booking(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    if len(parts) != 3:
        await callback.answer("Ошибка", show_alert=True)
        return

    date_str, time_str = parts[1], parts[2]
    if date_str not in SLOTS or time_str not in SLOTS[date_str]:
        await callback.answer("Слот не найден", show_alert=True)
        return

    load_bookings_from_sheet()
    if SLOTS[date_str][time_str]:
        await callback.answer("Слот уже занят!", show_alert=True)
        return

    await state.update_data(date=date_str, time=time_str)
    await state.set_state(BookingStates.waiting_for_name)
    await callback.message.edit_text("Введите ваше имя:")

@dp.message(BookingStates.waiting_for_name)
async def get_name(message: types.Message, state: FSMContext):
    if not message.text or len(message.text.strip()) < 2:
        await message.answer("Пожалуйста, введите корректное имя:")
        return
    await state.update_data(name=message.text.strip())
    await state.set_state(BookingStates.waiting_for_phone)
    await message.answer("Введите ваш телефон (только цифры, например: 79991234567):")

@dp.message(BookingStates.waiting_for_phone)
async def get_phone(message: types.Message, state: FSMContext):
    phone = (message.text or "").strip()
    if not phone.isdigit() or len(phone) < 10:
        await message.answer("Пожалуйста, введите корректный телефон (только цифры):")
        return

    data = await state.get_data()
    date_str = data["date"]
    time_str = data["time"]
    name = data["name"]

    # Защита от одновременной записи: проверка в таблице перед финалом
    if not is_slot_free_sheet(date_str, time_str):
        SLOTS[date_str][time_str] = True
        await message.answer("❌ Увы, этот слот только что заняли. Пожалуйста, выберите другое время: /start")
        await state.clear()
        return

    # Пишем в таблицу
    try:
        sheet = get_sheet()
        sheet.append_row([
            str(message.from_user.id),
            name,
            phone,
            date_str,
            time_str,
            "confirmed"
        ])
        # помечаем занятым в памяти
        SLOTS[date_str][time_str] = True
    except Exception as e:
        print(f"[append_row] Ошибка записи в таблицу: {e}")
        await message.answer("Произошла ошибка при записи. Попробуйте позже.")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Изменить запись", callback_data=f"change_{date_str}_{time_str}")],
            [InlineKeyboardButton(text="❌ Отменить запись", callback_data=f"cancel_{date_str}_{time_str}")],
        ]
    )

    await message.answer(
        "✅ Вы записаны!\n\n"
        f"📅 Дата: {date_str}\n"
        f"🕗 Время: {time_str}\n"
        f"👤 Имя: {name}\n"
        f"📞 Телефон: {phone}",
        reply_markup=keyboard
    )
    await state.clear()

@dp.callback_query(lambda c: c.data.startswith("cancel_"))
async def cancel_booking(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    if len(parts) != 3:
        return
    date_str, time_str = parts[1], parts[2]

    try:
        sheet = get_sheet()
        records = sheet.get_all_records()
        for i, row in enumerate(records, start=2):  # 1 — заголовок
            if (str(row.get("date", "")).strip() == date_str and
                str(row.get("time", "")).strip() == time_str and
                str(row.get("user_id", "")).strip() == str(callback.from_user.id)):
                sheet.update_cell(i, 6, "cancelled")  # F = status
                if date_str in SLOTS and time_str in SLOTS[date_str]:
                    SLOTS[date_str][time_str] = False
                break
    except Exception as e:
        print(f"[cancel_booking] Ошибка: {e}")

    await callback.message.edit_text("Ваша запись отменена.")

@dp.callback_query(lambda c: c.data.startswith("change_"))
async def change_booking(callback: types.CallbackQuery):
    await send_welcome(callback.message)

# =========================
# Webhook lifecycle
# =========================
async def on_startup(app: web.Application):
    # На старте ставим webhook
    await bot.set_webhook(WEBHOOK_URL)
    print(f"Webhook set to: {WEBHOOK_URL}")

async def on_shutdown(app: web.Application):
    # На выключении аккуратно удаляем webhook
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception as e:
        print(f"[on_shutdown] delete_webhook error: {e}")
    await bot.session.close()

async def main():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    # Регистрируем обработчик webhook
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()

    print(f"Server started on 0.0.0.0:{PORT}")
    # держим процесс живым
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
