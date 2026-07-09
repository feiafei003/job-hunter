"""统一日志配置：同时输出到控制台和 data/jobhunter.log。"""

import logging
from logging.handlers import RotatingFileHandler

from .config import get_settings

_CONFIGURED = False


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    settings = get_settings()
    log_file = settings.data_path / "jobhunter.log"

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    file_handler = RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    console = logging.StreamHandler()
    console.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console)

    # 我们自己的命名空间使用 INFO
    logging.getLogger("jobhunter").setLevel(logging.INFO)

    _CONFIGURED = True
    logging.getLogger("jobhunter").info("日志已初始化，文件: %s", log_file)
