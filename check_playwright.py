import os

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"

from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("https://books.toscrape.com/", wait_until="domcontentloaded")
    print("Заголовок страницы:", page.title())
    count = page.locator("div.side_categories ul.nav-list > li > ul > li > a").count()
    print("Категорий в меню:", count)
    browser.close()