import asyncio
import io
import base64
import requests
import re
import os
from threading import Thread
from flask import Flask
from aiogram import Bot, Dispatcher, types, F
from groq import Groq
from pypdf import PdfReader

# ==========================================
# ТЕПЕРЬ КЛЮЧИ БЕРУТСЯ ИЗ НАСТРОЕК СЕРВЕРА
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "9169161664:AAHBeKyRTDDnUinmKKuVZILP3HmW9zSE9gQ")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "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")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
client = Groq(api_key=GROQ_API_KEY)

app = Flask('')
@app.route('/')
def home(): return "Бот работает!"

def run(): app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
def keep_alive(): Thread(target=run).start()

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
    text = text.replace('\\times', '*').replace('\\cdot', '*')
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

@dp.message(F.text)
async def handle_text(message: types.Message):
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
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_bytes = await bot.download_file(file.file_path)
    base64_image = base64.b64encode(file_bytes.read()).decode('utf-8')
    prompt = message.caption if message.caption else "Что на этой картинке? Опиши подробно или реши задачу, если это фото задания."
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": VISION_MODEL, "messages": [{"role": "user", "content": [{"type": "text", "text": prompt + " (НЕ ИСПОЛЬЗУЙ LaTeX!)"}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]}]}
    try:
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=60)
        res_json = response.json()
        if response.status_code == 200 and 'choices' in res_json:
            await send_long_message(message.chat.id, res_json['choices'][0]['message']['content'])
        else: await message.answer(f"Ошибка: {res_json.get('error', {}).get('message', 'Неизвестно')}")
    except Exception as e: await message.answer(f"Ошибка картинки: {e}")

@dp.message(F.document)
async def handle_document(message: types.Message):
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
    keep_alive()
    print("Бот запущен в облаке!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
