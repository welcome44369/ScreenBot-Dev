import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(root_dir):
    logs_dir = Path(root_dir) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "ScreenBot.log"

    handler = RotatingFileHandler(str(log_path), maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    handler.setFormatter(formatter)

    logger = logging.getLogger("ScreenBot")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger
