import os
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./finance.db")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)  # NULL = общая
    name: Mapped[str] = mapped_column(String(60))
    type: Mapped[str] = mapped_column(String(10), default="expense")
    icon: Mapped[str] = mapped_column(String(8), default="🏷️")
    color: Mapped[str] = mapped_column(String(7), default="#6B7C8A")


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    type: Mapped[str] = mapped_column(String(10))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    occurred_on: Mapped["Date"] = mapped_column(Date, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)


DEFAULT_CATEGORIES = [
    ("Еда", "expense", "🍽️", "#E07A3F"), ("Транспорт", "expense", "🚌", "#3F7CE0"), ("Учеба", "expense", "🎓", "#7B5CD6"),
    ("Коммунальные услуги", "expense", "💡", "#D4A72C"), ("Жильё", "expense", "🏠", "#2F9E8F"),
    ("Связь и интернет", "expense", "📱", "#4FB3D9"), ("Здоровье", "expense", "💊", "#D95B6E"),
    ("Одежда", "expense", "👕", "#A65FB5"), ("Развлечения", "expense", "🎬", "#E0529C"), ("Прочее", "expense", "📦", "#7C8B97"),
    ("Зарплата", "income", "💼", "#2E9E5B"), ("Стипендия", "income", "🎓", "#5BAA3A"),
    ("Подработка", "income", "🛠️", "#8AAE2E"), ("Прочие доходы", "income", "➕", "#7C8B97"),
]


def init_db() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if not db.scalar(select(Category.id).where(Category.user_id.is_(None)).limit(1)):
            db.add_all(Category(user_id=None, name=n, type=t, icon=i, color=c) for n, t, i, c in DEFAULT_CATEGORIES)
            db.commit()
