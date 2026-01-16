import os
import json
import gspread
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
import asyncio

# === Настройки ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
CREDENTIALS_JSON = os.getenv("GOOGLE_SHEETS_CREDENTIALS")

if not all([BOT_TOKEN, GOOGLE_SHEET_ID, CREDENTIALS_JSON]):
    raise ValueError("Не заданы переменные окружения!")

# === Подключение к Google Sheets ===
def get_sheet():
    creds_dict = json.loads(CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    client = gspread.authorize(creds)
    sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
    return sheet

# === FSM состояния ===
class BookingStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()
    confirming = State()

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# === Слоты ===
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
    """Загружает занятые слоты из таблицы"""
    try:
        sheet = get_sheet()
        records = sheet.get_all_records()
        for row in records:
            if row.get("status") == "confirmed":
                date = row["date"]
                time = row["time"]
                if date in SLOTS and time in SLOTS[date]:
                    SLOTS[date][time] = True
    except Exception as e:
        print(f"Ошибка загрузки бронирований: {e}")

@dp.message(Command("start"))
async def send_welcome(message: types.Message):
    load_bookings_from_sheet()  # обновляем статус слотов
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton("Четверг, 12 февраля", callback_data="day_2026-02-12")],
            [InlineKeyboardButton("Пятница, 13 февраля", callback_data="day_2026-02-13")]
        ]
    )
    await message.answer(EVENT_INFO, reply_markup=keyboard)

@dp.callback_query(lambda c: c.data.startswith("day_"))
async def choose_time(callback: types.CallbackQuery, state: FSMContext):
    date_str = callback.data.split("_")[1]
    if date_str not in SLOTS:
        await callback.answer("Неверная дата", show_alert=True)
        return

    free_slots = [t for t, booked in SLOTS[date_str].items() if not booked]
    if not free_slots:
        await callback.message.edit_text("❌ Все слоты заняты!")
        return

    buttons = [
        [InlineKeyboardButton(f"{t}", callback_data=f"slot_{date_str}_{t}")]
        for t in free_slots[:20]
    ]
    back = [InlineKeyboardButton("⬅️ Назад", callback_data="back")]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons + [back])
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
    if SLOTS.get(date_str, {}).get(time_str) is None:
        await callback.answer("Слот не найден", show_alert=True)
        return

    if SLOTS[date_str][time_str]:
        await callback.answer("Слот уже занят!", show_alert=True)
        return

    # Сохраняем выбор
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
    phone = message.text.strip()
    if not phone.isdigit() or len(phone) < 10:
        await message.answer("Пожалуйста, введите корректный телефон (только цифры):")
        return

    data = await state.get_data()
    date_str = data["date"]
    time_str = data["time"]
    name = data["name"]

    # Бронируем в памяти
    SLOTS[date_str][time_str] = True

    # Сохраняем в Google Таблицу
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
    except Exception as e:
        print(f"Ошибка записи в таблицу: {e}")
        await message.answer("Произошла ошибка при записи. Попробуйте позже.")
        return

    # Кнопки подтверждения
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton("🔁 Изменить запись", callback_data=f"change_{date_str}_{time_str}")],
            [InlineKeyboardButton("❌ Отменить запись", callback_data=f"cancel_{date_str}_{time_str}")]
        ]
    )

    await message.answer(
        f"✅ Вы записаны!\n\n"
        f"📅 Дата: {date_str}\n"
        f"🕗 Время: {time_str}\n"
        f"👤 Имя: {name}\n"
        f"📞 Телефон: {phone}",
        reply_markup=keyboard
    )
    await state.clear()

# === Обработка отмены ===
@dp.callback_query(lambda c: c.data.startswith("cancel_"))
async def cancel_booking(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    if len(parts) != 3:
        return
    date_str, time_str = parts[1], parts[2]

    # Обновляем статус в таблице
    try:
        sheet = get_sheet()
        records = sheet.get_all_records()
        for i, row in enumerate(records, start=2):  # строки начинаются с 2 (1 — заголовок)
            if (row.get("date") == date_str and
                row.get("time") == time_str and
                str(row.get("user_id")) == str(callback.from_user.id)):
                sheet.update_cell(i, 6, "cancelled")  # колонка F = status
                SLOTS[date_str][time_str] = False
                break
    except Exception as e:
        print(f"Ошибка отмены: {e}")

    await callback.message.edit_text("Ваша запись отменена.")

# === Обработка изменения ===
@dp.callback_query(lambda c: c.data.startswith("change_"))
async def change_booking(callback: types.CallbackQuery):
    await send_welcome(callback.message)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
