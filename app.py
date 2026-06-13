# -*- coding: utf-8 -*-
"""Веб-панель ТОП-ТОЗА для директора.

• вход по одному паролю (SITE_PASSWORD)
• читает дневной журнал ДДС из Google Таблиц, копит историю в базе
• фильтры по периодам: сегодня / вчера / 7 дней / месяц / всё
• графики и сводка по обеим точкам
"""
import os
import io
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
_SYNC_EVERY = 120  # сек
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
    return {"lang": lang, "t": (lambda s: i18n.t(s, lang))}


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
    cur_inc = sum(o["income"] for o in cur)
    cur_exp = sum(o["expense"] for o in cur)
    prev_inc = sum(o["income"] for o in prev)
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
        ci = sum(x["income"] for x in ops)
        ce = sum(x["expense"] for x in ops)
        pi = sum(x["income"] for x in prev_ops)
        pex = sum(x["expense"] for x in prev_ops)

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
        inc = sum(o["income"] for o in pops)
        exp = sum(o["expense"] for o in pops)
        compare.append({"name": POINT_NAME[k], "income": round(inc),
                        "expense": round(exp), "net": round(inc - exp)})

    orders_data = None
    if section in ("orders", "debts"):
        allo = []
        for k in point_keys:
            for o in _orders_cache.get(k, []):
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
            "orders": [{
                "num": o["num"], "date": o["date_received"], "name": o["name"],
                "phone": o["phone"], "area": o["area"], "total": round(o["total"]),
                "issued": o["issued"], "point": POINT_NAME.get(o["point"], ""),
            } for o in order_list[:250]],
            "debtors": [{"name": d["name"], "phone": d["phone"],
                         "sum": round(d["sum"]), "cnt": d["cnt"]} for d in debtors[:60]],
        }

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
        "comparison": comparison, "orders_data": orders_data, "q": q,
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
