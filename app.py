# -*- coding: utf-8 -*-
"""Веб-панель ТОП-ТОЗА для директора.

• вход по одному паролю (SITE_PASSWORD)
• читает дневной журнал ДДС из Google Таблиц, копит историю в базе
• фильтры по периодам: сегодня / вчера / 7 дней / месяц / всё
• графики и сводка по обеим точкам
"""
import os
import io
import re
import csv
import time
import calendar as _cal
import datetime as dt
from functools import wraps

from flask import (Flask, request, session, redirect, url_for,
                   render_template, flash, Response, send_from_directory)

import sheets
import db
import insights
import i18n

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "toptoza-dev-secret-change-me")
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "toptoza")
GOAL_REVENUE = float(os.environ.get("GOAL_REVENUE", "0") or 0)  # цель выручки на месяц

_last_sync = 0
_SYNC_EVERY = 300  # сек — реже, чтобы не упираться в минутную квоту Sheets API
ZERO_MONTHLY = {"Выручка": 0, "Всего заказов": 0, "Выдано": 0,
                "В работе": 0, "Сумма долга": 0, "Чистая прибыль": 0}
_monthly_cache = {"km9": dict(ZERO_MONTHLY), "gulbuta": dict(ZERO_MONTHLY)}
_orders_cache = {"km9": [], "gulbuta": []}


def ensure_synced(force=False):
    """Раз в ~2 мин читает Google Таблицы: операции → в базу, месячный итог → в кэш.
    Так мы не упираемся в минутную квоту Sheets API при частых кликах."""
    global _last_sync
    if not force and time.time() - _last_sync < _SYNC_EVERY:
        return
    try:
        db.init_db()
        for p in sheets.POINTS:
            db.sync_point(p["key"], sheets.read_journal(p))
        for p in sheets.POINTS:
            _monthly_cache[p["key"]] = read_monthly([p["key"]])
            _orders_cache[p["key"]] = sheets.read_orders(p)
        _last_sync = time.time()
    except Exception as e:
        app.logger.warning("sync failed (оставляю прошлые данные): %s", e)


@app.context_processor
def inject_i18n():
    lang = request.cookies.get("lang", "ru")
    return {"lang": lang, "t": (lambda s: i18n.t(s, lang)),
            "today_str": dt.date.today().strftime("%d.%m.%Y")}


@app.route("/lang/<code>")
def set_lang(code):
    code = "tj" if code == "tj" else "ru"
    resp = redirect(request.referrer or url_for("dashboard"))
    resp.set_cookie("lang", code, max_age=60 * 60 * 24 * 365)
    return resp


def login_required(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get("auth"):
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return wrap


# ─────────────────────────── период ───────────────────────────
def period_bounds(period):
    today = dt.date.today()
    if period == "custom":   # свой период из полей from/to (формат YYYY-MM-DD)
        try:
            f = dt.date.fromisoformat((request.args.get("from") or "").strip())
            t = dt.date.fromisoformat((request.args.get("to") or "").strip())
            if f > t:
                f, t = t, f
            return f, t, f"{f.strftime('%d.%m.%Y')} – {t.strftime('%d.%m.%Y')}"
        except Exception:
            return None, None, "Свой период"
    if period == "today":
        return today, today, "Сегодня"
    if period == "yesterday":
        y = today - dt.timedelta(days=1)
        return y, y, "Вчера"
    if period == "7d":
        return today - dt.timedelta(days=6), today, "7 дней"
    if period == "month":
        return today.replace(day=1), today, "Этот месяц"
    return None, None, "Всё время"  # all


def fmt(n):
    n = round(n or 0)
    return f"{n:,}".replace(",", " ")


# ── капитальные/инкассация — НЕ операционные (показываем отдельно) ──
def _cap_exp(article):
    a = (article or "").lower()
    return (("главную кассу" in a) or ("закуп" in a) or ("ремонт помещ" in a)
            or ("приход в кассу" in a))  # перемещения/капитал — не операц. расход


def _cap_inc(article):
    return "приход в кассу" in (article or "").lower()


def op_amounts(ops):
    """Операционные приход/расход (без инкассации и капитальных)."""
    inc = sum(o["income"] for o in ops if not _cap_inc(o["article"]))
    exp = sum(o["expense"] for o in ops if not _cap_exp(o["article"]))
    return inc, exp


# ─────────────────────────── агрегации ───────────────────────────
def aggregate(ops):
    income = expense = 0.0
    by_article = {}
    inc_article = {}
    by_day = {}
    cap = {"Инкассация (в гл. кассу)": 0.0, "Закуп оборудования": 0.0,
           "Ремонт помещения": 0.0, "Пополнение кассы": 0.0}
    for o in ops:
        art = o["article"] or "Прочее"
        al = art.lower()
        ce, ci = _cap_exp(art), _cap_inc(art)
        if o["expense"]:
            if ce:
                if "главную кассу" in al:
                    cap["Инкассация (в гл. кассу)"] += o["expense"]
                elif "закуп" in al:
                    cap["Закуп оборудования"] += o["expense"]
                elif "приход в кассу" in al:
                    cap["Пополнение кассы"] += o["expense"]
                else:
                    cap["Ремонт помещения"] += o["expense"]
            else:
                expense += o["expense"]
                by_article[art] = by_article.get(art, 0) + o["expense"]
        if o["income"]:
            if ci:
                cap["Пополнение кассы"] += o["income"]
            else:
                income += o["income"]
                inc_article[art] = inc_article.get(art, 0) + o["income"]
        d = o["date"]
        if d:
            slot = by_day.setdefault(d, {"income": 0, "expense": 0})
            if not ci:
                slot["income"] += o["income"]
            if not ce:
                slot["expense"] += o["expense"]
    days = sorted(by_day)
    arts = sorted(by_article.items(), key=lambda x: -x[1])[:8]
    iarts = sorted(inc_article.items(), key=lambda x: -x[1])[:8]
    return {
        "income": income, "expense": expense, "net": income - expense,
        "count": len(ops),
        "by_day": [{"date": d.strftime("%d.%m"), "income": round(by_day[d]["income"]),
                    "expense": round(by_day[d]["expense"])} for d in days],
        "by_article": [{"article": a, "sum": round(s)} for a, s in arts],
        "inc_article": [{"article": a, "sum": round(s)} for a, s in iarts],
        "capital": [{"name": k, "sum": round(v)} for k, v in cap.items() if v],
        "capital_total": round(sum(cap.values())),
        "cap_inkass": round(cap["Инкассация (в гл. кассу)"]),
        "cap_capex": round(cap["Закуп оборудования"] + cap["Ремонт помещения"]),
        "cap_popoln": round(cap["Пополнение кассы"]),
        # остаток = приход − расход − капитальные (закуп+ремонт помещ.) − инкассация
        "ostatok": round(income - expense - cap["Закуп оборудования"]
                         - cap["Ремонт помещения"] - cap["Инкассация (в гл. кассу)"]),
    }


def read_monthly(point_keys):
    """Месячный итог из листов «Дашборд», суммированный по точкам."""
    agg_dash = {}
    for k in point_keys:
        p = next(p for p in sheets.POINTS if p["key"] == k)
        d = sheets.read_dashboard(p)
        for sec, items in d.items():
            for it, val in items.items():
                agg_dash[it] = agg_dash.get(it, 0) + val
    return {
        "Выручка": agg_dash.get("Выручка", 0),
        "Всего заказов": agg_dash.get("Всего заказов", 0),
        "Выдано": agg_dash.get("Выдано", 0),
        "В работе": agg_dash.get("В работе", 0),
        "Сумма долга": agg_dash.get("Сумма долга", 0),
        "Чистая прибыль": agg_dash.get("Чистая прибыль", 0),
    }


def prev_month_range(d):
    first = d.replace(day=1)
    pe = first - dt.timedelta(days=1)
    return pe.replace(day=1), pe


def prev_bounds(period, start, end):
    """Предыдущий сопоставимый период для сравнения."""
    if period == "month":
        return prev_month_range(dt.date.today())
    if start is None:          # «всё» — предыдущего нет
        return None, None
    length = (end - start).days + 1
    pend = start - dt.timedelta(days=1)
    pstart = pend - dt.timedelta(days=length - 1)
    return pstart, pend


def compute_health(point_keys):
    """Светофор, прогноз на месяц, сравнение с прошлым месяцем, план/факт."""
    today = dt.date.today()
    ms = today.replace(day=1)
    ps, pe = prev_month_range(today)
    cur = db.query_ops(point_keys, ms, today)
    prev = db.query_ops(point_keys, ps, pe)
    cur_inc, cur_exp = op_amounts(cur)       # операционные (без инкассации/капитала)
    prev_inc, _ = op_amounts(prev)
    net = cur_inc - cur_exp
    dim = _cal.monthrange(today.year, today.month)[1]
    forecast = round(cur_inc / today.day * dim) if today.day else 0
    mom = round((forecast - prev_inc) / prev_inc * 100) if prev_inc else None
    goal_pct = round(forecast / GOAL_REVENUE * 100) if GOAL_REVENUE else None
    if net >= 0 and (mom is None or mom >= 0):
        light, light_txt = "green", "Всё хорошо"
    elif net >= 0:
        light, light_txt = "gold", "Внимание — рост замедлился"
    else:
        light, light_txt = "red", "Расходы выше прихода"
    return {
        "light": light, "light_txt": light_txt,
        "month_income": round(cur_inc), "month_net": round(net),
        "forecast": forecast, "prev_income": round(prev_inc),
        "mom": mom, "goal": round(GOAL_REVENUE), "goal_pct": goal_pct,
    }


POINT_NAME = {"km9": "9 км", "gulbuta": "Гульбута"}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if (request.form.get("password") or "").strip() == SITE_PASSWORD:
            session["auth"] = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Неверный пароль")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ───────────────────────── зарплата сотрудников ─────────────────────────
# Сдельные ставки (сом за единицу). Разница только в шторах: водитель 3, мойщик 5.
DRV_RATE = {"carpet_area": 2, "blanket_cnt": 5, "curtain_kg": 3, "quilt_cnt": 10}
MOY_RATE = {"carpet_area": 2, "blanket_cnt": 5, "curtain_kg": 5, "quilt_cnt": 10}
ROLE_LABEL = {"moyshik": "Мойщик", "voditel": "Водитель",
              "operator": "Оператор", "povar": "Повар", "admin": "Администратор"}


def _driver_code(s):
    """Код водителя из даты заказа: «29.05МО» → «МО»."""
    m = re.search(r"([А-Яа-яЁё]+)\s*$", s or "")
    return m.group(1).upper() if m else ""


def _is_zp(article):
    a = (article or "").lower()
    return ("зп" in a) or ("зарплат" in a) or ("аванс" in a) or ("оклад" in a)


def _vol_pay(orders, rate):
    return sum(o.get("carpet_area", 0) * rate["carpet_area"]
              + o.get("blanket_cnt", 0) * rate["blanket_cnt"]
              + o.get("curtain_kg", 0) * rate["curtain_kg"]
              + o.get("quilt_cnt", 0) * rate["quilt_cnt"] for o in orders)


def compute_salary(point_keys, start, end):
    db.init_db()
    emps = db.list_employees(point_keys)
    orders_by_point = {}
    for k in point_keys:
        lst = []
        for o in _orders_cache.get(k, []):
            if start:
                d = o.get("date")
                if d is None or d < start or d > end:
                    continue
            lst.append(o)
        orders_by_point[k] = lst
    # мойщики: котёл делится ПО ДНЯМ — за каждый день объём (по дате приёма)
    # делится только между теми, кто в тот день работал (выходные исключены).
    moy_by_point = {k: [e for e in emps if e["active"] and e["role"] == "moyshik"
                        and e["point"] == k] for k in point_keys}
    moy_earn, moy_days = {}, {}
    for k in point_keys:
        by_date = {}
        for o in orders_by_point[k]:
            d = o.get("date")
            if d:
                by_date.setdefault(d, []).append(o)
        for d, ords in by_date.items():
            iso = d.isoformat()
            present = [m for m in moy_by_point[k] if iso not in m["off"]]
            if not present:
                continue
            share = _vol_pay(ords, MOY_RATE) / len(present)
            for m in present:
                moy_earn[m["id"]] = moy_earn.get(m["id"], 0) + share
                moy_days[m["id"]] = moy_days.get(m["id"], 0) + 1

    # авансы/выплаты из журнала (статья ЗП) — матчим по имени, для водителей по коду
    avans_auto = {e["id"]: 0.0 for e in emps}
    zp_ops = [o for o in db.query_ops(point_keys, start, end)
              if o["expense"] and _is_zp(o.get("article"))]
    unmatched_sum, unmatched_cnt = 0.0, 0
    for o in zp_ops:
        text = (((o.get("desc") or "") + " " + (o.get("article") or "")).lower())
        hit = None
        for e in emps:
            if e["point"] != o["point"]:
                continue
            if e["role"] == "voditel" and e["driver_code"]:
                if re.search(r"(?<![а-яёa-z])" + re.escape(e["driver_code"].lower()) + r"(?![а-яёa-z])", text):
                    hit = e; break
            elif e["name"] and e["name"].lower() in text:
                hit = e; break
        if hit:
            avans_auto[hit["id"]] += o["expense"]
        else:
            unmatched_sum += o["expense"]; unmatched_cnt += 1

    rows = []
    for e in emps:
        earned, detail = 0.0, ""
        pt = e["point"]
        pords = orders_by_point.get(pt, [])
        if e["role"] == "voditel":
            code = (e["driver_code"] or "").upper()
            myo = [o for o in pords if _driver_code(o.get("date_received")) == code] if code else []
            earned = _vol_pay(myo, DRV_RATE)
            detail = f"{len(myo)} заказов"
        elif e["role"] == "moyshik":
            earned = moy_earn.get(e["id"], 0)
            dcount = moy_days.get(e["id"], 0)
            detail = f"{dcount} раб. дней" + (f" · {len(e['off'])} вых." if e["off"] else "")
        else:
            earned = e["salary"]
            detail = "оклад/мес"
        avans_manual = e["avans"] or 0.0
        avans = avans_auto.get(e["id"], 0.0) + avans_manual
        rows.append({**e, "role_label": ROLE_LABEL.get(e["role"], e["role"]),
                     "earned": round(earned), "avans": round(avans),
                     "avans_manual": round(avans_manual),
                     "avans_jrnl": round(avans_auto.get(e["id"], 0.0)),
                     "to_pay": round(earned - avans), "detail": detail})
    rows.sort(key=lambda r: (not r["active"], -r["earned"]))
    act = [r for r in rows if r["active"]]
    sdel = sum(r["earned"] for r in act if r["role"] in ("moyshik", "voditel"))
    fix = sum(r["earned"] for r in act if r["role"] in ("operator", "povar", "admin"))
    # дни периода — для выбора выходных (если период не слишком длинный)
    days = []
    if start and end and (end - start).days <= 92:
        cur = start
        while cur <= end:
            days.append({"iso": cur.isoformat(), "d": cur.day, "m": cur.month})
            cur += dt.timedelta(days=1)
    return {
        "rows": rows, "total": round(sdel + fix), "sdel": round(sdel), "fix": round(fix),
        "avans": round(sum(r["avans"] for r in act)),
        "to_pay": round(sum(r["to_pay"] for r in act)),
        "count": len(act), "unmatched_sum": round(unmatched_sum), "unmatched_cnt": unmatched_cnt,
        "days": days,
    }


def _salary_back():
    period = request.form.get("period", "month")
    view = request.form.get("view", "total")
    return url_for("dashboard") + f"?section=salary&period={period}&view={view}"


@app.route("/employees/add", methods=["POST"])
@login_required
def emp_add():
    db.init_db()
    f = request.form
    if (f.get("name") or "").strip():
        db.add_employee(point=f.get("point", "km9"), name=f.get("name", ""),
                        role=f.get("role", "moyshik"), tseh=f.get("tseh", ""),
                        driver_code=f.get("driver_code", ""), salary=f.get("salary") or 0,
                        avans=f.get("avans") or 0,
                        active=1 if f.get("active", "1") == "1" else 0)
    return redirect(_salary_back())


@app.route("/employees/edit/<int:emp_id>", methods=["POST"])
@login_required
def emp_edit(emp_id):
    f = request.form
    db.update_employee(emp_id, point=f.get("point", "km9"), name=f.get("name", ""),
                       role=f.get("role", "moyshik"), tseh=f.get("tseh", ""),
                       driver_code=f.get("driver_code", ""), salary=f.get("salary") or 0,
                       avans=f.get("avans") or 0,
                       active=1 if f.get("active", "1") == "1" else 0)
    return redirect(_salary_back())


@app.route("/employees/delete/<int:emp_id>", methods=["POST"])
@login_required
def emp_del(emp_id):
    db.delete_employee(emp_id)
    return redirect(_salary_back())


@app.route("/employees/dayoff/<int:emp_id>", methods=["POST"])
@login_required
def emp_dayoff(emp_id):
    """Сохраняет выходные сотрудника: заменяет дни в пределах показанного периода,
    дни других месяцев сохраняет."""
    e = db.get_employee(emp_id)
    if not e:
        return redirect(_salary_back())
    period = request.form.get("period", "month")
    start, end, _ = period_bounds(period)
    submitted = set(request.form.getlist("day"))
    kept = set()
    for d in e["off"]:
        try:
            dd = dt.date.fromisoformat(d)
        except ValueError:
            continue
        if start and end and start <= dd <= end:
            continue  # этот день в показанном периоде — перезапишем
        kept.add(d)
    db.update_employee(emp_id, days_off=",".join(sorted(kept | submitted)))
    return redirect(_salary_back())


@app.route("/")
@login_required
def dashboard():
    period = request.args.get("period", "all")
    view = request.args.get("view", "total")  # total | km9 | gulbuta
    section = request.args.get("section", "summary")
    ensure_synced(force=request.args.get("refresh") == "1")

    point_keys = ["km9", "gulbuta"] if view == "total" else [view]
    start, end, plabel = period_bounds(period)
    ops = db.query_ops(point_keys, start, end)
    q = (request.args.get("q") or "").strip()
    if q:
        ql = q.lower()
        ops = [o for o in ops
               if ql in (o["article"] or "").lower() or ql in (o["desc"] or "").lower()]
    agg = aggregate(ops)
    health = compute_health(point_keys) if section == "summary" else None

    insights_data = None
    if section == "summary":
        prev_ops = None
        if period == "month":
            ps, pe = prev_month_range(dt.date.today())
            prev_ops = db.query_ops(point_keys, ps, pe)
        insights_data = insights.analytics(ops)
        insights_data["narrative"] = insights.narrative(ops, prev_ops)

    comparison = None
    if section == "compare":
        ps, pend = prev_bounds(period, start, end)
        prev_ops = db.query_ops(point_keys, ps, pend) if ps else []
        ci, ce = op_amounts(ops)
        pi, pex = op_amounts(prev_ops)

        def _d(a, b):
            return round((a - b) / b * 100) if b else None

        comparison = {
            "rows": [
                {"label": "Приход", "cur": round(ci), "prev": round(pi), "d": _d(ci, pi)},
                {"label": "Расход", "cur": round(ce), "prev": round(pex), "d": _d(ce, pex)},
                {"label": "Чистый поток", "cur": round(ci - ce), "prev": round(pi - pex), "d": _d(ci - ce, pi - pex)},
                {"label": "Операций", "cur": len(ops), "prev": len(prev_ops), "d": _d(len(ops), len(prev_ops))},
            ],
            "has_prev": bool(ps),
            "prev_label": (ps.strftime('%d.%m.%Y') + " – " + pend.strftime('%d.%m.%Y')) if ps else "—",
        }

    # последние операции
    recent = [{
        "in": bool(o["income"]),
        "desc": ((o["article"] or "") + (" · " + o["desc"] if o["desc"] else "")).strip(" ·") or "—",
        "meta": (o["date"].strftime("%d.%m.%Y") if o["date"] else "") + " · " + POINT_NAME.get(o["point"], ""),
        "amount": round(o["income"] or o["expense"]),
    } for o in ops[:40]]

    # сравнение точек за период
    compare = []
    for k in ["km9", "gulbuta"]:
        pops = db.query_ops([k], start, end)
        inc, exp = op_amounts(pops)
        compare.append({"name": POINT_NAME[k], "income": round(inc),
                        "expense": round(exp), "net": round(inc - exp)})

    orders_data = None
    if section in ("orders", "debts"):
        allo = []
        for k in point_keys:
            for o in _orders_cache.get(k, []):
                if start:  # фильтр по дате приёма (для периода кроме «Всё»)
                    d = o.get("date")
                    if d is None or d < start or d > end:
                        continue
                allo.append(dict(o, point=k))
        rab = [o for o in allo if not o["issued"]]
        vyd = [o for o in allo if o["issued"]]
        byc = {}
        for o in rab:
            key = o["name"] or o["client"] or "—"
            g = byc.setdefault(key, {"name": key, "phone": o["phone"], "sum": 0, "cnt": 0})
            g["sum"] += o["total"]
            g["cnt"] += 1
        debtors = sorted(byc.values(), key=lambda x: -x["sum"])

        # (name, volume_field, unit, sum_field, rate) — ставки фиксированные:
        # ковёр 12 сом/м², одеяло 50 сом/шт, шторы 30 сом/кг, курпача 70 сом/шт.
        # Объём берём из реальных колонок листа, сумму — из *_sum (уже посчитана).
        svc_defs = [
            ("Ковёр",   "carpet_area", "м²", "carpet_sum",  12),
            ("Одеяло",  "blanket_cnt", "шт", "blanket_sum", 50),
            ("Шторы",   "curtain_kg",  "кг", "curtain_sum", 30),
            ("Курпача", "quilt_cnt",   "шт", "quilt_sum",   70),
        ]
        services = []
        for nm, volf, unit, sumf, rate in svc_defs:
            svc_sum = round(sum(o.get(sumf, 0) for o in allo))
            vol = round(sum(o.get(volf, 0) for o in allo), 1)
            services.append({
                "name": nm, "unit": unit,
                "vol": vol, "sum": svc_sum, "price": rate,
            })
        tot_svc = sum(x["sum"] for x in services) or 1
        for x in services:
            x["share"] = round(x["sum"] / tot_svc * 100)
        services.sort(key=lambda x: -x["sum"])

        def _onum(o):
            s = "".join(ch for ch in str(o["num"]) if ch.isdigit())
            return int(s) if s else 0
        order_list = sorted(allo, key=_onum, reverse=True)  # свежие (больший №) сверху
        orders_data = {
            "total": len(allo), "issued": len(vyd), "work": len(rab),
            "revenue": round(sum(o["total"] for o in allo)),
            "area": round(sum(o["area"] for o in allo), 1),
            "debt": round(sum(o["total"] for o in rab)),
            "issued_sum": round(sum(o["total"] for o in vyd)),
            "avg_check": round(sum(o["total"] for o in allo) / len(allo)) if allo else 0,
            "max_check": round(max((o["total"] for o in allo), default=0)),
            "services": services,
            "orders": [{
                "num": o["num"], "date": o["date_received"], "name": o["name"],
                "phone": o["phone"], "area": o["area"], "total": round(o["total"]),
                "carpet_cnt": o["carpet_cnt"], "blanket": o["blanket_cnt"],
                "curtain": o["curtain_kg"], "quilt": o["quilt_cnt"],
                "issued": o["issued"], "point": POINT_NAME.get(o["point"], ""),
            } for o in order_list[:250]],
            "debt_orders": [{
                "num": o["num"], "date": o["date_received"], "name": o["name"],
                "phone": o["phone"], "area": o["area"], "blanket": o["blanket_cnt"],
                "curtain": o["curtain_kg"], "quilt": o["quilt_cnt"],
                "total": round(o["total"]), "point": POINT_NAME.get(o["point"], ""),
            } for o in order_list if not o["issued"]][:250],
            "debtors": [{"name": d["name"], "phone": d["phone"],
                         "sum": round(d["sum"]), "cnt": d["cnt"]} for d in debtors[:60]],
        }

    salary_data = compute_salary(point_keys, start, end) if section == "salary" else None

    # касса — нарастающим итогом с самого начала (старт 0).
    # «Было на начало периода» = весь поток ДО начала периода.
    if start:
        before = db.query_ops(point_keys, None, start - dt.timedelta(days=1))
        cash_before = aggregate(before)["ostatok"]
    else:
        cash_before = 0.0
    cash_now = cash_before + agg["ostatok"]

    # месячный итог — из кэша (обновляется в ensure_synced раз в 2 мин)
    monthly_by_point = {k: _monthly_cache.get(k, dict(ZERO_MONTHLY))
                        for k in ["km9", "gulbuta"]}
    monthly = {key: sum(monthly_by_point[k][key] for k in point_keys)
               for key in ZERO_MONTHLY}

    dmin, dmax = db.date_bounds(point_keys)

    data = {
        "agg": agg, "recent": recent, "compare": compare,
        "monthly": monthly, "monthly_by_point": monthly_by_point,
        "health": health, "insights": insights_data,
        "comparison": comparison, "orders_data": orders_data, "salary": salary_data, "q": q,
        "cash_before": round(cash_before), "cash_now": round(cash_now),
        "period": period, "view": view, "section": section, "plabel": plabel,
        "range": (f"{dmin.strftime('%d.%m.%Y')} — {dmax.strftime('%d.%m.%Y')}"
                  if dmin and dmax else "нет данных"),
    }
    return render_template("dashboard.html", data=data, fmt=fmt,
                           POINT_NAME=POINT_NAME)


@app.route("/export.csv")
@login_required
def export_csv():
    period = request.args.get("period", "all")
    view = request.args.get("view", "total")
    ensure_synced()
    point_keys = ["km9", "gulbuta"] if view == "total" else [view]
    start, end, _ = period_bounds(period)
    ops = db.query_ops(point_keys, start, end)
    buf = io.StringIO()
    buf.write("﻿")  # BOM, чтобы Excel понял кириллицу
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Дата", "Точка", "Раздел", "Статья", "Описание", "Приход", "Расход"])
    for o in ops:
        w.writerow([
            o["date"].strftime("%d.%m.%Y") if o["date"] else "",
            POINT_NAME.get(o["point"], o["point"]),
            o["section"], o["article"], o["desc"],
            round(o["income"]) if o["income"] else "",
            round(o["expense"]) if o["expense"] else "",
        ])
    fn = f"toptoza_{view}_{period}.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fn}"})


@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json", mimetype="application/manifest+json")


@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    db.init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
