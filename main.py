"""Точка входа приложения."""
import traceback
from pathlib import Path

import config  # noqa: F401  (задаёт PLAYWRIGHT_BROWSERS_PATH до запуска окна)


def main():
    from gui import App
    App().mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # у собранного exe нет консоли, поэтому пишем причину падения в файл
        crash = Path.home() / "ProductScraper" / "crash.log"
        crash.parent.mkdir(parents=True, exist_ok=True)
        crash.write_text(traceback.format_exc(), encoding="utf-8")
        raise