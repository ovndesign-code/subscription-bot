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
from telegram.ext import (Application, MessageHandler, CommandHandler, filters, ContextTypes)
from telegram.error import TelegramError

# ----- НАСТРОЙКИ -----
TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = -1002415978372
ADMIN_IDS = [5271825622]                  # ваш Telegram ID
CHANNEL_LINK = "https://t.me/yourchannel" # ← замените на реальную ссылку
# ---------------------

# --- База данных ---
DB_PATH = "posts.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Лог сообщений
    c.execute("""
        CREATE TABLE IF NOT EXISTS message_log (
            user_id INTEGER,
            chat_id INTEGER,
            timestamp REAL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_msg_log ON message_log(user_id, chat_id, timestamp)")
    # VIP-пользователи (пересоздаём, если таблицы нет)
    c.execute("""
        CREATE TABLE IF NOT EXISTS vip_users (
            user_id INTEGER NOT NULL,
            chat_id INTEGER,
            PRIMARY KEY (user_id, chat_id)
        )
    """)
    conn.commit()
    conn.close()

# --- VIP-функции ---
def is_vip(user_id, chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM vip_users WHERE user_id=? AND (chat_id IS NULL OR chat_id=?)",
              (user_id, chat_id))
    res = c.fetchone()
    conn.close()
    return res is not None

def add_vip(user_id, chat_id=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO vip_users (user_id, chat_id) VALUES (?, ?)",
              (user_id, chat_id))
    conn.commit()
    conn.close()

def remove_vip(user_id, chat_id=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if chat_id is None:
        c.execute("DELETE FROM vip_users WHERE user_id=?", (user_id,))
    else:
        c.execute("DELETE FROM vip_users WHERE user_id=? AND chat_id=?", (user_id, chat_id))
    conn.commit()
    conn.close()

def get_all_vips():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, chat_id FROM vip_users ORDER BY user_id")
    rows = c.fetchall()
    conn.close()
    return rows

# --- Функции лимита ---
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

# --- Служебная часть Render ---
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

# --- Проверки ---
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

    # VIP (глобальный или в этом чате) – полностью без проверок
    if is_vip(user_id, chat_id):
        log_message(user_id, chat_id)
        return

    # 1. Подписка на канал
    if not await is_subscribed(user_id, context):
        try:
            await message.delete()
        except TelegramError:
            pass
        name = f"@{user.username}" if user.username else user.first_name or "Пользователь"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Вступить", url=CHANNEL_LINK)]])
        reply = await message.chat.send_message(
            f"{name}, для отправки сообщений необходимо присоединиться",
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        asyncio.create_task(delete_warning_later(context.bot, reply.chat_id, reply.message_id))
        return

    # 2. Запрет ссылок
    if contains_link(message):
        try:
            await message.delete()
        except TelegramError:
            pass
        return

    # 3. Лимит 2 сообщения в сутки
    if count_user_messages_last_24h(user_id, chat_id) >= 2:
        try:
            await message.delete()
        except TelegramError:
            pass
        return

    # Всё хорошо – логируем
    log_message(user_id, chat_id)

# --- Команды управления VIP (только для админа) ---
async def vip_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if not args:
        await update.message.reply_text("Используйте: /vip_add <user_id> [chat_id]")
        return
    try:
        uid = int(args[0])
    except ValueError:
        await update.message.reply_text("Неверный user_id.")
        return
    chat_id = None
    if len(args) >= 2:
        try:
            chat_id = int(args[1])
        except ValueError:
            await update.message.reply_text("Неверный chat_id.")
            return
    add_vip(uid, chat_id)
    where = "во все чаты" if chat_id is None else f"в чате {chat_id}"
    await update.message.reply_text(f"Пользователь {uid} добавлен в VIP ({where}).")

async def vip_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if not args:
        await update.message.reply_text("Используйте: /vip_remove <user_id> [chat_id]")
        return
    try:
        uid = int(args[0])
    except ValueError:
        await update.message.reply_text("Неверный user_id.")
        return
    chat_id = None
    if len(args) >= 2:
        try:
            chat_id = int(args[1])
        except ValueError:
            await update.message.reply_text("Неверный chat_id.")
            return
    remove_vip(uid, chat_id)
    if chat_id is None:
        await update.message.reply_text(f"Все VIP-доступы пользователя {uid} удалены.")
    else:
        await update.message.reply_text(f"VIP-доступ пользователя {uid} в чате {chat_id} удалён.")

async def vip_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    rows = get_all_vips()
    if not rows:
        await update.message.reply_text("Список VIP пуст.")
        return
    globals_vip = [str(r[0]) for r in rows if r[1] is None]
    locals_dict = {}
    for r in rows:
        if r[1] is not None:
            locals_dict.setdefault(r[1], []).append(str(r[0]))
    text = ""
    if globals_vip:
        text += "🌍 Глобальные VIP:\n" + "\n".join(globals_vip) + "\n\n"
    for chat_id, users in locals_dict.items():
        # Попробуем найти название чата (TARGET_CHATS здесь нет, просто выведем ID)
        text += f"📌 Чат {chat_id}:\n" + "\n".join(users) + "\n\n"
    await update.message.reply_text(text.strip())

# --- Запуск ---
def main():
    init_db()
    cleanup_old_logs()

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

    # Команды управления VIP
    application.add_handler(CommandHandler("vip_add", vip_add))
    application.add_handler(CommandHandler("vip_remove", vip_remove))
    application.add_handler(CommandHandler("vip_list", vip_list))

    print("Бот запущен и ждёт сообщений...")
    application.run_polling()

if __name__ == "__main__":
    main()
