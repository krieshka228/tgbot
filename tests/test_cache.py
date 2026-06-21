"""Тесты in-memory TTL-кэша каталога (bot.cache)."""

import bot.cache as cache


async def test_get_or_set_caches_value():
    cache.invalidate_catalog_cache()
    calls = {"n": 0}

    async def loader():
        calls["n"] += 1
        return "value"

    v1 = await cache.get_or_set("k", loader, ttl=100)
    v2 = await cache.get_or_set("k", loader, ttl=100)
    assert v1 == v2 == "value"
    assert calls["n"] == 1  # второй вызов берётся из кэша


async def test_ttl_expiry_triggers_reload():
    cache.invalidate_catalog_cache()
    calls = {"n": 0}

    async def loader():
        calls["n"] += 1
        return calls["n"]

    await cache.get_or_set("k", loader, ttl=0)  # мгновенно протухает
    await cache.get_or_set("k", loader, ttl=0)
    assert calls["n"] == 2


async def test_invalidate_clears_cache():
    async def loader():
        return "x"

    await cache.get_or_set("k", loader, ttl=100)
    cache.invalidate_catalog_cache()
    assert cache._cache == {}
