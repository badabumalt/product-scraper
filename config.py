"""Настройки приложения и «профиль сайта» (селекторы) под Avito (поисковый запрос "макбук")."""
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"

APP_NAME = "Сборщик объявлений Avito (MacBook)"
BASE_URL = "https://www.avito.ru/"
ALL_PRODUCTS_URL = "https://www.avito.ru/rossiya?q=%D0%BC%D0%B0%D0%BA%D0%B1%D1%83%D0%BA"
DEFAULT_OUTPUT_DIR = "D:/claudepetpapka/AlotPro/product-scraper/ProductScraper"

PAGE_TIMEOUT_MS = 35000      # таймаут загрузки страницы
RETRIES = 3                  # повторы при ошибках сети
BACKUP_EVERY_SEC = 60        # сохранение бэкапа базы

# Признаки того, что сайт нас ограничил
BLOCK_STATUSES = (403, 429, 503)
BLOCK_TITLE_MARKERS = ("доступ ограничен", "подтвердите, что вы не робот", "security service", "captcha")

SIMULATE_BLOCK_AFTER = int(os.environ.get("SIMULATE_BLOCK_AFTER", "0") or 0)

# CSS-селекторы авито
SEL = {
"category_links": "", # На Авито категории через поиск, оставляем пустым или заглушкой
"catalog_items": "div[data-marker='item'] a[data-marker='item-title']",
"catalog_next": "a[data-marker='pagination-button/next']",
"title": "h1[data-marker='item-view/title-info']",
"price": "span[data-marker='item-view/item-price']",
"availability": "div[data-marker='item-view/item-status']", # или проверка на активное объявление
"rating": "", # У продавцов на Авито рейтинг идет отдельным блоком
"specs_rows": "ul.item-params-list li",
"category_crumb": "div.breadcrumbs-layout div._1w9h9"
}


@dataclass
class Settings:
    """Настройки одного запуска. Сохраняются в базу, чтобы при продолжении взять их обратно."""
    category_name: str = "MacBook (Поиск Avito)"
    category_url: str = ALL_PRODUCTS_URL
    threads: int = 1
    limit: int = 20               # 0 = без лимита
    delay: float = 5.0           # пауза перед каждым запросом, сек
    min_price: float = 0.0       # 0 = не ограничивать
    max_price: float = 0.0       # 0 = не ограничивать
    only_in_stock: bool = False
    keyword: str = "макбук"

    def to_json(self):
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text):
        return cls(**json.loads(text))