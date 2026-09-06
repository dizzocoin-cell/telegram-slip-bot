# Deploying on a DigitalOcean droplet (beginner walkthrough)

The bot must run on an always-on machine. This puts it on a small Ubuntu server
that starts the bot automatically and restarts it if it ever crashes or the
server reboots.

Nothing here needs a terminal on your own computer — you use DigitalOcean's
in-browser console and GitHub's website.

---

## Part A — create the droplet

1. **Create → Droplets.**
2. Region: **Bangalore**.
3. Image: **Ubuntu** (24.04 LTS).
4. Plan: **Basic → Regular**, then pick the **$6.00/mo** line
   (**1 vCPU · 1 GB RAM · 25 GB SSD**).
   *Not the $4 one — 512 MB RAM is too little and the bot will crash while
   drawing the confirmation image.*
5. Scroll to **Choose Authentication Method → Password**. Type a strong password
   and save it somewhere — this is the server's `root` password.
6. Hostname: `slipbot`.
7. **Create Droplet.** Wait ~1 minute; you'll see an IP address.

(If you already made the $4 one: open it → **Power off** → **Resize** → choose the
$6 plan → **Power on**.)

---

## Part B — put the code on GitHub (website only)

1. Go to **github.com**, sign in, click **+ → New repository**.
2. Name `telegram-slip-bot`, leave it **Public**, click **Create repository**.
   *(The code has no passwords in it — your keys only ever live in the `.env`
   file on the server, which is never uploaded.)*
3. Click **“uploading an existing file.”**
4. In Finder open `~/Downloads/telegram-slip-bot`. Select **everything except**
   the `.venv` folder, the `.env` file, `bot.log` and `preview.png`. Drag them
   into the browser (include the `assets` and `deploy` folders).
5. Click **Commit changes.**
6. Click the green **Code** button and copy the **HTTPS** URL
   (`https://github.com/<you>/telegram-slip-bot.git`).

---

## Part C — set up the server (in-browser console)

1. On the droplet's page click **Console** (top right). A black terminal opens in
   your browser. Wait for `root@slipbot:~#`.
2. Type these lines one at a time (paste, then Enter). Replace the URL with yours:

   ```
   apt update && apt install -y git
   git clone https://github.com/<you>/telegram-slip-bot.git
   cd telegram-slip-bot
   bash deploy/setup.sh
   ```

   `setup.sh` installs Python, sets everything up and adds the auto-start
   service. It takes 2–3 minutes.

3. Now add your keys:

   ```
   nano .env
   ```

   Edit the values (arrow keys to move):

   ```
   TELEGRAM_BOT_TOKEN=<from BotFather>
   OPENAI_API_KEY=<from platform.openai.com>
   EXTRACTION_MODEL=gpt-4o-mini
   MAX_CONCURRENCY=3
   DEFAULT_TRANSFER_MODE=IMPS
   COMPANY_NAME=wixpay
   COMPANY_TAGLINE=Payment Confirmation
   COMPANY_WEBSITE=www.wixpay.in
   COMPANY_SUPPORT=support@wixpay.in
   ```

   Save: **Ctrl+O**, **Enter**, then **Ctrl+X**.

   > Use freshly regenerated keys — revoke the ones shared in chat
   > (BotFather `/revoke`, and rotate the OpenAI key).

4. Start it:

   ```
   systemctl restart slipbot
   systemctl status slipbot
   ```

   You want to see **active (running)**. Press `q` to exit that screen.

5. Watch it work:

   ```
   journalctl -u slipbot -f
   ```

   Post a slip in the group — you'll see log lines and get the confirmation
   back. `Ctrl+C` stops watching (the bot keeps running).

Done. The bot now runs 24/7 and restarts itself.

---

## Updating later

Upload the changed files to GitHub again (same drag-and-drop), then in the
console:

```
cd ~/telegram-slip-bot
git pull
.venv/bin/pip install -r requirements.txt
systemctl restart slipbot
```

---

## Cost

**DigitalOcean** — billed hourly, **capped per month**, charged to your
card/PayPal:

| Item | Monthly |
|---|---|
| Droplet (1 GB) | $6.00 |
| GST (India, 18%) | ~$1.10 |
| Backups (optional, on the droplet's Backups tab) | ~$1.20 |
| **Total** | **~$7–8.50 / month** |

New accounts get **$200 credit for 60 days** — the first ~2 months are usually
free.

**OpenAI** is billed separately and is the bigger number: roughly **$90–230 /
month** at ~2,500 slips/day on current settings, or **~$15–40 (partly free)** if
extraction is switched to Google Gemini Flash. Check platform.openai.com → Usage
after the first day.
