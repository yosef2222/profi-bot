import asyncio
import json
import os
import re
from datetime import datetime
from pyppeteer import launch
import httpx
from dotenv import load_dotenv

load_dotenv()

# ===== CONFIG (from .env) =====
DOMAIN = os.getenv("PROFI_DOMAIN", "tomsk.profi.ru")
API_URL = f"https://{DOMAIN}/backoffice/api/"
BOARD_URL = f"https://{DOMAIN}/backoffice/n.php"
SESSION_FILE = "session.json"
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
DEFAULT_PRICE_ENGLISH = int(os.getenv("DEFAULT_PRICE_ENGLISH", "1000"))
DEFAULT_PRICE_ARABIC = int(os.getenv("DEFAULT_PRICE_ARABIC", "1500"))

ARABIC_MESSAGE = os.getenv("ARABIC_MESSAGE", "")
ENGLISH_MESSAGE = os.getenv("ENGLISH_MESSAGE", "")

if not OPENROUTER_KEY:
    raise ValueError("OPENROUTER_KEY is not set in .env")
if not ARABIC_MESSAGE or not ENGLISH_MESSAGE:
    raise ValueError("ARABIC_MESSAGE and ENGLISH_MESSAGE must be set in .env")

# ===== FILTERS =====
SKIP_PATTERNS = [
    r"бартер", r"обмен(?!\s*опытом)", r"взамен", r"вместо оплаты",
    r"перевод(чик)?\b(?!\s*язык)", r"письменный перевод",
    r"разово", r"один раз", r"1 раз", r"одно занятие", r"1 занятие",
    r"2 заняти", r"два заняти", r"на пару раз", r"несколько занятий",
    r"ваканси", r"возможно.*ваканси", r"корпоративн",
    r"приглашает.*сотрудничеств", r"приглашает.*преподавател",
    r"сотрудничеств", r"в штат", r"ищем преподавател",
    r"на постоянную работу", r"оформление по тк",
    r"трудовой договор", r"резюме", r"CV",
    r"онлайн[-\s]?школ", r"языковая\s+школа", r"образовательный\s+центр",
    r"лингва\s+центр", r"учебный\s+центр",
    r"повышение\s+успеваемости",
    r"подтянуть\s+(по\s+)?(школьной\s+)?программе",
    r"школьная\s+программа",
    r"помощь\s+с\s+(домашним|школьным)",
    r"\b([1-8])\s*(класс|класса|классу|классе|классов)\b",
    r"школьник", r"ученик\s+(начальной|средней)\s*школы",
    r"\bЕГЭ\b", r"\bОГЭ\b", r"подготовка\s+к\s+(ЕГЭ|ОГЭ|экзамен)",
    r"государственный\s+экзамен", r"выпускной\s+экзамен",
    r"поступление\s+в\s+(вуз|университет|институт)",
    r"не успеваете откликаться",
]

KEEP_PATTERNS = [
    r"\b[1-6]\s*(курс|курса|курсу|курсе)\b",
    r"(студент|студентка)\s+(вуза|университета|института|колледжа)",
    r"университет", r"институт", r"академия",
    r"взрослый", r"для\s+себя", r"для\s+работы", r"деловой\s+английский",
    r"разговорный", r"для\s+путешествий", r"бизнес",
]

PROCESSED_ORDERS = set()
_intercepted_items = []
_data_ready = asyncio.Event()


def escape_js(s):
    return s.replace('\\', '\\\\').replace("'", "\\'").replace('"', '\\"').replace('\n', '\\n')


def should_skip_local(text) -> tuple:
    if not isinstance(text, str):
        text = str(text or '')
    for pattern in KEEP_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            school_exam = [r"\bЕГЭ\b", r"\bОГЭ\b", r"школьная\s+программа",
                           r"\b([1-8])\s*(класс|класса|классу|классе)\b"]
            for sp in school_exam:
                if re.search(sp, text, re.IGNORECASE):
                    return True, f"School exam: '{sp}'"
            return False, ""
    for pattern in SKIP_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return True, f"'{match.group()}'"
    return False, ""


async def ask_ai(order_text: str) -> dict:
    prompt = f"""Analyze this Profi.ru order for a language tutor. Respond ONLY with valid JSON:
{{"apply": true/false, "category": "arabic"|"english"|null, "price": number, "reason": "brief Russian reason"}}

SKIP (apply: false) if ANY of these:
- Вакансия, найм, сотрудничество, резюме, корпоративное обучение
- Повышение успеваемости, школьная программа, помощь с домашним заданием
- Ученик младше 9 класса (1-8 класс)
- Бартер, перевод, разовое занятие, онлайн-школа

APPLY (apply: true) if:
- Взрослый, студент вуза, для себя, для работы, путешествий, эмиграции
- 9-11 класс is OK (старшеклассник готовится сам, не повышение успеваемости)
- Разговорный, деловой, IELTS, общение

CATEGORY: Arabic order → "arabic", English order → "english"

PRICE RULES (integer, per hour):
- Default: english={DEFAULT_PRICE_ENGLISH}, arabic={DEFAULT_PRICE_ARABIC}
- Adjust based on client budget and commission shown in order
- Commission is what you pay Profi.ru — price must be at least 3x commission
- If budget is low (under 600), set price to match budget max but never below 3x commission
- If budget is high (over 1500), you may increase price up to budget max
- Never set price below 500

ORDER: {order_text}"""

    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{AI_MODEL}:generateContent?key={OPENROUTER_KEY}",
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.3, "maxOutputTokens": 300}
                }
            )
            data = resp.json()
            if "error" in data:
                print(f"    ⚠️  AI raw error: {data['error']}")
                return {"apply": False, "category": None, "price": 0, "reason": "AI error"}
            content = data["candidates"][0]["content"]["parts"][0]["text"] or ""
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                result = json.loads(json_match.group())
                result['reason'] = result.get('reason') or ''
                return result
            return {"apply": False, "category": None, "price": 0, "reason": "parse error"}
    except Exception as e:
        return {"apply": False, "category": None, "price": 0, "reason": str(e)}


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
    await asyncio.sleep(6)

    # Step 1: Click "Продолжить" via TreeWalker text search
    clicked = await page.evaluate('''() => {
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let node;
        while (node = walker.nextNode()) {
            if (node.textContent.includes('Продолжить')) {
                let el = node.parentElement;
                while (el && el.tagName !== 'BUTTON' && el.tagName !== 'A') {
                    el = el.parentElement;
                }
                if (el) { el.click(); return 'found: ' + el.tagName; }
                node.parentElement.click();
                return 'clicked parent: ' + node.parentElement.tagName;
            }
        }
        return null;
    }''')

    if not clicked:
        print(f"    ⚠️  Продолжить not found by text, trying coordinate...")
        await page.mouse.click(600, 185)
        clicked = 'coordinate'

    print(f"    ✅ Clicked: {clicked}")
    await asyncio.sleep(8)

    # Step 2: Fill textarea using React's native prototype setter
    filled = await page.evaluate('''(msg) => {
        for (const ta of document.querySelectorAll('textarea')) {
            if (ta.offsetParent !== null) {
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value'
                ).set;
                setter.call(ta, msg);
                ta.dispatchEvent(new Event('input', {bubbles: true}));
                ta.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }
        }
        return false;
    }''', message)
    print(f"    ✍️  Message filled: {filled}")

    if not filled:
        await page.screenshot({'path': f'debug_textarea_{order_id}.png'})
        return False

    await asyncio.sleep(1)

    # Step 3: Fill price input with real keypresses (React needs this)
    price_input = await page.querySelector('input[placeholder*="ена"], input[placeholder*="цен"], input[placeholder*="Цена"]')
    if price_input:
        await price_input.click({'clickCount': 3})
        await price_input.type(str(price))
        print(f"    💰 Price typed: {price}")
    else:
        inputs = await page.querySelectorAll('input')
        for inp in inputs:
            visible = await page.evaluate('(el) => el.offsetParent !== null', inp)
            if visible:
                await inp.click({'clickCount': 3})
                await inp.type(str(price))
                print(f"    💰 Price typed (fallback): {price}")
                break
    await asyncio.sleep(1)

    # Step 4: Click "Отправить сообщение"
    submitted = await page.evaluate('''() => {
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let node;
        while (node = walker.nextNode()) {
            const t = node.textContent.trim();
            if (t.includes('Отправить') || t.includes('отправить')) {
                let el = node.parentElement;
                while (el && el.tagName !== 'BUTTON' && el.tagName !== 'A') {
                    el = el.parentElement;
                }
                if (el && el.offsetParent !== null) {
                    el.click();
                    return t;
                }
            }
        }
        return null;
    }''')

    if not submitted:
        print(f"    ⚠️  Отправить not found by text, clicking last visible button...")
        submitted = await page.evaluate('''() => {
            const visible = Array.from(document.querySelectorAll('button')).filter(b => b.offsetParent !== null);
            if (visible.length > 0) {
                const last = visible[visible.length - 1];
                last.click();
                return 'last-button: ' + (last.innerText || last.textContent || '').trim();
            }
            return null;
        }''')

    if submitted:
        print(f"    🚀 Submitted: {submitted}")
        await asyncio.sleep(4)
        print(f"    🎉 Applied!")
        return True
    else:
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
        budget = f"{price_data.get('value', '?')} {price_data.get('suffix', '₽')}" if price_data else "?"
        commission = price_data.get('commission', '') if price_data else ''
        schedule = order.get('schedule', '') or ''
        geo = order.get('geo', {}) or {}
        address = ''
        for loc in ['orderLocation', 'clientMayCome', 'remote']:
            if geo.get(loc) and geo[loc].get('address'):
                address = geo[loc]['address']
                break

        full_text = f"Title: {title or ''}\nDescription: {desc or ''}\nBudget: {budget or ''}\nCommission: {str(commission) or ''}\nSchedule: {schedule or ''}\nAddress: {address or ''}"
        print(f"\n  🔍 [{order_id}] {title[:80]}")

        skip, reason = should_skip_local(full_text)
        if skip:
            print(f"    ⏭️  SKIP (local): {reason}")
            continue

        print(f"    🤖 AI...", end=" ", flush=True)
        ai = await ask_ai(full_text)
        if ai.get('reason') in ('AI error', 'parse error') or not ai:
            print(f"⚠️  AI failed, applying with defaults")
            is_arabic = 'араб' in full_text.lower()
            ai = {
                "apply": True,
                "category": "arabic" if is_arabic else "english",
                "price": DEFAULT_PRICE_ARABIC if is_arabic else DEFAULT_PRICE_ENGLISH
            }
        if not ai.get('apply', False):
            print(f"⏭️  SKIP (AI): {ai.get('reason') or 'no reason'}")
            continue

        category = ai.get('category', 'english')
        price = ai.get('price', DEFAULT_PRICE_ENGLISH)
        message = ARABIC_MESSAGE if category == 'arabic' else ENGLISH_MESSAGE
        print(f"✅ {category}, {price}₽/hr")

        success = await apply_to_order(page, order_id, price, message)
        if success:
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
        print(f"❌ {SESSION_FILE} not found. Run save_session.py first.")
        await browser.close()
        return

    await page.setCookie(*saved['cookies'])
    setup_interception(page)

    print("🔄 Loading Profi.ru...")
    await page.goto(BOARD_URL, waitUntil='domcontentloaded', timeout=60000)
    await wait_for_board_data(timeout=15)

    if saved.get('localStorage'):
        for key, value in saved['localStorage'].items():
            await page.evaluate(f'localStorage.setItem("{escape_js(str(key))}", "{escape_js(str(value))}");')

    print(f"✅ Bot ready — Ctrl+C to stop\n")

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
                    let k = localStorage.key(i);
                    items[k] = localStorage.getItem(k);
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
                let k = localStorage.key(i);
                items[k] = localStorage.getItem(k);
            }
            return items;
        }''')
        with open(SESSION_FILE, 'w') as f:
            json.dump({'cookies': cookies, 'localStorage': ls}, f, indent=2)
        print("✅ Session saved.")
        await browser.close()


try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
