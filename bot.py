# -*- coding: utf-8 -*-
"""
Telegram-бот отчётов «ТОП-ТОЗА» — две точки, два файла Google Таблиц.

Команды:
  /otchet  — общий итог по обеим точкам + разбивка по каждой
  /km9     — только точка «9 км»
  /gulbuta — только точка «Гульбута»
  /myid    — узнать свой Telegram id

Что заполнить в разделе НАСТРОЙКИ:
  1) BOT_TOKEN        — токен от @BotFather
  2) SHEET_ID_KM9     — ID таблицы точки «9 км»
  3) SHEET_ID_GULBUTA — ID таблицы точки «Гульбута»

Файл ключа Google (service_account.json) кладётся рядом с этим файлом,
и его email добавляется в доступ ОБЕИХ таблиц (роль «Читатель»).
Подробности — в файле ИНСТРУКЦИЯ.md
"""

import os
import json
import logging
import datetime as dt

import gspread
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import sheets  # общий модуль чтения журнала/дашборда (используется и сайтом)

# Ссылка на веб-панель и время авто-отчёта
SITE_URL = os.environ.get("SITE_URL", "https://toptoza.up.railway.app")
REPORT_HOUR = int(os.environ.get("REPORT_HOUR", "21"))  # час по Душанбе (UTC+5)

# ======================= НАСТРОЙКИ =======================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬТЕ_СЮДА_ТОКЕН_БОТА")

# ID берётся из ссылки таблицы:
# https://docs.google.com/spreadsheets/d/ЭТОТ_КУСОК_И_ЕСТЬ_ID/edit
SHEET_ID_KM9 = os.environ.get("SHEET_ID_KM9", "ВСТАВЬТЕ_ID_ТАБЛИЦЫ_9КМ")
SHEET_ID_GULBUTA = os.environ.get("SHEET_ID_GULBUTA", "ВСТАВЬТЕ_ID_ТАБЛИЦЫ_ГУЛЬБУТА")

# Описание точек: название для отчёта + id таблицы + название листа со сводкой
POINTS = [
    {"name": "9 км", "sheet_id": SHEET_ID_KM9, "tab": "Дашборд"},
    {"name": "Гульбута", "sheet_id": SHEET_ID_GULBUTA, "tab": "Дашборд"},
]

# Из какого столбца дашборда брать цифры точки.
# В дашборде столбцы такие: A — метка, B — число точки, C — описание.
# Поэтому цифры берём из столбца B (индекс 1).
LABEL_COL = 0
VALUE_COL = 1

# Кому разрешено пользоваться ботом: список Telegram id (числа).
# Пустой список [] = разрешено всем. Узнать свой id: команда /myid
# Чтобы добавить человека: пусть пришлёт /myid и впишите его число сюда.
ALLOWED_USERS = [
    1975922784,  # Дима (владелец)
]

GOOGLE_KEY_FILE = "service_account.json"
# Можно вместо файла задать переменную окружения GOOGLE_CREDENTIALS
# (вставить туда ВСЁ содержимое service_account.json). Так делается на Railway.
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")
# =========================================================


logging.basicConfig(
    format="%(asctime)s — %(levelname)s — %(message)s", level=logging.INFO
)
log = logging.getLogger(__name__)

_gc = None


def get_client():
    """Возвращает авторизованный gspread-клиент. Кэшируется.
    Сначала пробует переменную GOOGLE_CREDENTIALS (Railway), потом файл."""
    global _gc
    if _gc is not None:
        return _gc
    if GOOGLE_CREDENTIALS.strip():
        info = json.loads(GOOGLE_CREDENTIALS)
        _gc = gspread.service_account_from_dict(info)
    else:
        _gc = gspread.service_account(filename=GOOGLE_KEY_FILE)
    return _gc

SECTION_EMOJI = {
    "КАССА": "💵",
    "КЛИЕНТЫ": "👥",
    "ВОДИТЕЛИ": "🚗",
    "КАПИТАЛЬНЫЕ": "🏗",
}

# Эти показатели НЕ складываются между точками (это счётчики/остатки — но в
# данном отчёте все показатели аддитивны, список оставлен на будущее)
NON_ADDITIVE = set()


def fmt_number(num):
    """54574 -> '54 574', 94172.8 -> '94 172,8'."""
    if num == int(num):
        return f"{int(num):,}".replace(",", " ")
    out = f"{num:,.2f}".replace(",", " ").replace(".", ",")
    return out.rstrip("0").rstrip(",")


def parse_number(text):
    """Пробует превратить текст ячейки в число. Возвращает float или None."""
    s = str(text).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    if not s or s.startswith("#"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def read_point(point):
    """Читает дашборд одной точки.
    Возвращает структуру: [(тип, текст), ...] где тип = 'title'|'section'|'item',
    для item — (метка, число или None, исходный текст)."""
    gc = get_client()
    sh = gc.open_by_key(point["sheet_id"])
    ws = sh.worksheet(point["tab"])
    rows = ws.get_all_values()

    def cell(row, idx):
        return row[idx].strip() if len(row) > idx and row[idx] is not None else ""

    parsed = []
    if rows:
        parsed.append(("title", cell(rows[0], 0)))
    for row in rows[1:]:
        label = cell(row, LABEL_COL)
        value = cell(row, VALUE_COL)
        if not label:
            continue
        num = parse_number(value)
        if num is None:
            # Строка-заголовок раздела: в столбце B стоит не число,
            # а метка точки («9 км»/«гульбута»), в A — название раздела.
            # Пустой B при заполненном A (подзаголовок) — пропускаем.
            if value:
                parsed.append(("section", label))
            continue
        parsed.append(("item", label, num, value))
    return parsed


def render_point(parsed, point_name):
    """Текст отчёта по одной точке."""
    lines = [f"📍 *{point_name.upper()}*"]
    for entry in parsed:
        if entry[0] == "section":
            emoji = ""
            for key, e in SECTION_EMOJI.items():
                if entry[1].upper().startswith(key):
                    emoji = e + " "
            lines.append("")
            lines.append(f"{emoji}*{entry[1]}*")
        elif entry[0] == "item":
            _, label, num, raw = entry
            if num is None:
                lines.append(f"• {label}: ⚠️ {raw}")
            else:
                lines.append(f"• {label}: *{fmt_number(num)}*")
    return "\n".join(lines)


def render_total(all_points):
    """Сводит показатели обеих точек: суммирует одинаковые строки разделов."""
    # порядок: (раздел, метка) -> сумма; structure сохраняет порядок первой точки
    order = []
    sums = {}
    broken = set()

    for parsed in all_points.values():
        section = ""
        for entry in parsed:
            if entry[0] == "section":
                section = entry[1]
                key = ("section", section)
                if key not in sums:
                    order.append(key)
                    sums[key] = None
            elif entry[0] == "item":
                _, label, num, _raw = entry
                key = (section, label)
                if key not in sums:
                    order.append(key)
                    sums[key] = 0.0
                if num is None:
                    broken.add(key)
                else:
                    sums[key] += num

    lines = ["🧮 *ИТОГО ПО ОБЕИМ ТОЧКАМ*"]
    for key in order:
        if key[0] == "section":
            emoji = ""
            for k, e in SECTION_EMOJI.items():
                if key[1].upper().startswith(k):
                    emoji = e + " "
            lines.append("")
            lines.append(f"{emoji}*{key[1]}*")
        else:
            _section, label = key
            if key in broken:
                lines.append(f"• {label}: ⚠️ ошибка в одной из таблиц")
            else:
                lines.append(f"• {label}: *{fmt_number(sums[key])}*")
    return "\n".join(lines)


def is_allowed(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True
    return update.effective_user and update.effective_user.id in ALLOWED_USERS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот отчётов ТОП-ТОЗА.\n\n"
        "Команды:\n"
        "/svodka — сводка за сегодня + за месяц\n"
        "/dashboard — ссылка на веб-панель\n"
        "/otchet — общий итог + обе точки\n"
        "/km9 — только точка «9 км»\n"
        "/gulbuta — только точка «Гульбута»\n"
        "/myid — узнать свой Telegram id\n\n"
        f"📲 Каждый вечер в {REPORT_HOUR}:00 пришлю сводку сам."
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Ваш Telegram id: {update.effective_user.id}")


async def send_one_point(update: Update, idx: int):
    if not is_allowed(update):
        await update.message.reply_text("⛔ У вас нет доступа к этому отчёту.")
        return
    point = POINTS[idx]
    await update.message.reply_text("Читаю таблицу, секунду…")
    try:
        parsed = read_point(point)
        await update.message.reply_text(
            render_point(parsed, point["name"]), parse_mode="Markdown"
        )
    except Exception as e:
        log.exception("Ошибка точки %s", point["name"])
        await update.message.reply_text(
            f"Не получилось прочитать таблицу «{point['name']}» 😕\n"
            f"Ошибка: {e}\n\n"
            "Проверьте ID таблицы, название листа «Дашборд» и доступ "
            "для service-аккаунта."
        )


async def km9(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_one_point(update, 0)


async def gulbuta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_one_point(update, 1)


async def otchet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ У вас нет доступа к этому отчёту.")
        return
    await update.message.reply_text("Читаю обе таблицы, секунду…")

    all_points = {}
    errors = []
    for point in POINTS:
        try:
            all_points[point["name"]] = read_point(point)
        except Exception as e:
            log.exception("Ошибка точки %s", point["name"])
            errors.append(f"«{point['name']}»: {e}")

    if errors and not all_points:
        await update.message.reply_text(
            "Не получилось прочитать ни одну таблицу 😕\n" + "\n".join(errors)
        )
        return

    # 1) Общий итог (если прочитались обе точки)
    if len(all_points) == len(POINTS):
        await update.message.reply_text(render_total(all_points), parse_mode="Markdown")
    # 2) Каждая точка отдельно
    for name, parsed in all_points.items():
        await update.message.reply_text(render_point(parsed, name), parse_mode="Markdown")
    # 3) Сообщить, если какая-то точка не прочиталась
    if errors:
        await update.message.reply_text(
            "⚠️ Часть данных недоступна:\n" + "\n".join(errors)
        )


def build_daily_report():
    """Текст ежедневной сводки: за сегодня (журнал) + за месяц (Дашборд) + светофор."""
    today = dt.date.today()
    day_in = day_exp = 0.0
    mon = {"Выручка": 0, "Чистая прибыль": 0, "Сумма долга": 0,
           "Всего заказов": 0, "Выдано": 0, "В работе": 0}
    notes = []
    for p in sheets.POINTS:
        try:
            for o in sheets.read_journal(p):
                if o["date"] == today:
                    day_in += o["income"]
                    day_exp += o["expense"]
        except Exception as e:
            notes.append(f"журнал {p['name']}: {e}")
        try:
            d = sheets.read_dashboard(p)
            for items in d.values():
                for k in mon:
                    if k in items:
                        mon[k] += items[k]
        except Exception as e:
            notes.append(f"дашборд {p['name']}: {e}")

    def f(n):
        return f"{round(n):,}".replace(",", " ")

    net = day_in - day_exp
    light = "🟢" if (mon["Выручка"] and mon["Чистая прибыль"] >= 0) else "🟡"
    lines = [
        f"📊 *ТОП-ТОЗА — сводка на {today.strftime('%d.%m.%Y')}*",
        "",
        "*За сегодня:*",
        f"• Приход: {f(day_in)} сом.",
        f"• Расход: {f(day_exp)} сом.",
        f"• Чистый поток: {f(net)} сом.",
        "",
        f"*За месяц* {light}",
        f"• Выручка: {f(mon['Выручка'])} сом.",
        f"• Чистая прибыль: {f(mon['Чистая прибыль'])} сом.",
        f"• Долг клиентов: {f(mon['Сумма долга'])} сом.",
        f"• Заказов: {f(mon['Всего заказов'])} (в работе {f(mon['В работе'])})",
        "",
        f"🔗 Подробнее на сайте: {SITE_URL}",
    ]
    if notes:
        lines.append("\n⚠️ " + "; ".join(notes))
    return "\n".join(lines)


async def dashboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ У вас нет доступа.")
        return
    await update.message.reply_text(
        f"🔗 Веб-панель отчётов:\n{SITE_URL}\n\nОткрой ссылку и войди по паролю.",
        disable_web_page_preview=True,
    )


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ У вас нет доступа.")
        return
    await update.message.reply_text("Собираю сводку, секунду…")
    try:
        text = build_daily_report()
    except Exception as e:
        log.exception("report")
        await update.message.reply_text(f"Не удалось собрать сводку: {e}")
        return
    await update.message.reply_text(text, parse_mode="Markdown",
                                    disable_web_page_preview=True)


async def daily_report_job(context: ContextTypes.DEFAULT_TYPE):
    """Авто-отправка сводки всем разрешённым пользователям."""
    try:
        text = build_daily_report()
    except Exception as e:
        log.exception("daily report build")
        return
    for uid in (ALLOWED_USERS or []):
        try:
            await context.bot.send_message(uid, text, parse_mode="Markdown",
                                           disable_web_page_preview=True)
        except Exception as e:
            log.warning("Не смог отправить отчёт %s: %s", uid, e)


def _report_time():
    """Время авто-отчёта. Пробуем Душанбе (UTC+5), иначе считаем в UTC."""
    try:
        from zoneinfo import ZoneInfo
        return dt.time(hour=REPORT_HOUR, tzinfo=ZoneInfo("Asia/Dushanbe"))
    except Exception:
        return dt.time(hour=(REPORT_HOUR - 5) % 24)


def main():
    problems = []
    if "ВСТАВЬТЕ" in BOT_TOKEN:
        problems.append("BOT_TOKEN")
    if "ВСТАВЬТЕ" in SHEET_ID_KM9:
        problems.append("SHEET_ID_KM9")
    if "ВСТАВЬТЕ" in SHEET_ID_GULBUTA:
        problems.append("SHEET_ID_GULBUTA")
    if problems:
        raise SystemExit(
            "Сначала заполните в разделе НАСТРОЙКИ: " + ", ".join(problems)
        )

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("otchet", otchet))
    app.add_handler(CommandHandler("km9", km9))
    app.add_handler(CommandHandler("gulbuta", gulbuta))
    app.add_handler(CommandHandler("dashboard", dashboard_cmd))
    app.add_handler(CommandHandler("sait", dashboard_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("svodka", report_cmd))

    # Авто-отчёт раз в день (нужен extra job-queue; если нет — просто пропускаем)
    if app.job_queue is not None:
        app.job_queue.run_daily(daily_report_job, time=_report_time(),
                                name="daily_report")
        log.info("Авто-отчёт включён на %02d:00 (Душанбе)", REPORT_HOUR)
    else:
        log.warning("job-queue недоступен — авто-отчёт выключен")

    log.info("Бот запущен. Ожидаю команды…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
