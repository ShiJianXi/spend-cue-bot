# SpendCue

SpendCue is a Telegram bot for spending, credit card payments, subscriptions, and monthly overviews. It uses buttons and short step-by-step forms. **It does not use Gemini, interpret open-ended messages, or extract receipt photos.**

There are two ways to run it:

- **Cloudflare Workers (recommended for sharing):** invite-only Telegram webhook with one private SQLite-backed Durable Object database per person. Friends only need to tap an invite link. The Worker wakes for messages and reminders; your computer does not stay on.
- **Local Python:** the original single-user long-polling bot and a local SQLite file. It remains available for local use and as the source for migrating existing history.

## Free Cloudflare deployment for a small group

The Cloudflare version uses [Workers Free and SQLite-backed Durable Objects](https://developers.cloudflare.com/durable-objects/platform/pricing/). A user account's storage is private to its Durable Object; the Worker derives that object **only from the verified Telegram sender ID**, never from a button value. No web dashboard, Gemini key, or separate database subscription is needed. Free plans have quotas and no uptime guarantee. If a quota is exceeded, requests can fail until it resets. The current Worker checks reminders about every six hours, so due-date reminders can arrive later in the day.

1. Create a [Cloudflare account](https://dash.cloudflare.com/sign-up) and use its Workers Free plan. Create a Telegram bot with [@BotFather](https://t.me/BotFather), or reuse your current bot and token. Find **your** numeric Telegram ID; this is the owner ID, not an ID your friends must supply.
2. Install Node.js 20+ and, if migrating old data, Python 3.11+. In this repository run `npm ci` and `npm test`. Change `TIMEZONE` and `DEFAULT_CURRENCY` in `wrangler.jsonc` if needed. The defaults are Asia/Singapore and SGD for all invited users.
3. Copy `.dev.vars.example` to `.dev.vars`. Fill `TELEGRAM_BOT_TOKEN` and `OWNER_TELEGRAM_USER_ID`. Generate **two different** random secrets for `TELEGRAM_WEBHOOK_SECRET` and `MIGRATION_SECRET`, for example with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` twice. `.dev.vars` is ignored by Git; never post its contents.
4. Run `npx wrangler login`, then `npx wrangler deploy --secrets-file .dev.vars`. Save the HTTPS `*.workers.dev` URL printed by Wrangler. The deployment stores secrets in Cloudflare, not in the published code. A dry run is available with `npx wrangler deploy --dry-run`.
5. If you have existing local records, **stop the local Python bot first**. Do not send new bot messages until the webhook is active; the cutover discards queued updates to prevent replaying records already imported. Import your history before anyone uses the hosted bot:

   ```sh
   set -a; . ./.env; . ./.dev.vars; set +a
   export WORKER_URL='https://your-worker.your-subdomain.workers.dev'
   python3 cloudflare/migrate.py --dry-run
   python3 cloudflare/migrate.py
   ```

   `DB_PATH` defaults to `spendcue.db`; use `--db /path/to/your.db` if yours differs. The script makes a consistent temporary SQLite snapshot, imports the owner's categories, expenses, subscriptions, cards, payments, and reminder history, and removes the snapshot. It sends the data directly to your Worker over HTTPS without writing an export file. Import is refused if the hosted owner account already has financial records or was imported before. If you have no local history, skip this step.
6. Point Telegram at the Worker **after** migration. With the same environment variables and `WORKER_URL` set, run `python3 cloudflare/setup_webhook.py`. This registers the commands, sets the webhook secret, and discards old queued polling updates during the switch. Send `/start` to the bot and verify the menu.
7. Send `/invite` in your private bot chat. Share the resulting one-use link privately with one friend. Generate a fresh link for each friend. Each link expires after seven days. `/users` lets you revoke access; revocation keeps that person's records stored but stops their use of the bot. Ask each friend to send `/start`, add a test expense, and check `/overview`.

Only one deployment should receive updates for a bot token. Do not run the Python poller after setting the webhook. To change code later, run `npx wrangler deploy`; Durable Object data survives code deployments. To change a secret, use `npx wrangler secret put SECRET_NAME` or deploy with an updated secrets file. The `MIGRATION_SECRET` protects the one-time owner import endpoint; keep it private even after migration.

The bot checks both the private chat ID and sender ID, and rejects group messages. It stores forms, expenses, cards, subscriptions, reminders, and update deduplication in each person's own database. Pending Telegram messages are kept in a per-user outbox and retried after temporary send failures. A send can still be duplicated if Telegram accepts it but its response is lost. The owner of the Cloudflare account can access the hosted data, and Telegram bot messages are [visible to the bot operator](https://telegram.org/privacy); tell friends this before inviting them.

## Local Python setup (single user)

1. Open [@BotFather](https://t.me/BotFather) in Telegram, send `/newbot`, and keep the token private.
2. Send a private message to the new bot. Get your numeric Telegram user ID from a trusted ID bot, or inspect a `getUpdates` response from the Bot API. Set `ALLOWED_TELEGRAM_USER_ID` to that ID. This **local Python version** ignores everyone else and group chats.
3. Install Python 3.11 or newer. Copy `.env.example` to `.env`, fill in the token and user ID, and run:

   ```sh
   set -a
   . ./.env
   set +a
   python3 spendcue.py
   ```

The Python bot registers its slash commands with Telegram at startup. Only one SpendCue process should use a bot token and database at a time. Long polling requires no webhook: if one was previously configured, remove it with the Bot API `deleteWebhook` method.

## Configuration

| Variable | Meaning |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Required token from BotFather. |
| `ALLOWED_TELEGRAM_USER_ID` | Required numeric ID for the local Python bot; for Cloudflare use `OWNER_TELEGRAM_USER_ID`. |
| `DB_PATH` | SQLite file; default `spendcue.db`. Use persistent storage. |
| `TIMEZONE` | IANA timezone for dates and reminders; default `Asia/Singapore`. |
| `DEFAULT_CURRENCY` | Three-letter currency code for amounts without a code; default `SGD`. |

No Gemini key, AI account, or Python package installation is needed.

## Use

Send `/menu` or `/start` to open the five main options. `/help` shows a short command list, `/cancel` stops the current form, and `/export` sends a CSV. You can also type `add` or use `/add` to begin an expense.

### Spending: `/add`

Choose a category, enter an amount such as `24.80` or `USD 24.80`, enter a merchant or short note, and choose Today, Yesterday, or a date you type as `YYYY-MM-DD`. Review the entry and tap **Save**. The confirmation has **Edit** and **Undo** buttons. Manage → Edit spending lists recent entries. New databases start with Food, Transport, Shopping, Bills, and Other; Manage → Categories adds or renames categories.

### Credit cards: `/cards` (or `/cc`)

Add a **nickname**, never a card number, and its recurring payment due day (1–31). A due day beyond a short month's end uses that month's final day. There is no statement-entry step. The Cards view shows **Unpaid** until you record a payment for the current due month, then **Paid** with the amount. The status resets to Unpaid in the next month. An unpaid card past its due day is marked **overdue**.

Tap **Record payment**, choose an unpaid card, enter how much you paid, and confirm. This records a payment on today's date for the **current due month** and marks that month Paid; it does not require a statement amount or support partial-payment status. The monthly overview shows each card's Paid or Unpaid status and any amount recorded for that due month. Card payments are excluded from spending totals so a purchase logged through `/add` is not counted again when its card bill is paid. The bot records a payment; it does not execute one.

Each card defaults to a reminder **7 days before** and **on** its due date. In the card's **Settings**, change its due day or choose a reminder lead time of 0, 1, 3, 7, or 14 days. Reminders stop for a due month once its payment is recorded. An existing card upgraded from an older database inherits its latest recorded bill's due day; if no old due date exists, open its Settings and set one. Existing card payments remain in history. Old partial payments count as Paid under the new one-payment-per-month rule.

### Subscriptions: `/subs`

Add a name, amount per renewal, **monthly**, **quarterly**, or **yearly** frequency, and the exact next payment date. You can edit the name, amount, or schedule. The original day is preserved across renewals; a date beyond the end of a shorter month uses that month's final day. On each due date, SpendCue automatically records a scheduled subscription expense, including any dates missed while the bot was offline. It appears in spending totals under **Subscriptions**, clearly marked as an **unverified scheduled charge**. This assumes the renewal happened; there is no bank confirmation. If an individual charge did not happen, remove that expense with Manage → Edit spending → Undo. Avoid adding the same renewal manually through `/add` or it will count twice.

**Cancel renewals** stops future scheduled expenses and reminders while retaining past charges. **Reactivate** starts recording again from the current date, without filling in the cancelled period. If you edit the amount, past charges retain their recorded amounts.

### Overview: `/overview`

See spending for this month or week, choose from the **most recent 12 months**, or enter a custom start and end date. Each previous-month view uses the full calendar month in the configured timezone. SpendCue **does not delete old spending automatically**: older months remain in SQLite and can be viewed with Date range or exported to CSV. Keeping the full history supports year-over-year comparisons without an arbitrary retention cutoff; use regular backups to preserve it.

Totals include only manually entered `/add` expenses and scheduled subscription charges, grouped by category and **kept separate by currency**. A separate card section shows each card's Paid or Unpaid status, due date, and recorded payment amount for the displayed month. The card payment amount is **not added to Total**, avoiding double counting. For a week or custom date range, the card section uses the month containing the range's end date. A card added later is not shown in earlier months. Upcoming payments lists active subscription renewals and unpaid card due dates for the next 30 days, including overdue dates in the current month.

### Manage: `/manage`

Add or rename categories, edit recent spending entries (including scheduled subscription charges), or download a CSV. Renaming a category updates saved expenses in that category. The CSV includes expenses, subscription schedules and charges, recorded card payments, and any older statement records. Text that could be interpreted as a spreadsheet formula is escaped on export.

Each person has one active form at a time and saves it in SQLite, so a restart does not lose the form. Old buttons from a cancelled form cannot change a newer form. Money uses integer minor units, and amounts and dates are validated before saving. Replayed Telegram update IDs cannot create duplicate records. Labels containing a full card number are redacted.

Receipt photos receive a message directing you to `/add`; they are not downloaded or processed. Free-form expense messages are not interpreted.

## Reminders and hosting

SpendCue checks subscription renewals **three days before** and **on** their due dates. It checks unpaid cards at each card's configured lead time and on its due date. A reminder is sent once per item and reminder date while the process runs. Scheduled subscription expenses catch up after downtime, but missed reminder dates are not sent later. Telegram delivery cannot be guaranteed exactly once across a crash or uncertain network response.

The Cloudflare version uses a webhook and Durable Object alarms. It catches up scheduled subscription charges after downtime. Reminders are checked about every six hours and deduplicated per item and date. The local Python version checks while its process is running and still needs an always-on host for 24/7 use. Neither version executes payments or verifies that a subscription actually charged.

## Tests

No credentials or network access are needed:

```sh
python3 -m unittest discover -s tests -v  # local Python bot
npm test                                   # Cloudflare Worker and privacy tests
```

The tests mock Telegram and cover amount precision, month boundaries, menu flows, edits and undo, duplicate updates, invite claiming, per-user isolation, CSV isolation, card status, subscription recurrence, reminder deduplication, access control, and migration.

## Backup and restore

Cloudflare SQLite-backed Durable Objects offer [point-in-time recovery for the previous 30 days](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/). Each person can also send `/export` to download their own CSV and keep a separate copy. CSV contains spending, categories, card settings and payments, and subscription schedules; it is not a complete database image or an automatic off-platform backup. Cloudflare account access and any exported CSV files should be protected.

For the local Python bot, use Python's SQLite backup API:

For a consistent backup while SpendCue is running, keep backups outside Git:

```sh
mkdir -p backups
python3 -c 'import sqlite3; source=sqlite3.connect("spendcue.db"); target=sqlite3.connect("backups/spendcue.db"); source.backup(target); target.close(); source.close()'
```

To restore, stop the bot, copy a known-good backup over `DB_PATH`, and restart it. Adjust paths if you configured a different database location. Protect the database and backups with host file permissions and storage encryption. `.gitignore` excludes `.env`, database files, and `backups/` from Git.

Opening an older SpendCue database adds categories, card due days, payment history, subscription schedules, and form storage while retaining saved expenses, subscriptions, bills, and transfers. Restart the running bot after upgrading and send `/menu`; in-progress forms from the former card-bill flow are cleared. Existing monthly subscriptions retain their renewal day and begin automatic expense recording from the first run after migration; past renewals are not backfilled. Older Gemini proposal drafts remain in the database but are not part of the new menu flow.

## Limits

The local Python bot is deliberately **single-user**. The Cloudflare version is invite-only and gives each user a separate database, but it has not been load-tested; start with the intended 2–3 people and monitor the free quotas. All invited users currently share the configured timezone and default currency, although individual entries may specify another currency. Card payments are recorded for the current due month using today's date; backdating or assigning a payment to another due month is not in the menu. Subscription charges are assumed from the schedule, not verified against a bank. No bank connection, payment execution, receipt OCR, dashboard, audio, or AI assistant is included.
