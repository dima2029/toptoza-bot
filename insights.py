# -*- coding: utf-8 -*-
"""Умная аналитика по операциям: дни недели, лучший/худший день,
ABC-расходов, точка безубыточности, текстовые выводы.

Все функции принимают список операций вида
{date, section, article, desc, income, expense} (как из db.query_ops).
"""
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _is_cap(o):
    """Капитальные/инкассация/перемещения — НЕ операционные (исключаем из аналитики)."""
    a = (o.get("article") or "").lower()
    if o.get("expense") and (("главную кассу" in a) or ("закуп" in a)
                             or ("ремонт помещ" in a) or ("приход в кассу" in a)):
        return True
    if o.get("income") and ("приход в кассу" in a):
        return True
    return False


def operational(ops):
    return [o for o in ops if not _is_cap(o)]


def fmt(n):
    return f"{round(n or 0):,}".replace(",", " ")


def _income_by_day(ops):
    by = {}
    for o in ops:
        if o["date"]:
            by[o["date"]] = by.get(o["date"], 0) + o["income"]
    return by


def weekday_income(ops):
    """Средний приход по дням недели. Возвращает [{wd, avg}] Пн..Вс."""
    by = _income_by_day(ops)
    sums = [0.0] * 7
    cnt = [0] * 7
    for d, v in by.items():
        sums[d.weekday()] += v
        cnt[d.weekday()] += 1
    return [{"wd": WEEKDAYS[i], "avg": (sums[i] / cnt[i] if cnt[i] else 0)}
            for i in range(7)]


def best_worst_day(ops):
    by = _income_by_day(ops)
    if not by:
        return None, None
    items = sorted(by.items(), key=lambda x: x[1])
    best = {"date": items[-1][0], "income": items[-1][1]}
    worst = {"date": items[0][0], "income": items[0][1]}
    return best, worst


def abc_expenses(ops, threshold=0.8):
    """Статьи расходов, дающие ~80% суммы. Возвращает ([{article,sum,share}], total)."""
    art = {}
    for o in ops:
        if o["expense"]:
            art[o["article"] or "Прочее"] = art.get(o["article"] or "Прочее", 0) + o["expense"]
    total = sum(art.values())
    if not total:
        return [], 0
    ranked = sorted(art.items(), key=lambda x: -x[1])
    acc, top = 0, []
    for a, v in ranked:
        acc += v
        top.append({"article": a, "sum": v, "share": v / total * 100})
        if acc / total >= threshold:
            break
    return top, total


def breakeven(ops):
    """Средний расход в день = сколько надо зарабатывать, чтобы не в минус."""
    by = {}
    for o in ops:
        if o["date"]:
            by[o["date"]] = by.get(o["date"], 0) + o["expense"]
    return sum(by.values()) / len(by) if by else 0


def narrative(ops, prev_ops=None, debt_now=None, debt_prev=None):
    """Список фраз-выводов по-человечески (только операционные суммы)."""
    ops = operational(ops)
    if prev_ops:
        prev_ops = operational(prev_ops)
    ci = sum(o["income"] for o in ops)
    ce = sum(o["expense"] for o in ops)
    net = ci - ce
    lines = []

    if prev_ops:
        pi = sum(o["income"] for o in prev_ops)
        if pi:
            d = (ci - pi) / pi * 100
            word = "выросла" if d >= 0 else "упала"
            lines.append(f"Выручка {word} на {abs(round(d))}% к прошлому периоду — {fmt(ci)} сом.")
        else:
            lines.append(f"Выручка за период — {fmt(ci)} сом.")
    else:
        lines.append(f"Выручка за период — {fmt(ci)} сом.")

    top, total = abc_expenses(ops)
    if top:
        t = top[0]
        lines.append(f"Главный расход — «{t['article']}»: {round(t['share'])}% всех затрат ({fmt(t['sum'])} сом.).")

    if net >= 0:
        lines.append(f"Чистый поток положительный: +{fmt(net)} сом.")
    else:
        lines.append(f"⚠️ Чистый поток отрицательный: {fmt(net)} сом. — расходы выше прихода.")

    be = breakeven(ops)
    if be:
        lines.append(f"Точка безубыточности — около {fmt(be)} сом. прихода в день.")

    best, worst = best_worst_day(ops)
    if best and worst and best["date"] != worst["date"]:
        lines.append(f"Лучший день — {best['date'].strftime('%d.%m')} ({fmt(best['income'])} сом.), "
                     f"слабее всего — {worst['date'].strftime('%d.%m')}.")

    if debt_now is not None and debt_prev:
        dd = debt_now - debt_prev
        if abs(dd) > 1:
            word = "вырос" if dd > 0 else "снизился"
            lines.append(f"Долг клиентов {word} на {fmt(abs(dd))} сом.")

    return lines


def analytics(ops):
    """Всё вместе — для передачи в шаблон сайта (только операционные суммы)."""
    ops = operational(ops)
    best, worst = best_worst_day(ops)
    top, total = abc_expenses(ops)
    return {
        "weekday": weekday_income(ops),
        "best": best, "worst": worst,
        "abc": top, "abc_total": total,
        "breakeven": breakeven(ops),
    }
