"""Логика парсинга (Playwright + BeautifulSoup) с надежным удержанием окна для прохождения капчи."""
import queue
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import re
import random

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

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
def browser_page():
    """Запускает браузер в абсолютно заводском режиме, полностью возвращая контроль мышке."""
    # Очищаем старые следы (папку)
    user_data_dir = Path("./chrome_profile")
    
    with sync_playwright() as pw:
        # Запуск БЕЗ единой настройки, ломающей работу Cloudflare
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--start-maximized" # Разворачивает окно через ОС, а не через скрипты
            ]
        )

        # Сюда мы БОЛЬШЕ НЕ пишем set_extra_http_headers и add_init_script. 
        # Именно они вешали скрипты кликов мыши на Авито.

        try:
            page = context.new_page() if not context.pages else context.pages[0]
            page.set_default_timeout(config.PAGE_TIMEOUT_MS)
            yield page
        finally:
            print("\n⏸️ [ОТЛАДКА] Окно удерживается. Прожмите проверку мышкой.")
            try:
                while len(context.pages) > 0:
                    time.sleep(1)
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
            
            with browser_page() as page:
                self.log(f"Открытие страницы: {self.settings.category_url}")
                
                try:
                    # Ждем networkidle (чтобы все скрипты Авито успели подгрузить блоки объявлений)
                    page.goto(self.settings.category_url, wait_until="networkidle", timeout=30000)
                except Exception as e:
                    self.log(f"⚠️ Сеть не освободилась полностью, пробуем продолжить: {e}")
                
                # Даем паузу и делаем легкий скролл для имитации живого человека
                time.sleep(random.uniform(2, 4))
                try:
                    page.mouse.wheel(0, random.randint(200, 400))
                    time.sleep(0.5)
                    page.mouse.wheel(0, -200)
                except Exception:
                    pass

                content = page.content()
                page_title = page.title()
                
                # Проверяем наличие капчи или защиты
                if "439" in page_title or "доступ ограничен" in content.lower() or "подтвердите, что вы не робот" in content.lower() or "квадрат" in content.lower():
                    self.log("⛔ Обнаружена защита/капча от Авито!")
                    self.log("💡 Внимание: Пройдите капчу РУКАМИ (мышкой) в открывшемся окне браузера!")
                    self.log("👉 Скрипт замер и ждет, пока вы кликните по кнопке проверки.")
                    
                    self.reason = "blocked"
                    
                    # Цикл удержания: ждем, пока в окне благодаря вашему ручному клику не появятся объявления
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
                    
                    self.reason = "done"
                    
        except Exception as e:
            self.log(f"❌ Ошибка во время выполнения: {e}")
            self.reason = "error"
        finally:
            try:
                if hasattr(self.storage, 'export_files'):
                    files = self.storage.export_files()
                    self.log(f"Файл сохранён: {files.get('csv')}")
                elif hasattr(self.storage, 'save'):
                    self.storage.save()
            except Exception as e:
                self.log(f"⚠️ Ошибка при сохранении данных: {e}")

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

