"""Preserve canonical error text while translating at the presentation boundary."""

from __future__ import annotations

from string import Formatter


def _placeholders(message: str) -> tuple[tuple[str, str, str], ...]:
    return tuple(sorted((name, spec or "", conversion or "") for _, name, spec, conversion in Formatter().parse(message)
                        if name is not None))


class LocalizedMessage:
    def __init__(self, message: str, *values: object):
        self.message_template = message
        self.message_values = values
        super().__init__(message.format(*values) if values else message)

    def translated(self, translate) -> str:
        message = translate(self.message_template)
        if not self.message_values:
            return message
        try:
            if _placeholders(message) == _placeholders(self.message_template):
                return message.format(*self.message_values)
        except (IndexError, KeyError, ValueError):
            pass
        return str(self)

    @classmethod
    def from_error(cls, error: Exception):
        if isinstance(error, LocalizedMessage):
            return cls(error.message_template, *error.message_values)
        return cls(str(error))


class LocalizedError(LocalizedMessage, ValueError):
    pass


class LocalizedRuntimeError(LocalizedMessage, RuntimeError):
    pass


def translate_error(error: Exception, translate) -> str:
    if isinstance(error, LocalizedMessage):
        return error.translated(translate)
    return translate(str(error))
