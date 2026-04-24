import logging
import os

from core.request_context import request_id_var

os.makedirs("logs", exist_ok=True)


class _RequestIdFilter(logging.Filter):
    """Injects the current request_id contextvar into every log record so the
    formatter can render `[req-abc123]` without the caller passing it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        return True


def get_logger(name: str):
    """
    Creates and configures a reusable logger with file + console handlers.
    Logs stored in logs/<name>.log
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s [%(request_id)s] %(name)s - %(levelname)s - %(message)s"
    )
    rid_filter = _RequestIdFilter()

    file_handler = logging.FileHandler(f"logs/{name}.log")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(rid_filter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(rid_filter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    logger.propagate = False

    return logger
