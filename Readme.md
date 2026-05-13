# Profi.ru Auto-Apply Bot

A bot that monitors the [Profi.ru](https://profi.ru) order board and automatically applies to relevant language tutoring orders (English and Arabic) using AI-based filtering.

---

## How It Works

1. **Session login** — the bot loads a saved browser session (`session.json`) so it doesn't need to log in manually every time.
2. **Board monitoring** — every N seconds, the bot navigates to the Profi.ru backoffice board and intercepts the GraphQL response the React app makes, extracting the list of new orders.
3. **Local filtering** — orders are first filtered by regex patterns to immediately skip obvious mismatches: vacancies, school kids (under grade 9), one-time jobs, barter, translation requests, online schools, etc.
4. **AI filtering** — orders that pass local filters are sent to an AI (via OpenRouter) which decides whether to apply, which category (arabic/english), and what price to set based on the client's budget and Profi.ru's commission.
5. **Auto-apply** — for approved orders, the bot opens the order page, clicks "Продолжить", fills in the message and price using React-compatible input simulation, and submits.
6. **Session saving** — cookies and localStorage are saved after every scan to keep the session alive.

---

## Requirements

- Python 3.10+
- Chromium browser installed (`/usr/bin/chromium` or adjust path in `bot.py`)
- A Profi.ru specialist account
- An [OpenRouter](https://openrouter.ai) API key (free tier works)

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/yourusername/profi-bot.git
cd profi-bot
```

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install pyppeteer httpx python-dotenv
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | Description |
|---|---|
| `PROFI_DOMAIN` | Your city subdomain, e.g. `tomsk.profi.ru` |
| `OPENROUTER_KEY` | Your OpenRouter API key |
| `AI_MODEL` | OpenRouter model ID (default: `baidu/cobuddy:free`) |
| `ARABIC_MESSAGE` | Your Arabic tutor intro message (max 500 chars) |
| `ENGLISH_MESSAGE` | Your English tutor intro message (max 500 chars) |
| `CHECK_INTERVAL` | Seconds between board scans (default: `60`) |
| `DEFAULT_PRICE_ENGLISH` | Default hourly price for English orders (default: `1000`) |
| `DEFAULT_PRICE_ARABIC` | Default hourly price for Arabic orders (default: `1500`) |

### 4. Generate a session file

You need to log in to Profi.ru manually once and save the session. Run this script:

```bash
python save_session.py
```

This opens a Chromium window. Log in to your Profi.ru account, then press Enter in the terminal. The session is saved to `session.json`.

```python
# save_session.py
import asyncio, json
from pyppeteer import launch

async def main():
    browser = await launch(headless=False, executablePath='/usr/bin/chromium',
                           args=['--no-sandbox'])
    page = await browser.newPage()
    await page.goto('https://profi.ru/login')
    input('Log in manually, then press Enter...')
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
    print('Session saved.')
    await browser.close()

asyncio.run(main())
```

### 5. Run the bot

```bash
python bot.py
```

---

## Filtering Logic

### Always skip
- Vacancies, hiring, employment contracts
- Corporate training requests
- Students below grade 9 (grades 1–8)
- ЕГЭ/ОГЭ exam prep
- Barter / exchange deals
- Translation requests
- One-time lessons
- Online schools / language centers

### Always apply
- Adults learning for themselves, work, travel, or emigration
- University students (years 1–6)
- Conversational / business / IELTS goals
- Grade 9–11 students (self-motivated, not school tutoring)

### AI decides
- Ambiguous cases are passed to the AI with full order text, budget, and commission info
- The AI also sets the price based on budget constraints and commission rules

### Price rules
- Default: English = 1000 ₽/hr, Arabic = 1500 ₽/hr
- Price is adjusted by AI based on client budget
- Price is never set below 1/3 of the Profi.ru commission
- Price is never set below 500 ₽

---

## File Structure

```
profi-bot/
├── bot.py              # main bot
├── save_session.py     # one-time session saver
├── .env.example        # config template
├── .env                # your config (git-ignored)
├── session.json        # saved browser session (git-ignored)
└── README.md
```

---

## Notes

- The bot runs with a visible browser window (`headless=False`) so you can see what it's doing.
- Debug screenshots are saved as `debug_*.png` when something goes wrong (git-ignored).
- The session expires periodically — re-run `save_session.py` if the bot stops working.
- Profi.ru uses persisted GraphQL queries with server-side hash validation, so the bot intercepts the browser's own network responses instead of making its own GraphQL calls.

---

## License

MIT
