"""Хранилище прогресса на SQLite: очередь ссылок, собранные товары, резервные копии."""
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS queue (url TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    sku TEXT UNIQUE,
    title TEXT NOT NULL,
    price REAL,
    currency TEXT,
    in_stock INTEGER,
    stock_qty INTEGER,
    category TEXT,
    rating INTEGER,
    specs TEXT,
    scraped_at TEXT
);
"""
# Статусы ссылок в очереди: pending (ждёт), in_progress (в работе), done (сохранён),
# skipped (не прошёл фильтр), duplicate (дубликат), error (ошибка), gone (404).


class Storage:
    def __init__(self, run_dir):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()   # одна база на все потоки: пускаем по одному
        self.conn = sqlite3.connect(self.run_dir / "progress.db", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- метаданные (настройки, место в каталоге) ---
    def get_meta(self, key, default=None):
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))
            self.conn.commit()

    # --- очередь ссылок ---
    def add_urls(self, urls):
        """Добавляет только новые ссылки, возвращает сколько добавлено."""
        with self.lock:
            before = self.conn.total_changes
            self.conn.executemany("INSERT OR IGNORE INTO queue(url) VALUES (?)", [(u,) for u in urls])
            self.conn.commit()
            return self.conn.total_changes - before

    def claim_next(self):
        with self.lock:
            row = self.conn.execute(
                "SELECT url FROM queue WHERE status='pending' ORDER BY rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self.conn.execute("UPDATE queue SET status='in_progress' WHERE url=?", (row["url"],))
            self.conn.commit()
            return row["url"]

    def set_status(self, url, status):
        with self.lock:
            self.conn.execute("UPDATE queue SET status=? WHERE url=?", (status, url))
            self.conn.commit()

    def reset_for_resume(self):
        """При старте/продолжении: недоделанное и ошибочное снова в очередь."""
        with self.lock:
            self.conn.execute("UPDATE queue SET status='pending' WHERE status IN ('in_progress', 'error')")
            self.conn.commit()

    # --- товары ---
    def count_products(self):
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]

    def add_product(self, rec, limit):
        """Возвращает 'saved', 'duplicate' или 'limit'. Дубликат = тот же url или тот же артикул (UPC)."""
        with self.lock:
            if limit:
                count = self.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
                if count >= limit:
                    return "limit"
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO products
                   (url, sku, title, price, currency, in_stock, stock_qty, category, rating, specs, scraped_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (rec["url"], rec["sku"], rec["title"], rec["price"], rec["currency"],
                 int(rec["in_stock"]), rec["stock_qty"], rec["category"], rec["rating"],
                 json.dumps(rec["specs"], ensure_ascii=False),
                 datetime.now().isoformat(timespec="seconds")),
            )
            self.conn.commit()
            return "saved" if cur.rowcount == 1 else "duplicate"

    def all_products(self):
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM products ORDER BY id")]

    # --- статистика, копии, закрытие ---
    def stats(self):
        with self.lock:
            rows = self.conn.execute("SELECT status, COUNT(*) AS c FROM queue GROUP BY status").fetchall()
            saved = self.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        result = {row["status"]: row["c"] for row in rows}
        result["total"] = sum(result.values())
        result["saved"] = saved
        return result

    def backup(self):
        """Копия базы в backup/ (встроенный механизм SQLite, безопасен во время работы). Храним 5 последних."""
        folder = self.run_dir / "backup"
        folder.mkdir(exist_ok=True)
        target = folder / f"progress_{datetime.now():%Y%m%d_%H%M%S}.db"
        with self.lock:
            dst = sqlite3.connect(target)
            self.conn.backup(dst)
            dst.close()
        for old in sorted(folder.glob("progress_*.db"))[:-5]:
            old.unlink(missing_ok=True)
        return target

    def close(self):
        with self.lock:
            self.conn.close()