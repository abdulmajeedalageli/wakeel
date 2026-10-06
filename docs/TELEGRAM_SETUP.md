# Telegram console setup

Telegram is your control panel for the bot. From it you can:
- **take over** a chat, so the bot goes silent in that chat only
- **answer customers** with Telegram's *Reply*; they receive your text on WhatsApp
- see messages from chats you've taken over, or while the bot is paused
- pause and resume the bot everywhere

It works with long polling: the app asks Telegram for your messages, so no extra public URL or setup is needed on the server.

## 1. Create your bot (2 minutes)
1. In Telegram, open **@BotFather** (the official one, with a blue tick).
2. Send `/newbot`, then choose a name (e.g. *frame console*) and a username ending in `bot` (e.g. `frame_console_bot`).
3. BotFather replies with a **token** like `123456789:AAH...`. Copy it into `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456789:AAH...
   ```
   Treat it like a password: anyone with the token can control the bot.
4. Open your new bot in Telegram and press **Start**. Bots can't message you until you've done this.

## 2. Find your Telegram user ID
The bot only obeys **one** Telegram account: yours. To find your numeric ID without using third-party bots:
1. Start the app (`uvicorn app.main:app`) with only `TELEGRAM_BOT_TOKEN` set.
   It won't be fully "configured" yet, so set a temporary value `TELEGRAM_OWNER_ID=1` for this step.
2. Send anything to your bot.
3. The app log shows: `Ignored Telegram message from user id 987654321 (not TELEGRAM_OWNER_ID)`.
4. Put that number in `.env`, then restart the app:
   ```
   TELEGRAM_OWNER_ID=987654321
   ```
Messages from anyone else are ignored and logged, and so are messages from group chats.

## 3. Check it works
- `/health` should show `"telegram": "configured"`.
- Send `/status` to your bot. It replies with the pause state, the mode, and the chats you've taken over.
- The command menu (the `/` button) lists all commands.

## 4. One topic per customer (recommended)
Without this, every customer's messages arrive mixed together in one chat. With it, each customer gets their **own topic**, listed like separate chats:
```
Your console (a group, or your chat with the bot)
├── General              ← /status, /leads, /pause …
├── 🙋 خالد · 9665…1234    ← everything about Khalid (you're handling him)
└── 🤖 سارة · 9665…5678    ← the bot is handling her
```
Telegram doesn't let a bot create a separate chat for each customer, so topics are the closest thing. There are two ways.

**Option A (recommended): a Telegram group with Topics.** Your staff can join later.
1. In Telegram, create a **new group** (e.g. "frame عملاء") and add your bot to it.
2. Open the group's settings, choose **Edit**, and turn on **Topics**.
3. Group settings → **Administrators** → add the bot as an admin with **Manage topics** and **Delete messages**.
4. In the group, send `/chatid`. The bot answers with a line like `TELEGRAM_GROUP_ID=-100…`.
   Turning Topics on changes the group's ID, so do this **after** step 2.
5. Railway → Variables: add `TELEGRAM_GROUP_ID` with that number, including the minus sign. Then **Deploy**.
6. In the group, send `/status`. It should say **"🧵 موضوع لكل عميل: مفعّل (في المجموعة…)"**.

Customers start with fresh topics in the group. The old threads stay in your chat with the bot as history.
- **Only you can answer customers or run commands.** Messages from other group members are ignored, so they can read along but not reply.
- **Privacy (PDPL):** everyone in the group can read customer conversations, so add only people you trust.

**Option B: threads in your private chat with the bot.**
1. Open **@BotFather**, then tap **Open** (the Mini App button) or send `/mybots`.
2. Choose your bot, then **Bot Settings**.
3. Turn on **Topics (threads) in private chats**. The wording may differ slightly.
4. In your chat with the bot, send `/status`. It should say **"🧵 موضوع لكل عميل: مفعّل"**. No restart needed.

**How it works:**
- **New threads:** a customer's first message creates their thread. Its title shows 🤖 (the bot answers) or 🙋 (you took over), plus their name once they give it, and their number.
- **Live copy:** every message and bot reply is copied there as **👤 customer / 🤖 bot**, so you can read along. Too many notifications? Set `TELEGRAM_LIVE_MIRROR=false` in Railway. Alerts and chats you've taken over still go to threads.
- **To answer: just type inside the customer's thread.** It goes to them on WhatsApp, and the bot stops answering that customer (🙋).
- **Commands inside a thread need no number:** `/human`, `/bot`, `/lead`, `/forget`.
- **`/forget` also deletes the customer's thread** with all its messages.
- **If you delete a thread by hand,** the bot makes a new one the next time that customer writes.
- If topics are off, everything works as before: one chat, and you answer with Reply.

## 5. Daily use
Everything the bot sends you is in Arabic. Command names stay in Latin letters (`/status`, `/bot`…) because Telegram requires it.

| You want to… | Do this |
|---|---|
| See what's going on | `/status` |
| Take over a customer | `/human 9665XXXXXXXX`: you get their last messages. **Reply** to that message to answer them. |
| Answer a customer | **Type inside their thread** (with threads on), or **Reply** (swipe left, or long-press → Reply) to one of their messages. |
| Give the chat back to the bot | `/bot 9665XXXXXXXX` (or wait `HUMAN_TIMEOUT_HOURS`) |
| Silence the bot everywhere | `/pause` (and `/resume` later). This is remembered even if the app restarts. |
| See leads | `/leads` (the latest 10) or `/lead 9665XXXXXXXX` (one customer, all details) |
| Delete a customer's data (PDPL request) | `/forget 9665XXXXXXXX`, then the confirmation it asks for: `/forget 9665XXXXXXXX تأكيد` |

- **Number formats:** `966512345678`, `+966 51 234 5678` and `0512345678` all work, including Arabic digits.
- **Replying takes over:** when you Reply to a customer, that chat is in human mode from then on. The bot won't talk over you.
  After `HUMAN_TIMEOUT_HOURS` (default 24) without a message from you, the chat goes back to the bot, and Telegram tells you: "↩️ … رجع للبوت".
- **Photos and files:** see the next section.
- **WhatsApp's 24-hour rule:** you can only send a free-form reply within 24 hours of the customer's last message.
  After that, WhatsApp refuses the message and Telegram tells you. The customer has to write again first.

## 5a. Updating your listings (your Excel file)
Keep editing your Excel file (e.g. on OneDrive) as you do now. When the listings change:
1. In OneDrive/Excel: **File → Save As → Download a Copy** (.xlsx).
2. In the Telegram group, open the **General** topic, send the file, and wait for the bot's reply, e.g. `✅ تم تحميل العقارات: 72 (64 متاح)`.
   Don't send it inside a customer's topic: files there go to the customer.

**What the bot reads:**
- **Tabs:** only the 4 offer tabs (عروض البيع سكني / تجاري+زراعي، عروض التأجير سكني / تجاري).
- **Never:** the request tabs (customers' names and phones) or المالية.
- **Columns:** رقم العقار، النوع، الحي، الواجهة، المساحة، عمر العقار، and for rentals حالة التأجير.
- **Price:** read only to match customers' budgets. **It's never told to customers.**
- **Never read:** owner, broker or staff names, phone numbers, notes, رقم اللوحة.

**Listing status:** تم البيع / منتهي / محجوز = not offered. Anything else, **even empty**, means available.

**Keep your tab names and headers as they are.** If you rename them, the names in `config/excel.json` must be updated too. New spellings of types (e.g. a new word for "farm") also go there.

A broken file, or one without the offer tabs, is refused, and the previous listings stay.

## 5b. Your team (employees who answer customers)
With the group console (§4 Option A), employees in the group can answer customers too.

**Add an employee:**
1. Add them to the Telegram group.
2. Ask them to send any message there, e.g. `هلا`. The bot ignores it for now.
3. **Reply** to their message with `/staff إضافة أحمد` (their name as you want it shown). The bot answers "✅ أحمد صار من الفريق".

**What employees can do:** everything you can: answer customers in their topics (text, photos, files), `/human`, `/bot`, `/pause`, `/resume`, `/status`, `/leads`, `/lead` and `/forget`.
- **Customers see a normal WhatsApp message** from frame's number, with no name.
- In Telegram, the ✅ confirmation shows who answered, e.g. `✅ انرسلت للعميل 9665… (أحمد)`.

**Only you** can add or remove employees:
- `/staff`: list the team
- `/staff حذف أحمد`: remove someone

People in the group who aren't on the team can read, but nothing they write reaches customers.
Everyone in the group can read customer conversations, so add only people you trust.

## 6. Photos, voice notes and files
**From customers to you.** Photos, videos, voice notes, files (PDF…) and locations arrive in the customer's thread, or in the main chat when threads are off:
- A photo shows with its caption, e.g. `👤 كم سعر هذي الشقة؟`. Voice notes play inside Telegram, and files keep their name. A location arrives as a map pin.
- **The bot can't see them, so they always come to you**, even with `TELEGRAM_LIVE_MIRROR=false`.
- In chats the bot answers, the customer gets a short reply, e.g. "وصلتني الصورة، شكراً! بيطّلع عليها الفريق 🙏".
- Files over **20 MB** can't be passed on. You get a note instead (`📎 ما قدرت أحوّل…`), and the customer is asked to type their question.
- Stickers and contact cards aren't passed on; they show as `[sticker]`.

**From you to customers.** Send a **photo** or a **file** (PDF, Word, Excel, PowerPoint, TXT) inside the customer's thread, or as a **Reply** to one of their messages. Your caption goes with it.
- You get `✅ انرسلت للعميل 966…`, and the chat is yours (🙋), the same as when you type.
- Limits: up to **20 MB**. Photos are sent as normal WhatsApp photos. A JPG/PNG sent "as a file" (up to 5 MB) also arrives as a photo; anything else arrives as a document with its name.
- If something can't be sent, Telegram tells you why (too big, a type WhatsApp refuses, or the 24-hour rule), and nothing reaches the customer.
- Voice notes, videos and stickers from you can't be sent yet. Only text, photos and files.
- **Privacy:** files are passed on and never saved by the app. The database keeps only the caption, or a location's place name (never its coordinates).

## 7. Alerts from the bot
The bot sends you a 🔔 alert when a customer:
- wants a viewing
- wants to negotiate
- is ready to buy or rent
- asks something the listings can't answer
- asks for a person
- is upset

The alert shows the reason, a short Arabic summary, the lead details saved so far, and the customer's last message.
- **Reply to an alert** to answer the customer. That takes the chat over, as usual.
- Alerts **don't** silence the bot: it keeps helping the customer until you reply or send `/human`.
- The same alert for the same customer isn't repeated within an hour.
- If the bot fails to answer (e.g. Gemini is down), you get a ⚠️ alert. The customer received the polite "the team will get back to you" reply, so answer them when you can.

## 8. Privacy note (PDPL)
Forwarded messages include the customer's phone number, text, photos and files, and they're stored in your Telegram chat.
Normal Telegram chats are **not end-to-end encrypted**. Keep your Telegram account secure: turn on two-step verification (Settings → Privacy and Security).
Delete old chats with the bot if you don't need them.
`/forget` deletes everything the **app** stores about a customer. With threads on, it also deletes their Telegram thread. Without threads, delete their messages in Telegram by hand.
From the server you can also run `python -m scripts.forget_customer 9665XXXXXXXX`.
