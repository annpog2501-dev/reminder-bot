"""Telegram-бот: расписание и напоминания.

Работает без сервера: GitHub Actions запускает этот файл каждые ~5 минут.
За один запуск бот читает новые сообщения, отвечает на них
и отправляет напоминания, время которых уже наступило.
Задачи хранятся в зашифрованном файле state.enc (ключ делается из токена бота).
"""
import base64
import hashlib
import json
import os
import traceback
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

TOKEN = os.environ["BOT_TOKEN"]
TZ = ZoneInfo(os.environ.get("TIMEZONE") or "Europe/Moscow")
STATE_FILE = "state.enc"
fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(TOKEN.encode()).digest()))

DAY_WORDS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}

HELP = (
    "Привет! Напиши мне задачу, например «Купить хлеб», и я спрошу день и время.\n\n"
    "Можно сразу одним сообщением:\n"
    "завтра 15:30 купить хлеб\n"
    "25.12 18:00 встреча с Аней\n\n"
    "/list — моё расписание\n"
    "/del 2 — удалить задачу номер 2\n"
    "/cancel — отменить добавление\n\n"
    "Я проверяю сообщения раз в несколько минут, поэтому ответы и напоминания "
    "приходят с небольшой задержкой."
)


# ---------- Telegram ----------
def api(method, **params):
    data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items()}
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=urllib.parse.urlencode(data).encode(),
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)["result"]


def send(chat_id, text, keyboard=None):
    params = {"chat_id": chat_id, "text": text}
    if keyboard:
        params["reply_markup"] = {"inline_keyboard": keyboard}
    api("sendMessage", **params)


# ---------- Состояние ----------
def load_state():
    try:
        with open(STATE_FILE, "rb") as f:
            return json.loads(fernet.decrypt(f.read()))
    except FileNotFoundError:
        return {"offset": 0, "next_id": 1, "tasks": [], "pending": {}}


def save_state(state):
    with open(STATE_FILE, "wb") as f:
        f.write(fernet.encrypt(json.dumps(state, ensure_ascii=False).encode()))


# ---------- Разбор даты и времени ----------
def now():
    return datetime.now(TZ).replace(tzinfo=None)


def parse_date(word):
    word = word.lower()
    today = now().date()
    if word in DAY_WORDS:
        return today + timedelta(days=DAY_WORDS[word])
    try:
        day_str, month_str = word.split(".")[:2]
        day = date(today.year, int(month_str), int(day_str))
        if day < today:
            day = day.replace(year=today.year + 1)
        return day
    except ValueError:
        return None


def parse_time(word):
    try:
        hours, minutes = word.replace(".", ":").split(":")
        return time(int(hours), int(minutes))
    except ValueError:
        return None


# ---------- Задачи ----------
def my_tasks(state, chat_id):
    mine = [t for t in state["tasks"] if t["chat_id"] == chat_id]
    return sorted(mine, key=lambda t: t["when"])


def add_task(state, chat_id, text, when):
    state["tasks"].append(
        {"id": state["next_id"], "chat_id": chat_id, "text": text, "when": when.isoformat()}
    )
    state["next_id"] += 1
    send(
        chat_id,
        f"✅ Записала: {text}\n📅 {when.strftime('%d.%m в %H:%M')}",
    )


def cmd_list(state, chat_id):
    mine = my_tasks(state, chat_id)
    if not mine:
        send(chat_id, "Расписание пустое. Напиши задачу, и я добавлю её.")
        return
    lines = []
    last_day = None
    for number, task in enumerate(mine, 1):
        when = datetime.fromisoformat(task["when"])
        if when.date() != last_day:
            last_day = when.date()
            lines.append(f"\n📅 {when.strftime('%d.%m')}")
        lines.append(f"{number}. {when.strftime('%H:%M')} — {task['text']}")
    send(chat_id, "\n".join(lines).strip())


def cmd_del(state, chat_id, args):
    mine = my_tasks(state, chat_id)
    try:
        number = int(args[0])
        if number < 1:
            raise IndexError
        task = mine[number - 1]
    except (IndexError, ValueError):
        send(chat_id, "Напиши номер из /list, например: /del 2")
        return
    state["tasks"] = [t for t in state["tasks"] if t["id"] != task["id"]]
    send(chat_id, f"Удалила: {task['text']}")


# ---------- Обработка сообщений ----------
def handle_message(state, message):
    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()
    if not text:
        return
    key = str(chat_id)

    if text.startswith("/"):
        parts = text.split()
        command = parts[0].split("@")[0].lower()
        if command == "/list":
            cmd_list(state, chat_id)
        elif command == "/del":
            cmd_del(state, chat_id, parts[1:])
        elif command == "/cancel":
            state["pending"].pop(key, None)
            send(chat_id, "Отменила.")
        else:
            send(chat_id, HELP)
        return

    pending = state["pending"].get(key)

    # Шаг: дата вводится вручную
    if pending and pending["step"] == "date_input":
        day = parse_date(text)
        if not day:
            send(chat_id, "Не поняла дату. Напиши так: 25.12")
            return
        pending["date"] = day.isoformat()
        pending["step"] = "time"
        send(chat_id, "Во сколько напомнить? Например: 15:30")
        return

    # Шаг: время
    if pending and pending["step"] == "time":
        t = parse_time(text)
        if not t:
            send(chat_id, "Не поняла время. Напиши так: 15:30")
            return
        when = datetime.combine(date.fromisoformat(pending["date"]), t)
        if when <= now():
            send(chat_id, "Это время уже прошло. Напиши другое время.")
            return
        add_task(state, chat_id, pending["text"], when)
        state["pending"].pop(key)
        return

    # Новая задача. Если день и время указаны сразу: «завтра 15:30 купить хлеб»
    parts = text.split(maxsplit=2)
    if len(parts) == 3:
        day, t = parse_date(parts[0]), parse_time(parts[1])
        if day and t:
            when = datetime.combine(day, t)
            if when <= now():
                send(chat_id, "Это время уже прошло. Напиши задачу ещё раз с другим временем.")
            else:
                add_task(state, chat_id, parts[2], when)
            return

    # Иначе спрашиваем день кнопками
    today = now().date()
    state["pending"][key] = {"text": text, "step": "date"}
    keyboard = [
        [
            {"text": "Сегодня", "callback_data": f"day:{today.isoformat()}"},
            {"text": "Завтра", "callback_data": f"day:{(today + timedelta(days=1)).isoformat()}"},
        ],
        [
            {"text": "Послезавтра", "callback_data": f"day:{(today + timedelta(days=2)).isoformat()}"},
            {"text": "Другая дата", "callback_data": "day:other"},
        ],
    ]
    send(chat_id, f"На какой день поставить «{text}»?", keyboard)


def handle_callback(state, callback):
    try:
        api("answerCallbackQuery", callback_query_id=callback["id"])
    except Exception:
        pass  # при задержке Telegram может отклонить ответ, это не страшно

    chat_id = callback["message"]["chat"]["id"]
    pending = state["pending"].get(str(chat_id))
    data = callback.get("data", "")
    if not pending or not data.startswith("day:"):
        send(chat_id, "Напиши задачу ещё раз 🙂")
        return

    value = data[4:]
    if value == "other":
        pending["step"] = "date_input"
        send(chat_id, "Напиши дату в формате ДД.ММ, например 25.12")
        return
    try:
        date.fromisoformat(value)
    except ValueError:
        send(chat_id, "Напиши задачу ещё раз 🙂")
        return
    pending["date"] = value
    pending["step"] = "time"
    send(chat_id, "Во сколько напомнить? Например: 15:30")


# ---------- Напоминания ----------
def send_due(state):
    current = now()
    left = []
    for task in state["tasks"]:
        when = datetime.fromisoformat(task["when"])
        if when <= current:
            try:
                send(task["chat_id"], f"⏰ Напоминание: {task['text']}")
                continue
            except Exception:
                traceback.print_exc()
                if current - when > timedelta(days=1):
                    continue  # слишком старое, не пытаемся бесконечно
        left.append(task)
    state["tasks"] = left


def main():
    state = load_state()
    before = json.dumps(state, sort_keys=True)

    updates = api(
        "getUpdates",
        offset=state["offset"],
        timeout=0,
        allowed_updates=["message", "callback_query"],
    )
    for update in updates:
        state["offset"] = update["update_id"] + 1
        try:
            if "message" in update:
                handle_message(state, update["message"])
            elif "callback_query" in update:
                handle_callback(state, update["callback_query"])
        except Exception:
            traceback.print_exc()

    send_due(state)

    if json.dumps(state, sort_keys=True) != before:
        save_state(state)
        print("Состояние обновлено")
    else:
        print("Без изменений")


if __name__ == "__main__":
    main()
