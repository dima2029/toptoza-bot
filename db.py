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
                        String as Str, UniqueConstraint)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


def _db_url():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return "sqlite:///toptoza.db"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


engine = create_engine(_db_url(), pool_pre_ping=True, future=True)
Session = sessionmaker(bind=engine, future=True)


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


def init_db():
    Base.metadata.create_all(engine)


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


def _uid(point, op):
    raw = f"{point}|{op['date']}|{op['section']}|{op['article']}|{op['desc']}|{op['income']}|{op['expense']}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def sync_point(point_key, ops):
    """Добавляет новые операции точки в базу (дубликаты пропускает)."""
    added = 0
    with Session() as s:
        existing = {u for (u,) in s.query(Transaction.uid)
                    .filter(Transaction.point == point_key).all()}
        for op in ops:
            if op["date"] is None:
                continue
            u = _uid(point_key, op)
            if u in existing:
                continue
            s.add(Transaction(
                uid=u, point=point_key, date=op["date"],
                section=op["section"], article=op["article"],
                descr=op["desc"], income=op["income"], expense=op["expense"]))
            existing.add(u)
            added += 1
        s.commit()
    return added


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
