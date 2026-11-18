"""SAGE Friday Communication Gateway Adapters."""

from src.communication.adapters.base import BaseChannelAdapter
from src.communication.adapters.telegram import TelegramChannelAdapter
from src.communication.adapters.whatsapp import WhatsAppChannelAdapter

__all__ = [
    "BaseChannelAdapter",
    "TelegramChannelAdapter",
    "WhatsAppChannelAdapter",
]
