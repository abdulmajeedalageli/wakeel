# Deploying the bot to Google Cloud (step by step)

> **Currently used instead: Railway** (see [DEPLOY_RAILWAY.md](DEPLOY_RAILWAY.md)).
> This guide is for a Linux VM of your own: Google Cloud, or any Ubuntu VPS with the same steps from step 3.
> Note: Google Cloud sign-ups from Saudi Arabia are redirected to CNTXT, where no free trial was found.

The result is the bot running 24/7 on Google Cloud's **forever-free** small server (`e2-micro`):
- HTTPS at `https://<name>.duckdns.org`
- restarts by itself after crashes and reboots
- one database backup per day
- an uptime monitor that emails you if it goes down

Checked against Google Cloud, Docker, Caddy and UptimeRobot docs on 2026-09-26.
Budget about **1–2 hours** the first time. Everything happens in your web browser, with no extra software on your PC.

> **Before you start, know this:**
> - The free server exists only in **US regions**, so customer data (messages, leads) is stored in the US.
>   Under Saudi Arabia's PDPL that's a cross-border transfer. Make sure you're comfortable with it: this isn't legal advice, so ask a specialist if unsure.
>   The Google Cloud terms include Google's data processing terms.
> - Google needs a card, and you must **upgrade to a paid billing account (step 1.3)**. Otherwise the server is **stopped and deleted** when the 90-day trial ends.
>   After upgrading, the free server still costs nothing. You only pay for things outside the free limits, and the $1 budget alert (step 1.4) warns you.
> - Google charges for **public IP addresses** (about $3.60/month). Google's documentation doesn't clearly say whether the free server's IP is included. Check your bill after a few days (step 1.4).

Placeholders you'll replace:
| Placeholder | Example |
|---|---|
| `<name>` | your DuckDNS subdomain, e.g. `frame-bot` |
| `<SERVER_IP>` | the server's external IP, e.g. `34.73.x.x` |

---

## 1. Google Cloud account
1. Open <https://console.cloud.google.com/freetrial> and sign in with your normal Google account. **Gmail is fine: no company email is needed.**
   - Country: Saudi Arabia. Account type: **Individual** if offered; if only "Business" is offered, a business *name* like `frame` is enough.
   - Add a card (for verification; you get $300 of trial credit).
   - ⚠️ If a page asks for **"Work email"**, "Company name" or "Job title", that's a *Contact sales* form (or Google Workspace), not the free trial. Close it and use the link above.
   - ⚠️ If it asks for your **business domain name** ("ما اسم نطاق نشاطك التجاري؟", e.g. `example.com`), you're in the **Google Workspace** signup (paid business email), not Google Cloud.
     Close it, open a **private window**, go to the link above, and **sign in with your existing Gmail**.
     If you must create a Google account, choose **"For my personal use" (لاستخدامي الشخصي)**, not "For work or my business".
2. Create a **project** called `frame-bot` (top bar → project picker → New project).
3. **Upgrade to a paid billing account now:** the **Activate** / "Upgrade" button at the top of the console, or **Billing → Overview → Upgrade**.
   This does **not** end the free server, and your trial credit is kept. It stops Google from shutting everything down after 90 days.
4. **Budget alert:** **Billing → Budgets & alerts → Create budget**. Amount **$1**, with email alerts at 50%, 90% and 100%.
   After 2–3 days, open **Billing → Reports** and check the costs are **$0.00**, or covered by credits. If the public IP shows a charge, it's small (about $3.60/month), and now you know about it.

## 2. Create the server
**Compute Engine → VM instances** (click **Enable** if asked to turn on the Compute Engine API) → **Create instance**.

Settings. These exact values keep it free:
- **Name:** `frame-bot`
- **Region:** **us-east1 (South Carolina)**. It must be `us-east1`, `us-central1` or `us-west1`, and us-east1 is the closest to Saudi Arabia. Any zone is fine.
- **Machine configuration:** series **E2**, machine type **e2-micro** (2 shared vCPUs, 1 GB memory).
- **OS and storage → Change:**
  - **Operating system:** Ubuntu
  - **Version:** **Ubuntu 24.04 LTS** (x86/64)
  - **Boot disk type:** **Standard persistent disk**. ⚠️ The default ("Balanced") is **not** free.
  - **Size:** **30 GB**
- **Networking:** tick **Allow HTTP traffic** and **Allow HTTPS traffic**. This opens ports 80 and 443.
- **Data protection / backups:** choose **No backups** (snapshots cost money; the bot makes its own backups).
- **Observability / Ops Agent:** leave it off.

Click **Create**. When it's running, copy its **External IP**. That's `<SERVER_IP>`.

## 3. Connect and prepare the server
On the VM list, click **SSH** next to `frame-bot`. A terminal opens in your browser. Every command below is typed there.
The window also has **Upload file** and **Download file** buttons, which we'll use later.

### 3.1 Updates
```
sudo apt update && sudo apt upgrade -y
```

### 3.2 Add 2 GB of swap (the server only has 1 GB of memory)
```
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h
```

### 3.3 Install Docker (the official method)
```
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
```
**Close the SSH window and open it again** (the SSH button), so you can use `docker` without `sudo`. Then check:
```
docker compose version
```

## 4. Your free domain (DuckDNS)
1. Go to <https://www.duckdns.org>, sign in, and create a subdomain (e.g. `frame-bot`). Put `<SERVER_IP>` in its "current ip" box and click **update ip**.
2. Copy your DuckDNS **token** (shown at the top of the page).
3. The server's IP can change when it's stopped and started, so keep the domain up to date automatically. Replace `NAME` and `TOKEN` in the commands below.

```
mkdir -p ~/duckdns && cd ~/duckdns
cat > duck.sh <<'EOF'
#!/bin/sh
curl -fsS "https://www.duckdns.org/update?domains=NAME&token=TOKEN&ip=" -o ~/duckdns/duck.log
EOF
chmod 700 duck.sh && ./duck.sh && cat duck.log
```
It should print `OK`. Then schedule it every 5 minutes: run `crontab -e` (choose `nano` if asked), and add this line at the bottom:
```
*/5 * * * * ~/duckdns/duck.sh >/dev/null 2>&1
```

## 5. Get the code (read-only deploy key)
```
ssh-keygen -t ed25519 -C "frame-server deploy key" -f ~/.ssh/github_deploy -N ""
cat ~/.ssh/github_deploy.pub
```
Copy the printed line. On GitHub, open your repository: **Settings → Deploy keys → Add deploy key**. Paste it, and leave **"Allow write access" unticked**. Then:
```
printf 'Host github.com\n  IdentityFile ~/.ssh/github_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
git clone git@github.com:<your-github-user>/<your-repo>.git ~/frame_agent
cd ~/frame_agent
mkdir -p secrets data
```
(Answer `yes` if it asks whether to trust github.com.)

## 6. Secrets and settings
In the SSH window, click **Upload file** and upload two files from the project folder on your PC:
- `.env`. If the file picker hides it, type `.env` in the file-name box.
- `secrets\service-account.json`

They arrive in your home folder. Move them into place:
```
mv ~/.env ~/frame_agent/.env
mv ~/service-account.json ~/frame_agent/secrets/
cd ~/frame_agent
nano .env
```
In `nano`, set `DOMAIN=<name>.duckdns.org` (without `https://`), then save with Ctrl+O and Enter, and exit with Ctrl+X.
The Meta, Gemini, Google and Telegram values stay as on your PC. `DB_PATH` is set automatically inside Docker.

Give the files to the right owners. The bot runs as user 10001 inside Docker:
```
chmod 600 .env
sudo chown 10001:10001 data secrets/service-account.json
sudo chmod 400 secrets/service-account.json
```

## 7. Start it
**First stop the bot on your PC** if it's running. Only one copy may use the Telegram bot at a time.
```
cd ~/frame_agent
docker compose up -d --build
```
The first build takes several minutes on the small server. Then:
```
docker compose ps
docker compose logs -f app
```
Both containers should be "running", and the log should show `App started` and `Database ready`. Press Ctrl+C to stop watching the log (the bot keeps running).

Check in your browser: `https://<name>.duckdns.org/health` should show `"status":"ok"` with a **padlock** (valid certificate).
The certificate can take a minute the first time; watch it with `docker compose logs caddy`. `https://<name>.duckdns.org/docs` should show **Not found**, which is correct.

## 8. Point Meta at the server
Meta App Dashboard → WhatsApp → **Configuration** → Webhook → **Edit**:
- **Callback URL:** `https://<name>.duckdns.org/webhook`
- **Verify token:** the same `WHATSAPP_VERIFY_TOKEN` as in `.env`

Click **Verify and save**. Message the test number from your phone: you should get the AI's reply. In Telegram, send `/status`.

## 9. Uptime monitor (free)
<https://uptimerobot.com>: sign up, then **New monitor**:
- **Type:** Keyword
- **URL:** `https://<name>.duckdns.org/health`
- **Keyword:** `"status":"ok"`, and alert when the keyword **does not exist**
- **Interval:** 5 minutes
- **Alert contact:** your email

## 10. Check that it survives a reboot
```
sudo reboot
```
Wait about 2 minutes, then reopen the SSH window. `/health` should be OK again without you doing anything: Docker starts on boot, and `restart: always` restarts both containers.

---

## Updating the bot
When new code is on GitHub:
```
cd ~/frame_agent && git pull && docker compose up -d --build
```
Once a month, also get security updates for Ubuntu, Python and Caddy:
```
sudo apt update && sudo apt upgrade -y
cd ~/frame_agent && docker compose build --pull && docker compose pull caddy && docker compose up -d
```
**Going back to an earlier version** if an update misbehaves:
```
git log --oneline
git checkout <commit>
docker compose up -d --build
```
Return to the latest with `git checkout main`, then `docker compose up -d --build`.

## Backups
- **Automatic:** one copy per day in `~/frame_agent/data/backups/`, kept for 14 days (`BACKUP_KEEP_DAYS`). Make one now with:
  ```
  docker compose exec app python -m app.backup
  ```
- **Download them to your PC every week.** A backup on the same server doesn't help if the server is lost. In the SSH window, pack them into one file:
  ```
  tar czf ~/backups.tar.gz -C ~/frame_agent/data backups && ls -l ~/backups.tar.gz
  ```
  Click **Download file** and enter `backups.tar.gz`. Then delete the copy on the server:
  ```
  rm ~/backups.tar.gz
  ```
  Backups contain customer data: keep them on your own PC, not in shared folders.
  Customers deleted with `/forget` stay in backups until those backups age out (14 days).
- **Restoring** a backup:
  ```
  cd ~/frame_agent && docker compose stop app
  sudo cp data/backups/bot-YYYY-MM-DD.db data/bot.db
  sudo rm -f data/bot.db-wal data/bot.db-shm
  sudo chown 10001:10001 data/bot.db
  docker compose start app
  ```

## Admin commands on the server
Run these from `~/frame_agent`:
```
docker compose exec app python -m scripts.chat_status list
docker compose exec app python -m scripts.forget_customer 9665XXXXXXXX
docker compose exec app python -m scripts.try_search --district النرجس --deal sale
```
The Telegram console (`/status`, `/human`, `/leads`, `/forget` …) does the same, and is easier.

## Rules
- **One running copy only** per Telegram bot. Two copies fight over Telegram messages (a "409 Conflict" error in the logs).
- **One worker only.** The Dockerfile already sets `--workers 1`; don't change it.
- **Never commit `.env`, `secrets/` or `data/`.** They're git-ignored; keep it that way.

## Staying free on Google Cloud
- **Only one** `e2-micro` server, in `us-east1`, `us-central1` or `us-west1`, with a **standard** disk of **30 GB** or less. A second server, a bigger machine type or a "Balanced" disk all cost money.
- **No snapshots** or other paid add-ons. The bot's own daily backups plus your weekly download are enough.
- **1 GB of outgoing data per month is free.** This bot uses far less: AI requests, WhatsApp replies and Telegram messages are small text.
- Keep the **$1 budget alert** on, and glance at **Billing → Reports** once a month.

## Before real customers use it
- [ ] **Gemini billing is on** (AI Studio). The free tier allows human review of messages, which isn't OK for customer data (PDPL). See `docs/GEMINI_SETUP.md`.
- [ ] You're comfortable with customer data being stored in the US (see the note at the top).
- [ ] You've decided which WhatsApp number the bot uses (`docs/META_SETUP.md`, the end), and it's registered.
- [ ] The Meta app is in **Live** mode, with a privacy policy URL.
- [ ] The Google Sheet has your real listings, and the yellow TEST rows are removed.
- [ ] You've gone through `docs/TEST_CONVERSATIONS.md` on WhatsApp.
- [ ] Telegram `/status` works, and you've tried `/human`, a Reply, and `/bot`.
- [ ] UptimeRobot shows "Up", and the weekly backup download is on your calendar.

## Troubleshooting
| Problem | Check |
|---|---|
| `/health` doesn't open, or there's a certificate error | `docker compose logs caddy`. Did you tick **Allow HTTP/HTTPS traffic** (step 2)? (Fix: VM → Edit → Networking.) Does `ping <name>.duckdns.org` show the current `<SERVER_IP>`? |
| Meta "Verify and save" fails | Is the verify token identical in `.env` and Meta? Does `/health` work in the browser? |
| Webhooks arrive but get rejected (403 in the app log) | `WHATSAPP_APP_SECRET` in `.env` doesn't match the app. After editing `.env`, run `docker compose up -d`. |
| The bot doesn't answer | `docker compose logs app`. Look for `GEMINI_API_KEY is not set`, sheet errors, or fallback reasons. |
| The first build is very slow or gets "Killed" | Check the swap is on (`free -h`, step 3.2), then run `docker compose up -d --build` again. |
| `409 Conflict` in the Telegram logs | Another copy is running (e.g. on your PC). Stop it. |
| The disk is getting full | `df -h`, then `docker system prune -f` (removes old image layers; your data is safe). |
| After editing `.env` | `docker compose up -d` (it recreates the container with the new settings). |
| An unexpected charge on the bill | Check the machine type (e2-micro), the region, the disk type (Standard) and size (30 GB); make sure there are no snapshots; check the public IP line. |
