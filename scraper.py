"""Ядро: Playwright, разбор страниц, потоки, остановка и продолжение."""
import queue
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urldefrag, urljoin

import config
from config import Settings
from exporter import export_all
from storage import Storage
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

RATING = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}


class Stopped(Exception):
    """Нажали «Стоп»."""


class AccessDenied(Exception):
    """Сайт ограничил доступ: 403, 429 или страница-капча."""


class PageGone(Exception):
    """Страница не найдена (404)."""


class NetworkFailure(Exception):
    """Страницу не удалось загрузить после всех повторов."""


class RunLog:
    """Пишет строки в log.txt и отдаёт их окну через очередь событий."""

    def __init__(self, path, events):
        self.file = open(path, "a", encoding="utf-8")
        self.events = events
        self.lock = threading.Lock()

    def __call__(self, text):
        line = f"{datetime.now():%H:%M:%S} {text}"
        with self.lock:
            if self.file.closed:
                return
            self.file.write(line + "\n")
            self.file.flush()
        self.events.put(("log", line))

    def close(self):
        with self.lock:
            self.file.close()


def describe(error):
    """Короткое описание ошибки для журнала (одна строка)."""
    text = (str(error).strip().splitlines() or [repr(error)])[0]
    if "Executable doesn't exist" in str(error):
        text += " — Chromium не установлен, см. шпаргалку, п. 7"
    return text


@contextmanager
def browser_page():
    """Свой Chromium для каждого потока (синхронный Playwright нельзя делить между потоками)."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(config.PAGE_TIMEOUT_MS)
            # картинки, шрифты и стили для сбора текста не нужны — так быстрее
            page.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,css}", lambda route: route.abort())
            yield page
        finally:
            browser.close()


def fetch_categories():
    """Список категорий для окна: [(название, ссылка), ...]."""
    with browser_page() as page:
        page.goto(config.BASE_URL, wait_until="domcontentloaded")
        items = page.eval_on_selector_all(
            config.SEL["category_links"], "els => els.map(e => [e.textContent.trim(), e.href])"
        )
    return [("Все товары", config.ALL_PRODUCTS_URL)] + [(name, url) for name, url in items]


def clean(text):
    return " ".join((text or "").split())


def text_of(page, selector):
    locator = page.locator(selector)
    return clean(locator.first.text_content()) if locator.count() else ""


def parse_product(page, url):
    """Читает открытую карточку товара. Для другого сайта правится именно эта функция."""
    sel = config.SEL
    title = text_of(page, sel["title"])
    if not title:
        raise ValueError("не найдено название товара (изменилась вёрстка?)")

    match = re.match(r"\s*(\D*?)\s*(\d[\d.,]*)", text_of(page, sel["price"]))
    currency = match.group(1) if match else ""
    try:
        price = float(match.group(2).replace(",", "")) if match else None
    except ValueError:
        price = None

    availability = text_of(page, sel["availability"])
    qty = re.search(r"\((\d+)", availability)

    rating_loc = page.locator(sel["rating"])
    words = (rating_loc.first.get_attribute("class") or "").split() if rating_loc.count() else []
    rating = RATING.get(words[-1]) if words else None

    pairs = page.eval_on_selector_all(
        sel["specs_rows"],
        "rows => rows.map(r => [r.querySelector('th')?.textContent ?? '', r.querySelector('td')?.textContent ?? ''])",
    )
    specs = {clean(k): clean(v) for k, v in pairs if clean(k)}

    return {
        "url": urldefrag(url)[0],
        "sku": specs.get("UPC"),
        "title": title,
        "price": price,
        "currency": currency,
        "in_stock": "in stock" in availability.lower(),
        "stock_qty": int(qty.group(1)) if qty else None,
        "category": text_of(page, sel["category_crumb"]),
        "rating": rating,
        "specs": specs,
    }


def passes_filters(rec, s):
    if s.only_in_stock and not rec["in_stock"]:
        return False
    price = rec["price"]
    if s.min_price and (price is None or price < s.min_price):
        return False
    if s.max_price and (price is None or price > s.max_price):
        return False
    if s.keyword and s.keyword.lower() not in rec["title"].lower():
        return False
    return True


def new_run_dir(base, category_name):
    safe = re.sub(r"[^\w\-]+", "_", category_name).strip("_")[:40] or "run"
    return Path(base) / "runs" / f"{datetime.now():%Y-%m-%d_%H-%M-%S}_{safe}"


class Runner:
    """Один запуск сбора. Работает в фоне; окно читает runner.events и runner.get_stats()."""

    def __init__(self, run_dir, settings=None):
        self.storage = Storage(run_dir)
        if settings is None:   # продолжение: настройки берём из базы
            saved = self.storage.get_meta("settings")
            if not saved:
                self.storage.close()
                raise ValueError("В этой папке нет сохранённого запуска (в progress.db нет настроек)")
            settings = Settings.from_json(saved)
        else:
            self.storage.set_meta("settings", settings.to_json())
        self.settings = settings
        self.events = queue.Queue()          # ("log", строка) и ("finished", причина, файлы, статистика)
        self.log = RunLog(self.storage.run_dir / "log.txt", self.events)
        self.stop_event = threading.Event()
        self.reason = None                   # None = всё доделали; иначе stopped/limit/blocked/error
        self._lock = threading.Lock()
        self._requests = 0
        self._thread = None

    # --- управление ---
    def start(self):
        self._thread = threading.Thread(target=self._run, name="runner", daemon=True)
        self._thread.start()

    def stop(self):
        self.log("Получена команда «Стоп»: дорабатываю текущие страницы и сохраняю прогресс…")
        self._set_reason("stopped")
        self.stop_event.set()

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    def get_stats(self):
        try:
            return self.storage.stats()
        except Exception:   # база уже закрыта — запуск завершился
            return None

    def _set_reason(self, reason):
        with self._lock:
            if self.reason is None:   # побеждает первая причина
                self.reason = reason

    # --- главный цикл ---
    def _run(self):
        st, s = self.storage, self.settings
        self.log(f"Папка запуска: {st.run_dir}")
        self.log(f"Настройки: «{s.category_name}», потоков {s.threads}, лимит {s.limit or 'нет'}, пауза {s.delay} с")
        try:
            st.reset_for_resume()
            while True:
                if st.get_meta("catalog_done") != "1":
                    self._discover()
                self._work()
                if self.reason is not None or st.get_meta("catalog_done") == "1":
                    break
        except AccessDenied as error:
            self.log(f"⛔ Доступ ограничен сайтом ({error}). Останавливаюсь, прогресс сохранён.")
            self._set_reason("blocked")
        except Stopped:
            pass
        except Exception as error:
            self.log(f"Критическая ошибка: {describe(error)}")
            self._set_reason("error")
        finally:
            self._finish()

    def _finish(self):
        st = self.storage
        reason = self.reason or "done"
        paths = []
        try:
            st.backup()
            paths = export_all(st.all_products(), st.run_dir)
            for path in paths:
                self.log(f"Файл сохранён: {path}")
        except Exception as error:
            self.log(f"Не удалось сохранить файлы: {describe(error)}")
        st.set_meta("last_reason", reason)
        stats = st.stats()
        self.log(f"Завершено ({reason}). Сохранено товаров: {stats['saved']}")
        self.log.close()
        st.close()
        self.events.put(("finished", reason, [str(p) for p in paths], stats))

    # --- этап 1: список ссылок из каталога ---
    def _discover(self):
        st, s = self.storage, self.settings
        sel = config.SEL
        filters_on = bool(s.min_price or s.max_price or s.only_in_stock or s.keyword)
        url = st.get_meta("catalog_next") or s.category_url
        number = 0
        with browser_page() as page:
            while url:
                self._load(page, url)
                number += 1
                links = page.eval_on_selector_all(sel["catalog_items"], "els => els.map(e => e.href)")
                added = st.add_urls([urldefrag(link)[0] for link in links])
                nxt = page.locator(sel["catalog_next"])
                url = urljoin(page.url, nxt.first.get_attribute("href")) if nxt.count() else None
                st.set_meta("catalog_next", url or "")
                self.log(f"Каталог, страница {number}: ссылок {len(links)}, новых {added}")
                # без фильтров хватит собрать столько ссылок, сколько лимит; с фильтрами читаем всё
                if s.limit and not filters_on and st.stats()["total"] >= s.limit:
                    break
        if not url:
            st.set_meta("catalog_done", "1")
            self.log("Каталог прочитан полностью")

    # --- этап 2: карточки товаров в потоках ---
    def _work(self):
        st, s = self.storage, self.settings
        if s.limit and st.count_products() >= s.limit:
            self._set_reason("limit")
            return
        pending = st.stats().get("pending", 0)
        if pending == 0:
            return
        count = min(s.threads, pending)
        self.log(f"Карточки: в очереди {pending}, запускаю потоков: {count}")
        threads = [threading.Thread(target=self._worker, args=(i + 1,), daemon=True) for i in range(count)]
        for thread in threads:
            thread.start()
        last_backup = time.monotonic()
        while any(thread.is_alive() for thread in threads):
            time.sleep(0.5)
            if time.monotonic() - last_backup >= config.BACKUP_EVERY_SEC:
                st.backup()
                last_backup = time.monotonic()

    def _worker(self, number):
        st = self.storage
        try:
            with browser_page() as page:
                while not self.stop_event.is_set():
                    url = st.claim_next()
                    if url is None:
                        return
                    try:
                        self._process(page, url)
                    except Stopped:
                        st.set_status(url, "pending")
                        return
                    except AccessDenied as error:
                        st.set_status(url, "pending")
                        self.log(f"⛔ Доступ ограничен сайтом ({error}). Останавливаю все потоки.")
                        self._set_reason("blocked")
                        self.stop_event.set()
                        return
                    except PageGone:
                        st.set_status(url, "gone")
                        self.log(f"Страница не найдена (404): {url}")
                    except (NetworkFailure, PlaywrightError, ValueError) as error:
                        st.set_status(url, "error")
                        self.log(f"Ошибка, товар пропущен: {url} — {describe(error)}")
        except Exception as error:
            self.log(f"Поток {number}: не удалось работать: {describe(error)}")
            self._set_reason("error")
            self.stop_event.set()

    def _process(self, page, url):
        st, s = self.storage, self.settings
        self._load(page, url)
        rec = parse_product(page, url)
        if not passes_filters(rec, s):
            st.set_status(url, "skipped")
            self.log(f"Отфильтровано: {rec['title']}")
            return
        result = st.add_product(rec, s.limit)
        if result == "limit":
            st.set_status(url, "pending")   # вернём в очередь: при большем лимите доберём
            self._set_reason("limit")
            self.stop_event.set()
            return
        if result == "duplicate":
            st.set_status(url, "duplicate")
            self.log(f"Дубликат, пропущен: {rec['title']}")
            return
        st.set_status(url, "done")
        self.log(f"Сохранено: {rec['title']} — {rec['price']} {rec['currency']}")
        if s.limit and st.count_products() >= s.limit:
            self.log("Достигнут лимит записей.")
            self._set_reason("limit")
            self.stop_event.set()

    # --- загрузка страницы: пауза, повторы, обнаружение блокировки ---
    def _load(self, page, url):
        problem = ""
        for attempt in range(1, config.RETRIES + 1):
            if self.stop_event.wait(self.settings.delay):   # пауза; True = нажали «Стоп»
                raise Stopped()
            self._count_request()
            try:
                response = page.goto(url, wait_until="domcontentloaded")
            except PlaywrightError as error:
                problem = describe(error)
                self.log(f"Сетевая ошибка (попытка {attempt}/{config.RETRIES}): {problem}")
                if self.stop_event.wait(2 * attempt):
                    raise Stopped()
                continue
            status = response.status if response else 0
            if status in config.BLOCK_STATUSES or self._looks_blocked(page):
                raise AccessDenied(f"HTTP {status}")
            if status == 404:
                raise PageGone(url)
            if status >= 400:
                problem = f"HTTP {status}"
                self.log(f"Ответ сайта {problem} (попытка {attempt}/{config.RETRIES})")
                if self.stop_event.wait(2 * attempt):
                    raise Stopped()
                continue
            return
        raise NetworkFailure(problem)

    @staticmethod
    def _looks_blocked(page):
        title = (page.title() or "").lower()
        return any(marker in title for marker in config.BLOCK_TITLE_MARKERS)

    def _count_request(self):
        if not config.SIMULATE_BLOCK_AFTER:
            return
        with self._lock:
            self._requests += 1
            blocked = self._requests > config.SIMULATE_BLOCK_AFTER
        if blocked:
            raise AccessDenied("тестовая блокировка")