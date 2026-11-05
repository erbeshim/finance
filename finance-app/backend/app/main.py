import hashlib
import hmac
import os
import secrets
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal, Optional

import jwt
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from .models import ApiKey, Category, SessionLocal, Transaction, User, init_db
from .sms import parse_sms

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")  # в проде — из Secret Manager
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Финансовый помощник", lifespan=lifespan)
bearer = HTTPBearer(auto_error=False)


# ---------- утилиты ----------
def get_db():
    with SessionLocal() as db:
        yield db


def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${h}"


def verify_password(pw: str, stored: str) -> bool:
    salt, h = stored.split("$")
    check = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return hmac.compare_digest(h, check)


def make_token(user_id: int) -> str:
    exp = datetime.now(timezone.utc) + timedelta(days=7)
    return jwt.encode({"sub": str(user_id), "exp": exp}, SECRET_KEY, algorithm="HS256")


def current_user(cred: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)) -> User:
    try:
        uid = int(jwt.decode(cred.credentials, SECRET_KEY, algorithms=["HS256"])["sub"])
    except Exception:
        raise HTTPException(401, "Нужно войти в аккаунт")
    user = db.get(User, uid)
    if not user:
        raise HTTPException(401, "Нужно войти в аккаунт")
    return user


def visible_category(db: Session, user: User, cat_id: int) -> Category:
    cat = db.scalar(select(Category).where(Category.id == cat_id, or_(Category.user_id.is_(None), Category.user_id == user.id)))
    if not cat:
        raise HTTPException(404, "Категория не найдена")
    return cat


def month_range(month: Optional[str]) -> tuple[date, date]:
    try:
        y, m = map(int, (month or date.today().strftime("%Y-%m")).split("-"))
        start = date(y, m, 1)
    except ValueError:
        raise HTTPException(422, "Месяц в формате ГГГГ-ММ")
    return start, date(y + (m == 12), m % 12 + 1, 1)


def tx_out(t: Transaction, c: Category) -> dict:
    return {"id": t.id, "amount": float(t.amount), "type": t.type, "description": t.description,
            "occurred_on": t.occurred_on.isoformat(),
            "category": {"id": c.id, "name": c.name, "icon": c.icon, "color": c.color}}


# ---------- схемы ----------
class Credentials(BaseModel):
    email: str = Field(min_length=5, max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=8, max_length=128)


class TxIn(BaseModel):
    category_id: int
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    occurred_on: date
    description: Optional[str] = Field(default=None, max_length=255)


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    type: Literal["income", "expense"] = "expense"
    icon: str = Field(default="🏷️", max_length=8)
    color: str = Field(default="#6B7C8A", pattern=r"^#[0-9a-fA-F]{6}$")


# ---------- auth ----------
@app.post("/api/auth/register")
def register(data: Credentials, db: Session = Depends(get_db)):
    email = data.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "Этот email уже зарегистрирован")
    user = User(email=email, password_hash=hash_password(data.password))
    db.add(user)
    db.commit()
    return {"token": make_token(user.id)}


@app.post("/api/auth/login")
def login(data: Credentials, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == data.email.lower()))
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "Неверный email или пароль")
    return {"token": make_token(user.id)}


# ---------- категории ----------
@app.get("/api/categories")
def list_categories(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Category).where(or_(Category.user_id.is_(None), Category.user_id == user.id)).order_by(Category.id))
    return [{"id": c.id, "name": c.name, "type": c.type, "icon": c.icon, "color": c.color} for c in rows]


@app.post("/api/categories", status_code=201)
def create_category(data: CategoryIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = Category(user_id=user.id, **data.model_dump())
    db.add(c)
    db.commit()
    return {"id": c.id, "name": c.name, "type": c.type, "icon": c.icon, "color": c.color}


# ---------- транзакции ----------
@app.get("/api/transactions")
def list_transactions(month: Optional[str] = None, category_id: Optional[int] = None,
                      type: Optional[Literal["income", "expense"]] = None,
                      user: User = Depends(current_user), db: Session = Depends(get_db)):
    start, end = month_range(month)
    q = (select(Transaction, Category).join(Category, Category.id == Transaction.category_id)
         .where(Transaction.user_id == user.id, Transaction.occurred_on >= start, Transaction.occurred_on < end)
         .order_by(Transaction.occurred_on.desc(), Transaction.id.desc()))
    if category_id:
        q = q.where(Transaction.category_id == category_id)
    if type:
        q = q.where(Transaction.type == type)
    return [tx_out(t, c) for t, c in db.execute(q)]


@app.post("/api/transactions", status_code=201)
def create_transaction(data: TxIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    cat = visible_category(db, user, data.category_id)
    t = Transaction(user_id=user.id, category_id=cat.id, type=cat.type, amount=data.amount,
                    description=data.description, occurred_on=data.occurred_on)
    db.add(t)
    db.commit()
    return tx_out(t, cat)


@app.delete("/api/transactions/{tx_id}", status_code=204)
def delete_transaction(tx_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = db.scalar(select(Transaction).where(Transaction.id == tx_id, Transaction.user_id == user.id))
    if not t:
        raise HTTPException(404, "Операция не найдена")
    db.delete(t)
    db.commit()


class BulkIn(BaseModel):
    items: list[TxIn] = Field(max_length=5000)


@app.post("/api/transactions/bulk")
def bulk_import(data: BulkIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Импорт выписки. Повторный импорт того же файла не создаёт дублей."""
    cats = {c.id: c for c in db.scalars(select(Category).where(or_(Category.user_id.is_(None), Category.user_id == user.id)))}
    if any(i.category_id not in cats for i in data.items):
        raise HTTPException(404, "Категория не найдена")
    key = lambda d, a, s: (d, Decimal(a).quantize(Decimal("0.01")), s or "")
    seen: Counter = Counter()
    if data.items:
        lo, hi = min(i.occurred_on for i in data.items), max(i.occurred_on for i in data.items)
        for t in db.scalars(select(Transaction).where(Transaction.user_id == user.id, Transaction.occurred_on.between(lo, hi))):
            seen[key(t.occurred_on, t.amount, t.description)] += 1
    imported = skipped = 0
    for i in data.items:
        k = key(i.occurred_on, i.amount, i.description)
        if seen[k] > 0:
            seen[k] -= 1
            skipped += 1
            continue
        db.add(Transaction(user_id=user.id, category_id=i.category_id, type=cats[i.category_id].type,
                           amount=i.amount, description=i.description, occurred_on=i.occurred_on))
        imported += 1
    db.commit()
    return {"imported": imported, "skipped": skipped}


# ---------- автоматический приём уведомлений с телефона ----------
class IngestIn(BaseModel):
    text: str = Field(min_length=3, max_length=2000)
    local_date: Optional[date] = None  # дата на телефоне, чтобы не путать часовые пояса


def ingest_user(cred: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)) -> User:
    key = db.scalar(select(ApiKey).where(ApiKey.key_hash == hashlib.sha256((cred.credentials if cred else "").encode()).hexdigest()))
    if not key:
        raise HTTPException(401, "Неверный ключ")
    return db.get(User, key.user_id)


@app.post("/api/ingest/key")
def create_ingest_key(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Выпускает новый ключ для телефона; старый перестаёт работать."""
    key = "fa_" + secrets.token_urlsafe(32)
    db.execute(delete(ApiKey).where(ApiKey.user_id == user.id))
    db.add(ApiKey(user_id=user.id, key_hash=hashlib.sha256(key.encode()).hexdigest()))
    db.commit()
    return {"key": key}


@app.post("/api/ingest/notification")
def ingest_notification(data: IngestIn, user: User = Depends(ingest_user), db: Session = Depends(get_db)):
    status, p = parse_sms(data.text, data.local_date or date.today())
    if status != "ok":
        return {"status": "foreign_currency" if status == "foreign" else "ignored"}
    cat = db.scalar(select(Category).where(Category.user_id.is_(None), Category.name == p["category"], Category.type == p["type"]))
    if not cat:
        return {"status": "ignored"}
    amount = Decimal(str(p["amount"])).quantize(Decimal("0.01"))
    since = datetime.utcnow() - timedelta(minutes=10)  # банк часто шлёт и СМС, и push: считаем один раз
    if db.scalar(select(Transaction.id).where(Transaction.user_id == user.id, Transaction.occurred_on == p["occurred_on"],
                                              Transaction.amount == amount, Transaction.type == p["type"],
                                              Transaction.created_at >= since).limit(1)):
        return {"status": "duplicate"}
    db.add(Transaction(user_id=user.id, category_id=cat.id, type=cat.type, amount=amount, description=p["description"],
                       occurred_on=p["occurred_on"], created_at=datetime.utcnow()))
    db.commit()
    return {"status": "added", "type": cat.type, "amount": float(amount), "category": cat.name}


# ---------- аналитика ----------
@app.get("/api/analytics/overview")
def overview(month: Optional[str] = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    start, end = month_range(month)
    # 6 месяцев для динамики
    y, m = start.year, start.month - 5
    while m < 1:
        m += 12
        y -= 1
    trend_start = date(y, m, 1)
    rows = db.execute(
        select(Transaction, Category).join(Category, Category.id == Transaction.category_id)
        .where(Transaction.user_id == user.id, Transaction.occurred_on >= trend_start, Transaction.occurred_on < end)
    ).all()

    by_cat: dict[int, dict] = {}
    totals = {"income": Decimal(0), "expense": Decimal(0)}
    trend: dict[str, dict] = defaultdict(lambda: {"income": 0.0, "expense": 0.0})
    for t, c in rows:
        trend[t.occurred_on.strftime("%Y-%m")][t.type] += float(t.amount)
        if t.occurred_on >= start:
            totals[t.type] += t.amount
            if t.type == "expense":
                d = by_cat.setdefault(c.id, {"id": c.id, "name": c.name, "icon": c.icon, "color": c.color, "total": Decimal(0), "count": 0})
                d["total"] += t.amount
                d["count"] += 1

    exp = totals["expense"]
    cats = sorted(by_cat.values(), key=lambda d: d["total"], reverse=True)
    for d in cats:
        d["percent"] = round(float(d["total"] / exp * 100), 1) if exp else 0
        d["total"] = float(d["total"])
    months = []
    cur = trend_start
    while cur < end:
        key = cur.strftime("%Y-%m")
        months.append({"month": key, **trend[key]})
        cur = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
    return {"month": start.strftime("%Y-%m"), "income": float(totals["income"]), "expenses": float(exp),
            "balance": float(totals["income"] - exp), "by_category": cats, "trend": months}


# ---------- фронтенд ----------
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(FRONTEND / "index.html")

app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
