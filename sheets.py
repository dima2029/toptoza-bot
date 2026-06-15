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
    """Понимает форматы: '12.06.2026', '12.06.26', '12.06', '12/6', '9-мая',
    '1 июн.', '9 май 2026'. Возвращает datetime.date или None."""
    if text is None:
        return None
    s = str(text).strip().lower().replace("ё", "е")
    if not s:
        return None
    year = default_year or dt.date.today().year
    # ведущие цифры из части: "05МО" -> 5, "29" -> 29, "мая" -> None
    def _lead(p):
        ds = ""
        for ch in p.strip():
            if ch.isdigit():
                ds += ch
            else:
                break
        return int(ds) if ds else None

    # 12.06.2026 / 12.06 / 12/6 / 12,06 / 29.05МО / 06,06АМ
    for sep in (".", "/", "-", ","):
        parts = s.split(sep)
        if len(parts) == 3:
            nums = [_lead(p) for p in parts]
            if all(n is not None for n in nums):
                d, m, y = nums
                if y < 100:
                    y += 2000
                try:
                    return dt.date(y, m, d)
                except ValueError:
                    return None
        if len(parts) == 2:
            nums = [_lead(p) for p in parts]
            if all(n is not None for n in nums):
                d, m = nums
                try:
                    return dt.date(year, m, d)
                except ValueError:
                    return None
    # «9-мая», «1 июн.», «29мая», «9 май 2026»
    s2 = s.replace("-", " ").replace(".", " ").replace(",", " ")
    tokens = [t for t in s2.split() if t]
    day = None
    month = None
    ynum = None
    for t in tokens:
        if t.isdigit():
            n = int(t)
            if n > 1900:
                ynum = n
            elif day is None:
                day = n
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
            return dt.date(ynum or year, month, day)
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


def read_orders(point):
    """Журнал заказов из листа «процесс работы».
    Колонки: A №, B дата приёма, C клиент(тел+имя), D ковёр шт, E м², F цена/м²,
    G одеяло шт, H цена/шт, I шторы кг, J цена/кг, K курпача шт, L цена/шт,
    M ИТОГО, N дата выдачи, O подпись.
    Суммы по услугам считаем сами: количество × цена (F, H, J, L — цены, не суммы).
    Заказ «в работе», если дата выдачи (N) пустая; иначе «выдан».
    """
    sh = _open(point)
    ws = None
    for w in sh.worksheets():
        if "работ" in w.title.lower():
            ws = w
            break
    if ws is None:
        return []
    rows = ws.get_all_values()
    out = []
    for r in rows[5:]:  # шапка занимает первые строки
        def c(i):
            return (r[i].strip() if len(r) > i and r[i] is not None else "")
        client = c(2)
        total = parse_number(c(12)) or 0.0
        area = parse_number(c(4)) or 0.0
        if total == 0 and area == 0:        # пустые/служебные строки
            continue
        date_iss = c(13)
        parts = client.split(" ", 1)
        phone = parts[0] if parts and parts[0].replace("+", "").isdigit() else ""
        name = (parts[1] if phone and len(parts) > 1 else client).strip()

        # F/H/J/L — это УЖЕ суммы по услугам (кол-во×ставка посчитано в листе),
        # а не цены за единицу. Берём напрямую.
        carpet_cnt = parse_number(c(3)) or 0.0
        carpet_sum = parse_number(c(5)) or 0.0   # F = сумма за ковры
        blanket_cnt = parse_number(c(6)) or 0.0
        blanket_sum = parse_number(c(7)) or 0.0  # H = сумма за одеяла
        curtain_kg = parse_number(c(8)) or 0.0
        curtain_sum = parse_number(c(9)) or 0.0  # J = сумма за шторы
        quilt_cnt = parse_number(c(10)) or 0.0
        quilt_sum = parse_number(c(11)) or 0.0   # L = сумма за курпача

        out.append({
            "num": c(0), "date_received": c(1), "date": parse_date(c(1)),
            "client": client, "phone": phone, "name": name or "—",
            "carpets": carpet_cnt, "area": area,
            "total": total, "issued": bool(date_iss), "date_issued": date_iss,
            "carpet_cnt": carpet_cnt, "carpet_area": area, "carpet_sum": carpet_sum,
            "blanket_cnt": blanket_cnt, "blanket_sum": blanket_sum,
            "curtain_kg": curtain_kg, "curtain_sum": curtain_sum,
            "quilt_cnt": quilt_cnt, "quilt_sum": quilt_sum,
        })
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
