# -*- coding: utf-8 -*-
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.formatting.rule import CellIsRule
from datetime import date

wb = Workbook()

thin = Side(style="thin", color="BFBFBF")
border = Border(left=thin, right=thin, top=thin, bottom=thin)
hdr_font = Font(bold=True, color="FFFFFF", size=11)
hdr_fill = PatternFill("solid", fgColor="4472C4")
month_font = Font(bold=True, color="FFFFFF", size=11)
month_fill = PatternFill("solid", fgColor="70AD47")
order_font = Font(bold=True, size=10)
order_fill = PatternFill("solid", fgColor="FFF2CC")
mtotal_font = Font(bold=True, size=11)
mtotal_fill = PatternFill("solid", fgColor="D9E1F2")
grand_font = Font(bold=True, size=12, color="FFFFFF")
grand_fill = PatternFill("solid", fgColor="2F5597")
money = '#,##0 ₽'

def style_row(ws, row, ncols, font=None, fill=None, fmt=None):
    for c in range(1, ncols+1):
        cell = ws.cell(row=row, column=c)
        cell.border = border
        if font: cell.font = font
        if fill: cell.fill = fill
        if fmt and c >= 8: cell.number_format = fmt

HEADERS = ["Дата", "Заказ ID", "TG ID", "Клиент", "Артикул", "Товар",
           "Кол-во", "Цена закупа", "Σ закупа", "Цена продажи", "Σ продажи",
           "Прибыль", "Форма оплаты", "Этап сборки", "Комментарий",
           "Статус", "ID позиции (служеб.)"]
NC = len(HEADERS)

def make_orders_sheet(ws, demo_rows, manual=False):
    """Возвращает dict: {month_label: mtotal_row}, grand_row."""
    ws.freeze_panes = "A2"
    for i, h in enumerate(HEADERS, 1):
        ws.cell(row=1, column=i, value=h)
    style_row(ws, 1, NC, hdr_font, hdr_fill)
    widths = [11, 9, 11, 22, 12, 30, 7, 12, 12, 13, 12, 11, 14, 13, 30, 12, 16]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    r = 2
    month_start_rows = {}
    month_mtotal = {}      # label -> row итога месяца
    order_blocks = []      # (first, last)

    for kind, payload in demo_rows:
        if kind == "month":
            ws.cell(row=r, column=1, value=f"=== {payload} ===")
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NC)
            style_row(ws, r, NC, month_font, month_fill)
            month_start_rows[payload] = r + 1
            r += 1
        elif kind == "order":
            first = r
            for item in payload:
                for c, v in enumerate(item, 1):
                    if v is not None:
                        ws.cell(row=r, column=c, value=v)
                ws.cell(row=r, column=9,  value=f"=G{r}*H{r}")
                ws.cell(row=r, column=11, value=f"=G{r}*J{r}")
                ws.cell(row=r, column=12, value=f"=K{r}-I{r}")
                style_row(ws, r, NC, fmt=money)
                ws.cell(row=r, column=1).number_format = 'DD.MM.YYYY'
                ws.cell(row=r, column=7).number_format = '0'
                r += 1
            last = r - 1
            order_blocks.append((first, last))
            oid = payload[0][1]
            ws.cell(row=r, column=6, value=f"ИТОГО ЗАКАЗ {oid}:")
            ws.cell(row=r, column=6).alignment = Alignment(horizontal="right")
            ws.cell(row=r, column=9,  value=f"=SUM(I{first}:I{last})")
            ws.cell(row=r, column=11, value=f"=SUM(K{first}:K{last})")
            ws.cell(row=r, column=12, value=f"=SUM(L{first}:L{last})")
            style_row(ws, r, NC, order_font, order_fill, money)
            r += 1
        elif kind == "mtotal":
            label = payload
            first = month_start_rows[label]
            last = r - 1
            ws.cell(row=r, column=1, value=f"ИТОГО {label}:")
            # кол-во заказов = число строк «ИТОГО ЗАКАЗ…» в диапазоне месяца
            ws.cell(row=r, column=6,
                    value=f'=COUNTIF(F{first}:F{last},"ИТОГО ЗАКАЗ*")')
            ws.cell(row=r, column=6).alignment = Alignment(horizontal="right")
            # суммы только по строкам позиций (у строк итогов заказов H пустая)
            ws.cell(row=r, column=7,
                    value=f'=SUMPRODUCT(ISNUMBER(G{first}:G{last})*G{first}:G{last})')
            for col in (9, 11, 12):
                L = get_column_letter(col)
                ws.cell(row=r, column=col,
                        value=f'=SUMPRODUCT(ISNUMBER(H{first}:H{last})*{L}{first}:{L}{last})')
            style_row(ws, r, NC, mtotal_font, mtotal_fill, money)
            ws.cell(row=r, column=7).number_format = '0'
            ws.cell(row=r, column=6).number_format = 'General'
            month_mtotal[label] = r
            r += 2

    if manual:
        ws.cell(row=r, column=1, value="↓ новые строки вносит продавец: дата, заказ, клиент, товар (из Справочника), кол-во, форма оплаты, этап сборки; "
                                       "цены подставьте формулой VLOOKUP по артикулу, Σ и Прибыль — скопируйте из строки выше")
        ws.cell(row=r, column=1).font = Font(italic=True, color="808080")
        r += 2

    ws.cell(row=r, column=1, value="ВСЕГО ЗА ВСЁ ВРЕМЯ:")
    parts = "+".join(f"{get_column_letter(c)}{mrow}" for mrow in month_mtotal.values() for c in [0] ) if False else None
    for col in (9, 11, 12):
        L = get_column_letter(col)
        formula = "=" + "+".join(f"{L}{mrow}" for mrow in month_mtotal.values())
        ws.cell(row=r, column=col, value=formula if month_mtotal else 0)
    style_row(ws, r, NC, grand_font, grand_fill, money)

    # --- Форма оплаты / Этап сборки: выпадающие списки + подсветка ---
    last_r = max(r, 200)
    dv_pay = DataValidation(type="list",
        formula1='"QR код,Наличные,Перевод,Не оплачен"', allow_blank=True)
    dv_pay.error = "Выберите значение из списка"
    dv_stage = DataValidation(type="list",
        formula1='"Принят,В работе,Собран,Отгружен"', allow_blank=True)
    ws.add_data_validation(dv_pay)
    ws.add_data_validation(dv_stage)
    dv_pay.add(f"M2:M{last_r}")
    dv_stage.add(f"N2:N{last_r}")

    green = PatternFill("solid", fgColor="C6EFCE")
    red = PatternFill("solid", fgColor="FFC7CE")
    yellow = PatternFill("solid", fgColor="FFF2CC")
    blue = PatternFill("solid", fgColor="DDEBF7")
    grey = PatternFill("solid", fgColor="E7E6E6")
    rng_pay = f"M2:M{last_r}"
    rng_stage = f"N2:N{last_r}"
    # оплачено — зелёным, не оплачен — красным
    for val in ("QR код", "Наличные", "Перевод"):
        ws.conditional_formatting.add(rng_pay, CellIsRule(
            operator="equal", formula=[f'"{val}"'], fill=green))
    ws.conditional_formatting.add(rng_pay, CellIsRule(
        operator="equal", formula=['"Не оплачен"'], fill=red))
    # этапы сборки
    ws.conditional_formatting.add(rng_stage, CellIsRule(
        operator="equal", formula=['"Принят"'], fill=grey))
    ws.conditional_formatting.add(rng_stage, CellIsRule(
        operator="equal", formula=['"В работе"'], fill=yellow))
    ws.conditional_formatting.add(rng_stage, CellIsRule(
        operator="equal", formula=['"Собран"'], fill=blue))
    ws.conditional_formatting.add(rng_stage, CellIsRule(
        operator="equal", formula=['"Отгружен"'], fill=green))

    return month_mtotal, r

# ---------------- ОПТ -------------------
ws_opt = wb.active
ws_opt.title = "Опт"
demo_opt = [
    ("month", "СЕНТЯБРЬ 2026"),
    ("order", [
        [date(2026,9,11), 154, 111111, "Иван (@ivan)", "AM-08", "Бусы из аметиста, 8 мм", 2, 1500, None, 2500, None, None, "Перевод", "Собран", None, "confirmed", 812],
        [date(2026,9,11), 154, 111111, "Иван (@ivan)", "GH-06", "Бусы из горного хрусталя, 6 мм",  1,  800, None, 1400, None, None, "Перевод", "Собран", None, "confirmed", 813],
    ]),
    ("order", [
        [date(2026,9,12), 155, 222222, "Ольга (@olga)", "AM-08", "Бусы из аметиста, 8 мм", 1, 1500, None, 2500, None, None, "QR код", "В работе", None, "confirmed", 814],
    ]),
    ("mtotal", "СЕНТЯБРЬ 2026"),
    ("month", "ОКТЯБРЬ 2026"),
    ("order", [
        [date(2026,10,3), 156, 111111, "Иван (@ivan)", "LAVA-10", "Бусы из вулканической лавы, 10 мм", 3, 300, None, 700, None, None, "Наличные", "Отгружен", None, "confirmed", 815],
    ]),
    ("mtotal", "ОКТЯБРЬ 2026"),
]
opt_months, opt_grand = make_orders_sheet(ws_opt, demo_opt)

# ---------------- РОЗНИЦА -------------------
ws_ret = wb.create_sheet("Розница")
demo_ret = [
    ("month", "СЕНТЯБРЬ 2026"),
    ("order", [
        [date(2026,9,15), "Р-01", None, "Розничный покупатель", "GH-06", "Бусы из горного хрусталя, 6 мм", 1, 800, None, 1400, None, None, "Наличные", "Отгружен", None, "продан", None],
    ]),
    ("order", [
        [date(2026,9,20), "Р-02", None, "Розничный покупатель", "AM-08", "Бусы из аметиста, 8 мм", 1, 1500, None, 2500, None, None, "QR код", "Отгружен", None, "продан", None],
        [date(2026,9,20), "Р-02", None, "Розничный покупатель", "LAVA-10", "Бусы из вулканической лавы, 10 мм",     2,  300, None,  700, None, None, "QR код", "Отгружен", None, "продан", None],
    ]),
    ("mtotal", "СЕНТЯБРЬ 2026"),
]
ret_months, ret_grand = make_orders_sheet(ws_ret, demo_ret, manual=True)

# =====================================================================
# СПРАВОЧНИК
# =====================================================================
ws_ref = wb.create_sheet("Справочник")
ref_headers = ["Артикул", "Название", "Категория", "Цена закупа (вручную)",
               "Цена продажи", "Наценка (коэфф.)", "В наличии"]
for i, h in enumerate(ref_headers, 1):
    ws_ref.cell(row=1, column=i, value=h)
style_row(ws_ref, 1, len(ref_headers), hdr_font, hdr_fill)
for i, w in enumerate([12, 30, 14, 22, 14, 18, 11], 1):
    ws_ref.column_dimensions[get_column_letter(i)].width = w
ref_data = [
    ["AM-08", "Бусы из аметиста, 8 мм", "Бусы", 1500, None, 1.67, True],
    ["GH-06", "Бусы из горного хрусталя, 6 мм",  "Бусы", 800, None, 1.75, True],
    ["LAVA-10", "Бусы из вулканической лавы, 10 мм",     "Бусы", 300, None, 2.33, True],
]
yellow = PatternFill("solid", fgColor="FFF2CC")
for r_i, row in enumerate(ref_data, 2):
    for c_i, v in enumerate(row, 1):
        ws_ref.cell(row=r_i, column=c_i, value=v)
    ws_ref.cell(row=r_i, column=5, value=f"=ROUND(D{r_i}*F{r_i},0)")
    style_row(ws_ref, r_i, len(ref_headers), fmt=money)
    ws_ref.cell(row=r_i, column=6).number_format = "0.00"
    ws_ref.cell(row=r_i, column=7).number_format = "General"
    ws_ref.cell(row=r_i, column=4).fill = yellow
    ws_ref.cell(row=r_i, column=6).fill = yellow
note_r = len(ref_data) + 3
ws_ref.cell(row=note_r, column=1, value="Жёлтые колонки («Цена закупа», «Наценка») заполняются вручную один раз — бот их НЕ перезаписывает. "
                                        "«Цена продажи» = ROUND(закуп × наценка) — обновится автоматически при правке закупа (этап 6). "
                                        "Артикул/название/категорию/наличие синхронизирует бот.")
ws_ref.cell(row=note_r, column=1).font = Font(italic=True, color="808080")

# =====================================================================
# ОТЧЁТ
# =====================================================================
ws_rep = wb.create_sheet("Отчёт")
ws_rep.column_dimensions["A"].width = 20
for col, w in zip("BCDE", (14, 14, 14, 12)):
    ws_rep.column_dimensions[col].width = w
ws_rep.column_dimensions["F"].width = 3
for col, w in zip("GHIJ", (24, 14, 14, 14)):
    ws_rep.column_dimensions[col].width = w

ws_rep.cell(row=1, column=1, value="СВОД ПО МЕСЯЦАМ (Опт + Розница)").font = Font(bold=True, size=12)
for i, h in enumerate(["Месяц", "Закуп", "Продажа", "Прибыль", "Заказов"], 1):
    ws_rep.cell(row=2, column=i, value=h)
style_row(ws_rep, 2, 5, hdr_font, hdr_fill)

all_months = list(dict.fromkeys(list(opt_months.keys()) + list(ret_months.keys())))
r = 3
for label in all_months:
    ws_rep.cell(row=r, column=1, value=label)
    refs = []
    if label in opt_months: refs.append(("Опт", opt_months[label]))
    if label in ret_months: refs.append(("Розница", ret_months[label]))
    for col, L in ((2,"I"), (3,"K"), (4,"L")):
        ws_rep.cell(row=r, column=col, value="=" + "+".join(f"'{s}'!{L}{mr}" for s, mr in refs))
    ws_rep.cell(row=r, column=5, value="=" + "+".join(f"'{s}'!F{mr}" for s, mr in refs))
    style_row(ws_rep, r, 5)
    for cc in (2, 3, 4): ws_rep.cell(row=r, column=cc).number_format = money
    ws_rep.cell(row=r, column=5).number_format = "0"
    r += 1
ws_rep.cell(row=r, column=1, value="ИТОГО")
for col in (2, 3, 4):
    L = get_column_letter(col)
    ws_rep.cell(row=r, column=col, value=f"=SUM({L}3:{L}{r-1})")
ws_rep.cell(row=r, column=5, value=f"=SUM(E3:E{r-1})")
style_row(ws_rep, r, 5, mtotal_font, mtotal_fill)
for cc in (2, 3, 4): ws_rep.cell(row=r, column=cc).number_format = money
ws_rep.cell(row=r, column=5).number_format = "0"

ws_rep.cell(row=2, column=7, value="СВОД ПО КЛИЕНТАМ (Опт)").font = Font(bold=True, size=12)
for i, h in enumerate(["Клиент", "Закуп", "Продажа", "Прибыль"], 7):
    ws_rep.cell(row=3, column=i, value=h)
style_row(ws_rep, 3, 10, hdr_font, hdr_fill)
clients = ["Иван (@ivan)", "Ольга (@olga)"]
rr = 4
for cl in clients:
    ws_rep.cell(row=rr, column=7, value=cl)
    ws_rep.cell(row=rr, column=8,  value=f"=SUMIFS('Опт'!I:I,'Опт'!D:D,G{rr},'Опт'!G:G,\">0\")")
    ws_rep.cell(row=rr, column=9,  value=f"=SUMIFS('Опт'!K:K,'Опт'!D:D,G{rr},'Опт'!G:G,\">0\")")
    ws_rep.cell(row=rr, column=10, value=f"=SUMIFS('Опт'!L:L,'Опт'!D:D,G{rr},'Опт'!G:G,\">0\")")
    style_row(ws_rep, rr, 10)
    for cc in (8, 9, 10): ws_rep.cell(row=rr, column=cc).number_format = money
    rr += 1
ws_rep.cell(row=rr+1, column=7, value="Все суммы — формулы, пересчитываются автоматически при правках строк.").font = Font(italic=True, color="808080")

# =====================================================================
# ИНСТРУКЦИЯ
# =====================================================================
ws_doc = wb.create_sheet("Как это работает")
ws_doc.column_dimensions["A"].width = 115
lines = [
    ("ШАБЛОН УЧЁТА ЗАКАЗОВ: Telegram-бот ↔ таблица", True),
    ("", False),
    ("Лист «Опт» — заказы из Telegram, заполняется БОТОМ автоматически после подтверждения оплаты.", False),
    ("Лист «Розница» — продажи в магазине, заполняется ПРОДАВЦОМ вручную (бот только читает для итогов).", False),
    ("Лист «Справочник» — номенклатура: жёлтые колонки («Цена закупа», «Наценка») вносят вручную один раз,", False),
    ("    бот их никогда не перезаписывает; «Цена продажи» считается автоматически = закуп × наценка.", False),
    ("Лист «Отчёт» — свод по месяцам и по клиентам, все суммы формулами.", False),
    ("", False),
    ("КОЛОНКИ «ФОРМА ОПЛАТЫ», «ЭТАП СБОРКИ», «КОММЕНТАРИЙ»:", True),
    ("• «Форма оплаты» — выпадающий список: QR код / Наличные / Перевод / Не оплачен.", False),
    ("  Оплаченные формы подсвечиваются зелёным, «Не оплачен» — красным (автоматически).", False),
    ("• «Этап сборки» — выпадающий список: Принят / В работе / Собран / Отгружен (с подсветкой).", False),
    ("• «Комментарий» — свободная колонка для любой информации о клиенте/заказе.", False),
    ("Бот заполняет строку заказа, а оплату/этап/комментарий продавец выбирает вручную", False),
    ("из выпадающих списков — бот эти колонки не перезаписывает.", False),
    ("", False),
    ("СТРУКТУРА ИТОГОВ (как договорились):", True),
    ("1. «ИТОГО ЗАКАЗ …» — жёлтая строка под каждым заказом: Σ закуп, Σ продажа, прибыль.", False),
    ("2. «=== МЕСЯЦ ===» — зелёный заголовок, месяцы визуально разделены.", False),
    ("3. «ИТОГО <месяц>» — голубая строка в конце месяца: кол-во, закуп, продажа, прибыль.", False),
    ("4. «ВСЕГО ЗА ВСЁ ВРЕМЯ» — тёмно-синяя строка общих итогов внизу листа.", False),
    ("", False),
    ("СЛУЖЕБНЫЕ КОЛОНКИ (не удалять):", True),
    ("• «TG ID» — стабильный идентификатор клиента (имя в Telegram можно сменить, ID — нет).", False),
    ("• «ID позиции (служеб.)» — защита от дублей: бот проверяет этот ID перед записью строки.", False),
    ("", False),
    ("ПРАВИЛА РУЧНОГО РЕДАКТИРОВАНИЯ:", True),
    ("• Новые позиции вставляйте ВНУТРИ блока месяца, ВЫШЕ строки «ИТОГО <месяц>» — итоги пересчитаются.", False),
    ("• Формулы строки позиции: Σ закупа =G×H; Σ продажи =G×J; Прибыль =K−I. При добавлении строки", False),
    ("  скопируйте формулы из соседней позиции.", False),
    ("• Демо-данные перед запуском удалите, структуру и формулы оставьте.", False),
    ("", False),
    ("При переносе в Google Sheets: Файл → Импорт → Заменить таблицу. Формулы совместимы;", False),
    ("«=== МЕСЯЦ ===» в Sheets удобнее заменить разделителем «синяя группа строк» (View → Group),", False),
    ("бот при записи нового месяца будет создавать новую группу автоматически.", False),
]
for i, (txt, bold) in enumerate(lines, 1):
    c = ws_doc.cell(row=i, column=1, value=txt)
    if bold: c.font = Font(bold=True, size=12)

out = "/home/admin1/tgbot/docs/shablon_uchet_zakazov.xlsx"
wb.save(out)
print("saved:", out)
print("opt month totals:", opt_months, "grand:", opt_grand)
print("ret month totals:", ret_months, "grand:", ret_grand)
