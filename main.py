import os
from aiogram import Bot, Dispatcher, executor, types
from datetime import datetime, timedelta

# Получаем токен из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# === Хранилище бронирований (в памяти) ===
# Ключ: "2026-02-12", значение: словарь {"10:00": user_id или None}
bookings = {
    "2026-02-12": {f"{h:02d}:{m:02d}": None for h in range(10, 20) for m in (0, 30)},
    "2026-02-13": {f"{h:02d}:{m:02d}": None for h in range(10, 20) for m in (0, 30)},
}

EVENT_INFO = (
    "🎉 Доброшествуем на наше мероприятие!\n\n"
    "📅 Доступные дни:\n"
    "• Четверг, 12 февраля 2026\n"
    "• Пятница, 13 февраля 2026\n\n"
    "🕗 Время: с 10:00 до 20:00\n"
    "⏳ Слоты по 30 минут\n"
    "👥 Один человек на слот\n\n"
    "👉 Выберите день ниже:"
)

@dp.message_handler(commands=["start"])
async def send_welcome(message: types.Message):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("Четверг, 12 февраля", callback_data="day_2026-02-12"),
        types.InlineKeyboardButton("Пятница, 13 февраля", callback_data="day_2026-02-13")
    )
    await message.answer(EVENT_INFO, reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data.startswith("day_"))
async def choose_time(callback: types.CallbackQuery):
    date_str = callback.data.split("_")[1]  # например: "2026-02-12"
    
    if date_str not in bookings:
        await callback.answer("Неверная дата", show_alert=True)
        return

    # Получаем свободные слоты
    free_slots = [
        time for time, user in bookings[date_str].items() if user is None
    ]
    
    if not free_slots:
        await callback.message.edit_text("❌ На этот день все слоты заняты!")
        return

    # Сортируем (хотя они и так отсортированы)
    free_slots.sort()
    
    # Telegram ограничивает кнопки — покажем максимум 20
    buttons = [
        [types.InlineKeyboardButton(f"{date_str} {t}", callback_data=f"slot_{date_str}_{t}")]
        for t in free_slots[:20]
    ]
    back_button = [types.InlineKeyboardButton("⬅️ Назад к выбору дня", callback_data="back")]
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=buttons + [back_button])
    
    await callback.message.edit_text(f"Выберите время на {date_str}:", reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data == "back")
async def go_back(callback: types.CallbackQuery):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("Четверг, 12 февраля", callback_data="day_2026-02-12"),
        types.InlineKeyboardButton("Пятница, 13 февраля", callback_data="day_2026-02-13")
    )
    await callback.message.edit_text(EVENT_INFO, reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data.startswith("slot_"))
async def book_slot(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    if len(parts) != 3:
        await callback.answer("Ошибка", show_alert=True)
        return
        
    date_str, time_str = parts[1], parts[2]
    
    if date_str not in bookings or time_str not in bookings[date_str]:
        await callback.answer("Слот не найден", show_alert=True)
        return

    if bookings[date_str][time_str] is not None:
        await callback.answer("Этот слот уже занят!", show_alert=True)
        return

    # Бронируем
    user_id = callback.from_user.id
    name = callback.from_user.full_name
    bookings[date_str][time_str] = user_id

    await callback.message.edit_text(
        f"✅ Вы успешно записаны!\n\n📅 Дата: {date_str}\n🕗 Время: {time_str}\n👤 {name}"
    )

if __name__ == "__main__":
    print("Бот запущен...")
    executor.start_polling(dp, skip_updates=True)