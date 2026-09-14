"""
gsheets.py — интеграция с Google Sheets (см. docs/gsheets_integration_design.md).

Архитектура:
* Заказы НЕ отправляются синхронно в хендлерах. При переходе заказа в
  ``confirmed`` позиции попадают в таблицу-очередь ``gsheets_outbox``
  (:func:`enqueue_order`), а job-джоба (:func:`gsheets_sync_job`) разбирает
  очередь раз в ``settings.gsheets_sync_interval_minutes`` минут.
  Это защищает оформление заказа от сбоев/задержек Google API и гарантирует
  доставку после рестартов (outbox живёт в БД).
* Защита от дублей: unique constraint по ``order_item_id`` (п.7.3 проекта) —
  одна позиция заказа не может попасть в очередь дважды; ``sent_at`` отмечает
  уже отправленные строки.
* Структура листа «Опт» соответствует шаблону
  ``docs/shablon_uchet_zakazov.xlsx``: блоки месяцев
  (``=== МЕСЯЦ ===`` … ``ИТОГО <МЕСЯЦ>:``), строка ``ИТОГО ЗАКАЗ <id>:``
  под каждым заказом, внизу ``ВСЕГО ЗА ВСЁ ВРЕМЯ:``. Бот вставляет строки
  ВНУТРЬ нужного месяца и ПЕРЕПИСЫВАЕТ формулы итогов — так суммы всегда
  корректны, даже если продавец правил лист вручную.
* gspread синхронный — все вызовы оборачиваются в ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from telegram.ext import ContextTypes

from .config import settings
from .db import (
    AsyncSession, GSheetsOutbox, Order, OrderItem,
    Product, User, get_session,
)

logger = logging.getLogger(__name__)

# --- Компоновка листа «Опт» (шаблон shablon_uchet_zakazov.xlsx) ---
NC = 17  # колонок: A..Q (M=Форма оплаты, N=Этап сборки, O=Комментарий — вручную)
GRAND_LABEL = "ВСЕГО ЗА ВСЁ ВРЕМЯ:"

MONTHS_RU = [
    "ЯНВАРЬ", "ФЕВРАЛЬ", "МАРТ", "АПРЕЛЬ", "МАЙ", "ИЮНЬ",
    "ИЮЛЬ", "АВГУСТ", "СЕНТЯБРЬ", "ОКТЯБРЬ", "НОЯБРЬ", "ДЕКАБРЬ",
]

MAX_ATTEMPTS_NOTIFY = 5  # после стольких ошибок — уведомить админа


def month_label(d: date | datetime) -> str:
    """'СЕНТЯБРЬ 2026' — формат заголовка месяца, как в шаблоне."""
    return f"{MONTHS_RU[d.month - 1]} {d.year}"


# ---------------------------------------------------------------------------
# Очередь (outbox)
# ---------------------------------------------------------------------------

async def enqueue_order(session: AsyncSession, order_id: int) -> int:
    """Добавляет все позиции заказа в outbox. Идемпотентно (unique по item id).

    Вызывается из хендлеров при переходе заказа в ``confirmed``.
    Возвращает число добавленных строк.

    При неактивной интеграции (``settings.gsheets_active`` == False) ничего не
    пишет и возвращает 0: заказ при этом оформляется штатно. Проверка стоит
    здесь, а не только в хендлере, чтобы ни один вызывающий код не мог
    наполнить таблицу-очередь, которую некому разбирать.
    """
    if not settings.gsheets_active:
        logger.info(
            "gsheets disabled — заказ %s не добавлен в outbox",
            order_id,
            extra={"event": "gsheets_enqueue_skipped", "order_id": order_id},
        )
        return 0

    items = (await session.execute(
        select(OrderItem.id).where(OrderItem.order_id == order_id)
    )).scalars().all()
    existing = set((await session.execute(
        select(GSheetsOutbox.order_item_id)
        .where(GSheetsOutbox.order_id == order_id)
    )).scalars().all())
    added = 0
    for item_id in items:
        if item_id in existing:
            continue  # уже в очереди — не дублируем
        session.add(GSheetsOutbox(order_id=order_id, order_item_id=item_id))
        added += 1
    if added:
        try:
            await session.commit()
        except IntegrityError:
            # гонка с параллельным вызовом — unique constraint защитил,
            # позиция уже в очереди
            await session.rollback()
            added = 0
    logger.info("gsheets enqueued", extra={"event": "gsheets_enqueue",
                                            "order_id": order_id, "items": added})
    return added


# ---------------------------------------------------------------------------
# Клиент Google Sheets
# ---------------------------------------------------------------------------

class GoogleSheetsClient:
    """Тонкая обёртка над gspread для структуры из шаблона."""

    def __init__(self) -> None:
        self._gc = None
        self._sheet = None

    # -- подключение ---------------------------------------------------
    def _open(self):
        if self._sheet is not None:
            return self._sheet
        import gspread
        self._gc = gspread.service_account(filename=settings.google_credentials_path)
        self._sheet = self._gc.open_by_key(settings.google_sheet_id)
        return self._sheet

    def _ws(self, title: str):
        sh = self._open()
        try:
            return sh.worksheet(title)
        except Exception:
            ws = sh.add_worksheet(title, rows=200, cols=NC)
            return ws

    # -- структура листа заказов ---------------------------------------
    def _find_rows(self, ws) -> dict:
        """Возвращает позиции якорных строк по колонке A (1-based)."""
        col_a = ws.col_values(1)  # список значений колонки A
        anchors = {"months": {}, "grand": None, "last": len(col_a)}
        for i, v in enumerate(col_a, start=1):
            s = str(v).strip()
            if s.startswith("===") and s.endswith("==="):
                anchors["months"][s.strip("= ").strip()] = i  # заголовок месяца
            elif s.startswith("ИТОГО ") and s.endswith(":") and "ЗАКАЗ" not in s:
                anchors.setdefault("mtotals", {})[s[len("ИТОГО "):-1].strip()] = i
            elif s == GRAND_LABEL:
                anchors["grand"] = i
        anchors.setdefault("mtotals", {})
        return anchors

    def _month_total_formulas(self, first: int, last: int, row: int) -> list:
        """Формулы строки «ИТОГО <месяц>:» (диапазон first..last)."""
        return [
            None, None, None, None, None,
            f'=COUNTIF(F{first}:F{last},"ИТОГО ЗАКАЗ*")',
            f"=SUMPRODUCT(ISNUMBER(G{first}:G{last})*G{first}:G{last})",
            None,
            f"=SUMPRODUCT(ISNUMBER(H{first}:H{last})*I{first}:I{last})",
            None,
            f"=SUMPRODUCT(ISNUMBER(H{first}:H{last})*K{first}:K{last})",
            f"=SUMPRODUCT(ISNUMBER(H{first}:H{last})*L{first}:L{last})",
            None, None, None, None, None,
        ]

    def _order_row(self, it: dict, r: int) -> list:
        return [
            it["date"], it["order_id"], it["tg_id"], it["client"],
            it["article"], it["product"], it["qty"],
            it["purchase"], f"=G{r}*H{r}",
            it["sale"], f"=G{r}*J{r}", f"=K{r}-I{r}",
            None, None, None,  # Форма оплаты / Этап сборки / Комментарий — вручную
            it["status"], it["item_id"],
        ]

    def _rewrite_totals(self, ws, anchors: dict) -> None:
        """Пересчитывает формулы всех строк «ИТОГО <месяц>» и «ВСЕГО…».

        Вызывается после каждой вставки — гарантирует корректность сумм.
        """
        mtotals = anchors["mtotals"]
        ordered = sorted(mtotals.values())
        for label, mrow in mtotals.items():
            header = anchors["months"].get(label)
            if header is None:
                continue
            first = header + 1
            # Диапазон месяца — от заголовка до строки перед «ИТОГО <месяц>:».
            # Строки «ИТОГО ЗАКАЗ…» внутри диапазона отсекаются гардами
            # ISNUMBER(G…)/ISNUMBER(H…) в SUMPRODUCT (у них пустые G и H),
            # поэтому двойного счёта нет. Строки предыдущего месяца попасть не
            # могут: между блоками всегда стоит заголовок следующего месяца.
            formulas = self._month_total_formulas(first, mrow - 1, mrow)
            for c, f in enumerate(formulas, start=1):
                if f is not None:
                    ws.update_cell(mrow, c, f)
        # ВСЕГО
        if anchors["grand"]:
            g = anchors["grand"]
            for col, letter in ((9, "I"), (11, "K"), (12, "L")):
                parts = "+".join(f"{letter}{r}" for r in ordered)
                ws.update_cell(g, col, f"={parts}" if parts else "=0")

    # -- публичные операции (синхронные, вызываются через to_thread) ----
    def append_order_sync(self, orders_data: list[dict]) -> None:
        """Вставляет блоки заказов в лист «Опт» с учётом структуры месяцев.

        orders_data: список словарей {order_id, date, tg_id, client, status,
        items: [{article, product, qty, purchase, sale, item_id}, ...]}
        """
        ws = self._ws(settings.gsheets_tab_orders)
        for od in orders_data:
            anchors = self._find_rows(ws)
            label = od["month"]
            rows = []
            r0 = None
            if label in anchors["mtotals"]:
                # месяц существует — вставляем позиции перед «ИТОГО <месяц>:»
                mt = anchors["mtotals"][label]
                n = len(od["items"]) + 1  # позиции + «ИТОГО ЗАКАЗ»
                ws.insert_rows(mt, amount=n)
                r0 = mt
            else:
                # новый месяц — создаём блок перед «ВСЕГО…» (или в конце)
                ins = anchors["grand"] if anchors["grand"] else anchors["last"] + 2
                n = len(od["items"]) + 1 + 3  # +заголовок месяца, +итог месяца, +пустая
                ws.insert_rows(ins, amount=n)
                ws.update_cell(ins, 1, f"=== {label} ===")
                r0 = ins + 1
            # позиции заказа
            rr = r0
            for it in od["items"]:
                vals = self._order_row({**it, "date": od["date"], "order_id": od["order_id"],
                                        "tg_id": od["tg_id"], "client": od["client"],
                                        "status": od["status"]}, rr)
                for c, v in enumerate(vals, start=1):
                    if v is not None:
                        ws.update_cell(rr, c, v)
                rr += 1
            # «ИТОГО ЗАКАЗ <id>:»
            first, last = r0, rr - 1
            ws.update_cell(rr, 6, f"ИТОГО ЗАКАЗ {od['order_id']}:")
            ws.update_cell(rr, 9, f"=SUM(I{first}:I{last})")
            ws.update_cell(rr, 11, f"=SUM(K{first}:K{last})")
            ws.update_cell(rr, 12, f"=SUM(L{first}:L{last})")
            if label not in anchors["mtotals"]:
                # строка итога нового месяца — через 2 строки после итога заказа
                mt_row = rr + 2
                ws.update_cell(mt_row, 1, f"ИТОГО {label}:")
            # перезаписываем все итоги (диапазоны сдвинулись)
            self._rewrite_totals(ws, self._find_rows(ws))

    def sync_nomenclature_sync(self, products: list[dict]) -> dict:
        """Синхронизация «Справочника»: бот пишет артикул/название/категорию/
        наличие и НИКОГДА не трогает «Цену закупа» и «Наценку» (жёлтые колонки
        D и F — заполняются вручную, требование п.2). Возвращает закуп-цены
        {article: price} для записи в БД.
        """
        ws = self._ws(settings.gsheets_tab_ref)
        values = ws.get_all_values()
        header = values[0] if values else []
        rows = values[1:] if len(values) > 1 else []
        by_article = {}
        for i, row in enumerate(rows, start=2):
            art = str(row[0]).strip() if row and row[0] else ""
            if art:
                by_article[art] = (i, row)
        purchases: dict[str, float] = {}
        for art, (i, row) in by_article.items():
            try:
                if len(row) > 3 and str(row[3]).strip():
                    purchases[art] = float(str(row[3]).replace(",", "."))
            except ValueError:
                pass
        next_row = len(rows) + 2
        for p in products:
            art = p["article"]
            if not art:
                continue
            if art in by_article:
                i, _ = by_article[art]
                ws.update_cell(i, 2, p["name"])
                ws.update_cell(i, 3, p["category"] or "")
                ws.update_cell(i, 7, "да" if p["in_stock"] else "нет")
            else:
                ws.update_cell(next_row, 1, art)
                ws.update_cell(next_row, 2, p["name"])
                ws.update_cell(next_row, 3, p["category"] or "")
                ws.update_cell(next_row, 5, f"=IF(F{next_row}=\"\",\"\",ROUND(D{next_row}*F{next_row},0))")
                ws.update_cell(next_row, 7, "да" if p["in_stock"] else "нет")
                by_article[art] = (next_row, [])
                next_row += 1
        return purchases

    def update_report_sync(self) -> None:
        """Лист «Отчёт»: добавляет недостающие строки месяцев и клиентов."""
        try:
            ws_rep = self._ws("Отчёт")
        except Exception:
            return
        ws_opt = self._ws(settings.gsheets_tab_orders)
        anchors = self._find_rows(ws_opt)
        rep_col_a = [str(v).strip() for v in ws_rep.col_values(1)]
        itogo_row = next((i for i, v in enumerate(rep_col_a, 1) if v == "ИТОГО"), None)
        for label, mrow in anchors["mtotals"].items():
            if label in rep_col_a:
                continue
            if itogo_row:
                ws_rep.insert_row([
                    label,
                    f"='{settings.gsheets_tab_orders}'!I{mrow}",
                    f"='{settings.gsheets_tab_orders}'!K{mrow}",
                    f"='{settings.gsheets_tab_orders}'!L{mrow}",
                    f"='{settings.gsheets_tab_orders}'!F{mrow}",
                ], itogo_row)
                itogo_row += 1


# ---------------------------------------------------------------------------
# Job: разбор outbox-очереди + синк номенклатуры
# ---------------------------------------------------------------------------

_client: GoogleSheetsClient | None = None


def _get_client() -> GoogleSheetsClient:
    global _client
    if _client is None:
        _client = GoogleSheetsClient()
    return _client


async def _notify_admin_error(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    try:
        await context.bot.send_message(chat_id=settings.admin_chat_id, text=text)
    except Exception:
        logger.exception("gsheets admin notify failed")


# Когда админу последний раз сообщали о конфигурационной ошибке gsheets
# (по типу ошибки): не чаще раза в _CONFIG_NOTIFY_COOLDOWN_SEC. Без троттлинга
# джоба с интервалом 5 минут присылала бы 288 одинаковых сообщений в сутки.
_CONFIG_NOTIFY_COOLDOWN_SEC = 6 * 3600.0
_last_config_notify: dict[str, float] = {}


def _config_error_hint(exc: Exception) -> str:
    """Человекочитаемая подсказка для частых конфигурационных ошибок."""
    cred = settings.google_credentials_path
    if isinstance(exc, FileNotFoundError):
        return (
            f"Файл ключа сервисного аккаунта не найден: `{cred}`.\n"
            f"Нужно положить credentials.json в каталог, примонтированный в "
            f"/app/secrets (на сервере: tgbot/secrets/), и выдать сервисному "
            f"аккаунту доступ к таблице."
        )
    if isinstance(exc, OSError):
        return f"Нет доступа к файлу ключа `{cred}`: {exc!r}"
    text = repr(exc)
    if "invalid_grant" in text or "Invalid" in text:
        return "Ключ сервисного аккаунта недействителен — перевыпустите credentials.json."
    if "PERMISSION_DENIED" in text or "does not have permission" in text:
        return (
            "Сервисный аккаунт не имеет доступа к таблице: откройте таблицу "
            "и добавьте e-mail сервисного аккаунта как редактора."
        )
    if "SpreadsheetNotFound" in text:
        return f"Таблица не найдена по GOOGLE_SHEET_ID: проверьте ID."
    return "См. подробную ошибку в логах контейнера."


async def _notify_config_error_once(
    context: ContextTypes.DEFAULT_TYPE, exc: Exception, where: str
) -> None:
    """Уведомляет админа о конфигурационной ошибке gsheets не чаще раза в 6 часов.

    Молчаливое падение джобы опаснее редкого уведомления: интеграция может не
    работать сутками, а в логе без LOG_JSON виден только «sync failed».
    """
    import time as _time

    key = f"{type(exc).__name__}:{where}"
    now = _time.monotonic()
    last = _last_config_notify.get(key)
    if last is not None and (now - last) < _CONFIG_NOTIFY_COOLDOWN_SEC:
        return
    _last_config_notify[key] = now
    if not settings.admin_chat_id:
        return
    text = (
        f"⚠️ Google Sheets ({where}): синхронизация не работает\n"
        f"Ошибка: `{repr(exc)[:300]}`\n"
        f"{_config_error_hint(exc)}"
    )
    try:
        await context.bot.send_message(chat_id=settings.admin_chat_id, text=text)
    except Exception:
        logger.exception("gsheets config notify failed")


async def gsheets_sync_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Периодическая джоба: номенклатура → закуп-цены в БД → outbox → лист."""
    if not settings.gsheets_active:
        return
    client = _get_client()

    async for session in get_session():
        # 1) Номенклатура: товары БД → «Справочник»; закуп-цены ← в БД.
        try:
            products = (await session.execute(
                select(Product).where(Product.is_active == True)  # noqa: E712
            )).scalars().all()
            prod_data = [{
                "article": p.article, "name": p.name, "category": p.category,
                "in_stock": p.in_stock,
            } for p in products]
            purchases = await asyncio.to_thread(client.sync_nomenclature_sync, prod_data)
            updated = 0
            for p in products:
                if p.article and p.article in purchases:
                    new_price = purchases[p.article]
                    if p.purchase_price != new_price:
                        p.purchase_price = new_price
                        updated += 1
            if updated:
                await session.commit()
                logger.info("purchase prices synced",
                            extra={"event": "gsheets_purchase_sync", "count": updated})
        except Exception as exc:
            # Текст ошибки дублируем в само сообщение: при LOG_JSON=false
            # форматтер не печатает поля extra, и без этого в логе виден только
            # «sync failed» без причины (так было 670 раз подряд).
            logger.error("gsheets nomenclature sync failed: %s", repr(exc),
                         extra={"event": "gsheets_error", "error": repr(exc)})
            # Отсутствующий/недоступный файл ключа — типовая причина: сообщаем
            # админу один раз (троттлинг в _notify_admin_error_once), чтобы
            # ошибка не молчала часами.
            await _notify_config_error_once(context, exc, "Справочник")

        # 2) Outbox: неотправленные позиции, группируем по заказам.
        pending = (await session.execute(
            select(GSheetsOutbox).where(GSheetsOutbox.sent_at.is_(None))
            .order_by(GSheetsOutbox.id)
        )).scalars().all()
        if not pending:
            return

        order_ids = sorted({p.order_id for p in pending})
        orders_data = []
        item_ids_by_order: dict[int, list[int]] = {}
        for oid in order_ids:
            stmt = (
                select(Order, User)
                .join(User, User.id == Order.user_id)
                .where(Order.id == oid)
            )
            row = (await session.execute(stmt)).first()
            if not row:
                continue
            order, user = row
            items_stmt = (
                select(OrderItem, Product)
                .join(Product, Product.id == OrderItem.product_id)
                .where(OrderItem.order_id == oid,
                       OrderItem.id.in_([p.order_item_id for p in pending
                                         if p.order_id == oid]))
            )
            items = (await session.execute(items_stmt)).all()
            if not items:
                continue
            client_name = user.full_name or ""
            if user.username:
                client_name = f"{client_name} (@{user.username})".strip()
            d = order.created_at or datetime.utcnow()
            orders_data.append({
                "order_id": order.id,
                "month": month_label(d),
                "date": d.strftime("%d.%m.%Y"),
                "tg_id": user.id,
                "client": client_name,
                "status": order.status.value if hasattr(order.status, "value") else str(order.status),
                "items": [{
                    "article": p.article or "",
                    "product": p.name,
                    "qty": it.quantity,
                    "purchase": p.purchase_price,
                    "sale": it.price_at_order,
                    "item_id": it.id,
                } for it, p in items],
            })
            item_ids_by_order[oid] = [it.id for it, _ in items]

        if not orders_data:
            return

        # 3) Отправка в лист (один поток на все заказы текущего цикла).
        try:
            await asyncio.to_thread(client.append_order_sync, orders_data)
            await asyncio.to_thread(client.update_report_sync)
        except Exception as exc:
            logger.error("gsheets append failed: %s", repr(exc),
                         extra={"event": "gsheets_error", "error": repr(exc)})
            await _notify_config_error_once(context, exc, settings.gsheets_tab_orders)
            for p in pending:
                p.attempts += 1
                p.error = repr(exc)[:2000]
            await session.commit()
            worst = max(p.attempts for p in pending)
            if worst == MAX_ATTEMPTS_NOTIFY:
                await _notify_admin_error(
                    context,
                    f"⚠️ Google Sheets: не удаётся отправить заказы в таблицу "
                    f"({worst} попыток). Ошибка: {repr(exc)[:300]}\n"
                    f"Проверьте доступ сервисного аккаунта к таблице.",
                )
            return

        # 4) Успех — помечаем отправленные позиции.
        sent_item_ids = {i for ids in item_ids_by_order.values() for i in ids}
        now = datetime.utcnow()
        for p in pending:
            if p.order_item_id in sent_item_ids:
                p.sent_at = now
                p.error = None
        await session.commit()
        logger.info("gsheets orders exported",
                    extra={"event": "gsheets_export",
                           "orders": len(orders_data),
                           "items": len(sent_item_ids)})


def register_jobs(app) -> None:
    """Регистрирует job-джобу синхронизации, если интеграция активна.

    Интеграция считается активной только при трёх условиях одновременно:
    ``GSHEETS_ENABLED`` не выключен, задан ``GOOGLE_SHEET_ID`` и файл ключа
    сервисного аккаунта реально существует на диске. Пока файла нет, джоба
    НЕ регистрируется: раньше она падала каждые 5 минут с FileNotFoundError
    и не приносила ничего, кроме шума в логах.
    """
    if not settings.gsheets_active:
        logger.info(
            "gsheets DISABLED: %s",
            settings.gsheets_inactive_reason,
            extra={"event": "gsheets_disabled",
                   "reason": settings.gsheets_inactive_reason},
        )
        return

    app.job_queue.run_repeating(
        gsheets_sync_job,
        interval=settings.gsheets_sync_interval_minutes * 60,
        first=30,
        name="gsheets_sync",
    )
    logger.info("gsheets sync job registered",
                extra={"event": "gsheets_enabled",
                       "interval_min": settings.gsheets_sync_interval_minutes})
