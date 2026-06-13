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
import io
import csv
import json
import logging
import datetime as dt

import gspread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          ContextTypes)

import sheets    # общий модуль чтения журнала/дашборда (используется и сайтом)
import db        # хранилище (Postgres) — для авто-синхронизации и бэкапа
import insights  # текстовые выводы (общие с сайтом)

# Ссылка на веб-панель и время авто-отчёта
SITE_URL = os.environ.get("SITE_URL", "https://toptoza.up.railway.app")
REPORT_HOUR = int(os.environ.get("REPORT_HOUR", "21"))  # час по Душанбе (UTC+5)
# Порог долга для алерта (0 = выключено). Аномальный расход и минусовой поток — без порога.
ALERT_DEBT = float(os.environ.get("ALERT_DEBT", "0") or 0)

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
        f"📲 Каждый вечер в {REPORT_HOUR}:00 пришлю сводку сам.",
        reply_markup=main_keyboard(),
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
    ]
    # выводы словами (по данным из базы за текущий месяц vs прошлый)
    try:
        ms = today.replace(day=1)
        cur = db.query_ops(["km9", "gulbuta"], ms, today)
        ps = (ms - dt.timedelta(days=1)).replace(day=1)
        pe = ms - dt.timedelta(days=1)
        ins = insights.narrative(cur, db.query_ops(["km9", "gulbuta"], ps, pe))
        if ins:
            lines.append("")
            lines.append("*Выводы:*")
            lines += ["• " + line for line in ins[:4]]
    except Exception as e:
        log.warning("narrative: %s", e)
    lines += ["", f"🔗 Подробнее: {SITE_URL}"]
    if notes:
        lines.append("\n⚠️ " + "; ".join(notes))
    return "\n".join(lines)


def make_chart_png():
    """PNG-график приход/расход по дням за 14 дней. None — если нет данных."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        log.warning("matplotlib недоступен: %s", e)
        return None
    today = dt.date.today()
    start = today - dt.timedelta(days=13)
    ops = db.query_ops(["km9", "gulbuta"], start, today)
    by = {}
    for o in ops:
        if o["date"]:
            s = by.setdefault(o["date"], [0, 0])
            s[0] += o["income"]
            s[1] += o["expense"]
    days = sorted(by)
    if not days:
        return None
    labels = [d.strftime("%d.%m") for d in days]
    inc = [by[d][0] for d in days]
    exp = [by[d][1] for d in days]
    fig, ax = plt.subplots(figsize=(8, 3.4), dpi=130)
    x = range(len(days))
    w = 0.4
    ax.bar([i - w / 2 for i in x], inc, width=w, color="#00B956", label="Приход")
    ax.bar([i + w / 2 for i in x], exp, width=w, color="#F0455A", label="Расход")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=7)
    ax.set_title("Приход и расход по дням (14 дней)", fontsize=11)
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=.2)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf


async def send_full_report(bot, chat):
    """Отправить текстовую сводку + график-картинку."""
    await bot.send_message(chat, build_daily_report(), parse_mode="Markdown",
                           disable_web_page_preview=True)
    try:
        buf = make_chart_png()
        if buf:
            await bot.send_photo(chat, photo=buf)
    except Exception as e:
        log.warning("chart send: %s", e)


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
        await send_full_report(context.bot, update.effective_chat.id)
    except Exception as e:
        log.exception("report")
        await update.message.reply_text(f"Не удалось собрать сводку: {e}")


async def daily_report_job(context: ContextTypes.DEFAULT_TYPE):
    """Авто-отправка сводки всем разрешённым пользователям."""
    if db.get_setting("daily_on", "1") != "1":
        return
    for uid in (ALLOWED_USERS or []):
        try:
            await send_full_report(context.bot, uid)
        except Exception as e:
            log.warning("Не смог отправить отчёт %s: %s", uid, e)


def _local_time(hour):
    """Время по Душанбе (UTC+5); если zoneinfo недоступен — считаем в UTC."""
    try:
        from zoneinfo import ZoneInfo
        return dt.time(hour=hour, tzinfo=ZoneInfo("Asia/Dushanbe"))
    except Exception:
        return dt.time(hour=(hour - 5) % 24)


async def _notify(context, text):
    """Отправить сообщение всем разрешённым пользователям (директору)."""
    for uid in (ALLOWED_USERS or []):
        try:
            await context.bot.send_message(uid, text, parse_mode="Markdown",
                                           disable_web_page_preview=True)
        except Exception as e:
            log.warning("notify %s: %s", uid, e)


# активные проблемы с таблицами — чтобы не слать алерт каждый час
_alerted = set()
# когда какой бизнес-алерт уже слали — чтобы не чаще раза в день
_alert_dates = {}


def fmt_money(n):
    return f"{round(n):,}".replace(",", " ")


def _alert_once(key):
    today = dt.date.today()
    if _alert_dates.get(key) == today:
        return False
    _alert_dates[key] = today
    return True


async def business_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Проактивные алерты: аномальный расход, минусовой поток за месяц, большой долг."""
    try:
        today = dt.date.today()
        keys = ["km9", "gulbuta"]
        day_ops = db.query_ops(keys, today, today)
        day_exp = sum(o["expense"] for o in day_ops)
        hist = db.query_ops(keys, today - dt.timedelta(days=30),
                            today - dt.timedelta(days=1))
        hist_days = {o["date"] for o in hist if o["date"]}
        avg_exp = (sum(o["expense"] for o in hist) / len(hist_days)) if hist_days else 0
        mon = db.query_ops(keys, today.replace(day=1), today)
        net = sum(o["income"] for o in mon) - sum(o["expense"] for o in mon)
        debt = 0
        try:
            for p in sheets.POINTS:
                for items in sheets.read_dashboard(p).values():
                    debt += items.get("Сумма долга", 0)
        except Exception:
            pass

        msgs = []
        if day_exp > 10000 and avg_exp > 0 and day_exp > 2 * avg_exp and _alert_once("day_exp"):
            msgs.append(f"⚠️ *Крупный расход сегодня*: {fmt_money(day_exp)} сом.\n"
                        f"Обычно ~{fmt_money(avg_exp)} в день.")
        if net < 0 and _alert_once("neg_net"):
            msgs.append(f"⚠️ *Расходы превышают приход* за месяц.\n"
                        f"Чистый поток: {fmt_money(net)} сом.")
        if ALERT_DEBT and debt > ALERT_DEBT and _alert_once("debt"):
            msgs.append(f"⚠️ *Долг клиентов высокий*: {fmt_money(debt)} сом.\n"
                        f"Порог: {fmt_money(ALERT_DEBT)} сом.")
        for m in msgs:
            await _notify(context, m + f"\n\n🔗 {SITE_URL}")
    except Exception as e:
        log.warning("business alerts: %s", e)


def _transient(msg):
    """Временные ошибки Google API — не повод слать алерт «таблица сломалась»."""
    m = str(msg).lower()
    return any(x in m for x in ("429", "quota", "rate limit", "rate_limit",
                                "timeout", "timed out", "503", "500", "unavailable",
                                "deadline"))


async def sync_job(context: ContextTypes.DEFAULT_TYPE):
    """Раз в час: тянем журнал из Google Таблиц в базу + проверяем, не сломалась ли таблица."""
    try:
        db.init_db()
    except Exception as e:
        log.warning("db init: %s", e)
        return
    problems = {}
    transient = False
    for p in sheets.POINTS:
        name = p["name"]
        try:
            ops = sheets.read_journal(p)
            if ops:
                db.sync_point(p["key"], ops)
            else:
                dmin, _ = db.date_bounds([p["key"]])
                if dmin is not None:  # раньше данные были, а теперь пусто → подозрительно
                    problems[f"журнал «{name}»"] = "пусто или нет доступа"
        except Exception as e:
            if _transient(e):
                transient = True
                log.warning("журнал %s: временная ошибка API (пропускаю): %s", name, str(e)[:60])
            else:
                problems[f"журнал «{name}»"] = str(e)[:140]
        try:
            if not sheets.read_dashboard(p):
                problems[f"дашборд «{name}»"] = "лист «Дашборд» пуст"
        except Exception as e:
            if _transient(e):
                transient = True
                log.warning("дашборд %s: временная ошибка API (пропускаю): %s", name, str(e)[:60])
            else:
                problems[f"дашборд «{name}»"] = str(e)[:140]

    if transient:
        return  # временный лимит/таймаут — не трогаем алерты и состояние, ждём след. цикла

    new = set(problems) - _alerted
    fixed = _alerted - set(problems)
    for k in sorted(new):
        await _notify(context, f"⚠️ *Проблема с таблицей*\n{k}: {problems[k]}\n"
                               "Сайт может показывать устаревшие данные.")
    for k in sorted(fixed):
        await _notify(context, f"✅ Таблица — {k} — снова в порядке.")
    _alerted.clear()
    _alerted.update(problems)

    # проактивные бизнес-алерты
    await business_alerts(context)


async def backup_job(context: ContextTypes.DEFAULT_TYPE):
    """Раз в день: выгружаем все операции из базы и присылаем файлом в Telegram."""
    try:
        ops = db.query_ops(["km9", "gulbuta"])
    except Exception as e:
        log.warning("backup query: %s", e)
        return
    if not ops:
        return
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Дата", "Точка", "Раздел", "Статья", "Описание", "Приход", "Расход"])
    for o in ops:
        w.writerow([
            o["date"].strftime("%d.%m.%Y") if o["date"] else "",
            o["point"], o["section"], o["article"], o["desc"],
            round(o["income"]) if o["income"] else "",
            round(o["expense"]) if o["expense"] else "",
        ])
    data = buf.getvalue().encode("utf-8")
    fname = f"toptoza_backup_{dt.date.today():%Y-%m-%d}.csv"
    cap = f"🗄 Бэкап базы на {dt.date.today():%d.%m.%Y} · {len(ops)} операций"
    for uid in (ALLOWED_USERS or []):
        try:
            bio = io.BytesIO(data)
            bio.name = fname
            await context.bot.send_document(uid, document=bio, filename=fname, caption=cap)
        except Exception as e:
            log.warning("backup send %s: %s", uid, e)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    """Тихо гасим Conflict (бывает при перезапуске), остальное логируем как ошибку."""
    err = context.error
    if isinstance(err, Conflict):
        log.warning("Conflict при опросе (обычно во время перезапуска, проходит сам)")
        return
    log.error("Необработанная ошибка: %s", err, exc_info=err)


# ───────────────────── кнопки, меню, настройки ─────────────────────
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Сводка", callback_data="svodka")],
        [InlineKeyboardButton("📍 9 км", callback_data="km9"),
         InlineKeyboardButton("📍 Гульбута", callback_data="gulbuta")],
        [InlineKeyboardButton("🌐 Открыть сайт", callback_data="site"),
         InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
    ])


def settings_keyboard():
    on = db.get_setting("daily_on", "1") == "1"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔔 Авто-отчёт: {'ВКЛ ✅' if on else 'ВЫКЛ ⛔'}",
                              callback_data="toggle_daily")],
        [InlineKeyboardButton("📤 Прислать сводку сейчас", callback_data="svodka")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu")],
    ])


def _settings_text():
    on = db.get_setting("daily_on", "1") == "1"
    return ("⚙️ *Настройки уведомлений*\n\n"
            f"• Ежедневная сводка в {REPORT_HOUR}:00 — {'включена ✅' if on else 'выключена ⛔'}\n"
            "• Итоги недели — по понедельникам\n"
            "• Итоги месяца — 1-го числа\n"
            "• Бэкап базы — каждый день в 23:00")


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ У вас нет доступа.")
        return
    await update.message.reply_text("Что показать?", reply_markup=main_keyboard())


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ У вас нет доступа.")
        return
    await update.message.reply_text(_settings_text(), parse_mode="Markdown",
                                    reply_markup=settings_keyboard())


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if ALLOWED_USERS and q.from_user.id not in ALLOWED_USERS:
        await q.message.reply_text("⛔ Нет доступа.")
        return
    chat = q.message.chat_id
    data = q.data
    if data == "svodka":
        await context.bot.send_message(chat, "Собираю сводку…")
        await send_full_report(context.bot, chat)
    elif data in ("km9", "gulbuta"):
        point = POINTS[0 if data == "km9" else 1]
        try:
            parsed = read_point(point)
            await context.bot.send_message(chat, render_point(parsed, point["name"]),
                                           parse_mode="Markdown")
        except Exception as e:
            await context.bot.send_message(chat, f"Не получилось: {e}")
    elif data == "site":
        await context.bot.send_message(chat, f"🔗 {SITE_URL}", disable_web_page_preview=True)
    elif data == "menu":
        await q.edit_message_text("Что показать?", reply_markup=main_keyboard())
    elif data == "settings":
        await q.edit_message_text(_settings_text(), parse_mode="Markdown",
                                  reply_markup=settings_keyboard())
    elif data == "toggle_daily":
        cur = db.get_setting("daily_on", "1") == "1"
        db.set_setting("daily_on", "0" if cur else "1")
        await q.edit_message_text(_settings_text(), parse_mode="Markdown",
                                  reply_markup=settings_keyboard())


def build_period_report(title, start, end):
    ops = db.query_ops(["km9", "gulbuta"], start, end)
    inc = sum(o["income"] for o in ops)
    exp = sum(o["expense"] for o in ops)
    lines = [f"🗓 *{title}*", "",
             f"• Приход: {fmt_money(inc)} сом.",
             f"• Расход: {fmt_money(exp)} сом.",
             f"• Чистый поток: {fmt_money(inc - exp)} сом."]
    ins = insights.narrative(ops)
    if ins:
        lines.append("")
        lines.append("*Выводы:*")
        lines += ["• " + line for line in ins[:4]]
    lines += ["", f"🔗 {SITE_URL}"]
    return "\n".join(lines)


async def period_report_job(context: ContextTypes.DEFAULT_TYPE):
    if db.get_setting("daily_on", "1") != "1":
        return
    today = dt.date.today()
    if today.weekday() == 0:  # понедельник — итоги прошлой недели
        start = today - dt.timedelta(days=7)
        end = today - dt.timedelta(days=1)
        await _notify(context, build_period_report(
            f"Итоги недели {start:%d.%m}–{end:%d.%m}", start, end))
    if today.day == 1:  # 1-е число — итоги прошлого месяца
        last = today - dt.timedelta(days=1)
        start = last.replace(day=1)
        await _notify(context, build_period_report(
            f"Итоги прошлого месяца ({start:%m.%Y})", start, last))


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
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("nastroiki", settings_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)

    try:
        db.init_db()  # чтобы таблица настроек существовала
    except Exception as e:
        log.warning("db init at start: %s", e)

    # Расписание (нужен extra job-queue; если нет — просто пропускаем)
    if app.job_queue is not None:
        jq = app.job_queue
        jq.run_daily(daily_report_job, time=_local_time(REPORT_HOUR), name="daily_report")
        jq.run_repeating(sync_job, interval=3600, first=15, name="hourly_sync")
        jq.run_daily(backup_job, time=_local_time(23), name="daily_backup")
        jq.run_daily(period_report_job, time=_local_time(9), name="period_reports")
        log.info("Расписание: отчёт %02d:00, неделя/месяц 09:00, синхр. каждый час, бэкап 23:00",
                 REPORT_HOUR)
    else:
        log.warning("job-queue недоступен — авто-задачи выключены")

    log.info("Бот запущен. Ожидаю команды…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
