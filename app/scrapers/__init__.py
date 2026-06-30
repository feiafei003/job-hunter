from .base import SCRAPERS, BaseScraper, ScrapeBlockedError, get_scraper

# 导入以触发插件注册
from . import zhilian  # noqa: F401,E402
from . import linkedin  # noqa: F401,E402
from . import boss  # noqa: F401,E402
from . import liepin  # noqa: F401,E402
from . import job51  # noqa: F401,E402

__all__ = ["SCRAPERS", "BaseScraper", "ScrapeBlockedError", "get_scraper"]
