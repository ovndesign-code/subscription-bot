import asyncio
import os
from flask import Flask
from threading import Thread
import requests
import time
from telegram import Update
from telegram.ext import (Application, MessageHandler, filters)
from telegram.error import TelegramError

# ----- НАСТРОЙКИ, КОТОРЫЕ НАДО ПОМЕНЯТЬ -----
TOKEN = "8458125587:AAFiXc-ETav0GsvXm2IqQ54g0h_rsvyIpEQ"
CHANNEL_ID = -1002415978372
ADMIN_IDS = [5271825622]
CHANNEL_LINK = "https://t.me/ovnsubcheck_bot"   # ← ЗАМЕНИТЕ ЭТУ ССЫЛКУ
# -------------------------------------------

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

# --- Логика бота ---
async def is_subscribed(user_id: int, context) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status not in ["left", "kicked"]
    except TelegramError:
        return False

async def delete_warning(context):
    job_data = context.job.data
    chat_id = job_data["chat_id"]
    message_id = job_data["message_id"]
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError:
        pass

async def check_subscription(update: Update, context):
    message = update.message
    if not message or not message.from_user:
        return

    user = message.from_user
    if user.id in ADMIN_IDS or user.id == context.bot.id:
        return

    if not await is_subscribed(user.id, context):
        try:
            await message.delete()
        except TelegramError:
            pass

        reply = await message.chat.send_message(
            f"@{user.username or 'Пользователь'}, чтобы писать в чат, подпишитесь на канал: {CHANNEL_LINK}",
            disable_web_page_preview=True
        )

        context.job_queue.run_once(
            delete_warning,
            when=60,
            data={"chat_id": reply.chat_id, "message_id": reply.message_id}
        )

def main():
    # Запускаем Flask и самопинг в отдельных потоках
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    ping_thread = Thread(target=ping_self)
    ping_thread.daemon = True
    ping_thread.start()

    # Запускаем бота
    application = Application.builder().token(TOKEN).build()
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
