# -*- coding: utf-8 -*-
"""Хранилище истории операций ТОП-ТОЗА.

Локально — SQLite (файл toptoza.db), на Railway — PostgreSQL (DATABASE_URL).
Операции из журнала Google Таблиц складываются сюда, чтобы история
сохранялась даже после смены месяца в таблице.
"""
import os
import hashlib
import datetime as dt

from sqlalchemy import (create_engine, Column, Integer, String, Float, Date,
                        String as Str)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


def _db_url():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return "sqlite:///toptoza.db"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


engine = create_engine(_db_url(), pool_pre_ping=True)
Session = sessionmaker(engine)


class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True)
    uid = Column(Str(64), unique=True, index=True)
    point = Column(String(32), index=True)
    date = Column(Date, index=True)
    section = Column(String(64))     # Приход / Расход
    article = Column(String(128))    # статья (ЗП, ГСМ, Выручка…)
    descr = Column(String(512))
    income = Column(Float, default=0.0)
    expense = Column(Float, default=0.0)


class Setting(Base):
    __tablename__ = "settings"
    key = Column(Str(64), primary_key=True)
    value = Column(String(256))


class Employee(Base):
    __tablename__ = "employees"
    id = Column(Integer, primary_key=True)
    point = Column(String(32), index=True)      # km9 / gulbuta
    name = Column(String(128))
    role = Column(String(32))                   # moyshik/voditel/operator/povar/admin
    tseh = Column(String(8), default="")        # «1» / «2» / ""
    driver_code = Column(String(16), default="")  # МО/АМ/ОУ/НБ (для водителей)
    salary = Column(Float, default=0.0)         # оклад/мес (для фиксов)
    avans = Column(Float, default=0.0)          # ручной аванс (доп. к журналу)
    days_off = Column(String(4000), default="")  # выходные: ISO-даты через запятую
    active = Column(Integer, default=1)


def _ensure_column(table, col, coltype, default="0"):
    """Лёгкая миграция: добавить колонку, если её ещё нет (Postgres/SQLite)."""
    from sqlalchemy import inspect, text
    try:
        cols = [c["name"] for c in inspect(engine).get_columns(table)]
        if col not in cols:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype} DEFAULT {default}"))
    except Exception:
        pass


def init_db():
    Base.metadata.create_all(engine)
    _ensure_column("employees", "avans", "FLOAT", "0")
    _ensure_column("employees", "days_off", "TEXT", "''")


# ───────────────────────── сотрудники (CRUD) ─────────────────────────
def _emp_dict(e):
    return {"id": e.id, "point": e.point, "name": e.name, "role": e.role,
            "tseh": e.tseh or "", "driver_code": e.driver_code or "",
            "salary": e.salary or 0.0, "avans": e.avans or 0.0,
            "off": [d for d in (e.days_off or "").split(",") if d.strip() and "-" in d],
            "active": bool(e.active)}


def get_employee(emp_id):
    with Session() as s:
        e = s.get(Employee, emp_id)
        return _emp_dict(e) if e else None


def list_employees(point_keys=None, only_active=False):
    with Session() as s:
        q = s.query(Employee)
        if point_keys:
            q = q.filter(Employee.point.in_(point_keys))
        if only_active:
            q = q.filter(Employee.active == 1)
        q = q.order_by(Employee.point, Employee.role, Employee.name)
        return [_emp_dict(e) for e in q.all()]


def add_employee(point, name, role, tseh="", driver_code="", salary=0.0, avans=0.0, active=1):
    with Session() as s:
        e = Employee(point=point, name=name.strip(), role=role, tseh=tseh,
                     driver_code=driver_code, salary=float(salary or 0),
                     avans=float(avans or 0), active=int(active))
        s.add(e)
        s.commit()
        return e.id


def update_employee(emp_id, **fields):
    with Session() as s:
        e = s.get(Employee, emp_id)
        if not e:
            return False
        for k, v in fields.items():
            if k in ("salary", "avans"):
                v = float(v or 0)
            elif k == "active":
                v = int(v)
            elif k == "name":
                v = (v or "").strip()
            setattr(e, k, v)
        s.commit()
        return True


def delete_employee(emp_id):
    with Session() as s:
        e = s.get(Employee, emp_id)
        if e:
            s.delete(e)
            s.commit()
        return bool(e)


def get_setting(key, default=None):
    try:
        with Session() as s:
            row = s.get(Setting, key)
            return row.value if row else default
    except Exception:
        return default


def set_setting(key, value):
    with Session() as s:
        row = s.get(Setting, key)
        if row:
            row.value = str(value)
        else:
            s.add(Setting(key=key, value=str(value)))
        s.commit()


def _uid(point, i, op):
    raw = f"{point}|{i}|{op['date']}|{op['section']}|{op['article']}|{op['desc']}|{op['income']}|{op['expense']}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def sync_point(point_key, ops):
    """Зеркалит журнал точки в базу: заменяет строки за период, который сейчас
    есть в таблице (delete+insert), чтобы правки/удаления в таблице не раздували
    суммы. Старые месяцы вне этого периода остаются в базе как архив."""
    rows = [o for o in ops if o.get("date") is not None]
    if not rows:
        return 0
    lo = min(o["date"] for o in rows)
    hi = max(o["date"] for o in rows)
    with Session() as s:
        s.query(Transaction).filter(
            Transaction.point == point_key,
            Transaction.date >= lo,
            Transaction.date <= hi).delete(synchronize_session=False)
        for i, o in enumerate(rows):
            s.add(Transaction(
                uid=_uid(point_key, i, o), point=point_key, date=o["date"],
                section=o["section"], article=o["article"],
                descr=o["desc"], income=o["income"], expense=o["expense"]))
        s.commit()
    return len(rows)


def query_ops(point_keys, start=None, end=None):
    with Session() as s:
        q = s.query(Transaction).filter(Transaction.point.in_(point_keys))
        if start:
            q = q.filter(Transaction.date >= start)
        if end:
            q = q.filter(Transaction.date <= end)
        q = q.order_by(Transaction.date.desc(), Transaction.id.desc())
        return [{
            "point": t.point, "date": t.date, "section": t.section,
            "article": t.article, "desc": t.descr,
            "income": t.income, "expense": t.expense,
        } for t in q.all()]


def date_bounds(point_keys):
    """Мин/макс дата в базе — чтобы знать, за какой период есть данные."""
    with Session() as s:
        from sqlalchemy import func
        row = s.query(func.min(Transaction.date), func.max(Transaction.date))\
               .filter(Transaction.point.in_(point_keys)).one()
        return row[0], row[1]
