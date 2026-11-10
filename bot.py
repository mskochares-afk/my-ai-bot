import asyncio
import io
import base64
import requests
import re
import os
import sqlite3
import random
import string
from datetime import datetime, timedelta
from threading import Thread
from flask import Flask
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import LabeledPrice, PreCheckoutQuery
from groq import Groq
from pypdf import PdfReader

# ==========================================
# НАСТРОЙКИ
# ==========================================
TELEGRAM_TOKEN = os.environ.get("9169161664:AAHBeKyRTDDnUinmKKuVZILP3HmW9zSE9gQ")
GROQ_API_KEY = os.environ.get("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")
OPENROUTER_API_KEY = os.environ.get("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")

# ВСТАВЬ СЮДА СВОЙ ID ИЗ @userinfobot (цифры)
ADMIN_ID = 123456789 

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
client = Groq(api_key=GROQ_API_KEY)

app = Flask('')
@app.route('/')
def home(): return "Бот работает!"
def run(): app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
def keep_alive(): Thread(target=run).start()

# ==========================================
# БАЗА ДАННЫХ
# ==========================================
def init_db():
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    # Таблица пользователей
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY, sub_type TEXT, sub_expires TEXT, free_count INTEGER, last_reset TEXT)''')
    # Таблица кодов
    c.execute('''CREATE TABLE IF NOT EXISTS codes (code TEXT PRIMARY KEY, is_used INTEGER)''')
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    user = c.fetchone()
    if not user:
        now = datetime.now().isoformat()
        c.execute("INSERT INTO users VALUES (?, 'free', NULL, 3, ?)", (user_id, now))
        conn.commit()
        user = (user_id, 'free', None, 3, now)
    conn.close()
    return user

def update_user(user_id, sub_type, sub_expires, free_count, last_reset):
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("UPDATE users SET sub_type=?, sub_expires=?, free_count=?, last_reset=? WHERE id=?",
              (sub_type, sub_expires, free_count, last_reset, user_id))
    conn.commit()
    conn.close()

def check_access(user_id):
    """Проверяет, может ли пользователь писать"""
    if user_id == ADMIN_ID: return True, ""
    
    user = get_user(user_id)
    sub_type, sub_expires, free_count, last_reset_str = user[1], user[2], user[3], user[4]
    now = datetime.now()
    
    # Вечная подписка
    if sub_type == 'lifetime': return True, ""
    
    # Подписка по Stars
    if sub_type == 'stars':
        if sub_expires and datetime.fromisoformat(sub_expires) > now:
            return True, ""
        else:
            # Подписка истекла, сбрасываем на бесплатную
            update_user(user_id, 'free', None, 3, now.isoformat())
            return check_access(user_id) # Рекурсивно проверяем дальше

    # Бесплатный тариф
    if sub_type == 'free':
        last_reset = datetime.fromisoformat(last_reset_str)
        if now - last_reset > timedelta(days=3):
            # Прошло 3 дня, сбрасываем счетчик
            update_user(user_id, 'free', None, 3, now.isoformat())
            free_count = 3
            last_reset = now
            
        if free_count > 0:
            # Тратим попытку
            update_user(user_id, 'free', None, free_count - 1, last_reset.isoformat())
            return True, ""
        else:
            # Ждем 3 дня
            next_reset = last_reset + timedelta(days=3)
            time_left = next_reset - now
            days = time_left.days
            hours = time_left.seconds // 3600
            return False, f"😔 Бесплатные попытки закончились.\nСледующие 3 запроса будут доступны через {days} д. {hours} ч.\n\nКупи подписку: /buy"

    return False, "Ошибка доступа."

# ==========================================
# ИИ ЛОГИКА (НЕ ТРОГАЕМ)
# ==========================================
user_histories = {}
TEXT_MODEL = "openai/gpt-oss-120b"

def get_working_vision_model():
    try:
        response = requests.get("https://openrouter.ai/api/v1/models", timeout=10)
        models = response.json()["data"]
        for m in models:
            if m["id"].endswith(":free"):
                if "vision" in m["id"].lower() or "vl" in m["id"].lower() or "gemini" in m["id"].lower():
                    return m["id"]
    except: pass
    return "qwen/qwen-2.5-vl-72b-instruct:free"

VISION_MODEL = get_working_vision_model()

def clean_latex(text):
    if not text: return text
    text = re.sub(r'\$\$', '', text)
    text = re.sub(r'\\\[', '', text)
    text = re.sub(r'\\\]', '', text)
    text = re.sub(r'\\\(', '', text)
    text = re.sub(r'\\\)', '', text)
    text = re.sub(r'\\frac\{(.*?)\}\{(.*?)\}', r'\1/\2', text)
    text = text.replace('\\quad', ' ').replace('\\left', '').replace('\\right', '')
    return text

def split_message(text, max_length=4000):
    parts = []
    while len(text) > max_length:
        split_index = text.rfind('\n', 0, max_length)
        if split_index == -1: split_index = max_length
        parts.append(text[:split_index])
        text = text[split_index:]
    parts.append(text)
    return parts

async def send_long_message(chat_id, text):
    text = clean_latex(text)
    parts = split_message(text)
    for part in parts:
        try: await bot.send_message(chat_id, part, parse_mode="Markdown")
        except: await bot.send_message(chat_id, part)

# ==========================================
# ОБРАБОТЧИКИ КОМАНД
# ==========================================
@dp.message(F.text.startswith("/start"))
async def cmd_start(message: types.Message):
    get_user(message.from_user.id)
    await message.answer("Привет! Я твой AI-ассистент.\n\n"
                         "У тебя есть 3 бесплатных запроса. Они обновляются каждые 3 дня.\n\n"
                         "💎 Купить подписку: /buy\n"
                         "🔑 Активировать код: /activate")

@dp.message(F.text.startswith("/buy"))
async def cmd_buy(message: types.Message):
    # Подписка на 30 дней за 50 Звезд (измени цену amount=50 на нужную)
    await bot.send_invoice(
        chat_id=message.chat.id,
        title="Подписка на 30 дней",
        description="Безлимитный доступ к ИИ на 30 дней!",
        payload="stars_sub_30",
        provider_token="", # Для Telegram Stars оставляем пустым
        currency="XTR",
        prices=[LabeledPrice(label="30 дней", amount=50)]
    )

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def success_payment(message: types.Message):
    expires = (datetime.now() + timedelta(days=30)).isoformat()
    update_user(message.from_user.id, 'stars', expires, 3, datetime.now().isoformat())
    await message.answer("🎉 Оплата прошла успешно! Твоя подписка активна 30 дней.")

# ==========================================
# АДМИН-ПАНЕЛЬ И АКТИВАЦИЯ
# ==========================================
@dp.message(F.text.startswith("/admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("👑 Привет, Админ!\n\n"
                         "Чтобы создать код для вечной подписки, напиши:\n"
                         "`/gencode`", parse_mode="Markdown")

@dp.message(F.text.startswith("/gencode"))
async def cmd_gencode(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    # Генерируем 14 символов
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=14))
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("INSERT INTO codes VALUES (?, 0)", (code,))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Код создан:\n`{code}`\n\nОтправь его клиенту. Он вводит: `/activate {code}`", parse_mode="Markdown")

@dp.message(F.text.startswith("/activate"))
async def cmd_activate(message: types.Message):
    try:
        code = message.text.split()[1].strip()
        conn = sqlite3.connect('bot.db')
        c = conn.cursor()
        c.execute("SELECT is_used FROM codes WHERE code = ?", (code,))
        result = c.fetchone()
        
        if not result:
            await message.answer("❌ Такой код не найден.")
        elif result[0] == 1:
            await message.answer("❌ Этот код уже был использован.")
        else:
            # Активируем вечную подписку
            update_user(message.from_user.id, 'lifetime', None, 3, datetime.now().isoformat())
            c.execute("UPDATE codes SET is_used = 1 WHERE code = ?", (code,))
            conn.commit()
            await message.answer("👑 Поздравляю! Вечная подписка активирована!")
        conn.close()
    except IndexError:
        await message.answer("Напиши код так: `/activate ТВОЙ_КОД`")

# ==========================================
# ОБРАБОТКА СООБЩЕНИЙ (С ПРОВЕРКОЙ)
# ==========================================
@dp.message(F.text)
async def handle_text(message: types.Message):
    if message.text.startswith("/"): return # Игнорируем команды
    
    access, error_msg = check_access(message.from_user.id)
    if not access:
        return await message.answer(error_msg)

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    chat_id = message.chat.id
    if chat_id not in user_histories:
        user_histories[chat_id] = [{"role": "system", "content": "Ты полезный ассистент."}]
    user_histories[chat_id].append({"role": "user", "content": message.text})
    if len(user_histories[chat_id]) > 12:
        user_histories[chat_id] = [user_histories[chat_id][0]] + user_histories[chat_id][-10:]
    try:
        chat_completion = client.chat.completions.create(messages=user_histories[chat_id], model=TEXT_MODEL)
        answer = chat_completion.choices[0].message.content
        user_histories[chat_id].append({"role": "assistant", "content": answer})
        await send_long_message(chat_id, answer)
    except Exception as e: await message.answer(f"Ошибка: {e}")

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    access, error_msg = check_access(message.from_user.id)
    if not access: return await message.answer(error_msg)
    
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_bytes = await bot.download_file(file.file_path)
    base64_image = base64.b64encode(file_bytes.read()).decode('utf-8')
    prompt = message.caption if message.caption else "Что на этой картинке? Опиши подробно."
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": VISION_MODEL, "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]}]}
    try:
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=60)
        res_json = response.json()
        if response.status_code == 200 and 'choices' in res_json:
            await send_long_message(message.chat.id, res_json['choices'][0]['message']['content'])
        else: await message.answer(f"Ошибка: {res_json.get('error', {}).get('message', 'Неизвестно')}")
    except Exception as e: await message.answer(f"Ошибка картинки: {e}")

@dp.message(F.document)
async def handle_document(message: types.Message):
    access, error_msg = check_access(message.from_user.id)
    if not access: return await message.answer(error_msg)

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    file = await bot.get_file(message.document.file_id)
    file_bytes = await bot.download_file(file.file_path)
    text = ""
    if message.document.file_name.endswith('.pdf'):
        reader = PdfReader(io.BytesIO(file_bytes.read()))
        for page in reader.pages: text += page.extract_text() + "\n"
    elif message.document.file_name.endswith('.txt'): text = file_bytes.read().decode('utf-8')
    else: return await message.answer("Я пока умею читать только PDF и TXT.")
    if not text.strip(): return await message.answer("Файл пуст.")
    prompt = message.caption if message.caption else "Сделай краткое содержание."
    full_prompt = f"{prompt}\n\nТекст:\n{text[:10000]}"
    try:
        chat_completion = client.chat.completions.create(messages=[{"role": "user", "content": full_prompt}], model=TEXT_MODEL)
        await send_long_message(message.chat.id, chat_completion.choices[0].message.content)
    except Exception as e: await message.answer(f"Ошибка файла: {e}")

async def main():
    init_db() # Инициализируем базу данных при старте
    keep_alive()
    print("Бот запущен в облаке!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
