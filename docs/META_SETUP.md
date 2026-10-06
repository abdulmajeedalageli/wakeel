# Meta / WhatsApp setup (manual steps)

These are the steps you do by hand in Meta's websites. The code can't do them for you.
The screens in Meta's dashboard change often, so if a menu name is different, look for the closest match.
Last checked against Meta's docs: **2026-09-25** (Graph API **v26.0**).

> ⚠️ **Do not move your real business number yet.** If you register a number directly on the
> Cloud API, it **stops working in the WhatsApp Business app** on your phone.
> Keeping both at the same time ("coexistence") needs a Meta partner (see "About coexistence" at the bottom).
> The bot uses **Telegram** as your console instead (docs/TELEGRAM_SETUP.md). Until you decide which number to use,
> develop with Meta's free **test number**.

---

## 1. Meta Business portfolio
1. Go to <https://business.facebook.com> and create a business portfolio for **frame** (or use your existing one).
2. Business verification (uploading your commercial registration) isn't needed for testing.
   You'll need it later for higher messaging limits and to get your display name approved.

## 2. Create the developer app
1. Go to <https://developers.facebook.com> → **My Apps** → **Create App**.
2. Choose the use case **"Connect with customers through WhatsApp"**, and link it to your business portfolio.
3. Meta automatically creates a **test WhatsApp Business Account (WABA)** and a free **test phone number**.

## 3. Collect the values for `.env`
| `.env` key | Where to find it |
|---|---|
| `WHATSAPP_PHONE_NUMBER_ID` | App Dashboard → WhatsApp → **API Setup** → "Phone number ID" (a long number, **not** the phone number itself) |
| `WHATSAPP_APP_SECRET` | App Dashboard → **App settings → Basic** → App secret → Show |
| `WHATSAPP_VERIFY_TOKEN` | Invent a long random string yourself, e.g. `frame-9f3k2...`. You'll type the same one in step 6. |
| `WHATSAPP_API_VERSION` | `v26.0` (the newest at time of writing; see <https://developers.facebook.com/docs/graph-api/changelog>) |
| `WHATSAPP_ACCESS_TOKEN` | Step 5 below |

## 4. Add yourself as a test recipient
The test number can only message numbers you've added to its allowed list (up to 5).
WhatsApp → **API Setup** → "To" → **Manage phone number list** → add your personal WhatsApp number and confirm the code.

## 5. Create a permanent access token (System User)
The token on the API Setup page expires after 24 hours. For the bot, create one that never expires:
1. <https://business.facebook.com> → **Settings** → **Users → System users** → **Add**. Pick any name (e.g. `frame-bot`) and set the role to **Admin**.
2. Select the system user → **Assign assets**:
   - **Apps** → your app → **Full control**
   - **WhatsApp accounts** → your WABA → **Full control**
3. Click **Generate token** → choose your app → **Token expiration: Never** → tick these permissions:
   - `whatsapp_business_messaging`
   - `whatsapp_business_management`
4. Copy the token into `WHATSAPP_ACCESS_TOKEN`. Meta shows it only once, so store it safely. Never share it or commit it.

## 6. Configure the webhook
Your app must be running and reachable over HTTPS first. For local testing, use the cloudflared tunnel described in the README.
1. App Dashboard → WhatsApp → **Configuration** → Webhook → **Edit**.
2. **Callback URL**: `https://<your-host>/webhook`
3. **Verify token**: the same value as `WHATSAPP_VERIFY_TOKEN`.
4. Click **Verify and save**. Meta calls `GET /webhook`, and our app answers the challenge.
5. Under **Webhook fields**, **Subscribe** to **`messages`**.

## 7. Later, for production (not needed yet)
- Add your real business number in WhatsApp Manager, get the **display name** approved, and **register** it with a 6-digit two-step PIN.
- Add a **payment method** to the WABA.
- Switch the app to **Live** mode (App Dashboard → Publish). This needs a **privacy policy URL**.
- Change the callback URL to your permanent domain (Phase 7: Caddy + DuckDNS).

---

## About coexistence (using the Business app and the bot on one number)
Checked against Meta's docs on 2026-09-25:
- It's only offered through **Embedded Signup** run by a **Tech Provider or Solution Partner**. A business can't onboard its own number from the dashboard.
- Becoming a Tech Provider yourself means:
  - **Business verification** in Meta Business Settings
  - **App review**, including two screen recordings: your app sending a WhatsApp message, and your app creating a message template
  - building an **Embedded Signup** page (Meta's "connect your WhatsApp" pop-up)

  The other route is using a Solution Partner (a paid service) that offers coexistence.
- The WhatsApp Business app must be version **2.24.17 or newer**.
- After onboarding, some app features stop working: broadcast lists, disappearing messages, view-once, live location, group chats sync, calls, and catalog/orders.
  Throughput is fixed at 20 messages per second.

**What this project does today:** the Telegram console (**Plan B**, `docs/TELEGRAM_SETUP.md`) gives you full takeover control without coexistence.
The code already handles the coexistence "echo" (`smb_message_echoes`): when you write to a customer from the Business app, that chat switches to human mode.
It stays unused until the number is onboarded with coexistence. If that happens, subscribe the app to these webhook fields:
`messages`, `smb_message_echoes`, `history`, `smb_app_state_sync`.

**Choosing the bot's number:** without coexistence, a number connected to the Cloud API can't also be used in the WhatsApp Business app.
Either give the bot a **new, dedicated number** (and keep your current one in the app), or move your current number and answer customers from Telegram.
