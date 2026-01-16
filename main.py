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
# ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
CREDENTIALS_JSON = os.getenv("GOOGLE_SHEETS_CREDENTIALS")

# Webhook (Render Web Service)
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
# FSM
# =========================
class BookingStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()


# =========================
# Bot / Dispatcher
# =========================
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# =========================
# Slots
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
    "👥 Один человек на слот\n"
    "🔒 Один аккаунт = один слот\n\n"
    "👉 Выберите день ниже:"
)

# Колонки (A..F)
COL_USER_ID = 1
COL_NAME = 2
COL_PHONE = 3
COL_DATE = 4
COL_TIME = 5
COL_STATUS = 6


def reset_slots():
    for d in SLOTS:
        for t in SLOTS[d]:
            SLOTS[d][t] = False


def load_bookings_from_sheet():
    """Полностью пересобирает занятость слотов по confirmed из таблицы."""
    try:
        reset_slots()
        sheet = get_sheet()
        records = sheet.get_all_records()
        for row in records:
            if str(row.get("status", "")).strip().lower() == "confirmed":
                date = str(row.get("date", "")).strip()
                time = str(row.get("time", "")).strip()
                if date in SLOTS and time in SLOTS[date]:
                    SLOTS[date][time] = True
    except Exception as e:
        print(f"[load_bookings_from_sheet] error: {e}")


def find_user_confirmed_booking(user_id: str):
    """
    Возвращает (row_index, row_dict) для confirmed брони пользователя,
    либо (None, None) если нет.
    """
    sheet = get_sheet()
    records = sheet.get_all_records()
    for i, row in enumerate(records, start=2):  # 2 = первая строка после заголовка
        if str(row.get("user_id")) == str(user_id) and str(row.get("status", "")).strip().lower() == "confirmed":
            return i, row
    return None, None


def slot_is_confirmed_in_sheet(date_str: str, time_str: str) -> bool:
    """Атомарная проверка слота по таблице: есть ли confirmed на дату+время."""
    sheet = get_sheet()
    records = sheet.get_all_records()
    for row in records:
        if (str(row.get("status", "")).strip().lower() == "confirmed"
            and str(row.get("date", "")).strip() == date_str
            and str(row.get("time", "")).strip() == time_str):
            return True
    return False


def manage_keyboard(date_str: str, time_str: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Изменить время", callback_data="change_booking")],
            [InlineKeyboardButton(text="❌ Отменить запись", callback_data="cancel_booking")],
        ]
    )


def days_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Четверг, 12 февраля", callback_data="day_2026-02-12")],
            [InlineKeyboardButton(text="Пятница, 13 февраля", callback_data="day_2026-02-13")],
        ]
    )


# =========================
# Handlers
# =========================
@dp.message(Command("start"))
async def send_welcome(message: types.Message, state: FSMContext):
    await state.clear()
    load_bookings_from_sheet()

    user_id = str(message.from_user.id)
    try:
        row_index, row = find_user_confirmed_booking(user_id)
    except Exception as e:
        print(f"[send_welcome] find_user_confirmed_booking error: {e}")
        row_index, row = None, None

    # Если у пользователя уже есть запись — не даём бронировать новую
    if row_index and row:
        date_str = str(row.get("date"))
        time_str = str(row.get("time"))
        await message.answer(
            "✅ У вас уже есть активная запись.\n\n"
            f"📅 Дата: {date_str}\n"
            f"🕗 Время: {time_str}\n\n"
            "Вы можете изменить время или отменить запись:",
            reply_markup=manage_keyboard(date_str, time_str)
        )
        return

    await message.answer(EVENT_INFO, reply_markup=days_keyboard())


@dp.callback_query(lambda c: c.data.startswith("day_"))
async def choose_time(callback: types.CallbackQuery, state: FSMContext):
    date_str = callback.data.split("_", 1)[1]
    if date_str not in SLOTS:
        await callback.answer("Неверная дата", show_alert=True)
        return

    user_id = str(callback.from_user.id)
    data = await state.get_data()
    mode = data.get("mode")  # "change" или None

    # В обычном режиме: если уже есть запись — блокируем
    if mode != "change":
        try:
            row_index, row = find_user_confirmed_booking(user_id)
            if row_index and row:
                await callback.answer("У вас уже есть активная запись.", show_alert=True)
                date0, time0 = str(row.get("date")), str(row.get("time"))
                await callback.message.edit_text(
                    "✅ У вас уже есть активная запись.\n\n"
                    f"📅 Дата: {date0}\n"
                    f"🕗 Время: {time0}\n\n"
                    "Вы можете изменить время или отменить запись:",
                    reply_markup=manage_keyboard(date0, time0)
                )
                return
        except Exception as e:
            print(f"[choose_time] user booking check error: {e}")

    load_bookings_from_sheet()
    free_slots = [t for t, booked in SLOTS[date_str].items() if not booked]
    if not free_slots:
        await callback.message.edit_text("❌ Все слоты на этот день заняты.")
        return

    buttons = [[InlineKeyboardButton(text=t, callback_data=f"slot_{date_str}_{t}")] for t in free_slots[:40]]
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=buttons + [[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_days")]]
    )
    await callback.message.edit_text(f"Выберите время на {date_str}:", reply_markup=keyboard)


@dp.callback_query(lambda c: c.data == "back_to_days")
async def back_to_days(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("mode") == "change":
        await callback.message.edit_text("Выберите новый день:", reply_markup=days_keyboard())
    else:
        await callback.message.edit_text(EVENT_INFO, reply_markup=days_keyboard())


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

    user_id = str(callback.from_user.id)
    data = await state.get_data()
    mode = data.get("mode")

    load_bookings_from_sheet()
    if SLOTS[date_str][time_str]:
        await callback.answer("Слот уже занят!", show_alert=True)
        return

    # === РЕЖИМ СМЕНЫ (без имени/телефона) ===
    if mode == "change":
        try:
            sheet_row = int(data["sheet_row"])
            old_date = str(data["old_date"])
            old_time = str(data["old_time"])

            # защита от гонки по таблице
            if slot_is_confirmed_in_sheet(date_str, time_str):
                await callback.answer("Этот слот только что заняли. Выберите другой.", show_alert=True)
                return

            sheet = get_sheet()
            sheet.update_cell(sheet_row, COL_DATE, date_str)
            sheet.update_cell(sheet_row, COL_TIME, time_str)
            sheet.update_cell(sheet_row, COL_STATUS, "confirmed")

            # обновим локально слоты
            if old_date in SLOTS and old_time in SLOTS[old_date]:
                SLOTS[old_date][old_time] = False
            SLOTS[date_str][time_str] = True

            await state.clear()
            await callback.message.edit_text(
                "✅ Запись изменена!\n\n"
                f"📅 Дата: {date_str}\n"
                f"🕗 Время: {time_str}",
                reply_markup=manage_keyboard(date_str, time_str)
            )
            return

        except Exception as e:
            print(f"[change slot] error: {e}")
            await callback.answer("Не удалось изменить запись. Попробуйте позже.", show_alert=True)
            return

    # === Обычная новая запись: проверка 1 аккаунт = 1 слот ===
    try:
        row_index, row = find_user_confirmed_booking(user_id)
        if row_index and row:
            await callback.answer("У вас уже есть активная запись.", show_alert=True)
            date0, time0 = str(row.get("date")), str(row.get("time"))
            await callback.message.edit_text(
                "✅ У вас уже есть активная запись.\n\n"
                f"📅 Дата: {date0}\n"
                f"🕗 Время: {time0}\n\n"
                "Вы можете изменить время или отменить запись:",
                reply_markup=manage_keyboard(date0, time0)
            )
            await state.clear()
            return
    except Exception as e:
        print(f"[start_booking] limit check error: {e}")

    # Сохраняем слот и продолжаем сбор данных
    await state.update_data(date=date_str, time=time_str)
    await state.set_state(BookingStates.waiting_for_name)
    await callback.message.edit_text("Введите ваше имя:")


@dp.message(BookingStates.waiting_for_name)
async def get_name(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Пожалуйста, введите корректное имя:")
        return
    await state.update_data(name=name)
    await state.set_state(BookingStates.waiting_for_phone)
    await message.answer("Введите ваш телефон (только цифры, например: 79991234567):")


@dp.message(BookingStates.waiting_for_phone)
async def get_phone(message: types.Message, state: FSMContext):
    phone = (message.text or "").strip()
    if not phone.isdigit() or len(phone) < 10:
        await message.answer("Пожалуйста, введите корректный телефон (только цифры):")
        return

    user_id = str(message.from_user.id)

    # 1 аккаунт = 1 слот (перед записью — ещё раз, чтобы исключить параллельные шаги)
    try:
        row_index, row = find_user_confirmed_booking(user_id)
        if row_index and row:
            date0, time0 = str(row.get("date")), str(row.get("time"))
            await message.answer(
                "✅ У вас уже есть активная запись.\n\n"
                f"📅 Дата: {date0}\n"
                f"🕗 Время: {time0}\n\n"
                "Вы можете изменить время или отменить запись:",
                reply_markup=manage_keyboard(date0, time0)
            )
            await state.clear()
            return
    except Exception as e:
        print(f"[get_phone] limit check error: {e}")
        await message.answer("Ошибка проверки записей. Попробуйте позже.")
        return

    data = await state.get_data()
    date_str = data["date"]
    time_str = data["time"]
    name = data["name"]

    # Защита от гонки: слот мог занять другой пользователь
    load_bookings_from_sheet()
    if SLOTS.get(date_str, {}).get(time_str) is None or SLOTS[date_str][time_str]:
        await message.answer("❌ Увы, этот слот только что заняли. Выберите другое время: /start")
        await state.clear()
        return

    # Атомарно по таблице (ещё раз)
    try:
        if slot_is_confirmed_in_sheet(date_str, time_str):
            SLOTS[date_str][time_str] = True
            await message.answer("❌ Увы, этот слот только что заняли. Выберите другое время: /start")
            await state.clear()
            return
    except Exception as e:
        print(f"[get_phone] slot sheet check error: {e}")
        await message.answer("Ошибка проверки слота. Попробуйте позже.")
        return

    # Запись в таблицу
    try:
        sheet = get_sheet()
        sheet.append_row([user_id, name, phone, date_str, time_str, "confirmed"])
        SLOTS[date_str][time_str] = True
    except Exception as e:
        print(f"[append_row] error: {e}")
        await message.answer("Произошла ошибка при записи. Попробуйте позже.")
        return

    await message.answer(
        "✅ Вы записаны!\n\n"
        f"📅 Дата: {date_str}\n"
        f"🕗 Время: {time_str}\n"
        f"👤 Имя: {name}\n"
        f"📞 Телефон: {phone}",
        reply_markup=manage_keyboard(date_str, time_str)
    )
    await state.clear()


# =========================
# Manage buttons
# =========================
@dp.callback_query(lambda c: c.data == "cancel_booking")
async def cancel_booking(callback: types.CallbackQuery, state: FSMContext):
    user_id = str(callback.from_user.id)

    try:
        sheet = get_sheet()
        row_index, row = find_user_confirmed_booking(user_id)
        if not row_index:
            await callback.answer("У вас нет активной записи.", show_alert=True)
            return

        date_str = str(row.get("date"))
        time_str = str(row.get("time"))

        # Удаляем строку целиком
        sheet.delete_rows(row_index)

        # Освобождаем слот
        if date_str in SLOTS and time_str in SLOTS[date_str]:
            SLOTS[date_str][time_str] = False

    except Exception as e:
        print(f"[cancel_booking] error: {e}")
        await callback.answer("Ошибка при удалении. Попробуйте позже.", show_alert=True)
        return

    await state.clear()
    await callback.message.edit_text("✅ Запись удалена.\n\nЧтобы записаться снова: /start")


@dp.callback_query(lambda c: c.data == "change_booking")
async def change_booking(callback: types.CallbackQuery, state: FSMContext):
    user_id = str(callback.from_user.id)

    try:
        row_index, row = find_user_confirmed_booking(user_id)
        if not row_index:
            await callback.answer("У вас нет активной записи.", show_alert=True)
            return

        old_date = str(row.get("date"))
        old_time = str(row.get("time"))

        # сохраняем режим change в FSM
        await state.update_data(mode="change", sheet_row=row_index, old_date=old_date, old_time=old_time)

    except Exception as e:
        print(f"[change_booking] error: {e}")
        await callback.answer("Ошибка. Попробуйте позже.", show_alert=True)
        return

    load_bookings_from_sheet()
    await callback.message.edit_text("Выберите новый день:", reply_markup=days_keyboard())


# =========================
# Webhook lifecycle
# =========================
async def on_startup(app: web.Application):
    await bot.set_webhook(WEBHOOK_URL)
    print(f"Webhook set to: {WEBHOOK_URL}")


async def on_shutdown(app: web.Application):
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception as e:
        print(f"[on_shutdown] delete_webhook error: {e}")
    await bot.session.close()


async def main():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()

    print(f"Server started on 0.0.0.0:{PORT}")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
