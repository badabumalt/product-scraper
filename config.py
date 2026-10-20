"""Настройки приложения и «профиль сайта» (селекторы).

Чтобы собирать данные с другого сайта, меняют словарь SEL и функцию parse_product в scraper.py.
"""
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

# Chromium лежит внутри пакета playwright — так его проще упаковать в установщик.
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"

APP_NAME = "Сборщик товаров"
BASE_URL = "https://books.toscrape.com/"
ALL_PRODUCTS_URL = "https://books.toscrape.com/catalogue/page-1.html"
DEFAULT_OUTPUT_DIR = Path.home() / "ProductScraper"



PAGE_TIMEOUT_MS = 30000      # сколько ждать загрузку страницы
RETRIES = 3                  # сколько раз повторять при сетевой ошибке
BACKUP_EVERY_SEC = 60        # как часто копировать базу прогресса

# Признаки того, что сайт нас ограничил
BLOCK_STATUSES = (403, 429)
BLOCK_TITLE_MARKERS = ("captcha", "access denied", "forbidden", "too many requests", "just a moment")

# Для проверки: после N запросов притвориться, что сайт ответил 403 (см. шпаргалку, п. 6)
SIMULATE_BLOCK_AFTER = int(os.environ.get("SIMULATE_BLOCK_AFTER", "0") or 0)

# CSS-селекторы нашего сайта
SEL = {
    "category_links": "div.side_categories ul.nav-list > li > ul > li > a",
    "catalog_items": "article.product_pod h3 a",
    "catalog_next": "li.next > a",
    "title": "div.product_main h1",
    "price": "div.product_main p.price_color",
    "availability": "div.product_main p.availability",
    "rating": "div.product_main p.star-rating",
    "specs_rows": "table.table-striped tr",
    "category_crumb": "ul.breadcrumb li:nth-child(3) a",
}


@dataclass
class Settings:
    """Настройки одного запуска. Сохраняются в базу, чтобы при продолжении взять их обратно."""
    category_name: str = "Все товары"
    category_url: str = ALL_PRODUCTS_URL
    threads: int = 2
    limit: int = 0               # 0 = без лимита
    delay: float = 1.0           # пауза перед каждым запросом, сек
    min_price: float = 0.0       # 0 = не ограничивать
    max_price: float = 0.0       # 0 = не ограничивать
    only_in_stock: bool = False
    keyword: str = ""

    def to_json(self):
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text):
        return cls(**json.loads(text))