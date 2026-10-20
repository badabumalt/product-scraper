"""Временная проверка ядра без окна: новый запуск сам «нажмёт Стоп» через 8 секунд."""
import queue
import sys
import time
from pathlib import Path

import config
from config import Settings
from scraper import Runner, new_run_dir

AUTO_STOP_AFTER_SEC = 8


def main():
    if len(sys.argv) > 1:   # python cli_test.py <папка запуска> — продолжить
        runner = Runner(Path(sys.argv[1]))
        auto_stop = False
    else:
        settings = Settings(limit=30, threads=2, delay=0.5)
        runner = Runner(new_run_dir(config.DEFAULT_OUTPUT_DIR, settings.category_name), settings)
        auto_stop = True
    runner.start()
    started = time.monotonic()
    while True:
        if auto_stop and time.monotonic() - started >= AUTO_STOP_AFTER_SEC:
            runner.stop()
            auto_stop = False
        try:
            event = runner.events.get(timeout=0.5)
        except queue.Empty:
            continue
        if event[0] == "log":
            print(event[1])
        elif event[0] == "finished":
            print("ИТОГ:", event[1], event[3])
            for path in event[2]:
                print("Файл:", path)
            break


if __name__ == "__main__":
    main()