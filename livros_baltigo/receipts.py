"""Only a concrete Telegram Message can confirm a document delivery."""
from __future__ import annotations
from dataclasses import dataclass
from .errors import TelegramError

@dataclass(frozen=True)
class DeliveryReceipt:
    chat_id: int
    message_id: int
    file_id: str
    file_size: int | None


def document_receipt(value: object, expected_chat: int) -> DeliveryReceipt:
    def unknown():
        return TelegramError(0, "Comprovante de envio ausente ou inconsistente; resultado desconhecido")
    if not isinstance(value, dict):
        raise unknown()
    chat, document = value.get("chat"), value.get("document")
    mid = value.get("message_id")
    if (not isinstance(chat, dict) or type(chat.get("id")) is not int
            or chat["id"] != expected_chat or type(mid) is not int or mid <= 0
            or not isinstance(document, dict)):
        raise unknown()
    fid, size = document.get("file_id"), document.get("file_size")
    if not isinstance(fid, str) or not fid or len(fid) > 4096:
        raise unknown()
    if size is not None and (type(size) is not int or size <= 0):
        raise unknown()
    return DeliveryReceipt(expected_chat, mid, fid, size)
