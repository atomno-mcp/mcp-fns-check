"""Типизированные исключения для atomno-mcp-fns-check.

Иерархия:
    McpFnsError                — корень
        ValidationError        — невалидный вход (ИНН/ОГРН не проходит контроль)
        NotFoundError          — контрагент не найден в ЕГРЮЛ/ЕГРИП
        SourceUnavailableError — внешний источник недоступен (5xx/таймаут)
        RateLimitedError       — превышен rate-limit провайдера
        ParseError             — не удалось распарсить ответ источника

Каждое исключение несёт человекочитаемое сообщение `message_ru` для агента
и опциональный `details` со структурированным контекстом.
"""

from __future__ import annotations

from typing import Any


class McpFnsError(Exception):
    """Базовое исключение пакета. Агент-friendly: имеет `code` и `message_ru`."""

    code: str = "mcp_fns_error"

    def __init__(self, message_ru: str, *, details: dict[str, Any] | None = None) -> None:
        self.message_ru = message_ru
        self.details = details or {}
        super().__init__(message_ru)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message_ru": self.message_ru,
            "details": self.details,
        }


class ValidationError(McpFnsError):
    code = "invalid_input"


class NotFoundError(McpFnsError):
    code = "not_found"


class SourceUnavailableError(McpFnsError):
    code = "source_unavailable"


class RateLimitedError(McpFnsError):
    code = "rate_limited"


class ParseError(McpFnsError):
    code = "parse_error"
