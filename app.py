# -*- coding: utf-8 -*-
"""Веб-панель ТОП-ТОЗА для директора.

• вход по одному паролю (SITE_PASSWORD)
• читает дневной журнал ДДС из Google Таблиц, копит историю в базе
• фильтры по периодам: сегодня / вчера / 7 дней / месяц / всё
• графики и сводка по обеим точкам
"""
import os
import time
import datetime as dt
from functools import wraps

from flask import (Flask, request, session, redirect, url_for,
                   render_template, flash)

import sheets
import db

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "toptoza-dev-secret-change-me")
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "toptoza")

_last_sync = 0
_SYNC_EVERY = 120  # сек
ZERO_MONTHLY = {"Выручка": 0, "Всего заказов": 0, "Выдано": 0,
                "В работе": 0, "Сумма долга": 0, "Чистая прибыль": 0}
_monthly_cache = {"km9": dict(ZERO_MONTHLY), "gulbuta": dict(ZERO_MONTHLY)}


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
        for k in ["km9", "gulbuta"]:
            _monthly_cache[k] = read_monthly([k])
        _last_sync = time.time()
    except Exception as e:
        app.logger.warning("sync failed (оставляю прошлые данные): %s", e)


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


# ─────────────────────────── агрегации ───────────────────────────
def aggregate(ops):
    income = sum(o["income"] for o in ops)
    expense = sum(o["expense"] for o in ops)
    by_article = {}
    inc_article = {}
    by_day = {}
    for o in ops:
        if o["expense"]:
            by_article[o["article"] or "Прочее"] = by_article.get(o["article"] or "Прочее", 0) + o["expense"]
        if o["income"]:
            inc_article[o["article"] or "Прочее"] = inc_article.get(o["article"] or "Прочее", 0) + o["income"]
        d = o["date"]
        if d:
            slot = by_day.setdefault(d, {"income": 0, "expense": 0})
            slot["income"] += o["income"]
            slot["expense"] += o["expense"]
    days = sorted(by_day)
    arts = sorted(by_article.items(), key=lambda x: -x[1])[:8]
    iarts = sorted(inc_article.items(), key=lambda x: -x[1])[:8]
    return {
        "income": income, "expense": expense, "net": income - expense,
        "count": len(ops),
        "by_day": [{"date": d.strftime("%d.%m"), "income": round(v["income"]),
                    "expense": round(v["expense"])} for d, v in
                   ((d, by_day[d]) for d in days)],
        "by_article": [{"article": a, "sum": round(s)} for a, s in arts],
        "inc_article": [{"article": a, "sum": round(s)} for a, s in iarts],
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
    agg = aggregate(ops)

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
        inc = sum(o["income"] for o in pops)
        exp = sum(o["expense"] for o in pops)
        compare.append({"name": POINT_NAME[k], "income": round(inc),
                        "expense": round(exp), "net": round(inc - exp)})

    # месячный итог — из кэша (обновляется в ensure_synced раз в 2 мин)
    monthly_by_point = {k: _monthly_cache.get(k, dict(ZERO_MONTHLY))
                        for k in ["km9", "gulbuta"]}
    monthly = {key: sum(monthly_by_point[k][key] for k in point_keys)
               for key in ZERO_MONTHLY}

    dmin, dmax = db.date_bounds(point_keys)

    data = {
        "agg": agg, "recent": recent, "compare": compare,
        "monthly": monthly, "monthly_by_point": monthly_by_point,
        "period": period, "view": view, "section": section, "plabel": plabel,
        "range": (f"{dmin.strftime('%d.%m.%Y')} — {dmax.strftime('%d.%m.%Y')}"
                  if dmin and dmax else "нет данных"),
    }
    return render_template("dashboard.html", data=data, fmt=fmt,
                           POINT_NAME=POINT_NAME)


if __name__ == "__main__":
    db.init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
