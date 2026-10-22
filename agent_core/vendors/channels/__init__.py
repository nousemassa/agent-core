"""Канальный путь платформы: порт и его реализация-адаптер.

Ядро работает с портом (`ChannelPath`), конкретную реализацию выбирает фабрика рантайма по настройке
`PLATFORM_VENDOR`. Имя платформы, её пути и авторизация живут только в адаптере.
"""
from agent_core.vendors.channels.port import ChannelPath, Reply, Tail

__all__ = ["ChannelPath", "Reply", "Tail"]
