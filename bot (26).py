import os
import sys
import asyncio
import logging
import base64
import re
from io import BytesIO
import aiohttp

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# ================= НАСТРОЙКИ И ЛОГИРОВАНИЕ =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

# Считываем переменные окружения, которые вы зададите на Bothost
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

if not BOT_TOKEN:
    logging.critical("ОШИБКА: Переменная окружения 'BOT_TOKEN' не установлена!")
    sys.exit(1)

if not OPENROUTER_API_KEY:
    logging.critical("ОШИБКА: Переменная окружения 'OPENROUTER_API_KEY' не установлена!")
    sys.exit(1)

# Актуальный список моделей с автоматическим переключением (Fallback)
MODELS = [
    "google/gemini-3-flash-preview",  # Основная модель (высокая скорость, отличное зрение)
    "anthropic/claude-haiku-4.5",     # Первый резерв (строгое следование промпту)
    "openai/gpt-5-nano"               # Второй резерв (легкая и быстрая модель)
]

# Системный промпт на основе ваших настроек ProTalk
SYSTEM_PROMPT = """Ты — эксперт по разбору составов косметики с клинико-дерматологическим подходом. Ты умеешь анализировать списки ингредиентов (INCI) и распознавать состав с фото. Твоя специализация — выявление потенциально нежелательных, спорных и рискованных компонентов. Ты не комментируешь полезные и нейтральные ингредиенты — только то, что может вызвать риск.

Цель: Помочь косметологу быстро и профессионально выявлять потенциально раздражающие, аллергенные или нежелательные ингредиенты в составах косметики. Давать краткие, клинически обоснованные оценки.

Работай по следующим правилам:
1. Не описывай полезные, нейтральные или «хорошие» компоненты.
2. Выделяй только: раздражающие вещества, аллергены, фотосенсибилизаторы, агрессивные ПАВы, щелочные базы, высокую кислотную нагрузку, комедогенные масла и плотные окклюзивы, эфирные масла, отдушки и красители, потенциально сенсибилизирующие консерванты, вещества, усиливающие проникновение раздражителей.
3. Учитывай суммарную нагрузку формулы — если несколько раздражающих компонентов присутствует одновременно, обязательно укажи это.
4. Пиши названия ингредиентов на русском языке.
5. Не используй нумерацию в ответе.
6. Не пиши длинные теоретические объяснения.
7. Тон — профессиональный, спокойный, клинический.
8. Без категоричных утверждений. Используй вероятностные формулировки: «может», «возможен риск», «при чувствительной коже».
9. Если состав короткий и явных рисков нет — укажи, что выраженных агрессивных компонентов не выявлено, но оценка зависит от концентраций.
10. Если пользователь прислал фото упаковки — распознай состав и применяй те же правила.
11. Если пользователь пишет что-то не связанное с анализом состава косметики — вежливо напомни, что ты специализируешься только на разборе ингредиентов.
12. Никогда не добавляй в конец ответа INLINE-подсказки, кнопки, дополнительные вопросы или любые служебные метки.

СТРУКТУРА ОТВЕТА ВСЕГДА СТРОГО ОДИНАКОВА:
Сначала перечисли каждый нежелательный компонент отдельным блоком — название на русском языке, затем 1–3 предложения объяснения риска.
Затем обязательно добавь раздел «⚠️ Итоговый вывод» с оценкой уровня раздражающей нагрузки, типа кожи в зоне риска и основного минуса формулы. Если явных рисков нет — напиши об этом прямо.

ВАЖНОЕ ПРАВИЛО ФОРМАТИРОВАНИЯ: Для выделения текста используй ТЕГИ HTML, например <b>жирный текст</b> или <i>курсив</i>. Не используй символы Markdown (такие как **, *, `), так как они вызовут ошибку отправки в Telegram."""

# ================= ИНИЦИАЛИЗАЦИЯ БОТА =================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================
def convert_markdown_to_html(text: str) -> str:
    """
    Защитная функция: если модель вопреки промпту пришлет markdown (знаки **),
    мы конвертируем их в HTML-теги <b>, чтобы избежать падения бота при отправке.
    """
    parts = text.split("**")
    new_text = ""
    for i, part in enumerate(parts):
        if i % 2 == 1:
            new_text += f"<b>{part}</b>"
        else:
            new_text += part
    # Дополнительно очищаем оставшиеся одиночные звездочки и знаки `
    new_text = new_text.replace("* ", "• ").replace("`", "")
    return new_text

# ================= КЛАВИАТУРЫ =================
def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ℹ️ Инструкция", callback_data="help_info")]
    ])

# ================= ВЗАИМОДЕЙСТВИЕ С OPENROUTER API =================
async def ask_openrouter(text: str = None, base64_image: str = None) -> str:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "X-Title": "Cosmetic Ingredients Analyzer Bot"
    }

    content = []
    if text:
        content.append({"type": "text", "text": text})
    else:
        content.append({"type": "text", "text": "Проанализируй состав косметического средства на этом изображении по правилам."})

    if base64_image:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
        })

    # Цикл по моделям (если первая недоступна или перегружена, сработает следующая)
    for model in MODELS:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content}
            ],
            "temperature": 0.2
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=45
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        raw_response = data['choices'][0]['message']['content']
                        return convert_markdown_to_html(raw_response)
                    else:
                        logging.warning(f"Модель {model} вернула статус {response.status}. Пробую следующую...")
                        continue
        except Exception as e:
            logging.error(f"Ошибка при запросе к модели {model}: {e}")
            continue

    return "❌ К сожалению, все доступные нейросети сейчас перегружены или недоступны. Пожалуйста, повторите запрос позже."

# ================= ХЭНДЛЕРЫ КОМАНД И КНОПОК =================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    welcome_text = (
        "<b>Привет!</b> На связи эксперт по клиническому разбору косметических составов. 🔬

"
        "Пришлите мне <b>текст состава (INCI)</b> или <b>фотографию этикетки</b>, где четко виден список ингредиентов.

"
        "Я проанализирую формулу и укажу только на потенциально нежелательные или раздражающие компоненты."
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard())

@dp.callback_query(F.data == "help_info")
async def process_help(callback: CallbackQuery):
    help_text = (
        "📋 <b>Как правильно использовать бота:</b>

"
        "1. <b>Фотография:</b> Сделайте четкий снимок состава на упаковке при хорошем освещении. Текст не должен быть смазан или перекрыт бликами.
"
        "2. <b>Текст:</b> Скопируйте состав с сайта интернет-магазина и отправьте обычным сообщением.

"
        "<i>Бот игнорирует полезные и нейтральные базы, фокусируясь исключительно на триггерах аллергии, комедогенности, фотосенсибилизации и агрессивных ПАВ.</i>"
    )
    await callback.message.answer(help_text)
    await callback.answer()

# ================= ОБРАБОТКА ВХОДЯЩИХ ДАННЫХ =================
@dp.message(F.text)
async def handle_text_ingredients(message: Message):
    # Отправляем "процесс думы"
    status_msg = await message.answer("⏳ <i>Анализирую текстовый состав... Пожалуйста, подождите.</i>")
    
    # Запрос к нейросети
    result = await ask_openrouter(text=message.text)
    
    # Обновляем сообщение готовым результатом
    await status_msg.edit_text(result)

@dp.message(F.photo)
async def handle_photo_ingredients(message: Message):
    status_msg = await message.answer("🔍 <i>Сканирую изображение и распознаю ингредиенты...</i>")
    
    try:
        # Получаем фото в максимальном разрешении
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        
        # Скачиваем файл в оперативную память
        photo_bytes = BytesIO()
        await bot.download_file(file_info.file_path, photo_bytes)
        
        # Переводим в Base64 для передачи через API
        base64_image = base64.b64encode(photo_bytes.getvalue()).decode('utf-8')
        
        # Если к фото было прикреплено текстовое описание
        caption = message.caption if message.caption else None
        
        # Изменяем статус перед отправкой в нейросеть
        await status_msg.edit_text("🔬 <i>Формирую клинический разбор формулы...</i>")
        
        result = await ask_openrouter(text=caption, base64_image=base64_image)
        await status_msg.edit_text(result)
        
    except Exception as e:
        logging.error(f"Ошибка при обработке фотографии: {e}")
        await status_msg.edit_text("❌ Произошла ошибка при обработке фото. Убедитесь, что файл отправлен как изображение, а не документ.")

# ================= ЗАПУСК БОТА =================
async def main():
    # Удаляем вебхуки и очищаем очередь старых сообщений перед стартом
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот успешно запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
