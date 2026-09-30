"""Тест на регрессию: пагинация в «Управление товарами».

manage_products_page_handler вызывал show_manage_products_page(category=...),
но функция принимает только (query, context, page=0) и берёт категорию из
context.user_data. Каждая кнопка «← Назад» / «Вперёд →» падала с
TypeError: unexpected keyword argument 'category'.
"""

import inspect

from bot.handlers import admin


def test_show_manage_products_page_has_no_category_param():
    """Сигнатура не принимает category — значит вызов должен быть без него."""
    sig = inspect.signature(admin.show_manage_products_page)
    assert "category" not in sig.parameters


def test_handler_passes_no_category_kwarg():
    """В КОДЕ хендлера нет вызова с category= (собственно регрессия).

    Разбираем AST, а не текст: в комментарии допустимо упоминание
    ``category=`` — тест должен реагировать только на реальный вызов.
    """
    import ast

    tree = ast.parse(inspect.getsource(admin.manage_products_page_handler))
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name == "show_manage_products_page":
                bad.extend(kw.arg for kw in node.keywords if kw.arg == "category")
    assert not bad, "пагинация снова передаёт category — TypeError вернётся"


def test_handler_is_callable_with_two_args():
    """PTB вызывает хендлер как (update, context)."""
    sig = inspect.signature(admin.manage_products_page_handler)
    assert list(sig.parameters) == ["update", "context"]


def test_all_show_manage_products_page_calls_compatible():
    """Все вызовы в модуле используют только page= (или позиционно)."""
    import ast

    src = inspect.getsource(admin)
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name == "show_manage_products_page":
                for kw in node.keywords:
                    if kw.arg not in (None, "page"):
                        bad.append((node.lineno, kw.arg))
    assert not bad, f"несовместимые именованные аргументы: {bad}"
