from unittest.mock import AsyncMock
import pytest
from livros_baltigo.errors import TelegramError
from livros_baltigo.interface import sync_interface


async def test_optional_metadata_honors_telegram_rate_limit(settings):
    tg = AsyncMock()
    tg.call.side_effect = TelegramError(429, 'Too many requests', 120)
    await sync_interface(tg, settings, [], 999)
    assert tg.call.await_count == 1
    await sync_interface(tg, settings, [], 999)
    assert tg.call.await_count == 1
    await sync_interface(tg, settings, [{'command':'changed'}], 999)
    assert tg.call.await_count == 1


async def test_successful_metadata_cached_across_restarts(settings):
    tg = AsyncMock()
    await sync_interface(tg, settings, [], 999)
    calls = tg.call.await_count
    assert calls > 0
    await sync_interface(tg, settings, [], 999)
    assert tg.call.await_count == calls
    await sync_interface(tg, settings, [{'command': 'new', 'description': 'New command'}], 999)
    assert tg.call.await_count > calls
