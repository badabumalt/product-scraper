"""Логика парсинга (Playwright + BeautifulSoup) с надежным удержанием окна для прохождения капчи."""
import queue
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import re

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
    """Запускает браузер с постоянным профилем, в котором клики по капче работают нормально."""
    user_data_dir = Path("./chrome_profile")
    user_data_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        # Используем persistent_context вместо обычного launch()
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--window-size=1920,1080",
                "--lang=ru-RU,ru"
            ],
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            permissions=["geolocation"],
            color_scheme="light"
        )

        context.set_extra_http_headers({
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1"
        })

        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
            window.chrome = { runtime: {} };
        """)

        try:
            page = context.new_page() if not context.pages else context.pages[0]
            page.set_default_timeout(config.PAGE_TIMEOUT_MS)
            yield page
        finally:
            print("\n⏸️ [ОТЛАДКА] Браузер открыт. Теперь вы можете кликать по капче мышкой.")
            print("👉 После прохождения проверки просто закройте окно браузера крестиком.")
            
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
                    page.goto(self.settings.category_url, wait_until="domcontentloaded", timeout=20000)
                except Exception as e:
                    self.log(f"⚠️ Таймаут ожидания DOM, продолжаем: {e}")
                
                time.sleep(3)
                
                try:
                    page.evaluate("window.stop()")
                except Exception:
                    pass

                content = page.content()
                
                # Проверяем наличие капчи или защиты
                if "439" in page.title() or "доступ ограничен" in content.lower() or "подтвердите, что вы не робот" in content.lower() or "квадрат" in content.lower():
                    self.log("⛔ Обнаружена защита/капча от Авито!")
                    self.log("👉 Ищем фрейм капчи для автоматической активации клика...")
                    
                    # Пытаемся найти фрейм капчи (iframe) и кликнуть внутри него программно
                    clicked = False
                    for frame in page.frames:
                        try:
                            # Ищем элементы чекбокса или кнопки подтверждения во фреймах
                            checkbox = frame.locator("input[type='checkbox'], iframe, .Checkbox, [class*='checkbox'], [class*='captcha']")
                            if checkbox.count() > 0:
                                self.log("🎯 Найден фрейм капчи, пытаемся кликнуть...")
                                checkbox.first.click(timeout=3000)
                                clicked = True
                                break
                        except Exception:
                            continue
                    
                    if not clicked:
                        self.log("💡 Попробуйте кликнуть по капче в открытом окне вручную. Если мышь не реагирует, скрипт ждет вашего действия.")
                    
                    self.reason = "blocked"
                else:
                    self.log("✅ Страница получена, проверяем элементы...")
                    soup = BeautifulSoup(content, 'html.parser')
                    items = soup.find_all('div', {'data-marker': 'item'})
                    self.log(f"Найдено элементов на странице: {len(items)}")
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
    """Заглушка для получения категорий."""
    return {"Поиск Avito (MacBook)": config.ALL_PRODUCTS_URL}