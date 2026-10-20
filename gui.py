"""Окно приложения (Tkinter)."""
import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import config
from config import Settings
from scraper import Runner, fetch_categories, new_run_dir

REASONS = {
    "done": "Готово: все товары обработаны.",
    "limit": "Готово: достигнут лимит записей.",
    "stopped": "Остановлено. Прогресс сохранён — можно нажать «Продолжить…».",
    "blocked": "Сайт ограничил доступ. Прогресс сохранён. Подождите и нажмите «Продолжить…».",
    "error": "Остановлено из-за ошибки. Прогресс сохранён, подробности — в журнале.",
}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(config.APP_NAME)
        self.geometry("900x760")
        self.minsize(820, 640)

        self.runner = None
        self.run_dir = None
        self.cur_limit = 0
        self.started_at = 0.0
        self.categories = {"Все товары": config.ALL_PRODUCTS_URL}
        self.bg_events = queue.Queue()   # сообщения от фоновых потоков окну

        self.var_out = tk.StringVar(value=str(config.DEFAULT_OUTPUT_DIR))
        self.var_cat = tk.StringVar(value="Все товары")
        self.var_threads = tk.StringVar(value="2")
        self.var_limit = tk.StringVar(value="20")
        self.var_delay = tk.StringVar(value="1.0")
        self.var_min = tk.StringVar(value="")
        self.var_max = tk.StringVar(value="")
        self.var_kw = tk.StringVar(value="")
        self.var_stock = tk.BooleanVar(value=False)

        self._build()
        self._load_categories()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(400, self._poll)

    # ---------- построение окна ----------
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        box = ttk.LabelFrame(self, text="Настройки")
        box.pack(fill="x", padx=10, pady=8)
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="Папка результатов:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(box, textvariable=self.var_out).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(box, text="Обзор…", command=self._pick_folder).grid(row=0, column=2, **pad)

        ttk.Label(box, text="Категория:").grid(row=1, column=0, sticky="w", **pad)
        self.cmb = ttk.Combobox(box, textvariable=self.var_cat, state="readonly", values=["Все товары"])
        self.cmb.grid(row=1, column=1, sticky="ew", **pad)
        self.btn_cats = ttk.Button(box, text="Обновить список", command=self._load_categories)
        self.btn_cats.grid(row=1, column=2, **pad)

        nums = ttk.Frame(box)
        nums.grid(row=2, column=0, columnspan=3, sticky="w")
        self._spin(nums, "Потоков (1–5):", self.var_threads, 1, 5, 1, 0)
        self._spin(nums, "Лимит записей (0 = без лимита):", self.var_limit, 0, 100000, 10, 1)
        self._spin(nums, "Пауза между запросами, сек:", self.var_delay, 0, 10, 0.5, 2)

        flt = ttk.Frame(box)
        flt.grid(row=3, column=0, columnspan=3, sticky="w")
        ttk.Label(flt, text="Цена, £ — от:").grid(row=0, column=0, padx=(8, 2), pady=4)
        ttk.Entry(flt, textvariable=self.var_min, width=8).grid(row=0, column=1, pady=4)
        ttk.Label(flt, text="до:").grid(row=0, column=2, padx=(8, 2), pady=4)
        ttk.Entry(flt, textvariable=self.var_max, width=8).grid(row=0, column=3, pady=4)
        ttk.Label(flt, text="Название содержит:").grid(row=0, column=4, padx=(16, 2), pady=4)
        ttk.Entry(flt, textvariable=self.var_kw, width=20).grid(row=0, column=5, pady=4)
        ttk.Checkbutton(flt, text="Только в наличии", variable=self.var_stock).grid(row=0, column=6, padx=16)

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", padx=10)
        self.btn_start = ttk.Button(buttons, text="▶ Запустить", command=self._start)
        self.btn_start.pack(side="left", padx=(0, 6))
        self.btn_resume = ttk.Button(buttons, text="⟳ Продолжить…", command=self._resume)
        self.btn_resume.pack(side="left", padx=6)
        self.btn_stop = ttk.Button(buttons, text="■ Остановить", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(buttons, text="Открыть папку", command=self._open_folder).pack(side="right")

        self.lbl_status = ttk.Label(self, text="Готов к запуску", font=("Segoe UI", 10, "bold"), wraplength=860)
        self.lbl_status.pack(fill="x", padx=10, pady=(10, 2))
        self.progress = ttk.Progressbar(self, maximum=100)
        self.progress.pack(fill="x", padx=10, pady=2)
        self.lbl_stats = ttk.Label(self, text="", wraplength=860)
        self.lbl_stats.pack(fill="x", padx=10, pady=2)

        frame = ttk.LabelFrame(self, text="Журнал событий")
        frame.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        self.txt = tk.Text(frame, height=14, wrap="word", state="disabled", font=("Consolas", 9))
        scroll = ttk.Scrollbar(frame, command=self.txt.yview)
        self.txt.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)

    def _spin(self, parent, text, var, low, high, step, column):
        ttk.Label(parent, text=text).grid(row=0, column=column * 2, padx=(8, 2), pady=4)
        ttk.Spinbox(parent, from_=low, to=high, increment=step, width=7, textvariable=var).grid(
            row=0, column=column * 2 + 1, padx=(0, 8), pady=4
        )

    # ---------- настройки ----------
    @staticmethod
    def _number(text, name, integer=False):
        text = text.strip().replace(",", ".")
        if not text:
            return 0
        try:
            value = float(text)
        except ValueError:
            raise ValueError(f"Поле «{name}» должно быть числом")
        if value < 0:
            raise ValueError(f"Поле «{name}» не может быть отрицательным")
        return int(value) if integer else value

    def _read_settings(self):
        threads = self._number(self.var_threads.get(), "Потоков", integer=True)
        if not 1 <= threads <= 5:
            raise ValueError("Потоков должно быть от 1 до 5")
        low = self._number(self.var_min.get(), "Цена от")
        high = self._number(self.var_max.get(), "Цена до")
        if high and low > high:
            raise ValueError("«Цена от» больше «Цена до»")
        name = self.var_cat.get()
        return Settings(
            category_name=name,
            category_url=self.categories.get(name, config.ALL_PRODUCTS_URL),
            threads=threads,
            limit=self._number(self.var_limit.get(), "Лимит записей", integer=True),
            delay=self._number(self.var_delay.get(), "Пауза"),
            min_price=low,
            max_price=high,
            only_in_stock=self.var_stock.get(),
            keyword=self.var_kw.get().strip(),
        )

    def _fill_settings(self, s):
        self.categories.setdefault(s.category_name, s.category_url)
        self.var_cat.set(s.category_name)
        self.var_threads.set(str(s.threads))
        self.var_limit.set(str(s.limit))
        self.var_delay.set(str(s.delay))
        self.var_min.set(str(s.min_price) if s.min_price else "")
        self.var_max.set(str(s.max_price) if s.max_price else "")
        self.var_kw.set(s.keyword)
        self.var_stock.set(s.only_in_stock)

    # ---------- категории ----------
    def _load_categories(self):
        self.btn_cats.configure(state="disabled", text="Загружаю…")

        def work():
            try:
                self.bg_events.put(("categories", fetch_categories(), None))
            except Exception as error:
                self.bg_events.put(("categories", None, str(error).strip().splitlines()[0] if str(error).strip() else repr(error)))

        threading.Thread(target=work, daemon=True).start()

    def _categories_loaded(self, cats, error):
        self.btn_cats.configure(state="normal", text="Обновить список")
        if error:
            self._append_log(f"Не удалось загрузить категории: {error}")
            self._append_log("Проверьте интернет. При запуске из исходников — шпаргалка, п. 7.")
            return
        self.categories = dict(cats)
        self.cmb.configure(values=list(self.categories))
        if self.var_cat.get() not in self.categories:
            self.var_cat.set("Все товары")
        self._append_log(f"Категорий загружено: {len(cats) - 1}")

    # ---------- кнопки ----------
    def _busy(self):
        return self.runner is not None

    def _start(self):
        if self._busy():
            return
        try:
            settings = self._read_settings()
            runner = Runner(new_run_dir(Path(self.var_out.get()), settings.category_name), settings)
        except ValueError as error:
            messagebox.showerror(config.APP_NAME, str(error))
            return
        except OSError as error:
            messagebox.showerror(config.APP_NAME, f"Не удалось создать папку запуска:\n{error}")
            return
        self._launch(runner)

    def _resume(self):
        if self._busy():
            return
        runs = Path(self.var_out.get()) / "runs"
        folder = filedialog.askdirectory(
            title="Выберите папку запуска (в ней лежит progress.db)",
            initialdir=str(runs if runs.exists() else Path(self.var_out.get())),
        )
        if not folder:
            return
        if not (Path(folder) / "progress.db").exists():
            messagebox.showerror(config.APP_NAME, "В этой папке нет файла progress.db")
            return
        try:
            runner = Runner(Path(folder))
        except Exception as error:
            messagebox.showerror(config.APP_NAME, f"Не удалось открыть запуск:\n{error}")
            return
        self._fill_settings(runner.settings)
        self._launch(runner)

    def _stop(self):
        if self.runner is not None:
            self.runner.stop()
            self.btn_stop.configure(state="disabled")
            self.lbl_status.configure(text="Останавливаю… доделываю текущие страницы и сохраняю прогресс")

    def _open_folder(self):
        folder = Path(self.run_dir or self.var_out.get())
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)

    def _pick_folder(self):
        folder = filedialog.askdirectory(initialdir=self.var_out.get())
        if folder:
            self.var_out.set(folder)

    def _launch(self, runner):
        self.runner = runner
        self.run_dir = runner.storage.run_dir
        self.cur_limit = runner.settings.limit
        self.started_at = time.time()
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")
        self.progress["value"] = 0
        self._set_running(True)
        self.lbl_status.configure(text=f"Работает… Папка запуска: {self.run_dir}")
        runner.start()

    def _set_running(self, running):
        idle = "disabled" if running else "normal"
        self.btn_start.configure(state=idle)
        self.btn_resume.configure(state=idle)
        self.btn_stop.configure(state="normal" if running else "disabled")

    # ---------- опрос фоновых потоков ----------
    def _poll(self):
        try:
            while True:
                kind, *data = self.bg_events.get_nowait()
                if kind == "categories":
                    self._categories_loaded(*data)
        except queue.Empty:
            pass

        if self.runner is not None:
            finished = None
            try:
                while True:
                    event = self.runner.events.get_nowait()
                    if event[0] == "log":
                        self._append_log(event[1])
                    elif event[0] == "finished":
                        finished = event
            except queue.Empty:
                pass
            if finished:
                self._on_finished(finished)
            else:
                self._show_stats(self.runner.get_stats())
        self.after(400, self._poll)

    def _on_finished(self, event):
        _, reason, _paths, stats = event
        self._show_stats(stats)
        self.runner = None
        self._set_running(False)
        self.lbl_status.configure(text=REASONS.get(reason, reason))
        self._append_log(f"Результаты: {self.run_dir}")
        if reason == "blocked":
            messagebox.showwarning(config.APP_NAME, REASONS["blocked"])

    def _show_stats(self, st):
        if not st:
            return
        total = st.get("total", 0)
        pending = st.get("pending", 0) + st.get("in_progress", 0)
        processed = total - pending
        saved = st.get("saved", 0)
        if self.cur_limit:
            self.progress["value"] = min(saved, self.cur_limit) / self.cur_limit * 100
        else:
            self.progress["value"] = processed / total * 100 if total else 0
        seconds = int(time.time() - self.started_at)
        self.lbl_stats.configure(text=(
            f"Найдено ссылок: {total} · обработано: {processed} · сохранено: {saved} · "
            f"отфильтровано: {st.get('skipped', 0)} · дубликатов: {st.get('duplicate', 0)} · "
            f"ошибок: {st.get('error', 0) + st.get('gone', 0)} · прошло: {seconds // 60}:{seconds % 60:02d}"
        ))

    def _append_log(self, line):
        self.txt.configure(state="normal")
        self.txt.insert("end", line + "\n")
        if int(self.txt.index("end-1c").split(".")[0]) > 2000:
            self.txt.delete("1.0", "200.0")   # держим в окне не больше ~2000 строк
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _on_close(self):
        if self.runner is not None and self.runner.is_running():
            if not messagebox.askyesno(config.APP_NAME, "Идёт сбор. Остановить и закрыть? Прогресс сохранится."):
                return
            self.runner.stop()
            self.runner.join(30)
        self.destroy()