# Deploying the bot to Railway (free trial) + live test

Railway runs the bot's Docker image for you. There's no server to maintain: HTTPS and a web address are included, and every push to `main` redeploys automatically.
Checked against Railway's docs on 2026-09-26. Railway's screens change sometimes, so look for the closest matching button.

**The free trial:** $5 of credit, 30 days, no card needed. That's plenty for testing.
After that, the Hobby plan is $5/month (card required; see the end of this guide).

---

## 1. Create the account, and check you have the *Full* trial
1. Go to <https://railway.com>, click **Login**, and choose **GitHub** (the account that has the code).
2. In your account/usage page, check that the trial says **Full Trial**, or has full network access.
   ⚠️ If it says **Limited Trial**, outgoing network access is restricted and **the bot can't reach WhatsApp, Gemini, Telegram or Google**.
   Railway grants the full trial based on your GitHub account's age and activity. If it's limited, contact Railway support or use your own server instead (docs/DEPLOY.md).

## 2. Deploy the code from GitHub
1. **New Project → Deploy from GitHub repo.**
2. When asked, allow Railway's GitHub app to access your repository (it's private, so Railway needs permission).
3. Pick your repository, branch **`main`**. Railway finds `Dockerfile` and `railway.json` by itself and starts a first build.
   The first deploy will fail its health check until the settings (step 5) are in; that's expected.

## 3. Region
Service → **Settings → Region**: choose **EU West (Amsterdam)**, the closest to Saudi Arabia.
Customer data will be stored there, outside Saudi Arabia (a PDPL cross-border consideration).

## 4. Add a volume (where the database lives)
In the project canvas: **+ New → Volume** (or right-click the service → **Attach volume**). Attach it to the bot service with mount path:
```
/app/data
```
Without a volume, every redeploy would erase the database: chats, leads, the pause state.

## 5. Settings (variables)
On your PC, in the project folder:
```
.venv\Scripts\python -m scripts.env_for_railway
```
It writes `railway-variables.env`, which is git-ignored and contains all your secrets. It prints only the variable names.
- **Converted from `.env`:** comments are removed, and your Google key file becomes one `GOOGLE_SERVICE_ACCOUNT_JSON` line.
- **Added:** `DB_PATH=/app/data/bot.db` and `RAILWAY_RUN_UID=0`. The second one is needed because Railway volumes belong to the root user.

Then:
1. Open `railway-variables.env` in Notepad and copy everything.
2. Railway → the bot service → **Variables → Raw Editor**. Paste, then click **Update Variables** (Railway redeploys).
3. **Delete `railway-variables.env`** from your PC.

If Railway complains about the `GOOGLE_SERVICE_ACCOUNT_JSON` line, delete that line from the Raw Editor. Instead add it with **New Variable**: name `GOOGLE_SERVICE_ACCOUNT_JSON`, and as the value paste the whole content of `secrets\service-account.json`.

## 6. Public address
Service → **Settings → Networking → Generate Domain**.
If Railway asks for a port, use the one shown in the deploy log line `Uvicorn running on http://0.0.0.0:<port>`. The app listens on the `PORT` Railway gives it.
You get an address like `https://frame-agent-production.up.railway.app`.

## 7. Only one copy of the bot
**Stop the bot on your PC** (any `uvicorn` window) before testing. Two copies would fight over Telegram messages.

## 8. First checks
1. In your browser, `https://<your-app>.up.railway.app/health` should show `"status":"ok"` and `"sheet": {"status": "ok", "properties": 33, …}`.
2. Run the automatic public checks from your PC:
   ```
   python -m scripts.smoke_test https://<your-app>.up.railway.app
   ```
   All 10 checks should PASS.

## 9. Point Meta at Railway
Meta App Dashboard → WhatsApp → **Configuration** → Webhook → **Edit**:
- **Callback URL:** `https://<your-app>.up.railway.app/webhook`
- **Verify token:** your `WHATSAPP_VERIFY_TOKEN`

Click **Verify and save**, and make sure **`messages`** is subscribed.

---

## 10. Live end-to-end test (tick each one)
Use your phone (a test recipient of the Meta test number) and Telegram. The conversations are from `docs/TEST_CONVERSATIONS.md`.

| # | Test | What should happen | ✓ |
|---|---|---|---|
| 1 | Meta **Verify and save** | Accepted | |
| 2 | WhatsApp: `السلام عليكم` | A short greeting as frame's assistant, then one question | |
| 3 | `أبي شقة للإيجار في النرجس` | P-1030, 62,000 SAR **per year**, license + photos link (the sheet and AI work) | |
| 4 | Conversation #16 (name, budget, timeline over 3 messages), then Telegram `/leads` and `/lead 9665…` | The lead is saved with those details | |
| 5 | `Can I see P-1007 tomorrow at 5pm?` | No time promised; 🔔 **طلب معاينة** alert in Telegram | |
| 6 | In Telegram, **Reply** to that alert with a message | It arrives on your WhatsApp; Telegram shows ✅ انرسلت | |
| 7 | Send another WhatsApp message | **No AI reply**; it's forwarded to Telegram (the chat is yours now) | |
| 8 | Telegram `/status`, then `/bot 9665…`, then WhatsApp again | Status lists your chat; after `/bot` the AI answers again | |
| 9 | `/pause`, a WhatsApp message, then `/resume` | While paused: forwarded, no AI reply. After resume: the AI answers | |
| 10 | Send a photo **with a caption**, then a voice note and a PDF | Each arrives in Telegram (the caption shows, the voice note plays, the PDF keeps its name); you get "وصلتني الصورة…"-style replies | |
| 10b | In Telegram, send a photo with a caption, then a PDF, in the customer's thread (or as a Reply) | Both arrive on your WhatsApp; Telegram shows ✅ انرسلت | |
| 10c | Write to the bot **in English** (e.g. `Hi, any villas in Yasmin?`) | The reply is in Arabic, with property details in Arabic | |
| 11 | Railway → **Redeploy**, then `/leads` | The lead and the chat history are still there (the volume works) | |
| 12 | `/forget 9665…`, then `/forget 9665… تأكيد`, then `السلام عليكم` | Data deleted; the bot greets you as a new chat | |
| 13 | Railway → **Deployments → View logs** | `Database backup saved`, no errors, and phone numbers shown masked like `9665****1234` | |

If anything fails, copy the relevant log lines (Railway → View logs) and send them to me.

---

## If WhatsApp messages get no reply (what we learned on 2026-09-27)
Check these in order:
1. **Meta's "Test" button** (WhatsApp → Configuration → Webhook fields → `messages` → **Test**).
   - Railway's Deploy Logs should show `Ignoring webhook for a different phone_number_id`. That's expected, because Meta's test uses a fake number, and it proves Meta can reach the bot and the app secret is right.
   - `Rejected webhook: invalid signature` means `WHATSAPP_APP_SECRET` is wrong.
2. **The app must be *Published*.** While it's unpublished, Meta delivers **only** dashboard test events, never real messages.
   - Publishing needs an app icon, a category, a Privacy Policy URL (`https://<your-app>.up.railway.app/privacy`) and a data deletion URL (`…/privacy#delete`), all in **App settings → Basic**.
3. **The WhatsApp Business Account must be linked ("subscribed") to the app.** If tests arrive but real messages don't, that's the cause.
   - Open **Graph API Explorer**, choose your app, and give the token the permissions `whatsapp_business_management` and `whatsapp_business_messaging`.
   - `GET <WABA_ID>/subscribed_apps`: if it returns `"data": []`, send `POST <WABA_ID>/subscribed_apps` and expect `{"success": true}`.
   - The WABA ID is on the API Setup page (and it's the `entry.id` in webhook payloads). **Do this again when you add your real number**, if it's on a new WhatsApp Business Account.
4. **`409 Conflict … only one bot instance`** in the logs: two copies are polling Telegram. Once, during a redeploy, is normal. If it repeats, stop the copy on your PC.

## Everyday use
- **Update the bot:** push to `main` on GitHub, and Railway rebuilds and redeploys by itself. A redeploy causes a few seconds of downtime; WhatsApp retries, and duplicates are skipped.
- **Logs:** service → **Deployments → View logs**.
- **Change a setting:** **Variables**, then edit the value. Railway redeploys.
- **Restart:** Deployments → ⋮ → **Restart** (or **Redeploy**).

## When the trial ends (important)
- The trial lasts **30 days or $5 of usage**, whichever comes first. Then the bot stops.
- **Trial volumes are deleted 30 days after the credit runs out**, and that's the database: chats, leads and backups.
- To keep going: upgrade to **Hobby, $5/month** (card required). It includes $5 of usage. This bot uses roughly 0.25 GB of memory and very little CPU, estimated at **$3–4/month**, so normally you pay just the $5.
- The free plan ($1/month of credit, 0.5 GB of memory) is **not enough** for an always-on bot.
- If you move elsewhere instead, export the database first, before the trial ends.
