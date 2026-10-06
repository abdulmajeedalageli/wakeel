# frame — WhatsApp AI assistant

An automated WhatsApp assistant for **frame** (real estate, Saudi Arabia).
Customers message the business number, and the assistant answers from the property sheet until the owner takes over the chat.

> Work in progress, built in phases:

| Phase | What | Status |
|---|---|---|
| 0 | Project setup, config, docs | ✅ |
| 1 | Webhook: verification, signature check | ✅ |
| 2 | Database & chat state | ✅ |
| 3 | Property sheet | ✅ |
| 4 | AI agent (Gemini) | ✅ |
| 5 | Human takeover (Telegram console) | ✅ |
| 6 | Leads & alerts | ✅ |
| 7 | Deployment (Docker; Railway, or a VM with Caddy) | ✅ |
| 8 | Hardening | ⏳ |

---

## 1. First-time setup (Windows)

Requires Python 3.12 or newer.

```bash
py -3.14 -m venv .venv
```
```bash
.venv\Scripts\activate
```
```bash
pip install -r requirements-dev.txt
```

Then create a file named `.env` in the project folder for your settings (one `NAME=value` per line).
[docs/META_SETUP.md](docs/META_SETUP.md) explains the WhatsApp values; the other guides in `docs/` explain the rest.
Every setting, with its default, is listed in `app/config.py`.
Never commit `.env`.

## 2. Run the tests

```bash
pytest
```

The tests don't need internet access or real keys, because Meta and the other services are mocked.

## 3. Run the app locally

```bash
uvicorn app.main:app --reload
```

Open <http://localhost:8000/health>. You should see `"status":"ok"`, `"db":"ok"`, and a `"sheet"` section
(`not_configured` until you connect the sheet, then `ok` with the number of properties loaded),
and `"ai"` (`configured` once `GEMINI_API_KEY` is set).

The database is created automatically at `data/bot.db` (set by `DB_PATH`).

## 4. Test with real WhatsApp messages (temporary tunnel)

Meta needs a public HTTPS URL to send webhooks to. For testing, a free **cloudflared quick tunnel** exposes your local app. It doesn't need an account.

1. Install cloudflared once:
   ```bash
   winget install --id Cloudflare.cloudflared
   ```
2. Keep the app running (step 3). In a **second terminal**, run:
   ```bash
   cloudflared tunnel --url http://localhost:8000
   ```
   It prints a URL like `https://random-words.trycloudflare.com`.
3. In Meta: App Dashboard → WhatsApp → **Configuration** → Webhook → Edit.
   - Callback URL: `https://random-words.trycloudflare.com/webhook`
   - Verify token: your `WHATSAPP_VERIFY_TOKEN`
   - Click **Verify and save**, then **Subscribe** to the `messages` field.
4. From your phone (added as a test recipient), send any text to the **test number**.
   You should see "typing…" and then get the AI agent's reply (or the polite fallback reply if `GEMINI_API_KEY` isn't set).
   The app's log shows your number masked, like `9665****1234`.

The quick-tunnel URL changes every time you restart cloudflared, so update it in Meta each time.
Phase 7 replaces the tunnel with a permanent domain.

## 5. Connect the property sheet

Follow [docs/GOOGLE_SETUP.md](docs/GOOGLE_SETUP.md) (service account, share the sheet, `.env`). Then check it:

```bash
python -m scripts.try_search
```
Try some searches the way the agent will:
```bash
python -m scripts.try_search --district النرجس --deal sale
```
```bash
python -m scripts.try_search --id P-1023
```

The app reloads the sheet on startup and every `SHEET_REFRESH_MINUTES`. If Google can't be reached, it keeps using the last good copy.
Rows with problems (no ID, invalid price, duplicate ID) are skipped, and the log gives their row number so you can fix them.

**What you can edit without touching code** (restart the app afterwards):
- `config/sheet.json`: which header in your sheet is which field, and Arabic or synonym values (شقة → apartment, متاح → available).
- `config/locations.json`: district and city spellings (Arabic, English, common typos).

## 6. The AI agent (Gemini)

Setup: [docs/GEMINI_SETUP.md](docs/GEMINI_SETUP.md) explains how to get a key. The free tier is fine for testing, but **turn on billing before real customers** (privacy).

**Try it in the terminal, without WhatsApp:**
```bash
python -m scripts.chat_cli --sample
```
`--sample` uses the sample sheet; leave it out to use your Google Sheet. Then work through
[docs/TEST_CONVERSATIONS.md](docs/TEST_CONVERSATIONS.md) (16 conversations in Arabic and English).

How it works:
- For each customer text, the agent reads the chat's last 20 messages and may search the sheet (max 5 tool rounds).
- It answers **only** from the sheet, and never negotiates or makes promises.
- If anything fails, the customer gets a polite fallback reply ("the team will get back to you shortly").
- Messages from the same customer are answered one at a time, in order.
- **`internal_notes` never reaches the AI.** The code only sends an approved list of fields (`app/agent_tools.py`).

**What you can edit without touching code** (restart afterwards):
- `prompts/system.md`: the agent's instructions (tone, greeting, rules).
- `app/replies.py`: the fixed replies (fallback, "please type your question"). Strictly this is a code file, but only the quoted texts need changing.

All replies to customers are in Arabic, even when a customer writes in English, and everything the bot sends you on Telegram is Arabic too.

## 7. Taking over a chat (Telegram console)

Set up your console once: [docs/TELEGRAM_SETUP.md](docs/TELEGRAM_SETUP.md) (about 5 minutes).

- **`/human 9665XXXXXXXX`**: you take over that chat. The bot goes silent **in that chat only** and keeps answering everyone else.
  The customer's new messages come to your Telegram.
- **One topic per customer:** in a Telegram group with Topics (`TELEGRAM_GROUP_ID`), or in your chat with the bot (topics on in @BotFather). See [docs/TELEGRAM_SETUP.md](docs/TELEGRAM_SETUP.md) §4.
- **Listings from your Excel file:** send the .xlsx in the group's General topic and the bot loads it (§5a).
- **Privacy rule:** the bot never tells customers a price, a person's name or a phone number. It's enforced in code, and price questions send you a 🔔 alert.
- **Your team:** employees in the group can answer customers too. Add one with a Reply to their message: `/staff إضافة <name>` (§5b).
  Each customer's messages and the bot's replies appear live in their own thread. **Type inside the thread** to answer them on WhatsApp.
- Without threads: **Reply** to a forwarded message in Telegram, and the customer gets your text on WhatsApp.
  Answering also takes over the chat, so the bot never talks over you.
- **Photos and files both ways** (up to 20 MB): customers' photos, voice notes, files and locations arrive in Telegram.
  A photo or PDF you send in their thread (or as a Reply) reaches them on WhatsApp. See [docs/TELEGRAM_SETUP.md](docs/TELEGRAM_SETUP.md) §6.
- **`/bot 9665XXXXXXXX`**: the bot answers that chat again. This also happens automatically after `HUMAN_TIMEOUT_HOURS`
  (default 24) without a message from you, and Telegram tells you.
- **`/pause`** / **`/resume`**: silence the bot everywhere (it's remembered across restarts). While paused, messages come to Telegram.
- **`/status`**: shows the pause state, the mode, and the chats you've taken over.
- **Alerts:** the bot sends you a 🔔 alert (with an Arabic summary and the saved lead) when a customer wants a viewing,
  wants to negotiate, is ready, asks something the sheet can't answer, asks for a person, or is upset. Reply to an alert to answer them.
  If the bot itself fails, you get a ⚠️ alert.
- **Leads:** the bot saves what customers tell it (name, budget, needs, timeline). See them with `/leads` and `/lead 9665…`.
- **Delete a customer's data** (PDPL request): `/forget 9665…` (asks you to confirm), or `python -m scripts.forget_customer 9665…`.
- **Working hours:** set `AVAILABILITY_MODE=schedule` and `WORK_HOURS=09:00-17:00` (Riyadh time, every day).
  The bot then answers only outside those hours, and during them messages come to you.

Without Telegram configured, takeover still works (e.g. via `python -m scripts.chat_status human 9665XXXXXXXX`),
but messages meant for you are only written to the log.

Why Telegram, not the WhatsApp Business app? Using the app on the same number ("coexistence") requires a Meta partner.
See the end of [docs/META_SETUP.md](docs/META_SETUP.md). The code is already prepared for it.

## 8. Deploying to the server

- **Railway (in use):** [docs/DEPLOY_RAILWAY.md](docs/DEPLOY_RAILWAY.md). There's no server to maintain; HTTPS is included, and it redeploys on every push to `main`.
  It includes the live end-to-end test checklist. Settings are generated by `python -m scripts.env_for_railway`, and public checks run with `python -m scripts.smoke_test <url>`.
- **Your own Linux VM** (Google Cloud or any VPS): [docs/DEPLOY.md](docs/DEPLOY.md), with Docker compose, Caddy for automatic HTTPS on a free DuckDNS domain, daily backups and a free uptime monitor.

- **What runs:** `docker compose up -d --build` starts two containers, the bot and Caddy. Both restart by themselves after a crash or reboot.
- **Updating later:** `git pull && docker compose up -d --build` on the server.
- **Public paths:** only `/webhook` and `/health` are reachable from the internet.
- **Backups:** daily in `data/backups/` (kept 14 days). Download them to your PC weekly (see the guide).

## 9. Project layout

```
app/
  main.py           FastAPI app: /webhook, /health, background processing
  agent.py          the AI agent: Gemini tool-use loop, history, fallback
  agent_tools.py    the agent's 2 tools + the privacy allowlist
  takeover.py       who answers now? (human mode, pause, work hours, timeout)
  owner_console.py  your Telegram console: commands, replies, forwards
  telegram.py       small Telegram Bot API client
  backup.py         daily database backups (python -m app.backup)
  config.py         settings loaded from .env
  security.py       checks Meta's X-Hub-Signature-256
  webhook.py        turns Meta's webhook JSON into simple events
  whatsapp.py       sends messages via the Graph API
  db.py             SQLite storage: chats, messages, leads, dedupe
  sheet.py          reads + validates the Google Sheet
  properties.py     in-memory property list, refresh, search, details
  locations.py      district/city name matching
  normalize.py      text/number clean-up (Arabic digits, "950,000", ...)
  replies.py        fixed reply texts (edit freely)
  logging_setup.py  logging + phone masking
scripts/
  chat_status.py    admin: switch a chat between bot and human mode
  try_search.py     admin: load the sheet and try searches
  chat_cli.py       chat with the agent in the terminal (no WhatsApp); shows leads and alerts
  forget_customer.py  admin: delete everything stored about one customer
  env_for_railway.py  makes the variables file to paste into Railway (from .env)
  smoke_test.py     public checks of a deployed bot (python -m scripts.smoke_test <url>)
prompts/
  system.md         the agent's instructions (edit freely)
config/
  sheet.json        column mapping + value synonyms (edit freely)
  locations.json    district/city spellings (edit freely)
tests/              automated tests (+ sample payloads in tests/fixtures/)
docs/META_SETUP.md  manual Meta setup
docs/GOOGLE_SETUP.md  manual Google Sheets setup
docs/GEMINI_SETUP.md  Gemini key, free vs paid, cost, changing the model
docs/TELEGRAM_SETUP.md  your Telegram console
docs/DEPLOY_RAILWAY.md  deploying on Railway + the live test checklist
docs/DEPLOY.md      deploying on your own Linux VM (Google Cloud / VPS)
railway.json        Railway settings (Dockerfile build, /health check, always restart)
Dockerfile, docker-compose.yml, deploy/Caddyfile   the server setup (see docs/DEPLOY.md)
docs/TEST_CONVERSATIONS.md  16 conversations to test the agent
test_data/          sample property sheet (hypothetical data)
```
