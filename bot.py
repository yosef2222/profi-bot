import asyncio
import json
import os
import re
from datetime import datetime
from pyppeteer import launch
from dotenv import load_dotenv

load_dotenv()

# ===== CONFIG =====
DOMAIN = os.getenv("PROFI_DOMAIN", "tomsk.profi.ru")
BOARD_URL = f"https://{DOMAIN}/backoffice/n.php"
SESSION_FILE = "session.json"
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))

MIN_PRICE = int(os.getenv("MIN_PRICE", "800"))
MAX_PRICE = int(os.getenv("MAX_PRICE", "2000"))
DEFAULT_PRICE = int(os.getenv("DEFAULT_PRICE", "1200"))
COMMISSION_MULTIPLIER = float(os.getenv("COMMISSION_MULTIPLIER", "3.0"))

ARABIC_MESSAGE = os.getenv("ARABIC_MESSAGE", "")
ENGLISH_MESSAGE = os.getenv("ENGLISH_MESSAGE", "")

if not ARABIC_MESSAGE or not ENGLISH_MESSAGE:
    raise ValueError("MESSAGE vars not set in .env")

SKIP_PATTERNS = [
    r"бартер",
    r"обмен(?!\s*опытом)",
    r"школ",
    r"перевод(чик)?\b(?!\s*язык)",
    r"ваканси",
]

PROCESSED_ORDERS = set()
_intercepted_items = []
_data_ready = asyncio.Event()


def escape_js(s):
    return s.replace('\\', '\\\\').replace("'", "\\'").replace('"', '\\"').replace('\n', '\\n')


def should_skip(text) -> tuple:
    if not isinstance(text, str):
        text = str(text or '')
    for pattern in SKIP_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return True, f"'{match.group()}'"
    return False, ""


def extract_commission(page_text):
    """Extract commission — ONLY from 'Комиссия' keyword."""
    m = re.search(r'[Кк]омисс[ия]+\s*:?\s*(\d+)\s*[₽р]', page_text)
    if m:
        return int(m.group(1))
    return 0


def extract_budget(page_text):
    """Extract client budget."""
    # Replace narrow non-breaking spaces with regular spaces
    page_text = page_text.replace('\u202f', ' ').replace('\u00a0', ' ')
    
    # Range: "650–1100 ₽"
    m = re.search(r'(\d+)\s*[–—-]\s*(\d+)\s*[₽р]', page_text)
    if m:
        return int(m.group(2))
    
    # "до 950 ₽" or "с 1000 ₽" or "от 800 ₽" or just "1500 ₽"
    m = re.search(r'(?:до|с|от)?\s*(\d+)\s*[₽р]', page_text)
    if m:
        return int(m.group(1))
    
    return 0


def calculate_price(budget_value, commission_value):
    """Bid within client budget. If commission is very high, bid at top of budget."""
    if not budget_value:
        return DEFAULT_PRICE
    
    # If commission is more than 2x the budget (expensive order), bid at budget max
    if commission_value > budget_value * 2:
        return min(budget_value, MAX_PRICE)
    
    # Otherwise bid slightly under budget to be competitive
    return max(budget_value - 200, MIN_PRICE)


def determine_category(text):
    """Determine if Arabic or English. Default to English."""
    text_lower = text.lower()
    # Arabic keywords
    if any(w in text_lower for w in ['арабск', 'араб', 'arabic']):
        return 'arabic'
    # Everything else → English
    return 'english'

def setup_interception(page):
    async def handle_response(response):
        if 'graphql' not in response.url:
            return
        try:
            data = await response.json()
            items = data.get('data', {}).get('boSearchBoardItems', {}).get('items', [])
            if items:
                global _intercepted_items
                _intercepted_items = items
                _data_ready.set()
        except Exception:
            pass

    page.on('response', lambda resp: asyncio.ensure_future(handle_response(resp)))


async def wait_for_board_data(timeout=20):
    _data_ready.clear()
    try:
        await asyncio.wait_for(_data_ready.wait(), timeout=timeout)
        return _intercepted_items
    except asyncio.TimeoutError:
        return _intercepted_items if _intercepted_items else []


async def apply_to_order(page, order_id, price, message):
    print(f"    🖱️  Opening order...")
    order_url = f"https://{DOMAIN}/backoffice/n.php?o={order_id}"
    await page.goto(order_url, waitUntil='domcontentloaded', timeout=60000)
    await asyncio.sleep(5)

    # Get page text and extract commission + budget
    page_text = await page.evaluate('() => document.body.innerText')
    
    # Check for vacancy on the page
    if re.search(r'ваканси', page_text, re.IGNORECASE):
        print(f"    ⏭️  SKIP: Vacancy detected on page")
        return False
    
    commission = extract_commission(page_text)
    budget_value = extract_budget(page_text)
    
    print(f"    💸 Commission: {commission}₽ | Budget: {budget_value}₽" if commission else f"    💸 Commission: ? | Budget: {budget_value}₽")
    
    # Recalculate price with actual data
    price = calculate_price(budget_value, commission)
    print(f"    💰 Final price: {price}₽ (3×{commission} = {commission * 3})")

    # Click "Продолжить"
    clicked = await page.evaluate('''() => {
        const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let n;
        while (n = w.nextNode()) {
            if (n.textContent.includes('Продолжить')) {
                let el = n.parentElement;
                while (el && el.tagName !== 'BUTTON' && el.tagName !== 'A') el = el.parentElement;
                if (el) { el.click(); return 'button'; }
                n.parentElement.click(); return 'parent';
            }
        }
        return null;
    }''')
    if not clicked:
        print(f"    ⚠️  Продолжить not found, coordinate click...")
        await page.mouse.click(600, 185)
    print(f"    ✅ Clicked continue")
    await asyncio.sleep(4)

    # Fill textarea
    filled = await page.evaluate('''(msg) => {
        for (const ta of document.querySelectorAll('textarea')) {
            if (ta.offsetParent !== null) {
                const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
                setter.call(ta, msg);
                ta.dispatchEvent(new Event('input', {bubbles: true}));
                ta.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }
        }
        return false;
    }''', message)
    print(f"    ✍️  Message: {filled}")
    if not filled:
        await page.screenshot({'path': f'debug_textarea_{order_id}.png'})
        return False
    await asyncio.sleep(1)

    # Fill price
    for inp in await page.querySelectorAll('input'):
        try:
            visible = await page.evaluate('(el) => el.offsetParent !== null', inp)
            if visible:
                await inp.click({'clickCount': 3})
                await inp.type(str(price))
                print(f"    💰 Price typed: {price}₽")
                break
        except:
            pass
    await asyncio.sleep(1)

    # Submit
    submitted = await page.evaluate('''() => {
        const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let n;
        while (n = w.nextNode()) {
            if (/Отправить|отправить/.test(n.textContent)) {
                let el = n.parentElement;
                while (el && el.tagName !== 'BUTTON' && el.tagName !== 'A') el = el.parentElement;
                if (el && el.offsetParent !== null) { el.click(); return n.textContent.trim(); }
            }
        }
        return null;
    }''')
    if not submitted:
        btns = await page.querySelectorAll('button')
        for b in reversed(btns):
            visible = await page.evaluate('(el) => el.offsetParent !== null', b)
            if visible:
                txt = await page.evaluate('(el) => el.innerText', b)
                await b.click()
                submitted = txt
                break
    if submitted:
        print(f"    🚀 Submitted: {submitted[:60]}")
        await asyncio.sleep(4)
        print(f"    🎉 Applied!")
        return True
    print(f"    ❌ Submit failed")
    await page.screenshot({'path': f'debug_submit_{order_id}.png'})
    return False


async def check_board(page, scan_num):
    print(f"\n{'='*60}")
    print(f"🔍 SCAN #{scan_num} | {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*60}")

    await page.goto(BOARD_URL, waitUntil='domcontentloaded', timeout=30000)
    items = await wait_for_board_data(timeout=15)

    if not items:
        print("  📭 No items")
        return 0

    orders = [i for i in items if i.get('type') == 'SNIPPET' and i.get('id') and i.get('title')]
    new_orders = [o for o in orders if o['id'] not in PROCESSED_ORDERS]
    print(f"  📋 {len(orders)} orders, {len(new_orders)} new")

    if not new_orders:
        return 0

    applied = 0
    for order in new_orders:
        order_id = order['id']
        PROCESSED_ORDERS.add(order_id)

        title = order.get('title', '')
        desc = order.get('description', '') or ''
        price_data = order.get('price', {}) or {}
        budget = price_data.get('value', '') if price_data else ''
        if budget:
            # Clean: remove special spaces and ₽
            budget_clean = str(budget).replace('\u202f', '').replace('\u00a0', '').replace('₽', '').strip()
            budget_str = f"{budget_clean} ₽"
        else:
            budget_str = "?"
            budget_clean = ""

        schedule = order.get('schedule', '') or ''

        full_text = f"{title} {desc} {schedule}"
        print(f"\n  🔍 [{order_id}] {title[:80]} | Budget: {budget_str}")

        skip, reason = should_skip(full_text)
        if skip:
            print(f"    ⏭️  SKIP: {reason}")
            continue

        cat = determine_category(full_text)
        msg = ARABIC_MESSAGE if cat == 'arabic' else ENGLISH_MESSAGE
        
        # Initial price estimate (will be recalculated on order page)
        budget_value = 0
        if budget_clean:
            # Handle ranges like "700–1300" or "700-1300"
            numbers = re.findall(r'(\d+)', budget_clean)
            if numbers:
                budget_value = int(numbers[-1])  # Take the high end of the range

        price = calculate_price(budget_value, 0)
        print(f"    📝 {cat}, est. price: {price}₽/hr")

        if await apply_to_order(page, order_id, price, msg):
            applied += 1

        await asyncio.sleep(4)

    return applied


async def main():
    browser = await launch(
        headless=False,
        executablePath='/usr/bin/chromium',
        args=['--no-sandbox', '--disable-setuid-sandbox']
    )
    page = await browser.newPage()
    await page.setViewport({'width': 1280, 'height': 900})

    try:
        with open(SESSION_FILE, 'r') as f:
            saved = json.load(f)
    except FileNotFoundError:
        print(f"❌ {SESSION_FILE} not found.")
        await browser.close()
        return

    # Clean cookies before setting — remove problematic fields
    clean_cookies = []
    for cookie in saved['cookies']:
        clean = {
            'name': cookie.get('name', ''),
            'value': cookie.get('value', ''),
            'domain': cookie.get('domain', ''),
            'path': cookie.get('path', '/'),
        }
        # Only add optional fields if present and valid
        if cookie.get('expires') and cookie['expires'] > 0:
            clean['expires'] = cookie['expires']
        if cookie.get('httpOnly'):
            clean['httpOnly'] = cookie['httpOnly']
        if cookie.get('secure'):
            clean['secure'] = cookie['secure']
        if cookie.get('sameSite') and cookie['sameSite'] in ('Strict', 'Lax', 'None'):
            clean['sameSite'] = cookie['sameSite']
        clean_cookies.append(clean)
    
    await page.setCookie(*clean_cookies)
    setup_interception(page)

    print("🔄 Loading Profi.ru...")
    await page.goto(BOARD_URL, waitUntil='domcontentloaded', timeout=60000)
    await wait_for_board_data(timeout=15)

    if saved.get('localStorage'):
        for k, v in saved['localStorage'].items():
            await page.evaluate(f'localStorage.setItem("{escape_js(str(k))}", "{escape_js(str(v))}");')

    # Verify login
    profile_text = await page.evaluate('() => document.body.innerText')
    if 'Вход' in profile_text or 'Авторизация' in profile_text:
        print(f"❌ Session expired. Please re-run save_session.py")
        await browser.close()
        return

    print(f"✅ Bot ready — Ctrl+C to stop\n")
    # ... rest of main

    scan_num = 0
    total_applied = 0

    try:
        while True:
            scan_num += 1
            applied = await check_board(page, scan_num)
            total_applied += applied
            if applied > 0:
                print(f"\n  📊 Total applied: {total_applied}")

            cookies = await page.cookies()
            ls = await page.evaluate('''() => {
                let items = {};
                for (let i = 0; i < localStorage.length; i++) {
                    let k = localStorage.key(i); items[k] = localStorage.getItem(k);
                }
                return items;
            }''')
            with open(SESSION_FILE, 'w') as f:
                json.dump({'cookies': cookies, 'localStorage': ls}, f, indent=2)

            print(f"  ⏳ Next check in {CHECK_INTERVAL}s...")
            await asyncio.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        print(f"\n\n🛑 Stopped. Total applied: {total_applied}")
    finally:
        cookies = await page.cookies()
        ls = await page.evaluate('''() => {
            let items = {};
            for (let i = 0; i < localStorage.length; i++) {
                let k = localStorage.key(i); items[k] = localStorage.getItem(k);
            }
            return items;
        }''')
        with open(SESSION_FILE, 'w') as f:
            json.dump({'cookies': cookies, 'localStorage': ls}, f, indent=2)
        print("✅ Session saved.")
        await browser.close()


asyncio.run(main())