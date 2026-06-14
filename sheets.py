# -*- coding: utf-8 -*-
"""Чтение данных ТОП-ТОЗА из Google Таблиц.

Два источника на каждую точку:
  • лист «Дашборд»  — месячный итог (KPI: выручка, прибыль, долг, заказы)
  • лист «ддс …»    — дневной журнал операций (приход/расход по датам)

Ключи берутся из переменных окружения (как у бота):
  GOOGLE_CREDENTIALS, SHEET_ID_KM9, SHEET_ID_GULBUTA
"""
import os
import re
import json
import datetime as dt

import gspread

POINTS = [
    {"key": "km9", "name": "9 км", "sheet_env": "SHEET_ID_KM9",
     "dashboard": "Дашборд", "journal": "ддс 9 км"},
    {"key": "gulbuta", "name": "Гульбута", "sheet_env": "SHEET_ID_GULBUTA",
     "dashboard": "Дашборд", "journal": "ддс гулбута"},
]

_gc = None


def get_client():
    global _gc
    if _gc is not None:
        return _gc
    creds = os.environ.get("GOOGLE_CREDENTIALS", "").strip()
    if creds:
        _gc = gspread.service_account_from_dict(json.loads(creds))
    else:
        _gc = gspread.service_account(filename="service_account.json")
    return _gc


# ─────────────────────────── разбор чисел и дат ───────────────────────────
def parse_number(text):
    s = str(text).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    if not s or s.startswith("#"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


_MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}


def parse_date(text, default_year=None):
    """Понимает форматы: '12.06.2026', '12.06.26', '9-мая', '1 июн.'.
    Возвращает datetime.date или None."""
    if not text:
        return None
    s = str(text).strip().lower().replace("ё", "е")
    year = default_year or dt.date.today().year
    # 12.06.2026 / 12.06.26 / 12/06/2026
    for sep in (".", "/", "-"):
        parts = s.split(sep)
        if len(parts) == 3 and all(p.strip().isdigit() for p in parts):
            d, m, y = (int(p) for p in parts)
            if y < 100:
                y += 2000
            try:
                return dt.date(y, m, d)
            except ValueError:
                return None
    # «9-мая», «1 июн.», «29мая»
    s2 = s.replace("-", " ").replace(".", " ")
    tokens = [t for t in s2.split() if t]
    day = None
    month = None
    for t in tokens:
        if t.isdigit():
            day = int(t)
        else:
            # Разделить слитный токен вида «29мая» на цифровую и буквенную части
            m = re.match(r'^(\d+)([а-яё]+)$', t)
            if m and day is None:
                day = int(m.group(1))
                t = m.group(2)
            for pref, mnum in _MONTHS.items():
                if t.startswith(pref):
                    month = mnum
                    break
    if day and month:
        try:
            return dt.date(year, month, day)
        except ValueError:
            return None
    return None


# ─────────────────────────── чтение листов ───────────────────────────
def _open(point):
    gc = get_client()
    return gc.open_by_key(os.environ[point["sheet_env"]])


def read_dashboard(point):
    """Месячный итог. Возвращает {section: {item: number}}."""
    sh = _open(point)
    ws = sh.worksheet(point["dashboard"])
    rows = ws.get_all_values()
    out = {}
    section = None
    for row in rows[1:]:
        label = (row[0] if len(row) > 0 else "").strip()
        value = (row[1] if len(row) > 1 else "").strip()
        if not label:
            continue
        num = parse_number(value)
        if num is None:
            if value:  # заголовок раздела (в B — метка точки, не число)
                section = label
                out.setdefault(section, {})
            continue
        if section:
            out[section][label] = num
    return out


def read_journal(point):
    """Дневной журнал. Возвращает список операций:
    {date(date|None), section, article, desc, income(float), expense(float)}."""
    sh = _open(point)
    try:
        ws = sh.worksheet(point["journal"])
    except gspread.WorksheetNotFound:
        return []
    rows = ws.get_all_values()
    ops = []
    for row in rows[1:]:
        def c(i):
            return (row[i] if len(row) > i else "").strip()
        date = parse_date(c(0))
        section = c(1)
        article = c(2)
        desc = c(3)
        income = parse_number(c(4)) or 0.0
        expense = parse_number(c(5)) or 0.0
        if not (income or expense) and not section:
            continue
        ops.append({
            "date": date, "section": section, "article": article,
            "desc": desc, "income": income, "expense": expense,
        })
    return ops
