import asyncio
import os
import json
import sqlite3
import datetime
import re
from flask import Flask
from threading import Thread
import requests
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    ConversationHandler
)
from telegram.error import TelegramError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ----- НАСТРОЙКИ (замените ссылку) -----
TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = -1002415978372
ADMIN_IDS = [5271825622]
CHANNEL_LINK = "https://t.me/AiFinVibe"   # ← ЗАМЕНИТЕ
# -----------------------------------

# Чаты для автопостинга
TARGET_CHATS = {
    "Москва": -1002204699744,
    "Казань": -1002157313246,
    "Нижний Новгород": -1002499208891,
    "Красноярск": -1002248509509,
    "Краснодар": -1002236116506,
    "Новосибирск": -1002196411040,
    "Санкт-Петербург": -1002245791663,
    "Ростов-на-Дону": -1002195160686,
    "Екатеринбург": -1002246383883,
}

# --- База данных ---
DB_PATH = "posts.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Таблица автопостинга
    c.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_ids TEXT NOT NULL,
            content_type TEXT NOT NULL,
            file_ids TEXT,
            text TEXT,
            interval_hours REAL,
            start_date TEXT,
            end_date TEXT,
            active INTEGER DEFAULT 1
        )
    """)
    # VIP-пользователи (пересоздаём)
    c.execute("DROP TABLE IF EXISTS vip_users")
    c.execute("""
        CREATE TABLE vip_users (
            user_id INTEGER NOT NULL,
            chat_id INTEGER,
            PRIMARY KEY (user_id, chat_id)
        )
    """)
    # Стоп-слова
    c.execute("""
        CREATE TABLE IF NOT EXISTS stop_words (
            word TEXT PRIMARY KEY
        )
    """)
    # Журнал сообщений для лимита
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

# Функции VIP (переписаны)
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

# Остальные функции БД без изменений
def add_stop_word(word):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO stop_words VALUES (?)", (word.lower(),))
    conn.commit()
    conn.close()

def remove_stop_word(word):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM stop_words WHERE word=?", (word.lower(),))
    conn.commit()
    conn.close()

def get_stop_words():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT word FROM stop_words")
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

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

# --- Планировщик (автопостинг) ---
scheduler = AsyncIOScheduler()

async def send_post_to_chat(context, chat_id, content_type, file_ids, text):
    try:
        if content_type == "text":
            await context.bot.send_message(chat_id=chat_id, text=text)
        elif content_type == "photo":
            await context.bot.send_photo(chat_id=chat_id, photo=file_ids[0], caption=text)
        elif content_type == "video":
            await context.bot.send_video(chat_id=chat_id, video=file_ids[0], caption=text)
        elif content_type == "album":
            media = []
            for i, fid in enumerate(file_ids):
                if i == 0:
                    media.append(InputMediaPhoto(media=fid, caption=text))
                else:
                    media.append(InputMediaPhoto(media=fid))
            if media:
                await context.bot.send_media_group(chat_id=chat_id, media=media)
    except Exception as e:
        print(f"Ошибка при отправке в {chat_id}: {e}")

async def job_function_wrapper(context, post_id):
    post = get_post(post_id)
    if not post or not post[7]:
        return
    chat_ids = json.loads(post[1])
    content_type = post[2]
    file_ids = json.loads(post[3]) if post[3] else []
    text = post[4]
    for cid in chat_ids:
        await send_post_to_chat(context, cid, content_type, file_ids, text)

    end_date = post[6]
    if end_date:
        end = datetime.datetime.fromisoformat(end_date)
        if datetime.datetime.now() >= end:
            update_post_active(post_id, 0)
            scheduler.remove_job(f"post_{post_id}")

def save_post(chat_ids, content_type, file_ids, text, interval_hours, start_date, end_date):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO posts (chat_ids, content_type, file_ids, text, interval_hours, start_date, end_date, active)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
    """, (json.dumps(chat_ids), content_type, json.dumps(file_ids) if file_ids else None,
          text, interval_hours, start_date, end_date))
    post_id = c.lastrowid
    conn.commit()
    conn.close()
    return post_id

def get_active_posts():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM posts WHERE active=1")
    rows = c.fetchall()
    conn.close()
    return rows

def get_post(post_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM posts WHERE id=?", (post_id,))
    row = c.fetchone()
    conn.close()
    return row

def update_post_active(post_id, active):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE posts SET active=? WHERE id=?", (active, post_id))
    conn.commit()
    conn.close()

def delete_post(post_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM posts WHERE id=?", (post_id,))
    conn.commit()
    conn.close()

def restore_jobs(application):
    posts = get_active_posts()
    now = datetime.datetime.now()
    for post in posts:
        post_id = post[0]
        interval = post[5]
        start_str = post[6]
        start = datetime.datetime.fromisoformat(start_str)
        if start < now:
            while start < now:
                start += datetime.timedelta(hours=interval)
        scheduler.add_job(
            lambda pid=post_id: asyncio.create_task(job_function_wrapper(application, pid)),
            trigger="interval",
            hours=interval,
            start_date=start,
            id=f"post_{post_id}"
        )

# --- Проверка сообщений в группах ---
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

def is_caps(text):
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 10:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters) > 0.5

def contains_stop_words(text):
    stop_words = get_stop_words()
    if not stop_words:
        return False
    text_lower = text.lower()
    return any(word in text_lower for word in stop_words)

async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.from_user:
        return

    user = message.from_user
    user_id = user.id
    chat_id = message.chat_id

    # Админы и сам бот пропускаются без проверок
    if user_id in ADMIN_IDS or user_id == context.bot.id:
        return

    # VIP (глобальный или в этом чате) проходят без подписки и ограничений
    if is_vip(user_id, chat_id):
        log_message(user_id, chat_id)
        return

    # Проверка подписки на канал
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

    # Остальные проверки – только удаление
    text = message.text or message.caption or ""
    violations = False

    if message.forward_from or message.forward_from_chat:
        violations = True
    elif message.entities and any(e.type in ("url", "text_link") for e in message.entities):
        violations = True
    elif re.search(r"https?://", text):
        violations = True
    elif is_caps(text):
        violations = True
    elif contains_stop_words(text):
        violations = True
    elif count_user_messages_last_24h(user_id, chat_id) >= 2:
        violations = True

    if violations:
        try:
            await message.delete()
        except TelegramError:
            pass
        return

    log_message(user_id, chat_id)

# --- Команды управления VIP и стоп-словами (только админ) ---
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
        # Попробуем найти название чата
        chat_name = next((k for k, v in TARGET_CHATS.items() if v == chat_id), str(chat_id))
        text += f"📌 {chat_name}:\n" + "\n".join(users) + "\n\n"
    if text:
        text = text.strip()
    else:
        text = "Список VIP пуст."
    await update.message.reply_text(text)

async def stopword_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if not args:
        await update.message.reply_text("Используйте: /stopword_add <слово>")
        return
    word = " ".join(args).lower()
    add_stop_word(word)
    await update.message.reply_text(f"Стоп-слово '{word}' добавлено.")

async def stopword_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if not args:
        await update.message.reply_text("Используйте: /stopword_remove <слово>")
        return
    word = " ".join(args).lower()
    remove_stop_word(word)
    await update.message.reply_text(f"Стоп-слово '{word}' удалено.")

async def stopword_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    words = get_stop_words()
    if words:
        text = "Список стоп-слов:\n" + "\n".join(words)
    else:
        text = "Стоп-слова отсутствуют."
    await update.message.reply_text(text)

# --- Автопостинг (личный кабинет) ---
MENU, CREATE_CONTENT, SELECT_CHATS, PREVIEW_CONFIRM, SET_INTERVAL, SET_START, SET_END = range(7)
user_state = {}

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Новый пост", callback_data="new_post")],
        [InlineKeyboardButton("📋 Мои посты", callback_data="my_posts")],
    ])

async def start_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Добро пожаловать в панель автопостинга:", reply_markup=main_keyboard())
    return MENU

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "new_post":
        await query.edit_message_text("Отправьте мне пост (текст, фото, видео или несколько фото/видео как альбом).")
        return CREATE_CONTENT
    elif data == "my_posts":
        posts = get_active_posts()
        if not posts:
            await query.edit_message_text("Нет активных постов.", reply_markup=main_keyboard())
            return MENU
        text = "📋 <b>Активные посты:</b>\n"
        keyboard = []
        for p in posts:
            pid, chat_ids_json, ctype, file_ids_json, ptext, interval, start, end, _ = p
            chats = ", ".join([k for k,v in TARGET_CHATS.items() if v in json.loads(chat_ids_json)])
            text += f"\n#{pid} → {chats}\n{ptext[:50]}...\nИнтервал: {interval} ч\n"
            keyboard.append([InlineKeyboardButton(f"❌ Удалить #{pid}", callback_data=f"del_{pid}"),
                             InlineKeyboardButton(f"⏸ Пауза #{pid}", callback_data=f"pause_{pid}")])
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_menu")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return MENU
    elif data.startswith("del_"):
        pid = int(data.split("_")[1])
        delete_post(pid)
        try:
            scheduler.remove_job(f"post_{pid}")
        except:
            pass
        await query.answer("Пост удалён.")
        return await menu_handler(update, context)
    elif data.startswith("pause_"):
        pid = int(data.split("_")[1])
        post = get_post(pid)
        if post:
            new_active = 0 if post[7] else 1
            update_post_active(pid, new_active)
            if new_active == 0:
                try:
                    scheduler.remove_job(f"post_{pid}")
                except:
                    pass
            else:
                interval = post[5]
                start = datetime.datetime.fromisoformat(post[6])
                now = datetime.datetime.now()
                if start < now:
                    while start < now:
                        start += datetime.timedelta(hours=interval)
                scheduler.add_job(
                    lambda pid=pid: asyncio.create_task(job_function_wrapper(context.application, pid)),
                    trigger="interval", hours=interval, start_date=start, id=f"post_{pid}"
                )
            await query.answer("Статус изменён.")
        return await menu_handler(update, context)
    elif data == "back_menu":
        await query.edit_message_text("Меню:", reply_markup=main_keyboard())
        return MENU

async def receive_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = update.effective_user.id
    user_state[uid] = {"post": msg}
    if msg.photo:
        content_type = "photo"
        file_ids = [msg.photo[-1].file_id]
    elif msg.video:
        content_type = "video"
        file_ids = [msg.video.file_id]
    elif msg.text:
        content_type = "text"
        file_ids = []
    else:
        await msg.reply_text("Неподдерживаемый формат. Отправьте текст, фото, видео или альбом.")
        return CREATE_CONTENT

    text = msg.caption or msg.text or ""
    user_state[uid].update({"content_type": content_type, "file_ids": file_ids, "text": text})

    if content_type == "text":
        await msg.reply_text(f"Предпросмотр:\n\n{text}\n\nВсё верно?",
                             reply_markup=InlineKeyboardMarkup([
                                 [InlineKeyboardButton("✅ Да", callback_data="preview_yes"),
                                  InlineKeyboardButton("❌ Нет", callback_data="preview_no")]
                             ]))
    elif content_type == "photo":
        await msg.reply_photo(photo=file_ids[0], caption=text + "\n\nВсё верно?",
                              reply_markup=InlineKeyboardMarkup([
                                  [InlineKeyboardButton("✅ Да", callback_data="preview_yes"),
                                   InlineKeyboardButton("❌ Нет", callback_data="preview_no")]
                              ]))
    elif content_type == "video":
        await msg.reply_video(video=file_ids[0], caption=text + "\n\nВсё верно?",
                              reply_markup=InlineKeyboardMarkup([
                                  [InlineKeyboardButton("✅ Да", callback_data="preview_yes"),
                                   InlineKeyboardButton("❌ Нет", callback_data="preview_no")]
                              ]))
    return PREVIEW_CONFIRM

async def preview_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    if query.data == "preview_no":
        user_state.pop(uid, None)
        await query.edit_message_text("Создание поста отменено.", reply_markup=main_keyboard())
        return MENU
    keyboard = [[InlineKeyboardButton(name, callback_data=f"chat_{chat_id}")] for name, chat_id in TARGET_CHATS.items()]
    keyboard.append([InlineKeyboardButton("✅ Готово", callback_data="chats_done")])
    await query.edit_message_text("Выберите чаты (можно несколько):", reply_markup=InlineKeyboardMarkup(keyboard))
    user_state.setdefault(uid, {})["selected_chats"] = []
    return SELECT_CHATS

async def select_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    uid = update.effective_user.id
    if data == "chats_done":
        if not user_state[uid].get("selected_chats"):
            await query.answer("Не выбрано ни одного чата!")
            return SELECT_CHATS
        await query.edit_message_text("Введите интервал в часах (например, 1 – каждый час, 0.5 – каждые 30 мин):")
        return SET_INTERVAL
    chat_id = int(data.split("_")[1])
    selected = user_state[uid].setdefault("selected_chats", [])
    if chat_id in selected:
        selected.remove(chat_id)
    else:
        selected.append(chat_id)
    await query.answer("Изменено")
    return SELECT_CHATS

async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        interval = float(update.message.text)
        if interval <= 0:
            raise ValueError
        user_state[update.effective_user.id]["interval"] = interval
        await update.message.reply_text("Введите дату и время первого выхода в формате ГГГГ-ММ-ДД ЧЧ:ММ (например, 2026-04-25 14:00):")
        return SET_START
    except:
        await update.message.reply_text("Неверный формат. Введите число (например, 1, 0.5):")
        return SET_INTERVAL

async def set_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        start_str = update.message.text
        datetime.datetime.strptime(start_str, "%Y-%m-%d %H:%M")
        user_state[update.effective_user.id]["start"] = start_str
        await update.message.reply_text("Введите дату окончания в том же формате (или '0' для бесконечного):")
        return SET_END
    except:
        await update.message.reply_text("Неверный формат. Попробуйте ещё раз (ГГГГ-ММ-ДД ЧЧ:ММ):")
        return SET_START

async def set_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    end_str = update.message.text
    uid = update.effective_user.id
    if end_str == "0":
        end_str = None
    else:
        try:
            datetime.datetime.strptime(end_str, "%Y-%m-%d %H:%M")
        except:
            await update.message.reply_text("Неверный формат. Введите дату или '0':")
            return SET_END
    data = user_state[uid]
    post_id = save_post(
        chat_ids=data["selected_chats"],
        content_type=data["content_type"],
        file_ids=data["file_ids"],
        text=data["text"],
        interval_hours=data["interval"],
        start_date=data["start"],
        end_date=end_str
    )
    start = datetime.datetime.fromisoformat(data["start"])
    interval = data["interval"]
    now = datetime.datetime.now()
    if start < now:
        while start < now:
            start += datetime.timedelta(hours=interval)
    scheduler.add_job(
        lambda pid=post_id: asyncio.create_task(job_function_wrapper(context.application, pid)),
        trigger="interval",
        hours=interval,
        start_date=start,
        id=f"post_{post_id}"
    )
    await update.message.reply_text(f"✅ Пост создан! Будет публиковаться с {data['start']} каждые {interval} ч.", reply_markup=main_keyboard())
    user_state.pop(uid, None)
    return MENU

# --- Новая асинхронная инициализация планировщика ---
async def post_init(application: Application):
    scheduler.start()
    restore_jobs(application)

# --- Основная функция ---
def main():
    init_db()
    cleanup_old_logs()

    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    ping_thread = Thread(target=ping_self)
    ping_thread.daemon = True
    ping_thread.start()

    application = Application.builder().token(TOKEN).post_init(post_init).build()

    # Групповые сообщения
    application.add_handler(
        MessageHandler(filters.ALL & filters.ChatType.GROUPS & ~filters.COMMAND, check_subscription)
    )

    # Команды управления VIP и стоп-словами
    application.add_handler(CommandHandler("vip_add", vip_add))
    application.add_handler(CommandHandler("vip_remove", vip_remove))
    application.add_handler(CommandHandler("vip_list", vip_list))
    application.add_handler(CommandHandler("stopword_add", stopword_add))
    application.add_handler(CommandHandler("stopword_remove", stopword_remove))
    application.add_handler(CommandHandler("stopword_list", stopword_list))

    # Автопостинг
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start_admin, filters=filters.User(ADMIN_IDS[0]))],
        states={
            MENU: [CallbackQueryHandler(menu_handler)],
            CREATE_CONTENT: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_content)],
            PREVIEW_CONFIRM: [CallbackQueryHandler(preview_confirm)],
            SELECT_CHATS: [CallbackQueryHandler(select_chats)],
            SET_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_interval)],
            SET_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_start)],
            SET_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_end)],
        },
        fallbacks=[]
    )
    application.add_handler(conv_handler)

    print("Бот запущен и ждёт сообщений...")
    application.run_polling()

if __name__ == "__main__":
    main()
