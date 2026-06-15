import enum
from datetime import datetime, timedelta
from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, Float,
    ForeignKey, Integer, String, Text, func, select,
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, selectinload
from .config import DATABASE_URL

engine = create_async_engine(DATABASE_URL, echo=False)

async def get_session() -> AsyncSession:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session

class Base(DeclarativeBase):
    pass

class OrderStatus(str, enum.Enum):
    draft = "draft"
    pending = "pending"
    paid = "paid"
    confirmed = "confirmed"
    exported = "exported"
    cancelled = "cancelled"

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(128))
    full_name: Mapped[str | None] = mapped_column(String(256))
    phone: Mapped[str | None] = mapped_column(String(32))
    address: Mapped[str | None] = mapped_column(Text)
    consented: Mapped[bool] = mapped_column(Boolean, default=False)
    consented_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    orders: Mapped[list["Order"]] = relationship(back_populates="user")

class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(512))
    price: Mapped[float] = mapped_column(Float)
    photo_file_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_file_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    article: Mapped[str | None] = mapped_column(String(32), nullable=True)
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    items: Mapped[list["OrderItem"]] = relationship(back_populates="product")

class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.draft)
    delivery_address: Mapped[str | None] = mapped_column(Text)
    contact_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    receipt_file_id: Mapped[str | None] = mapped_column(String(512))
    total_amount: Mapped[float] = mapped_column(Float, default=0.0)
    delivery_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reminder_sent_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    user: Mapped["User"] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")

class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id"))
    product_id: Mapped[int] = mapped_column(Integer, ForeignKey("products.id"))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    price_at_order: Mapped[float] = mapped_column(Float)
    order: Mapped["Order"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship(back_populates="items")

class BotSetting(Base):
    __tablename__ = "bot_settings"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=True)

class PendingOrder(Base):
    __tablename__ = "pending_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    product_id: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_or_create_user(session: AsyncSession, user_id: int, **kwargs) -> User:
    result = await session.get(User, user_id)
    if result is None:
        result = User(id=user_id, **kwargs)
        session.add(result)
        await session.commit()
    return result

async def get_bot_setting(session: AsyncSession, key: str) -> str | None:
    result = await session.get(BotSetting, key)
    return result.value if result else None

async def set_bot_setting(session: AsyncSession, key: str, value: str):
    setting = await session.get(BotSetting, key)
    if not setting:
        setting = BotSetting(key=key, value=value)
        session.add(setting)
    else:
        setting.value = value
    await session.commit()

async def get_draft_order(session: AsyncSession, user_id: int) -> Order | None:
    stmt = (
        select(Order)
        .where(Order.user_id == user_id, Order.status == OrderStatus.draft)
        .options(selectinload(Order.items).selectinload(OrderItem.product))
        .order_by(Order.created_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()

async def get_or_create_draft(session: AsyncSession, user_id: int) -> Order:
    order = await get_draft_order(session, user_id)
    if order is None:
        order = Order(user_id=user_id, status=OrderStatus.draft)
        session.add(order)
        await session.commit()
        await session.refresh(order)
    return order

async def upsert_product(
    session: AsyncSession,
    post_id: str,
    name: str,
    price: float,
    photo_file_ids: str | None = None,
    video_file_ids: str | None = None,
    article: str | None = None,
    category: str | None = None,
    description: str | None = None,
    in_stock: bool = True,
    stock: int | None = None,
) -> Product:
    stmt = select(Product).where(Product.post_id == post_id)
    result = await session.execute(stmt)
    product = result.scalar_one_or_none()

    if stock is None:
        stock = 0
    is_active = (stock > 0) and in_stock

    if product is None:
        product = Product(
            post_id=post_id,
            name=name,
            price=price,
            photo_file_ids=photo_file_ids,
            video_file_ids=video_file_ids,
            article=article,
            category=category,
            description=description,
            in_stock=in_stock,
            stock=stock,
            is_active=is_active,
        )
        session.add(product)
    else:
        product.stock = stock
        product.in_stock = in_stock
        product.is_active = is_active
        product.name = name
        product.price = price
        product.photo_file_ids = photo_file_ids
        product.video_file_ids = video_file_ids
        product.article = article
        product.category = category
        product.description = description

    await session.commit()
    await session.refresh(product)
    return product

async def add_item_to_order(
    session: AsyncSession, order: Order, product: Product, qty: int
) -> OrderItem:
    for item in order.items:
        if item.product_id == product.id:
            item.quantity += qty
            order.total_amount = sum(i.quantity * i.price_at_order for i in order.items)
            await session.commit()
            return item
    item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        quantity=qty,
        price_at_order=product.price,
    )
    session.add(item)
    order.total_amount += product.price * qty
    await session.commit()
    await session.refresh(order)
    return item

async def remove_item_from_order(
    session: AsyncSession, order: Order, item_id: int
) -> bool:
    for item in order.items:
        if item.id == item_id:
            order.items.remove(item)
            await session.delete(item)
            order.total_amount = sum(i.quantity * i.price_at_order for i in order.items)
            await session.commit()
            return True
    return False

async def recalculate_total(session: AsyncSession, order: Order) -> None:
    total = sum(i.quantity * i.price_at_order for i in order.items)
    order.total_amount = total
    await session.commit()

async def get_order_with_items(session: AsyncSession, order_id: int) -> Order | None:
    stmt = (
        select(Order)
        .where(Order.id == order_id)
        .options(selectinload(Order.items).selectinload(OrderItem.product), selectinload(Order.user))
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()

async def get_all_paid_orders_not_exported(session: AsyncSession) -> list[Order]:
    stmt = (
        select(Order)
        .where(Order.status == OrderStatus.confirmed)
        .options(selectinload(Order.items).selectinload(OrderItem.product), selectinload(Order.user))
    )
    result = await session.execute(stmt)
    return result.scalars().all()

async def get_unpaid_orders_for_reminder(session: AsyncSession) -> list[Order]:
    now = datetime.utcnow()
    stmt = (
        select(Order)
        .where(
            Order.status == OrderStatus.pending,
            Order.reminder_sent_count < 2,
            Order.updated_at <= now - timedelta(days=1),
        )
        .options(selectinload(Order.user))
    )
    result = await session.execute(stmt)
    return result.scalars().all()

async def get_all_users(session: AsyncSession) -> list[User]:
    result = await session.execute(select(User))
    return result.scalars().all()