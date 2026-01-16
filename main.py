import os
import json
import asyncio
from datetime import datetime, time, date
from zoneinfo import ZoneInfo

import gspread
from aiohttp import web
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

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
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")  # spreadsheetId
CREDENTIALS_JSON = os.getenv("GOOGLE_SHEETS_CREDENTIALS")

BASE_URL = os.getenv("BASE_URL") or os.getenv("RENDER_EXTERNAL_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change_me_please")
PORT = int(os.getenv("PORT", "10000"))

ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")  # numeric telegram id as string

if not all([BOT_TOKEN, GOOGLE_SHEET_ID, CREDENTIALS_JSON]):
    raise ValueError("Не заданы переменные окружения: BOT_TOKEN / GOOGLE_SHEET_ID / GOOGLE_SHEETS_CREDENTIALS")

if not BASE_URL:
    raise ValueError("Не задан BASE_URL (или RENDER_EXTERNAL_URL). Пример: https://your-service.onrender.com")

BASE_URL = BASE_URL.rstrip("/")
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"

if not ADMIN_USER_ID:
    # Не падаем, но админка не будет работать корректно.
    print("[WARN] ADMIN_USER_ID не задан. /admin будет недоступен.")


# =========================
# TIMEZONE / REMINDER
# =========================
TZ = ZoneInfo("Europe/Berlin")
REMINDER_DAY = date(2026, 2, 10)
REMINDER_TIME_LOCAL = time(10, 0)  # 10:00 по Берлину


# =========================
# GOOGLE AUTH / SERVICES
# =========================
def get_creds():
    creds_dict = json.loads(CREDENTIALS_JSON)
    return Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )

def get_sheet_gspread():
    client = gspread.authorize(get_creds())
    return client.open_by_key(GOOGLE_SHEET_ID).sheet1

def get_sheets_service():
    return build("sheets", "v4", credentials=get_creds(), cache_discovery=False)


# =========================
# RUS HEADERS / KEYS
# =========================
H_USER_ID = "ID пользователя"
H_NAME = "Имя"
H_PHONE = "Телефон"
H_DATE = "Дата"
H_TIME = "Время"
H_STATUS = "Статус"
H_REMINDER_SENT = "Напоминание отправлено"
H_ATTENDANCE_CONFIRMED = "Подтверждение"

HEADERS_RU = [
    H_USER_ID,
    H_NAME,
    H_PHONE,
    H_DATE,
    H_TIME,
    H_STATUS,
    H_REMINDER_SENT,
    H_ATTENDANCE_CONFIRMED,
]

# Колонки (A..H)
COL_USER_ID = 1
COL_NAME = 2
COL_PHONE = 3
COL_DATE = 4
COL_TIME = 5
COL_STATUS = 6
COL_REMINDER_SENT = 7
COL_ATTENDANCE_CONFIRMED = 8


# =========================
# RUS STATUSES
# =========================
STATUS_BOOKED = "Записан"
STATUS_PENDING = "Ждёт подтверждения"

OCCUPYING_STATUSES = {STATUS_BOOKED, STATUS_PENDING}


def ensure_sheet_headers_ru_and_format():
    """
    1) Делает заголовки русскими (перезаписывает строку 1).
    2) Красиво форматирует лист: жирный заголовок, заливка, закрепление строки,
       авто-ширина колонок, фильтр по заголовкам.
    """
    sheet = get_sheet_gspread()
    sheet.update("A1:H1", [HEADERS_RU])

    svc = get_sheets_service()
    spreadsheet = svc.spreadsheets().get(spreadsheetId=GOOGLE_SHEET_ID).execute()
    sheets = spreadsheet.get("sheets", [])
    if not sheets:
        return

    sheet_id = sheets[0]["properties"]["sheetId"]

    requests = []

    # Freeze header row
    requests.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {"frozenRowCount": 1}
            },
            "fields": "gridProperties.frozenRowCount"
        }
    })

    # Header styling A1:H1
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0,
                "endRowIndex": 1,
                "startColumnIndex": 0,
                "endColumnIndex": 8
            },
            "cell": {
                "userEnteredFormat": {
                    "textFormat": {"bold": True},
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                    "backgroundColor": {"red": 0.95, "green": 0.95, "blue": 0.95}
                }
            },
            "fields": "userEnteredFormat(textFormat,horizontalAlignment,verticalAlignment,backgroundColor)"
        }
    })

    # Filter over columns A..H
    requests.append({
        "setBasicFilter": {
            "filter": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1_000_000,
                    "startColumnIndex": 0,
                    "endColumnIndex": 8
                }
            }
        }
    })

    # Auto resize columns A..H
    requests.append({
        "autoResizeDimensions": {
            "dimensions": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": 0,
                "endIndex": 8
            }
        }
    })

    svc.spreadsheets().batchUpdate(
        spreadsheetId=GOOGLE_SHEET_ID,
        body={"requests": requests}
    ).execute()


# =========================
# FSM
# =========================
class BookingStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()


# =========================
# BOT / DISPATCHER
# =========================
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# =========================
# EVENT / SLOTS
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


def days_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Четверг, 12 февраля", callback_data="day_2026-02-12")],
            [InlineKeyboardButton(text="Пятница, 13 февраля", callback_data="day_2026-02-13")],
        ]
    )


def manage_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Изменить время", callback_data="change_booking")],
            [InlineKeyboardButton(text="❌ Отменить запись", callback_data="cancel_booking")],
        ]
    )


def reminder_keyboard(row_index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтверждаю", callback_data=f"rem_yes_{row_index}")],
            [InlineKeyboardButton(text="❌ Отменить запись", callback_data=f"rem_cancel_{row_index}")],
        ]
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📣 Разослать напоминания сейчас", callback_data="admin_send_reminders")],
        ]
    )


def admin_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, разослать", callback_data="admin_send_reminders_confirm")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_send_reminders_cancel")],
        ]
    )


def is_admin(user_id: int) -> bool:
    if not ADMIN_USER_ID:
        return False
    return str(user_id) == str(ADMIN_USER_ID).strip()


def reset_slots():
    for d in SLOTS:
        for t in SLOTS[d]:
            SLOTS[d][t] = False


def load_bookings_from_sheet():
    """Пересобирает занятость слотов по русским статусам из таблицы."""
    try:
        reset_slots()
        sheet = get_sheet_gspread()
        records = sheet.get_all_records()
        for row in records:
            status = str(row.get(H_STATUS, "")).strip()
            if status in OCCUPYING_STATUSES:
                date_str = str(row.get(H_DATE, "")).strip()
                time_str = str(row.get(H_TIME, "")).strip()
                if date_str in SLOTS and time_str in SLOTS[date_str]:
                    SLOTS[date_str][time_str] = True
    except Exception as e:
        print(f"[load_bookings_from_sheet] error: {e}")


def find_user_active_booking(user_id: str):
    """Ищет активную запись по ID пользователя (строго 1 аккаунт = 1 слот)."""
    sheet = get_sheet_gspread()
    records = sheet.get_all_records()
    for i, row in enumerate(records, start=2):
        uid = str(row.get(H_USER_ID, "")).strip()
        status = str(row.get(H_STATUS, "")).strip()
        if uid == str(user_id) and status in OCCUPYING_STATUSES:
            return i, row
    return None, None


def slot_is_occupied_in_sheet(date_str: str, time_str: str) -> bool:
    sheet = get_sheet_gspread()
    records = sheet.get_all_records()
    for row in records:
        status = str(row.get(H_STATUS, "")).strip()
        if status in OCCUPYING_STATUSES and str(row.get(H_DATE, "")).strip() == date_str and str(row.get(H_TIME, "")).strip() == time_str:
            return True
    return False


# =========================
# ADMIN / UTIL
# =========================
@dp.message(Command("myid"))
async def myid(message: types.Message):
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору.")
        return

    await message.answer(
        "🛠 Админ-панель\n\n"
        "Отсюда можно вручную разослать напоминания всем записанным.",
        reply_markup=admin_keyboard()
    )


@dp.callback_query(lambda c: c.data == "admin_send_reminders")
async def admin_send_reminders(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return

    await callback.message.edit_text(
        "⚠️ Вы уверены, что хотите разослать напоминания всем записанным?\n\n"
        "Будут отправлены сообщения с кнопками подтверждения/отмены.",
        reply_markup=admin_confirm_keyboard()
    )


@dp.callback_query(lambda c: c.data == "admin_send_reminders_cancel")
async def admin_send_reminders_cancel(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return

    await callback.message.edit_text(
        "Ок, отменено. Если нужно — нажмите /admin ещё раз."
    )


async def send_reminders_now(force: bool) -> tuple[int, int]:
    """
    Рассылает напоминания всем, у кого активная запись на наши слоты.
    Возвращает (sent_ok, sent_fail).

    force=True: игнорирует 'Напоминание отправлено' (перешлёт даже если уже отправляли).
    force=False: отправляет только тем, кому ещё не отправляли.
    """
    sent_ok = 0
    sent_fail = 0

    sheet = get_sheet_gspread()
    records = sheet.get_all_records()

    now = datetime.now(TZ)

    for idx, row in enumerate(records, start=2):
        status = str(row.get(H_STATUS, "")).strip()
        if status not in OCCUPYING_STATUSES:
            continue

        d = str(row.get(H_DATE, "")).strip()
        t = str(row.get(H_TIME, "")).strip()
        user_id = str(row.get(H_USER_ID, "")).strip()

        # только наши даты/слоты
        if d not in SLOTS or t not in SLOTS[d]:
            continue

        reminder_sent = str(row.get(H_REMINDER_SENT, "")).strip()
        if reminder_sent and not force:
            continue

        # Переводим в "ждёт подтверждения", слот всё равно занят
        try:
            sheet.update_cell(idx, COL_STATUS, STATUS_PENDING)
        except Exception:
            pass

        text = (
            "🔔 Напоминание о записи!\n\n"
            f"📅 Дата: {d}\n"
            f"🕗 Время: {t}\n\n"
            "Пожалуйста, подтвердите, что вы придёте:\n"
            "✅ Подтверждаю — всё ок\n"
            "❌ Отменить — освободим слот для других"
        )

        try:
            await bot.send_message(chat_id=int(user_id), text=text, reply_markup=reminder_keyboard(idx))
            # пишем дату/время отправки (обновим всегда при force)
            sheet.update_cell(idx, COL_REMINDER_SENT, now.strftime("%Y-%m-%d %H:%M:%S"))
            sent_ok += 1
        except Exception as e:
            print(f"[reminder send] to {user_id} row {idx} failed: {e}")
            sent_fail += 1

    return sent_ok, sent_fail


@dp.callback_query(lambda c: c.data == "admin_send_reminders_confirm")
async def admin_send_reminders_confirm(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return

    await callback.message.edit_text("⏳ Рассылаю напоминания…")

    try:
        ok, fail = await send_reminders_now(force=True)
        await callback.message.edit_text(
            f"✅ Готово!\n\nОтправлено: {ok}\nНе доставлено: {fail}\n\n"
            "Если нужно — можно нажать /admin и разослать ещё раз."
        )
    except Exception as e:
        print(f"[admin_send_reminders_confirm] error: {e}")
        await callback.message.edit_text("❌ Ошибка при рассылке. Посмотрите логи Render.")


# =========================
# USER FLOW
# =========================
@dp.message(Command("start"))
async def send_welcome(message: types.Message, state: FSMContext):
    await state.clear()
    load_bookings_from_sheet()

    user_id = str(message.from_user.id)
    row_index, row = None, None
    try:
        row_index, row = find_user_active_booking(user_id)
    except Exception as e:
        print(f"[send_welcome] error: {e}")

    if row_index and row:
        date_str = str(row.get(H_DATE, ""))
        time_str = str(row.get(H_TIME, ""))
        status = str(row.get(H_STATUS, ""))
        extra = "\n\n⚠️ Мы ждём подтверждение по напоминанию." if status == STATUS_PENDING else ""

        await message.answer(
            "✅ У вас уже есть активная запись.\n\n"
            f"📅 Дата: {date_str}\n"
            f"🕗 Время: {time_str}\n"
            f"📌 Статус: {status}{extra}\n\n"
            "Вы можете изменить время или отменить запись:",
            reply_markup=manage_keyboard()
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

    if mode != "change":
        try:
            row_index, row = find_user_active_booking(user_id)
            if row_index and row:
                await callback.answer("У вас уже есть активная запись.", show_alert=True)
                await callback.message.edit_text(
                    "✅ У вас уже есть активная запись.\n\n"
                    f"📅 Дата: {row.get(H_DATE)}\n"
                    f"🕗 Время: {row.get(H_TIME)}\n"
                    f"📌 Статус: {row.get(H_STATUS)}\n\n"
                    "Вы можете изменить время или отменить запись:",
                    reply_markup=manage_keyboard()
                )
                return
        except Exception as e:
            print(f"[choose_time] limit check error: {e}")

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

    # === СМЕНА ВРЕМЕНИ (без повторного ввода) ===
    if mode == "change":
        try:
            sheet_row = int(data["sheet_row"])
            old_date = str(data["old_date"])
            old_time = str(data["old_time"])

            if slot_is_occupied_in_sheet(date_str, time_str):
                await callback.answer("Этот слот только что заняли. Выберите другой.", show_alert=True)
                return

            sheet = get_sheet_gspread()
            sheet.update_cell(sheet_row, COL_DATE, date_str)
            sheet.update_cell(sheet_row, COL_TIME, time_str)
            sheet.update_cell(sheet_row, COL_STATUS, STATUS_BOOKED)

            if old_date in SLOTS and old_time in SLOTS[old_date]:
                SLOTS[old_date][old_time] = False
            SLOTS[date_str][time_str] = True

            await state.clear()
            await callback.message.edit_text(
                "✅ Запись изменена!\n\n"
                f"📅 Дата: {date_str}\n"
                f"🕗 Время: {time_str}\n"
                f"📌 Статус: {STATUS_BOOKED}",
                reply_markup=manage_keyboard()
            )
            return

        except Exception as e:
            print(f"[change slot] error: {e}")
            await callback.answer("Не удалось изменить запись. Попробуйте позже.", show_alert=True)
            return

    # === НОВАЯ ЗАПИСЬ: 1 аккаунт = 1 слот ===
    try:
        row_index, row = find_user_active_booking(user_id)
        if row_index and row:
            await callback.answer("У вас уже есть активная запись.", show_alert=True)
            await callback.message.edit_text(
                "✅ У вас уже есть активная запись.\n\n"
                f"📅 Дата: {row.get(H_DATE)}\n"
                f"🕗 Время: {row.get(H_TIME)}\n"
                f"📌 Статус: {row.get(H_STATUS)}\n\n"
                "Вы можете изменить время или отменить запись:",
                reply_markup=manage_keyboard()
            )
            await state.clear()
            return
    except Exception as e:
        print(f"[start_booking] limit check error: {e}")

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

    # супер-строго: 1 аккаунт = 1 слот
    try:
        row_index, row = find_user_active_booking(user_id)
        if row_index and row:
            await message.answer(
                "✅ У вас уже есть активная запись.\n\n"
                f"📅 Дата: {row.get(H_DATE)}\n"
                f"🕗 Время: {row.get(H_TIME)}\n"
                f"📌 Статус: {row.get(H_STATUS)}\n\n"
                "Вы можете изменить время или отменить запись:",
                reply_markup=manage_keyboard()
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

    load_bookings_from_sheet()
    if SLOTS.get(date_str, {}).get(time_str) is None or SLOTS[date_str][time_str]:
        await message.answer("❌ Увы, этот слот только что заняли. Выберите другое время: /start")
        await state.clear()
        return

    if slot_is_occupied_in_sheet(date_str, time_str):
        SLOTS[date_str][time_str] = True
        await message.answer("❌ Увы, этот слот только что заняли. Выберите другое время: /start")
        await state.clear()
        return

    try:
        sheet = get_sheet_gspread()
        sheet.append_row([user_id, name, phone, date_str, time_str, STATUS_BOOKED, "", ""])
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
        f"📞 Телефон: {phone}\n"
        f"📌 Статус: {STATUS_BOOKED}",
        reply_markup=manage_keyboard()
    )
    await state.clear()


# =========================
# MANAGE BUTTONS
# =========================
@dp.callback_query(lambda c: c.data == "cancel_booking")
async def cancel_booking(callback: types.CallbackQuery, state: FSMContext):
    user_id = str(callback.from_user.id)
    try:
        sheet = get_sheet_gspread()
        row_index, row = find_user_active_booking(user_id)
        if not row_index:
            await callback.answer("У вас нет активной записи.", show_alert=True)
            return

        date_str = str(row.get(H_DATE))
        time_str = str(row.get(H_TIME))

        sheet.delete_rows(row_index)

        if date_str in SLOTS and time_str in SLOTS[date_str]:
            SLOTS[date_str][time_str] = False

    except Exception as e:
        print(f"[cancel_booking] error: {e}")
        await callback.answer("Ошибка при отмене. Попробуйте позже.", show_alert=True)
        return

    await state.clear()
    await callback.message.edit_text("✅ Запись отменена и удалена.\n\nЧтобы записаться снова: /start")


@dp.callback_query(lambda c: c.data == "change_booking")
async def change_booking(callback: types.CallbackQuery, state: FSMContext):
    user_id = str(callback.from_user.id)
    try:
        row_index, row = find_user_active_booking(user_id)
        if not row_index:
            await callback.answer("У вас нет активной записи.", show_alert=True)
            return

        old_date = str(row.get(H_DATE))
        old_time = str(row.get(H_TIME))

        await state.update_data(mode="change", sheet_row=row_index, old_date=old_date, old_time=old_time)

    except Exception as e:
        print(f"[change_booking] error: {e}")
        await callback.answer("Ошибка. Попробуйте позже.", show_alert=True)
        return

    load_bookings_from_sheet()
    await callback.message.edit_text("Выберите новый день:", reply_markup=days_keyboard())


# =========================
# REMINDER CONFIRM / CANCEL
# =========================
@dp.callback_query(lambda c: c.data.startswith("rem_yes_"))
async def reminder_yes(callback: types.CallbackQuery):
    try:
        row_index = int(callback.data.split("_")[-1])
        sheet = get_sheet_gspread()

        user_id = str(callback.from_user.id)
        row_vals = sheet.row_values(row_index)
        if not row_vals or len(row_vals) < 6:
            await callback.answer("Запись не найдена.", show_alert=True)
            return

        if str(row_vals[COL_USER_ID - 1]).strip() != user_id:
            await callback.answer("Это не ваша запись.", show_alert=True)
            return

        sheet.update_cell(row_index, COL_STATUS, STATUS_BOOKED)
        sheet.update_cell(row_index, COL_ATTENDANCE_CONFIRMED, "Подтверждено ✅")

        await callback.message.edit_text("✅ Отлично! Мы вас ждём. До встречи на мероприятии 🙂")

    except Exception as e:
        print(f"[reminder_yes] error: {e}")
        await callback.answer("Не удалось подтвердить. Попробуйте позже.", show_alert=True)


@dp.callback_query(lambda c: c.data.startswith("rem_cancel_"))
async def reminder_cancel(callback: types.CallbackQuery):
    try:
        row_index = int(callback.data.split("_")[-1])
        sheet = get_sheet_gspread()

        user_id = str(callback.from_user.id)
        row_vals = sheet.row_values(row_index)
        if not row_vals or len(row_vals) < 6:
            await callback.answer("Запись не найдена.", show_alert=True)
            return

        if str(row_vals[COL_USER_ID - 1]).strip() != user_id:
            await callback.answer("Это не ваша запись.", show_alert=True)
            return

        date_str = str(row_vals[COL_DATE - 1]).strip()
        time_str = str(row_vals[COL_TIME - 1]).strip()

        sheet.delete_rows(row_index)

        if date_str in SLOTS and time_str in SLOTS[date_str]:
            SLOTS[date_str][time_str] = False

        await callback.message.edit_text("✅ Запись отменена и удалена.\n\nЕсли передумаете — можно записаться снова: /start")

    except Exception as e:
        print(f"[reminder_cancel] error: {e}")
        await callback.answer("Не удалось отменить. Попробуйте позже.", show_alert=True)


# =========================
# AUTO REMINDER LOOP (best-effort on free)
# =========================
async def send_reminders_if_needed():
    """
    10 февраля (по Берлину) — best-effort. На бесплатном Render может не сработать,
    если сервис спит. Для надёжности используйте /admin.
    """
    try:
        now = datetime.now(TZ)
        if now.date() != REMINDER_DAY:
            return
        if now.time() < REMINDER_TIME_LOCAL:
            return

        # только тем, кому ещё не отправляли
        ok, fail = await send_reminders_now(force=False)
        if ok or fail:
            print(f"[auto reminders] ok={ok} fail={fail}")

    except Exception as e:
        print(f"[send_reminders_if_needed] error: {e}")


async def reminder_loop():
    while True:
        await send_reminders_if_needed()
        await asyncio.sleep(600)


# =========================
# WEBHOOK LIFECYCLE
# =========================
async def on_startup(app: web.Application):
    try:
        ensure_sheet_headers_ru_and_format()
    except Exception as e:
        print(f"[format sheet] error: {e}")

    load_bookings_from_sheet()

    await bot.set_webhook(WEBHOOK_URL)
    print(f"Webhook set to: {WEBHOOK_URL}")

    app["reminder_task"] = asyncio.create_task(reminder_loop())


async def on_shutdown(app: web.Application):
    task = app.get("reminder_task")
    if task:
        task.cancel()

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
