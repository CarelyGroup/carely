import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from datetime import datetime, timedelta
import asyncio

# Получаем токен из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# === Хранилище бронирований (в памяти) ===
bookings = {
    "2026-02-12": {f"{h:02d}:{m:02d}": None for h in range(10, 20) for m in (0, 30)},
    "2026-02-13": {f"{h:02d}:{m:02d}": None for h in range(10, 20) for m in (0, 30)},
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

@dp.message(Command("start"))
async def send_welcome(message: types.Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton("Четверг, 12 февраля", callback_data="day_2026-02-12")],
            [InlineKeyboardButton("Пятница, 13 февраля", callback_data="day_2026-02-13")]
        ]
    )
    await message.answer(EVENT_INFO, reply_markup=keyboard)

@dp.callback_query(lambda c: c.data.startswith("day_"))
async def choose_time(callback: types.CallbackQuery):
    date_str = callback.data.split("_")[1]
    
    if date_str not in bookings:
       
