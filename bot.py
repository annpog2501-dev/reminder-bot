import json
import os
import threading
from datetime import date, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ====== НАСТРОЙКИ ======
# Токен и часовой пояс задаются в настройках хостинга (переменные BOT_TOKEN и TIMEZONE),
# а не в коде, чтобы токен не попал в GitHub.
TOKEN = os.environ["BOT_TOKEN"]
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Moscow"))
DB_FILE = "tasks.json"  # тут хранятся задачи
# =======================


class PingHandler(BaseHTTPRequestHandler):
    """Маленький веб-адрес: на него «стучится» внешний сервис, чтобы бот не засыпал."""

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def start_web_server():
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def load_tasks():
    try:
        with open(DB_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def save_tasks():
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)


tasks = load_tasks()


def schedule(job_queue, task):
    when = datetime.fromisoformat(task["when"]).replace(tzinfo=TZ)
    job_queue.run_once(remind, when=when, data=task, name=str(task["id"]))


async def remind(context: ContextTypes.DEFAULT_TYPE):
    task = context.job.data
    await context.bot.send_message(task["chat_id"], f"⏰ Напоминание: {task['text']}")
    tasks[:] = [t for t in tasks if t["id"] != task["id"]]
    save_tasks()


def my_tasks(chat_id):
    mine = [t for t in tasks if t["chat_id"] == chat_id]
    return sorted(mine, key=lambda t: t["when"])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Просто напиши мне задачу, например «Купить хлеб» или «Встреча с Аней».\n"
        "Я спрошу, на какой день и время, и напомню.\n\n"
        "/list — моё расписание\n"
        "/del 2 — удалить задачу номер 2"
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mine = my_tasks(update.effective_chat.id)
    if not mine:
        await update.message.reply_text("Расписание пустое. Напиши задачу, и я добавлю её.")
        return
    lines = []
    last_day = None
    for i, t in enumerate(mine, 1):
        dt = datetime.fromisoformat(t["when"])
        if dt.date() != last_day:
            last_day = dt.date()
            lines.append(f"\n📅 {dt.strftime('%d.%m')}")
        lines.append(f"{i}. {dt.strftime('%H:%M')} — {t['text']}")
    await update.message.reply_text("\n".join(lines).strip())


async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mine = my_tasks(update.effective_chat.id)
    try:
        number = int(context.args[0])
        task = mine[number - 1]
        if number < 1:
            raise IndexError
    except (IndexError, ValueError):
        await update.message.reply_text("Напиши номер из /list, например: /del 2")
        return
    for job in context.job_queue.get_jobs_by_name(str(task["id"])):
        job.schedule_removal()
    tasks[:] = [t for t in tasks if t["id"] != task["id"]]
    save_tasks()
    await update.message.reply_text(f"Удалила: {task['text']}")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    pending = context.user_data.get("pending")

    # Шаг: ввод даты вручную
    if pending and pending["step"] == "date_input":
        try:
            day_str, month_str = text.split(".")[:2]
            today = datetime.now(TZ).date()
            day = date(today.year, int(month_str), int(day_str))
            if day < today:
                day = day.replace(year=today.year + 1)
        except ValueError:
            await update.message.reply_text("Не поняла дату. Напиши так: 25.12")
            return
        pending["date"] = day.isoformat()
        pending["step"] = "time"
        await update.message.reply_text("Во сколько напомнить? Например: 15:30")
        return

    # Шаг: ввод времени
    if pending and pending["step"] == "time":
        try:
            hours, minutes = text.replace(".", ":").split(":")
            t = time(int(hours), int(minutes))
        except ValueError:
            await update.message.reply_text("Не поняла время. Напиши так: 15:30")
            return
        when = datetime.combine(date.fromisoformat(pending["date"]), t)
        if when.replace(tzinfo=TZ) <= datetime.now(TZ):
            await update.message.reply_text("Это время уже прошло. Напиши другое время.")
            return
        task = {
            "id": max([x["id"] for x in tasks], default=0) + 1,
            "chat_id": update.effective_chat.id,
            "text": pending["text"],
            "when": when.isoformat(),
        }
        tasks.append(task)
        save_tasks()
        schedule(context.job_queue, task)
        context.user_data.pop("pending")
        await update.message.reply_text(
            f"✅ Записала: {task['text']}\n📅 {when.strftime('%d.%m в %H:%M')}"
        )
        return

    # Новая задача: спрашиваем день
    today = datetime.now(TZ).date()
    context.user_data["pending"] = {"text": text, "step": "date"}
    buttons = [
        [
            InlineKeyboardButton("Сегодня", callback_data=f"day:{today.isoformat()}"),
            InlineKeyboardButton("Завтра", callback_data=f"day:{(today + timedelta(days=1)).isoformat()}"),
        ],
        [
            InlineKeyboardButton("Послезавтра", callback_data=f"day:{(today + timedelta(days=2)).isoformat()}"),
            InlineKeyboardButton("Другая дата", callback_data="day:other"),
        ],
    ]
    await update.message.reply_text(
        f"На какой день поставить «{text}»?", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pending = context.user_data.get("pending")
    if not pending:
        await query.edit_message_text("Напиши задачу ещё раз 🙂")
        return
    value = query.data.split(":", 1)[1]
    if value == "other":
        pending["step"] = "date_input"
        await query.edit_message_text("Напиши дату в формате ДД.ММ, например 25.12")
    else:
        pending["date"] = value
        pending["step"] = "time"
        await query.edit_message_text("Во сколько напомнить? Например: 15:30")


async def post_init(app: Application):
    # После перезапуска снова ставим напоминания из файла
    for task in tasks:
        schedule(app.job_queue, task)


def main():
    start_web_server()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(CallbackQueryHandler(on_button, pattern="^day:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    print("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
