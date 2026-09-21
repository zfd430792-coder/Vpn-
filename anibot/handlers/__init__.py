"""Сборка роутеров.

Порядок важен: в catalog есть перехват любого текста (поиск), поэтому он
идёт после админки — иначе съел бы ввод в её пошаговых формах.
"""

from __future__ import annotations

from aiogram import Dispatcher

from . import (admin, attach, catalog, channel, start, subscription,
               suggestions, support, watch)


def setup(dp: Dispatcher) -> None:
    dp.include_router(attach.router)
    dp.include_router(admin.router)
    dp.include_router(subscription.router)
    dp.include_router(start.router)
    dp.include_router(support.router)
    dp.include_router(suggestions.router)
    dp.include_router(watch.router)
    dp.include_router(catalog.router)
    dp.include_router(channel.router)
