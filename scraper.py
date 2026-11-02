"""Логика парсинга (Playwright + BeautifulSoup) с точным поиском по data-marker и выгрузкой в Excel."""
import queue
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import re
import random
import shutil

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import openpyxl

import config
from config import Settings


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


@contextmanager
def browser_page(stop_event=None):
    """Запускает браузер и удерживает его, пока окно открыто ИЛИ пока не принудительно остановлен процесс."""
    user_data_dir = Path("./chrome_profile")
    if user_data_dir.exists():
        # Полный список папок, где Chrome хранит куки, сессии и следы антибота
        targets = [
            "Default/Cache", 
            "Default/Code Cache", 
            "Default/GPUCache", 
            "SingletonLock",
            "Default/Network",
            "Default/Local Storage",
            "Default/Session Storage",
            "Default/Cookies",
            "Default/Network Persistent State"
        ]
        
        for subfolder in targets:
            p = user_data_dir / subfolder
            if p.exists():
                try:
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        p.unlink(missing_ok=True)
                except Exception:
                    pass
    
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--start-maximized"
            ]
        )

        try:
            page = context.new_page() if not context.pages else context.pages[0]
            page.set_default_timeout(config.PAGE_TIMEOUT_MS)
            yield page
        finally:
            print("\n⏸️ [ОТЛАДКА] Окно завершило сбор данных.")
            try:
                while len(context.pages) > 0:
                    if stop_event and stop_event.is_set():
                        break
                    time.sleep(0.5)
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass


class Runner:
    """Управляет процессом сбора данных."""

    def __init__(self, run_dir, settings=None):
        from storage import Storage
        self.storage = Storage(run_dir)
        if settings is None:
            saved = self.storage.get_meta("settings")
            if not saved:
                self.storage.close()
                raise ValueError("В этой папке нет сохранённого запуска")
            settings = Settings.from_json(saved)
        else:
            self.storage.set_meta("settings", settings.to_json())
        self.settings = settings
        self.events = queue.Queue()
        self.log = RunLog(self.storage.run_dir / "log.txt", self.events)
        self.stop_event = threading.Event()
        self.reason = None
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self.stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.stop_event.set()

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    def get_stats(self):
        return self.storage.stats() if hasattr(self.storage, 'stats') else {}

    def _run_loop(self):
        try:
            self.log(f"Папка запуска: {self.storage.run_dir}")
            self.log(f"Запуск сбора по запросу: {self.settings.category_name}")
            
            with browser_page(stop_event=self.stop_event) as page:
                self.log(f"Открытие страницы: {self.settings.category_url}")
                
                try:
                    page.goto(self.settings.category_url, wait_until="networkidle", timeout=30000)
                except Exception as e:
                    self.log(f"⚠️ Сеть не освободилась полностью, пробуем продолжить: {e}")
                
                time.sleep(random.uniform(2, 4))
                try:
                    page.mouse.wheel(0, random.randint(200, 400))
                    time.sleep(0.5)
                    page.mouse.wheel(0, -200)
                except Exception:
                    pass

                content = page.content()
                page_title = page.title()
                
                if "439" in page_title or "доступ ограничен" in content.lower() or "подтвердите, что вы не робот" in content.lower() or "квадрат" in content.lower():
                    self.log("⛔ Обнаружена защита/капча от Авито!")
                    self.log("💡 Внимание: Пройдите капчу РУКАМИ (мышкой) в открывшемся окне браузера!")
                    
                    self.reason = "blocked"
                    
                    while not self.stop_event.is_set():
                        try:
                            if not page.is_closed() and 'data-marker="item"' in page.content():
                                self.log("🎉 Вы успешно прошли капчу руками! Продолжаем сбор объявлений...")
                                content = page.content()
                                self.reason = "done"
                                break
                        except Exception:
                            break
                        time.sleep(2)
                
                if self.reason != "blocked":
                    self.log("✅ Страница успешно получена, парсим элементы...")
                    soup = BeautifulSoup(content, 'html.parser')
                    items = soup.find_all('div', {'data-marker': 'item'})
                    self.log(f"Найдено элементов на странице: {len(items)}")
                    
                    if len(items) == 0:
                        self.log("⚠️ Объявления не найдены. Если страница пустая, проверьте чистоту интернета.")
                    else:
                        limit_val = getattr(self.settings, 'limit', 100)
                        
                        for item in items:
                            try:
                                # Точный поиск названия и ссылки через data-marker="item-title"
                                title_tag = item.find('a', {'data-marker': 'item-title'})
                                
                                if title_tag:
                                    title = title_tag.get_text(strip=True) or title_tag.get('title', 'Без названия')
                                    raw_link = title_tag.get('href', '')
                                    link = "https://avito.ru" + raw_link if raw_link.startswith('/') else raw_link
                                else:
                                    # Запасной вариант
                                    title_tag_alt = item.find('h3', {'itemprop': 'name'}) or item.find('h2', {'itemprop': 'name'})
                                    title = title_tag_alt.get_text(strip=True) if title_tag_alt else "Без названия"
                                    
                                    link_tag = item.find('a', {'itemprop': 'url'}) or item.find('a', {'data-marker': 'item-title'})
                                    link = "https://avito.ru" + link_tag.get('href') if link_tag and link_tag.get('href') else "Нет ссылки"
                                
                                # Числовая цена из мета-тегов Авито
                                price_tag = item.find('meta', {'itemprop': 'price'})
                                price = float(price_tag.get('content')) if price_tag and price_tag.get('content') else 0.0
                                
                                # ID объявления (артикул)
                                item_id = item.get('data-item-id')
                                if not item_id and link != "Нет ссылки":
                                    match = re.search(r'_(\d+)$', link.split('?')[0])
                                    item_id = match.group(1) if match else str(random.randint(100000, 999999))
                                else:
                                    item_id = item_id or str(random.randint(100000, 999999))

                                rec = {
                                    "url": link,
                                    "sku": item_id,
                                    "title": title,
                                    "price": price,
                                    "currency": "RUB",
                                    "in_stock": 1,
                                    "stock_qty": 1,
                                    "category": self.settings.category_name or "MacBook",
                                    "rating": 0,
                                    "specs": {}
                                }
                                
                                status = self.storage.add_product(rec, limit=limit_val)
                                if status == "limit":
                                    self.log("Достигнут лимит сбора товаров.")
                                    self.stop()
                                    break
                                    
                            except Exception:
                                continue
                                
                    self.reason = "done"
                    
        except Exception as e:
            self.log(f"❌ Ошибка во время выполнения: {e}")
            self.reason = "error"
        finally:
            try:
                # ВЫГРУЗКА В ПОЛНОЦЕННЫЙ EXCEL (.xlsx) ФОРМАТ С ЧИСТОЙ КИРИЛЛИЦЕЙ
                all_prods = self.storage.all_products()
                if all_prods and len(all_prods) > 0:
                    timestamp_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                    xlsx_filename = f"scraped_products_{timestamp_str}.xlsx"
                    xlsx_path = self.storage.run_dir / xlsx_filename
                    
                    wb = openpyxl.Workbook()
                    ws = wb.active
                    ws.title = "Объявления"
                    
                    headers = ["Название", "Цена", "Ссылка", "Артикул", "Категория"]
                    ws.append(headers)
                    
                    for prod in all_prods:
                        sku_val = str(prod.get("sku", ""))
                        row = [
                            prod.get("title", ""),
                            prod.get("price", 0.0),
                            prod.get("url", ""),
                            sku_val,
                            prod.get("category", "")
                        ]
                        ws.append(row)
                        
                    for col in ws.columns:
                        max_len = max(len(str(cell.value or '')) for cell in col)
                        col_letter = openpyxl.utils.get_column_letter(col[0].column)
                        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)
                        
                    wb.save(xlsx_path)
                    self.log(f"📊 Таблица успешно выгружена в Excel-файл: {xlsx_path}")
                else:
                    self.log("⚠️ База данных пуста, нечего выгружать в Excel.")
                
                if hasattr(self.storage, 'save'):
                    self.storage.save()
            except Exception as e:
                self.log(f"⚠️ Ошибка при сохранении данных в Excel: {e}")

            self.log(f"Завершено ({self.reason}).")
            stats = self.storage.stats() if hasattr(self.storage, 'stats') else {}
            self.events.put(("finished", self.reason, {}, stats))
            self.log.close()


def new_run_dir(base_dir, category_name=""):
    """Создает уникальную папку для текущего запуска."""
    safe_name = re.sub(r'[^\w\-]', '_', category_name)[:30]
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    folder_name = f"{timestamp}_{safe_name}" if safe_name else timestamp
    run_dir = Path(base_dir) / "runs" / folder_name
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir

def fetch_categories(url):
    return {"Поиск Avito (MacBook)": config.ALL_PRODUCTS_URL}