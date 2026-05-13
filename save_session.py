import asyncio
import json
import os
from pyppeteer import launch
from dotenv import load_dotenv

load_dotenv()
DOMAIN = os.getenv("PROFI_DOMAIN", "tomsk.profi.ru")


async def main():
    browser = await launch(
        headless=False,
        executablePath='/usr/bin/chromium',
        args=['--no-sandbox', '--disable-setuid-sandbox']
    )
    page = await browser.newPage()
    await page.setViewport({'width': 1280, 'height': 900})
    await page.goto(f'https://{DOMAIN}/backoffice/', waitUntil='domcontentloaded')

    print(f"🌐 Opened https://{DOMAIN}/backoffice/")
    print("👉 Log in to your Profi.ru account in the browser window.")
    print("   Then come back here and press Enter.")
    input()

    cookies = await page.cookies()
    ls = await page.evaluate('''() => {
        let items = {};
        for (let i = 0; i < localStorage.length; i++) {
            let k = localStorage.key(i);
            items[k] = localStorage.getItem(k);
        }
        return items;
    }''')

    with open('session.json', 'w') as f:
        json.dump({'cookies': cookies, 'localStorage': ls}, f, indent=2)

    print("✅ Session saved to session.json")
    await browser.close()


asyncio.run(main())
