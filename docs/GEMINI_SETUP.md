# Gemini (AI model) setup

The agent uses Google's Gemini API. Checked against Google's docs on **2026-09-25**.

## 1. Get an API key
1. Go to <https://aistudio.google.com/apikey> and sign in with your Google account.
2. Click **Create API key** and copy it.
3. Put it in `.env`:
   ```
   GEMINI_API_KEY=your-key-here
   GEMINI_MODEL=gemini-3.6-flash
   GEMINI_THINKING_LEVEL=low
   ```
4. Restart the app. `/health` should now show `"ai": "configured"`.

Keep the key secret, like a password. It only goes in `.env`, which is never committed.

## 2. Free tier vs paid: important for customer privacy (PDPL)
| | Free tier | Paid tier (billing on) |
|---|---|---|
| Cost | Free, with daily limits (shown in AI Studio) | Pay per use (see below) |
| Human review of your prompts | **Yes, possible** | No |
| Used to improve Google's products | **Yes** | No |
| Google's advice | "Do not submit sensitive, confidential, or personal information" | Kept only briefly, to detect abuse |

- **Testing** with the sample sheet and your own messages: the free tier is fine.
- **Before real customers use the bot, turn on billing** (AI Studio → set up billing on the key's project).
  Customer messages contain personal data (names, phone numbers, budgets). Switching tiers is only a billing setting: no code or model change.

## 3. Cost (paid tier, per 1 million tokens)
`gemini-3.6-flash`: $0.75 input / $3.75 output until 31 Dec 2026, then $1.50 / $7.50.
One customer message typically uses a few thousand input tokens, plus a few hundred output tokens.
That's roughly **$5–6 per 1,000 customer messages** now, and about double from 2027.
Each reply writes its token usage to the log (`Gemini tokens: prompt=… cached=… output=… thinking=…`), so you can check real numbers.

About caching: Gemini automatically discounts a repeated prompt beginning once it is at least about 4,096 tokens long.
Our instructions plus tool definitions are smaller than that, so `cached=0` in the log is expected.
We'll review this during the Phase 8 cost check.

## 4. Changing the model
Set `GEMINI_MODEL` in `.env` to any current model ID from <https://ai.google.dev/gemini-api/docs/models>, then restart the app. Examples:
- `gemini-3.5-flash-lite`: cheaper ($0.30 / $2.50), but weaker at reasoning
- `gemini-3.8-flash`: newest Flash model, same price as 3.6 Flash for now

`GEMINI_THINKING_LEVEL` (`minimal` / `low` / `medium` / `high`) controls how much the model "thinks" before replying.
Thinking is billed as output, so higher levels are slower and cost more. `low` is a good default for short chat replies.

## 5. How we call Gemini (for future maintenance)
- We use the stateless `generateContent` API. Google calls it "legacy but fully supported"; the newer Interactions API **stores conversations on Google's servers by default**. With `generateContent`, our own database is the only conversation memory.
- All Gemini-specific code is in `app/agent.py`.
- The agent's instructions are in `prompts/system.md`. Edit them freely, then restart the app.
- Try it without WhatsApp: `python -m scripts.chat_cli --sample`.
