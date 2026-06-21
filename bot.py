import asyncio
import os
import sqlite3
import datetime
import re
from flask import Flask
from threading import Thread
import requests
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, MessageHandler, filters, ContextTypes)
from telegram.error import TelegramError

# ----- НАСТРОЙКИ (замените ссылку на канал) -----
TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = -1002415978372          # ID канала для проверки подписки
ADMIN_IDS = [5271825622]            # ваш Telegram ID
CHANNEL_LINK = "https://t.me/AiFinVibe"   # ← замените на реальную ссылку
# ------------------------------------------------

# --- База данных (только для учёта сообщений) ---
DB_PATH = "posts.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS message_log (
            user_id INTEGER,
            chat_id INTEGER,
            timestamp REAL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_msg_log ON message_log(user_id, chat_id, timestamp)")
    conn.commit()
    conn.close()

def count_user_messages_last_24h(user_id, chat_id):
    since = datetime.datetime.now() - datetime.timedelta(hours=24)
    timestamp_since = since.timestamp()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM message_log WHERE user_id=? AND chat_id=? AND timestamp >= ?",
              (user_id, chat_id, timestamp_since))
    count = c.fetchone()[0]
    conn.close()
    return count

def log_message(user_id, chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO message_log (user_id, chat_id, timestamp) VALUES (?, ?, ?)",
              (user_id, chat_id, datetime.datetime.now().timestamp()))
    conn.commit()
    conn.close()

def cleanup_old_logs():
    since = datetime.datetime.now() - datetime.timedelta(hours=24)
    timestamp_since = since.timestamp()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM message_log WHERE timestamp < ?", (timestamp_since,))
    conn.commit()
    conn.close()

# --- Служебная часть для Render.com ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

def ping_self():
    app_url = os.environ.get("RENDER_EXTERNAL_URL")
    if app_url:
        while True:
            time.sleep(600)
            try:
                requests.get(app_url)
                print(f"Пингую сам себя: {app_url}")
            except Exception as e:
                print(f"Ошибка пинга: {e}")

# --- Логика проверок ---
async def is_subscribed(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status not in ["left", "kicked"]
    except TelegramError:
        return False

async def delete_warning_later(bot, chat_id: int, message_id: int):
    await asyncio.sleep(60)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError as e:
        print(f"Не удалось удалить предупреждение: {e}")

def contains_link(message):
    """Проверяет, есть ли в сообщении ссылки."""
    if message.entities:
        if any(e.type in ("url", "text_link") for e in message.entities):
            return True
    text = message.text or message.caption or ""
    if re.search(r"https?://", text):
        return True
    return False

async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.from_user:
        return

    user = message.from_user
    user_id = user.id
    chat_id = message.chat_id

    # Администраторы и сам бот проходят без ограничений
    if user_id in ADMIN_IDS or user_id == context.bot.id:
        return

    # 1. Проверка подписки на канал
    if not await is_subscribed(user_id, context):
        try:
            await message.delete()
        except TelegramError:
            pass
        # Публичное предупреждение (единственное)
        name = f"@{user.username}" if user.username else user.first_name or "Пользователь"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Вступить", url=CHANNEL_LINK)]])
        reply = await message.chat.send_message(
            f"{name}, для отправки сообщений необходимо присоединиться",
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        asyncio.create_task(delete_warning_later(context.bot, reply.chat_id, reply.message_id))
        return

    # 2. Запрет ссылок (молчаливое удаление)
    if contains_link(message):
        try:
            await message.delete()
        except TelegramError:
            pass
        return

    # 3. Лимит 2 сообщения в сутки (молчаливое удаление)
    if count_user_messages_last_24h(user_id, chat_id) >= 2:
        try:
            await message.delete()
        except TelegramError:
            pass
        return

    # Если всё в порядке — записываем сообщение в лог
    log_message(user_id, chat_id)

# --- Запуск ---
def main():
    init_db()
    cleanup_old_logs()

    # Потоки для Flask и самопинга
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    ping_thread = Thread(target=ping_self)
    ping_thread.daemon = True
    ping_thread.start()

    application = Application.builder().token(TOKEN).build()

    # Обработчик сообщений в группах
    application.add_handler(
        MessageHandler(
            filters.ALL & filters.ChatType.GROUPS & ~filters.COMMAND,
            check_subscription
        )
    )

    print("Бот запущен и ждёт сообщений...")
    application.run_polling()

if __name__ == "__main__":
    main()
