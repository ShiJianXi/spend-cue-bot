# SpendCue

SpendCue is a private Telegram bot for spending, credit card bills, subscriptions, and monthly overviews. It uses buttons and short step-by-step forms. **It does not use Gemini, interpret open-ended messages, or extract receipt photos.** It runs on Python 3.11+ with the standard library, SQLite, and Telegram Bot API long polling.

## Set up

1. Open [@BotFather](https://t.me/BotFather) in Telegram, send `/newbot`, and keep the token private.
2. Send a private message to the new bot. Get your numeric Telegram user ID from a trusted ID bot, or inspect a `getUpdates` response from the Bot API. Set `ALLOWED_TELEGRAM_USER_ID` to that ID. SpendCue ignores everyone else and group chats.
3. Install Python 3.11 or newer. Copy `.env.example` to `.env`, fill in the token and user ID, and run:

   ```sh
   set -a
   . ./.env
   set +a
   python3 spendcue.py
   ```

The bot registers its slash commands with Telegram at startup. Only one SpendCue process should use a bot token and database at a time. Long polling requires no webhook: if one was previously configured, remove it with the Bot API `deleteWebhook` method.

## Configuration

| Variable | Meaning |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Required token from BotFather. |
| `ALLOWED_TELEGRAM_USER_ID` | Required numeric ID of the single permitted user. |
| `DB_PATH` | SQLite file; default `spendcue.db`. Use persistent storage. |
| `TIMEZONE` | IANA timezone for dates and reminders; default `Asia/Singapore`. |
| `DEFAULT_CURRENCY` | Three-letter currency code for amounts without a code; default `SGD`. |

No Gemini key, AI account, or Python package installation is needed.

## Use

Send `/menu` or `/start` to open the five main options. `/help` shows a short command list, `/cancel` stops the current form, and `/export` sends a CSV. You can also type `add` or use `/add` to begin an expense.

### Spending: `/add`

Choose a category, enter an amount such as `24.80` or `USD 24.80`, enter a merchant or short note, and choose Today, Yesterday, or a date you type as `YYYY-MM-DD`. Review the entry and tap **Save**. The confirmation has **Edit** and **Undo** buttons. Manage → Edit spending lists recent entries. New databases start with Food, Transport, Shopping, Bills, and Other; Manage → Categories adds or renames categories.

### Credit cards: `/cards` (or `/cc`)

Add a **nickname**, never a card number. For each month's statement, choose the card and enter that month's amount and exact due date. The Cards view shows the bill due in the current month as **Unpaid**, **Part paid**, or **Paid**, and highlights older outstanding bills as **OVERDUE**. Record a payment by choosing an outstanding bill, entering the actual amount paid, and choosing the payment date. Partial payments reduce the remaining balance; a full payment marks the bill paid.

Each due month has its own bill. A new month shows **No bill entered** until you add that month's statement; older bills and payments stay in history. **Edit bill** can correct an entered amount or due date. A corrected amount cannot be less than payments already recorded. SpendCue does not guess variable statement balances or due dates and cannot remind you about a bill you have not entered. The monthly overview shows recorded card payments separately from purchase spending: a purchase charged to the card and its later bill payment would otherwise be counted twice. The bot records a payment; it does not execute one.

### Subscriptions: `/subs`

Add a name, amount per renewal, **monthly**, **quarterly**, or **yearly** frequency, and the exact next payment date. You can edit the name, amount, or schedule. The original day is preserved across renewals; a date beyond the end of a shorter month uses that month's final day. On each due date, SpendCue automatically records a scheduled subscription expense, including any dates missed while the bot was offline. It appears in spending totals under **Subscriptions**, clearly marked as an **unverified scheduled charge**. This assumes the renewal happened; there is no bank confirmation. If an individual charge did not happen, remove that expense with Manage → Edit spending → Undo. Avoid adding the same renewal manually through `/add` or it will count twice.

**Cancel renewals** stops future scheduled expenses and reminders while retaining past charges. **Reactivate** starts recording again from the current date, without filling in the cancelled period. If you edit the amount, past charges retain their recorded amounts.

### Overview: `/overview`

See spending for this month or week, choose from the **most recent 12 months**, or enter a custom start and end date. Each previous-month view uses the full calendar month in the configured timezone. SpendCue **does not delete old spending automatically**: older months remain in SQLite and can be viewed with Date range or exported to CSV. Keeping the full history supports year-over-year comparisons without an arbitrary retention cutoff; use regular backups to preserve it.

Totals include manually entered expenses and scheduled subscription charges, grouped by category and **kept separate by currency**. Recorded card payments appear in a separate line for the same date range; they are visible monthly cash outflows but are not added to purchase spending totals. The view shows how many card bills remain unpaid or part paid **now**, even when viewing a past month. Upcoming payments lists active subscription renewals and outstanding card bills for the next 30 days, including overdue bills.

### Manage: `/manage`

Add or rename categories, edit recent spending entries (including scheduled subscription charges), or download a CSV. Renaming a category updates saved expenses in that category. The CSV includes expenses, subscription schedules and charges, card bills, and recorded card payment transfers. Text that could be interpreted as a spreadsheet formula is escaped on export.

The bot holds one active form at a time and saves it in SQLite, so a restart does not lose the form. Old buttons from a cancelled form cannot change a newer form. It stores money as integer minor units and validates amounts and dates before saving. It processes each Telegram update ID once and checks that messages come from the configured user in a private chat. Labels containing a full card number are redacted.

Receipt photos receive a message directing you to `/add`; they are not downloaded or processed. Free-form expense messages are not interpreted.

## Reminders and hosting

SpendCue checks for subscription and unpaid card bill reminders **three days before** and **on** each due date. A reminder is sent once per item and reminder date while the process runs. Scheduled subscription expenses catch up after downtime, but missed reminder dates are not sent later. Telegram delivery cannot be guaranteed exactly once across a crash or uncertain network response.

This repository **does not deploy the bot**. The current implementation uses long polling and a local SQLite file, so 24/7 operation needs an always-on host with persistent storage. An ephemeral free web service can lose the database or sleep through reminders. Removing Gemini makes ongoing AI cost zero, but does not itself make the existing process serverless. A webhook, hosted database, and scheduled reminder job would be required for a scale-to-zero cloud deployment.

## Tests

No credentials or network access are needed:

```sh
python3 -m unittest discover -s tests -v
```

The tests mock Telegram and cover amount precision, menu expense edits and undo, duplicate updates, stale buttons, partial and full card payments, separate monthly totals, historical month boundaries and older date ranges, overdue status, subscription recurrence and catch-up, cancellation, reminder deduplication, access control, and migration of saved data from the earlier Gemini version.

## Backup and restore

For a consistent backup while SpendCue is running, use Python's SQLite backup API. Keep backups outside Git:

```sh
mkdir -p backups
python3 -c 'import sqlite3; source=sqlite3.connect("spendcue.db"); target=sqlite3.connect("backups/spendcue.db"); source.backup(target); target.close(); source.close()'
```

To restore, stop the bot, copy a known-good backup over `DB_PATH`, and restart it. Adjust paths if you configured a different database location. Protect the database and backups with host file permissions and storage encryption. `.gitignore` excludes `.env`, database files, and `backups/` from Git.

Opening an older SpendCue database adds categories, cards, payment history, subscription schedules, and form storage while retaining saved expenses, subscriptions, bills, and transfers. Existing monthly subscriptions retain their renewal day and begin automatic expense recording from the first run after migration; past renewals are not backfilled. Older Gemini proposal drafts remain in the database but are not part of the new menu flow; start a new form in `/menu`.

## Limits

SpendCue is deliberately **single-user** and handles updates sequentially. It has no multi-user data isolation or load-test capacity claim. Card bill amounts and due dates must be entered each month. Subscription charges are assumed from the schedule, not verified against a bank. No bank connection, payment execution, receipt OCR, dashboard, audio, or AI assistant is included.
