"""Small, local-only runtime logging for startup and network diagnostics."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOGGER_NAME = "telescope.network"
LOG_FILE_NAME = "network.log"


def configure_runtime_logging(data_dir: str | Path) -> Path:
    """Point the shared runtime logger at this application's local data folder."""

    log_path = Path(data_dir).expanduser().resolve() / "runtime" / LOG_FILE_NAME
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        if isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename) == log_path:
            return log_path
        logger.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=4, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return log_path


def network_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def release_runtime_logging(data_dir: str | Path) -> None:
    """Close the file handler for a stopped application instance on Windows."""

    log_path = (Path(data_dir).expanduser().resolve() / "runtime" / LOG_FILE_NAME)
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        if isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename) == log_path:
            logger.removeHandler(handler)
            handler.close()
