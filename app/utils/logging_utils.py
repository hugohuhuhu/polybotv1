from __future__ import annotations

import logging
from typing import Any

from rich.logging import RichHandler


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, markup=True)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


class StructuredLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        context = kwargs.pop("context", {})
        if context:
            suffix = " ".join(f"{key}={value}" for key, value in context.items())
            msg = f"{msg} | {suffix}"
        return msg, kwargs


def get_logger(name: str) -> StructuredLoggerAdapter:
    return StructuredLoggerAdapter(logging.getLogger(name), {})
