"""SpendCue: a private, menu-driven Telegram finance tracker."""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

DEFAULT_CATEGORIES = ("Food", "Transport", "Shopping", "Bills", "Other")
ZERO_DECIMAL = frozenset("BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX UYI VND VUV XAF XOF XPF".split())
THREE_DECIMAL = frozenset("BHD IQD JOD KWD LYD OMR TND".split())


def places(curr: str) -> int:
    return 0 if curr in ZERO_DECIMAL else 3 if curr in THREE_DECIMAL else 2


def money(value: str, curr: str = "SGD") -> int:
    try:
        n = Decimal(str(value).replace(",", "").strip())
        factor = 10 ** places(curr)
        if not n.is_finite() or n <= 0 or n * factor != (n * factor).to_integral_value():
            raise ValueError
        minor = int(n * factor)
        if minor > 9_223_372_036_854_775_807:
            raise ValueError
        return minor
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Enter a positive amount with valid currency precision") from exc


def currency(value: str | None, default: str) -> str:
    result = (value or default).upper().strip()
    if not re.fullmatch(r"[A-Z]{3}", result):
        raise ValueError("Use a three-letter currency code")
    return result


def amount_input(text: str, default_currency: str) -> tuple[int, str]:
    match = re.fullmatch(
        r"\s*(?:(?P<before>[A-Za-z]{3})\s*)?\$?(?P<number>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(?P<after>[A-Za-z]{3})?\s*",
        text,
    )
    if not match or (match["before"] and match["after"] and match["before"].upper() != match["after"].upper()):
        raise ValueError("Enter an amount such as 24.80 or USD 24.80")
    curr = currency(match["before"] or match["after"], default_currency)
    return money(match["number"], curr), curr


def clean(value: str | None, limit: int = 100) -> str:
    value = (value or "").strip()
    value = re.sub(r"(?<!\d)(?:\d[ .-]?){12,19}(?!\d)", "[redacted card]", value)
    return value[:limit]


def resolve_date(phrase: str | None, today: date) -> date:
    phrase = (phrase or "").strip().lower()
    if phrase == "today":
        return today
    if phrase == "yesterday":
        return today - timedelta(days=1)
    if phrase == "tomorrow":
        return today + timedelta(days=1)
    try:
        return date.fromisoformat(phrase)
    except ValueError as exc:
        raise ValueError("Enter a valid date as YYYY-MM-DD") from exc


def next_renewal(first_due: date, frequency: str, today: date) -> date:
    """Return the next due date, keeping the original day across short months."""
    if frequency not in ("monthly", "quarterly", "yearly"):
        raise ValueError("Choose monthly, quarterly, or yearly")
    if today <= first_due:
        return first_due
    months = {"monthly": 1, "quarterly": 3, "yearly": 12}[frequency]
    elapsed = (today.year - first_due.year) * 12 + today.month - first_due.month
    period = max(0, elapsed // months)
    while True:
        month_index = first_due.year * 12 + first_due.month - 1 + period * months
        year, month = divmod(month_index, 12)
        due = date(year, month + 1, min(first_due.day, monthrange(year, month + 1)[1]))
        if due >= today:
            return due
        period += 1


def subscription_anchor(value: str, frequency: str, today: date) -> date:
    # Leap-year anchor preserves the chosen day after a shortened renewal month.
    value = value.strip()
    if frequency == "monthly":
        if not re.fullmatch(r"\d{1,2}", value) or not 1 <= int(value) <= 31:
            raise ValueError("Enter a day from 1 to 31")
        first = date(2000, 1, int(value))
    elif frequency == "quarterly":
        match = re.fullmatch(r"(\d{1,2})-(\d{1,2})", value)
        if not match:
            raise ValueError("Enter a month and day as MM-DD")
        try:
            first = date(2000, int(match[1]), int(match[2]))
        except ValueError as exc:
            raise ValueError("Enter a valid month and day as MM-DD") from exc
    elif frequency == "yearly":
        first = resolve_date(value, today)
        if first < today:
            raise ValueError("Enter today or a future payment date")
    else:
        raise ValueError("Choose monthly, quarterly, or yearly")
    return first


def monthly_due(day: int, month: date) -> date:
    return date(month.year, month.month, min(day, monthrange(month.year, month.month)[1]))


def csv_safe(value: str) -> str:
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


class Telegram:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, data: dict | None = None):
        request = urllib.request.Request(
            self.base + method,
            data=json.dumps(data or {}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.load(response)
        if not result.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {result.get('description')}")
        return result["result"]

    def send(self, chat: int, text: str, buttons: list[tuple[str, str]] | None = None, *, prompt: bool = False):
        data = {"chat_id": chat, "text": text[:4096]}
        if buttons:
            keys = [{"text": label, "callback_data": action} for label, action in buttons]
            data["reply_markup"] = {"inline_keyboard": [[key] for key in keys] if len(keys) > 3 else [keys]}
        elif prompt:
            data["reply_markup"] = {"force_reply": True}
        return self.call("sendMessage", data)

    def csv(self, chat: int, filename: str, content: bytes) -> None:
        boundary = "spendcuecsvboundary"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{filename}\"\r\n"
                "Content-Type: text/csv\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(self.base + "sendDocument", data=body,
                                         headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.load(response)
        if not result.get("ok"):
            raise RuntimeError("CSV send failed")


def open_db(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS expenses (id INTEGER PRIMARY KEY, amount INTEGER NOT NULL, currency TEXT NOT NULL, merchant TEXT NOT NULL, category TEXT NOT NULL, spent_on TEXT NOT NULL, source_update INTEGER UNIQUE, receipt_hash TEXT, confirmation_message INTEGER, deleted INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS subscriptions (id INTEGER PRIMARY KEY, merchant TEXT NOT NULL, amount INTEGER NOT NULL, currency TEXT NOT NULL, day INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, source_update INTEGER UNIQUE);
    CREATE TABLE IF NOT EXISTS bills (id INTEGER PRIMARY KEY, card_name TEXT NOT NULL, amount INTEGER NOT NULL, currency TEXT NOT NULL, due_on TEXT NOT NULL, paid_on TEXT, source_update INTEGER UNIQUE);
    CREATE TABLE IF NOT EXISTS transfers (id INTEGER PRIMARY KEY, bill_id INTEGER NOT NULL UNIQUE REFERENCES bills(id), amount INTEGER NOT NULL, currency TEXT NOT NULL, paid_on TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS processed_updates (update_id INTEGER PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS reminders (kind TEXT NOT NULL, item_id INTEGER NOT NULL, due_on TEXT NOT NULL, reminder_on TEXT NOT NULL, PRIMARY KEY(kind,item_id,due_on,reminder_on));
    CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS cards (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS card_payments (id INTEGER PRIMARY KEY, bill_id INTEGER NOT NULL REFERENCES bills(id), amount INTEGER NOT NULL, currency TEXT NOT NULL, paid_on TEXT NOT NULL, source_update INTEGER UNIQUE, legacy_transfer_id INTEGER UNIQUE);
    CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL UNIQUE, kind TEXT NOT NULL, step TEXT NOT NULL, payload TEXT NOT NULL);
    """)
    if "cycle_month" not in {row[1] for row in db.execute("PRAGMA table_info(bills)")}:
        db.execute("ALTER TABLE bills ADD COLUMN cycle_month TEXT")
    if "generated" not in {row[1] for row in db.execute("PRAGMA table_info(bills)")}:
        db.execute("ALTER TABLE bills ADD COLUMN generated INTEGER NOT NULL DEFAULT 0")
    card_columns = {row[1] for row in db.execute("PRAGMA table_info(cards)")}
    migrating_cards = "due_day" not in card_columns
    if migrating_cards:
        db.execute("ALTER TABLE cards ADD COLUMN due_day INTEGER")
    if "reminder_days" not in card_columns:
        db.execute("ALTER TABLE cards ADD COLUMN reminder_days INTEGER NOT NULL DEFAULT 7")
    if "created_on" not in card_columns:
        db.execute("ALTER TABLE cards ADD COLUMN created_on TEXT")
    expense_columns = {row[1] for row in db.execute("PRAGMA table_info(expenses)")}
    if "subscription_id" not in expense_columns:
        db.execute("ALTER TABLE expenses ADD COLUMN subscription_id INTEGER")
    if "scheduled_due" not in expense_columns:
        db.execute("ALTER TABLE expenses ADD COLUMN scheduled_due TEXT")
    subscription_columns = {row[1] for row in db.execute("PRAGMA table_info(subscriptions)")}
    if "frequency" not in subscription_columns:
        db.execute("ALTER TABLE subscriptions ADD COLUMN frequency TEXT NOT NULL DEFAULT 'monthly'")
    if "first_due_on" not in subscription_columns:
        db.execute("ALTER TABLE subscriptions ADD COLUMN first_due_on TEXT")
    if "auto_from" not in subscription_columns:
        db.execute("ALTER TABLE subscriptions ADD COLUMN auto_from TEXT")
    with db:
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_subscription_charge ON expenses(subscription_id,scheduled_due)")
        db.execute("UPDATE subscriptions SET first_due_on=printf('2000-01-%02d',day) WHERE first_due_on IS NULL")
        db.execute("UPDATE bills SET cycle_month=substr(due_on,1,7) WHERE cycle_month IS NULL")
        for name in DEFAULT_CATEGORIES:
            db.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", (name,))
        db.execute("INSERT OR IGNORE INTO categories(name) SELECT DISTINCT category FROM expenses WHERE category<>''")
        db.execute("INSERT OR IGNORE INTO cards(name) SELECT DISTINCT card_name FROM bills WHERE card_name<>''")
        db.execute("""UPDATE cards SET created_on=(SELECT MIN(due_on) FROM bills
                     WHERE card_name=cards.name COLLATE NOCASE)
                     WHERE created_on IS NULL AND EXISTS(SELECT 1 FROM bills WHERE card_name=cards.name COLLATE NOCASE)""")
        db.execute("""UPDATE cards SET due_day=(SELECT CAST(substr(due_on,9,2) AS INTEGER)
                     FROM bills WHERE card_name=cards.name COLLATE NOCASE ORDER BY due_on DESC,id DESC LIMIT 1)
                     WHERE due_day IS NULL AND EXISTS(SELECT 1 FROM bills WHERE card_name=cards.name COLLATE NOCASE)""")
        db.execute("INSERT OR IGNORE INTO card_payments(bill_id,amount,currency,paid_on,legacy_transfer_id) SELECT bill_id,amount,currency,paid_on,id FROM transfers")
        if migrating_cards:
            db.execute("DELETE FROM sessions WHERE kind IN ('card_add','bill','bill_edit','payment')")
    return db


class SpendCue:
    def __init__(self, db: sqlite3.Connection, telegram, user_id: int, tz: str = "Asia/Singapore",
                 default_currency: str = "SGD", clock=None):
        self.db, self.telegram, self.user_id = db, telegram, user_id
        self.zone = ZoneInfo(tz)
        self.default_currency = currency(default_currency, "SGD")
        self.clock = clock or (lambda: datetime.now(self.zone))
        with self.db:
            self.db.execute("UPDATE subscriptions SET auto_from=? WHERE auto_from IS NULL", (self.today().isoformat(),))
            self.db.execute("UPDATE cards SET created_on=? WHERE created_on IS NULL", (self.today().isoformat(),))

    def today(self) -> date:
        return self.clock().astimezone(self.zone).date()

    def fmt(self, amount: int, curr: str) -> str:
        return f"{curr} {Decimal(amount) / (10 ** places(curr)):.{places(curr)}f}"

    def send(self, text: str, buttons=None, *, prompt=False):
        return self.telegram.send(self.user_id, text, buttons, prompt=prompt)

    def home(self) -> None:
        self.send("SpendCue · choose what to do:", [
            ("Add spending", "nav:add"), ("Credit cards", "nav:cards"),
            ("Subscriptions", "nav:subs"), ("Overview", "nav:overview"),
            ("Manage", "nav:manage"),
        ])

    def session(self):
        return self.db.execute("SELECT * FROM sessions WHERE user_id=?", (self.user_id,)).fetchone()

    def start(self, kind: str, step: str, payload: dict | None = None) -> None:
        self.db.execute("DELETE FROM sessions WHERE user_id=?", (self.user_id,))
        self.db.execute("INSERT INTO sessions(user_id,kind,step,payload) VALUES(?,?,?,?)",
                        (self.user_id, kind, step, json.dumps(payload or {})))
        self.prompt_step()

    def set_step(self, step: str, payload: dict) -> None:
        self.db.execute("UPDATE sessions SET step=?, payload=? WHERE user_id=?",
                        (step, json.dumps(payload), self.user_id))
        self.prompt_step()

    def clear(self) -> None:
        self.db.execute("DELETE FROM sessions WHERE user_id=?", (self.user_id,))

    def choices(self, session, rows, prefix: str) -> list[tuple[str, str]]:
        return [(str(label), f"s:{session['id']}:{prefix}:{value}") for label, value in rows]

    def prompt_step(self) -> None:
        session = self.session()
        if not session:
            return
        kind, step, ident = session["kind"], session["step"], session["id"]
        p = json.loads(session["payload"])
        if kind in ("expense", "expense_edit"):
            if step == "category":
                rows = self.db.execute("SELECT id,name FROM categories WHERE active=1 ORDER BY id").fetchall()
                self.send("Choose a spending category:", self.choices(session, [(r["name"], r["id"]) for r in rows], "cat"))
            elif step == "amount": self.send("Enter the amount, for example 24.80 or USD 24.80:", prompt=True)
            elif step == "merchant": self.send("What was this for? Enter a merchant or short note:", prompt=True)
            elif step == "date": self.send("When was it spent?", self.choices(session, [("Today", "today"), ("Yesterday", "yesterday"), ("Enter date", "custom")], "date"))
            elif step == "date_input": self.send("Enter the spending date as YYYY-MM-DD:", prompt=True)
            elif step == "edit_field":
                self.send("What would you like to change?", self.choices(session, [("Category", "category"), ("Amount", "amount"), ("Merchant", "merchant"), ("Date", "date")], "field"))
            elif step == "review":
                label = "Save changes" if kind == "expense_edit" else "Save expense"
                self.send(f"Review · {p['merchant']} · {self.fmt(p['amount'], p['currency'])} · {p['category']} · {p['spent_on']}",
                          self.choices(session, [(label, "save"), ("Edit", "edit"), ("Cancel", "cancel")], "action"))
        elif kind == "expense_undo":
            self.send("Delete this expense from spending totals?", self.choices(session, [("Undo expense", "save"), ("Cancel", "cancel")], "action"))
        elif kind in ("card_add", "card_due", "category_add", "category_rename"):
            if step == "name":
                self.send("Enter a card nickname (never a card number):" if kind == "card_add" else "Enter the category name:", prompt=True)
            elif step == "due_day":
                self.send("What day of each month is this card due? Enter 1–31:", prompt=True)
            elif step == "review":
                label = (f"{p['name']} · due day {p['due_day']} · remind 7 days before" if kind == "card_add"
                         else f"{p['name']} · due day {p['due_day']}" if kind == "card_due" else p['name'])
                self.send(f"Review · {label}", self.choices(session, [("Save", "save"), ("Cancel", "cancel")], "action"))
        elif kind == "payment":
            if step == "card":
                month = self.today().strftime("%Y-%m")
                rows = [r for r in self.db.execute("SELECT * FROM cards WHERE active=1 AND due_day IS NOT NULL ORDER BY name")
                        if not self.card_paid(r["name"], month)]
                self.send(f"Which card did you pay for {month}?", self.choices(session, [(r["name"], r["id"]) for r in rows], "card"))
            elif step == "amount": self.send("How much did you pay? For example 450 or USD 450:", prompt=True)
            elif step == "review":
                self.send(f"Review · {p['card_name']} · {self.fmt(p['amount'], p['currency'])} paid {p['paid_on']} for {p['cycle_month']}. This marks the month Paid.",
                          self.choices(session, [("Record payment", "save"), ("Cancel", "cancel")], "action"))
        elif kind in ("subscription", "subscription_edit"):
            if step == "name": self.send("Enter the subscription name:", prompt=True)
            elif step == "amount": self.send("Enter the amount per renewal:", prompt=True)
            elif step == "frequency": self.send("How often is it charged?", self.choices(session, [("Monthly", "monthly"), ("Quarterly", "quarterly"), ("Yearly", "yearly")], "frequency"))
            elif step == "due_on":
                prompt = {"monthly": "Enter the payment day each month (1–31):",
                          "quarterly": "Enter the payment month and day as MM-DD (for example 10-15):",
                          "yearly": "Enter the next payment date as YYYY-MM-DD (today or later):"}[p["frequency"]]
                self.send(prompt, prompt=True)
            elif step == "edit_field": self.send("What would you like to change?", self.choices(session, [("Name", "name"), ("Amount", "amount"), ("Schedule", "frequency")], "field"))
            elif step == "review":
                self.send(f"Review subscription · {p['merchant']} · {self.fmt(p['amount'], p['currency'])} {p['frequency']} · next payment {next_renewal(date.fromisoformat(p['first_due_on']), p['frequency'], self.today())}. Scheduled charges are added to spending automatically.",
                          self.choices(session, [("Save", "save"), ("Edit", "edit"), ("Cancel", "cancel")], "action"))
        elif kind == "overview_range":
            self.send("Enter the " + ("start" if step == "start" else "end") + " date as YYYY-MM-DD:", prompt=True)

    def cards(self) -> None:
        month = self.today().strftime("%Y-%m")
        rows = self.db.execute("SELECT * FROM cards WHERE active=1 ORDER BY name").fetchall()
        lines = [f"Credit cards · {month}"]
        for card in rows:
            payment = self.card_paid(card["name"], month)
            if card["due_day"] is None:
                lines.append(f"{card['name']}: {'Paid' if payment else 'Unpaid'} · set a due day")
                continue
            due = monthly_due(card["due_day"], self.today())
            status = "Paid" if payment else "Unpaid · overdue" if due < self.today() else "Unpaid"
            paid = f" · {self.fmt(payment['amount'],payment['currency'])} recorded" if payment else ""
            reminder = f"{card['reminder_days']} days before" if card["reminder_days"] else "due day only"
            lines.append(f"{card['name']}: {status} · due {due} · reminder {reminder}{paid}")
        if not rows: lines.append("No cards yet.")
        buttons = [("Add card", "cards:add"), ("Record payment", "cards:pay")]
        buttons += [(f"Settings · {r['name'][:20]}", f"cards:view:{r['id']}") for r in rows[:8]]
        self.send("\n".join(lines), buttons + [("Back", "nav:home")])

    def card_paid(self, name: str, month: str):
        return self.db.execute("""SELECT p.currency,SUM(p.amount) amount,MAX(p.paid_on) paid_on
            FROM card_payments p JOIN bills b ON b.id=p.bill_id
            WHERE b.card_name=? COLLATE NOCASE AND b.cycle_month=? GROUP BY p.currency""", (name, month)).fetchone()

    def unpaid_cards(self):
        month = self.today().strftime("%Y-%m")
        return [r for r in self.db.execute("SELECT * FROM cards WHERE active=1 ORDER BY name")
                if not self.card_paid(r["name"], month)]

    def sync_subscriptions(self) -> None:
        """Record scheduled charges through today, including dates missed during downtime."""
        today = self.today()
        with self.db:
            for row in self.db.execute("SELECT * FROM subscriptions WHERE active=1"):
                due = next_renewal(date.fromisoformat(row["first_due_on"]), row["frequency"],
                                   date.fromisoformat(row["auto_from"]))
                while due <= today:
                    self.db.execute("INSERT OR IGNORE INTO expenses(amount,currency,merchant,category,spent_on,subscription_id,scheduled_due) VALUES(?,?,?,?,?,?,?)",
                                    (row["amount"], row["currency"], row["merchant"], "Subscriptions", due.isoformat(), row["id"], due.isoformat()))
                    due = next_renewal(date.fromisoformat(row["first_due_on"]), row["frequency"], due + timedelta(days=1))

    def subscriptions(self) -> None:
        self.sync_subscriptions()
        rows = self.db.execute("SELECT * FROM subscriptions ORDER BY merchant").fetchall()
        lines = ["Subscriptions"] + [f"#{r['id']} {r['merchant']} · {self.fmt(r['amount'],r['currency'])} {r['frequency']} · next {next_renewal(date.fromisoformat(r['first_due_on']), r['frequency'], self.today())} · {'Active' if r['active'] else 'Cancelled'}" for r in rows]
        if not rows: lines.append("None yet.")
        buttons = [("Add subscription", "subs:add")]
        buttons += [(f"Edit {r['merchant'][:20]}", f"subs:view:{r['id']}") for r in rows[:8]]
        buttons.append(("Back", "nav:home"))
        self.send("\n".join(lines), buttons)

    def manage(self) -> None:
        self.send("Manage your records:", [("Categories", "manage:categories"), ("Edit spending", "manage:expenses"), ("Export CSV", "nav:export"), ("Back", "nav:home")])

    def categories(self) -> None:
        rows = self.db.execute("SELECT id,name FROM categories WHERE active=1 ORDER BY id").fetchall()
        self.send("Categories: " + ", ".join(r["name"] for r in rows), [("Add category", "category:add"), ("Rename category", "category:rename"), ("Back", "nav:manage")])

    def expenses_to_edit(self) -> None:
        rows = self.db.execute("SELECT * FROM expenses WHERE deleted=0 ORDER BY id DESC LIMIT 8").fetchall()
        if not rows:
            self.send("No expenses to edit.", [("Back", "nav:manage")]); return
        self.send("Choose an expense:", [(f"#{r['id']} {r['merchant'][:20]} {self.fmt(r['amount'],r['currency'])}", f"expense:edit:{r['id']}") for r in rows] + [("Back", "nav:manage")])

    def months(self) -> None:
        month = self.today().replace(day=1)
        buttons = []
        for _ in range(12):
            buttons.append((month.strftime("%b %Y"), f"overview:month:{month:%Y-%m}"))
            month = (month - timedelta(days=1)).replace(day=1)
        self.send("Choose a month. Older records remain available through Date range.",
                  buttons + [("Older date range", "overview:range"), ("Back", "nav:overview")])

    def overview(self, start: date | None = None, end: date | None = None) -> None:
        self.sync_subscriptions()
        end = end or self.today()
        start = start or end.replace(day=1)
        rows = self.db.execute("SELECT currency,category,SUM(amount) total FROM expenses WHERE deleted=0 AND spent_on BETWEEN ? AND ? GROUP BY currency,category ORDER BY currency,category", (start.isoformat(), end.isoformat())).fetchall()
        lines = [f"Spending · {start} to {end}"]
        totals: dict[str, int] = {}
        for row in rows:
            totals[row["currency"]] = totals.get(row["currency"], 0) + row["total"]
            lines.append(f"{row['category']}: {self.fmt(row['total'], row['currency'])}")
        lines += [f"Total: {self.fmt(total, curr)}" for curr, total in sorted(totals.items())]
        if not rows: lines.append("No spending recorded.")
        scheduled = self.db.execute("SELECT currency,SUM(amount) total FROM expenses WHERE deleted=0 AND subscription_id IS NOT NULL AND spent_on BETWEEN ? AND ? GROUP BY currency ORDER BY currency", (start.isoformat(), end.isoformat())).fetchall()
        if scheduled:
            lines.append("Scheduled subscriptions included above (payment not verified): " + ", ".join(self.fmt(r['total'], r['currency']) for r in scheduled))
        month = end.replace(day=1)
        month_end = monthly_due(31, month)
        cards = self.db.execute("SELECT * FROM cards WHERE active=1 AND created_on<=? ORDER BY name", (month_end.isoformat(),)).fetchall()
        lines.append(f"Card payments · {month:%Y-%m} (excluded from spending total to avoid double counting):")
        for card in cards:
            payment = self.card_paid(card["name"], f"{month:%Y-%m}")
            status = f"Paid · {self.fmt(payment['amount'], payment['currency'])} recorded" if payment else "Unpaid"
            due = f" · due {monthly_due(card['due_day'], month)}" if card["due_day"] else ""
            lines.append(f"{card['name']}: {status}{due}")
        if not cards: lines.append("No cards for this month.")
        self.send("\n".join(lines), [("This week", "overview:week"), ("This month", "overview:month"), ("Recent months", "overview:months"), ("Date range", "overview:range"), ("Upcoming payments", "overview:upcoming"), ("Back", "nav:home")])

    def upcoming(self) -> None:
        self.sync_subscriptions()
        today = self.today()
        end = today + timedelta(days=30)
        items = []
        for row in self.db.execute("SELECT * FROM subscriptions WHERE active=1"):
            due = next_renewal(date.fromisoformat(row["first_due_on"]), row["frequency"], today)
            if due <= end: items.append((due, f"{row['merchant']} · {self.fmt(row['amount'],row['currency'])} expected"))
        month = today.replace(day=1)
        next_month = (month + timedelta(days=32)).replace(day=1)
        for card in self.db.execute("SELECT * FROM cards WHERE active=1 AND due_day IS NOT NULL"):
            for cycle in (month, next_month):
                due = monthly_due(card["due_day"], cycle)
                if due <= end and not self.card_paid(card["name"], cycle.strftime("%Y-%m")):
                    items.append((due, f"{card['name']} · unpaid card payment"))
        items.sort()
        self.send("Upcoming payments:\n" + "\n".join(f"{d}: {label}" for d, label in items) if items else "No upcoming payments in the next 30 days.", [("Back", "nav:overview")])

    def export(self) -> None:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(("type", "id", "name", "amount_minor", "currency", "date_or_day", "category_or_status"))
        for r in self.db.execute("SELECT * FROM expenses WHERE deleted=0 ORDER BY id"):
            writer.writerow(("expense", r["id"], csv_safe(r["merchant"]), r["amount"], r["currency"], r["spent_on"], csv_safe(r["category"])))
        for r in self.db.execute("SELECT * FROM subscriptions ORDER BY id"):
            writer.writerow(("subscription_" + r["frequency"], r["id"], csv_safe(r["merchant"]), r["amount"], r["currency"], r["first_due_on"], "active" if r["active"] else "paused"))
        for r in self.db.execute("SELECT * FROM bills WHERE generated=0 ORDER BY id"):
            writer.writerow(("card_bill", r["id"], csv_safe(r["card_name"]), r["amount"], r["currency"], r["due_on"], "paid" if r["paid_on"] else "unpaid"))
        for r in self.db.execute("SELECT p.*,b.card_name FROM card_payments p JOIN bills b ON b.id=p.bill_id ORDER BY p.id"):
            writer.writerow(("card_payment", r["id"], csv_safe(r["card_name"]), r["amount"], r["currency"], r["paid_on"], "transfer"))
        self.telegram.csv(self.user_id, f"spendcue-{self.today()}.csv", output.getvalue().encode("utf-8-sig"))

    def handle_text(self, text: str) -> None:
        raw = text.strip().lower()
        command = raw.split(maxsplit=1)[0] if raw.startswith("/") else raw
        routes = {"/start": "home", "/menu": "home", "menu": "home", "/add": "add", "add": "add",
                  "/cards": "cards", "/cc": "cards", "/creditcard": "cards", "/subs": "subs", "/subscriptions": "subs",
                  "/overview": "overview", "/manage": "manage", "/export": "export"}
        if command in ("/cancel", "cancel"):
            self.clear(); self.home(); return
        if command == "/help":
            self.send("Use /add, /cards, /subs, /overview, or /manage. /menu shows buttons; /cancel stops the current form. /export sends a CSV.")
            return
        if command in routes:
            self.clear(); self.navigate(routes[command]); return
        session = self.session()
        if not session:
            self.send("Choose a menu option or send /help.", [("Open menu", "nav:home")]); return
        kind, step = session["kind"], session["step"]
        p = json.loads(session["payload"])
        try:
            if kind in ("expense", "expense_edit") and step in ("amount", "merchant", "date_input"):
                if step == "amount":
                    p["amount"], p["currency"] = amount_input(text, p.get("currency", self.default_currency))
                    next_step = "review" if "spent_on" in p else "merchant"
                elif step == "merchant":
                    p["merchant"] = clean(text)
                    if not p["merchant"]: raise ValueError("Enter a merchant or note")
                    next_step = "review" if "spent_on" in p else "date"
                else:
                    p["spent_on"] = resolve_date(text, self.today()).isoformat(); next_step = "review"
            elif kind in ("card_add", "category_add", "category_rename") and step == "name":
                p["name"] = clean(text, 40)
                if not p["name"] or p["name"] == "[redacted card]": raise ValueError("Enter a short name, not a card number")
                next_step = "due_day" if kind == "card_add" else "review"
            elif kind in ("card_add", "card_due") and step == "due_day":
                if not re.fullmatch(r"\d{1,2}", text.strip()) or not 1 <= int(text.strip()) <= 31:
                    raise ValueError("Enter a day from 1 to 31")
                p["due_day"] = int(text.strip()); next_step = "review"
            elif kind == "payment" and step == "amount":
                p["amount"], p["currency"] = amount_input(text, self.default_currency)
                p["paid_on"] = self.today().isoformat(); next_step = "review"
            elif kind in ("subscription", "subscription_edit") and step in ("name", "amount", "due_on"):
                if step == "name":
                    p["merchant"] = clean(text)
                    if not p["merchant"]: raise ValueError("Enter a subscription name")
                    next_step = "review" if "first_due_on" in p else "amount"
                elif step == "amount":
                    p["amount"], p["currency"] = amount_input(text, p.get("currency", self.default_currency))
                    next_step = "review" if "first_due_on" in p else "frequency"
                else:
                    due = subscription_anchor(text, p["frequency"], self.today())
                    p["first_due_on"] = due.isoformat(); p["day"] = due.day; p["auto_from"] = self.today().isoformat()
                    next_step = "review"
            elif kind == "overview_range" and step in ("start", "end"):
                selected = resolve_date(text, self.today())
                if step == "start":
                    p["start"] = selected.isoformat(); next_step = "end"
                else:
                    if selected < date.fromisoformat(p["start"]): raise ValueError("End date is before start date")
                    self.clear(); self.overview(date.fromisoformat(p["start"]), selected); return
            else:
                self.send("Use the buttons shown above, or /cancel to stop."); return
            self.set_step(next_step, p)
        except ValueError as exc:
            self.send(str(exc)); self.prompt_step()

    def navigate(self, destination: str) -> None:
        if destination == "home": self.home()
        elif destination == "add": self.start("expense", "category")
        elif destination == "cards": self.cards()
        elif destination == "subs": self.subscriptions()
        elif destination == "overview": self.overview()
        elif destination == "manage": self.manage()
        elif destination == "export": self.export()

    def handle_callback(self, callback: dict, update_id: int) -> None:
        try:
            self.telegram.call("answerCallbackQuery", {"callback_query_id": callback["id"]})
        except (urllib.error.URLError, RuntimeError):
            pass  # An expired button still has an action to process.
        data = callback.get("data", "")
        if data.startswith("nav:"):
            self.clear(); self.navigate(data.split(":", 1)[1]); return
        if data.startswith("cards:"):
            action = data.split(":", 1)[1]
            if action == "add": self.start("card_add", "name")
            elif action == "pay":
                if any(r["due_day"] is not None for r in self.unpaid_cards()): self.start("payment", "card")
                else: self.send("No unpaid cards with a due day this month.")
            elif action.startswith("view:") and action[5:].isdigit():
                card = self.db.execute("SELECT * FROM cards WHERE id=? AND active=1", (int(action[5:]),)).fetchone()
                if card:
                    reminder = f"{card['reminder_days']} days before" if card["reminder_days"] else "due day only"
                    self.send(f"{card['name']} · due day {card['due_day'] or 'not set'} · remind {reminder}",
                              [("Change due day", f"cards:due:{card['id']}"), ("Reminder timing", f"cards:alerts:{card['id']}"), ("Back", "nav:cards")])
            elif action.startswith("due:") and action[4:].isdigit():
                card = self.db.execute("SELECT * FROM cards WHERE id=? AND active=1", (int(action[4:]),)).fetchone()
                if card: self.start("card_due", "due_day", {"id": card["id"], "name": card["name"]})
            elif action.startswith("alerts:") and action[7:].isdigit():
                card = self.db.execute("SELECT * FROM cards WHERE id=? AND active=1", (int(action[7:]),)).fetchone()
                if card:
                    self.send(f"Remind me before {card['name']} is due:",
                              [("Due day only", f"cards:alert:{card['id']}:0"),
                               ("1 day", f"cards:alert:{card['id']}:1"), ("3 days", f"cards:alert:{card['id']}:3"),
                               ("1 week", f"cards:alert:{card['id']}:7"), ("2 weeks", f"cards:alert:{card['id']}:14")])
            elif re.fullmatch(r"alert:\d+:(0|1|3|7|14)", action):
                _, card_id, days = action.split(":")
                self.db.execute("UPDATE cards SET reminder_days=? WHERE id=? AND active=1", (int(days), int(card_id)))
                self.cards()
            return
        if data.startswith("subs:"):
            parts = data.split(":")
            if parts[1] == "add": self.start("subscription", "name")
            elif len(parts) == 3 and parts[1] == "view" and parts[2].isdigit():
                row = self.db.execute("SELECT * FROM subscriptions WHERE id=?", (int(parts[2]),)).fetchone()
                if row:
                    self.send(f"{row['merchant']} · {self.fmt(row['amount'],row['currency'])} {row['frequency']} · next {next_renewal(date.fromisoformat(row['first_due_on']), row['frequency'], self.today())}",
                              [("Edit", f"subs:edit:{row['id']}"), ("Cancel renewals" if row["active"] else "Reactivate", f"subs:toggle:{row['id']}"), ("Back", "nav:subs")])
            elif len(parts) == 3 and parts[2].isdigit() and parts[1] in ("edit", "toggle"):
                row = self.db.execute("SELECT * FROM subscriptions WHERE id=?", (int(parts[2]),)).fetchone()
                if not row: return
                if parts[1] == "edit": self.start("subscription_edit", "edit_field", dict(row))
                else:
                    if row["active"]: self.sync_subscriptions()
                    self.db.execute("UPDATE subscriptions SET active=1-active,auto_from=? WHERE id=?",
                                    (self.today().isoformat() if not row["active"] else row["auto_from"], row["id"]))
                    self.subscriptions()
            return
        if data.startswith("manage:"):
            if data == "manage:categories": self.categories()
            elif data == "manage:expenses": self.expenses_to_edit()
            return
        if data == "category:add": self.start("category_add", "name"); return
        if data == "category:rename":
            session_kind = "category_rename"
            self.start(session_kind, "choose");
            rows = self.db.execute("SELECT id,name FROM categories WHERE active=1 ORDER BY id").fetchall()
            self.send("Choose a category to rename:", self.choices(self.session(), [(r["name"], r["id"]) for r in rows], "category"))
            return
        if data.startswith("expense:"):
            parts = data.split(":")
            if len(parts) != 3 or not parts[2].isdigit(): return
            row = self.db.execute("SELECT * FROM expenses WHERE id=? AND deleted=0", (int(parts[2]),)).fetchone()
            if not row:
                self.send("Expense not found."); return
            if parts[1] == "edit": self.start("expense_edit", "edit_field", dict(row))
            elif parts[1] == "undo": self.start("expense_undo", "review", {"expense_id": row["id"]})
            return
        if data.startswith("overview:"):
            action = data.split(":", 1)[1]
            if action == "week": self.overview(self.today() - timedelta(days=self.today().weekday()), self.today())
            elif action == "month": self.overview()
            elif action == "months": self.months()
            elif re.fullmatch(r"month:\d{4}-\d{2}", action):
                try:
                    month = date.fromisoformat(action[6:] + "-01")
                except ValueError:
                    return
                if month <= self.today().replace(day=1):
                    end = date(month.year, month.month, monthrange(month.year, month.month)[1])
                    self.overview(month, min(end, self.today()))
            elif action == "range": self.start("overview_range", "start")
            elif action == "upcoming": self.upcoming()
            return
        if not data.startswith("s:"): return
        parts = data.split(":", 2)
        if len(parts) != 3 or not parts[1].isdigit(): return
        session = self.session()
        if not session or session["id"] != int(parts[1]):
            self.send("That form has expired. Open /menu to start again."); return
        self.session_action(session, parts[2], update_id)

    def session_action(self, session, action: str, update_id: int) -> None:
        kind, step = session["kind"], session["step"]
        p = json.loads(session["payload"])
        if action == "action:cancel":
            self.clear(); self.home(); return
        if step == "review":
            if action == "action:save": self.save_session(session, p, update_id)
            elif action == "action:edit":
                self.set_step("edit_field", p) if kind in ("expense", "expense_edit", "subscription", "subscription_edit") else self.send("Use Cancel and start again to change this entry.")
            return
        if kind in ("expense", "expense_edit"):
            if step == "category" and action.startswith("cat:") and action[4:].isdigit():
                row = self.db.execute("SELECT name FROM categories WHERE id=? AND active=1", (int(action[4:]),)).fetchone()
                if row:
                    p["category"] = row["name"]
                    self.set_step("review" if kind == "expense_edit" or "amount" in p else "amount", p)
            elif step == "edit_field" and action.startswith("field:"):
                field = action[6:]
                if field in ("category", "amount", "merchant", "date"):
                    self.set_step(field, p)
            elif step == "date" and action.startswith("date:"):
                choice = action[5:]
                if choice == "custom": self.set_step("date_input", p)
                elif choice in ("today", "yesterday"):
                    p["spent_on"] = resolve_date(choice, self.today()).isoformat(); self.set_step("review", p)
        elif kind == "category_rename" and step == "choose" and action.startswith("category:") and action[9:].isdigit():
            row = self.db.execute("SELECT id,name FROM categories WHERE id=? AND active=1", (int(action[9:]),)).fetchone()
            if row: p["category_id"] = row["id"]; p["old_name"] = row["name"]; self.set_step("name", p)
        elif kind == "payment" and step == "card" and action.startswith("card:") and action[5:].isdigit():
            row = self.db.execute("SELECT * FROM cards WHERE id=? AND active=1 AND due_day IS NOT NULL", (int(action[5:]),)).fetchone()
            month = self.today().strftime("%Y-%m")
            if row and not self.card_paid(row["name"], month):
                p.update({"card_id": row["id"], "card_name": row["name"], "cycle_month": month})
                self.set_step("amount", p)
        elif kind in ("subscription", "subscription_edit"):
            if step == "edit_field" and action.startswith("field:"):
                field = action[6:]
                if field in ("name", "amount", "frequency"): self.set_step(field, p)
            elif step == "frequency" and action.startswith("frequency:"):
                frequency = action[10:]
                if frequency in ("monthly", "quarterly", "yearly"):
                    p["frequency"] = frequency
                    self.set_step("due_on", p)

    def save_session(self, session, p: dict, update_id: int) -> None:
        kind = session["kind"]
        try:
            if kind == "expense":
                row = self.db.execute("INSERT INTO expenses(amount,currency,merchant,category,spent_on,source_update) VALUES(?,?,?,?,?,?)",
                                      (p["amount"], p["currency"], p["merchant"], p["category"], p["spent_on"], update_id))
                expense_id = row.lastrowid
                label = "Saved expense"
            elif kind == "expense_edit":
                self.db.execute("UPDATE expenses SET amount=?,currency=?,merchant=?,category=?,spent_on=? WHERE id=? AND deleted=0",
                                (p["amount"], p["currency"], p["merchant"], p["category"], p["spent_on"], p["id"]))
                expense_id = p["id"]; label = "Updated expense"
            elif kind == "expense_undo":
                self.db.execute("UPDATE expenses SET deleted=1 WHERE id=?", (p["expense_id"],))
                self.clear(); self.send("Expense removed from spending totals."); return
            elif kind == "card_add":
                self.db.execute("INSERT INTO cards(name,due_day,created_on) VALUES(?,?,?)", (p["name"], p["due_day"], self.today().isoformat()))
                self.clear(); self.cards(); return
            elif kind == "card_due":
                self.db.execute("UPDATE cards SET due_day=? WHERE id=? AND active=1", (p["due_day"], p["id"]))
                self.clear(); self.cards(); return
            elif kind == "category_add":
                self.db.execute("INSERT INTO categories(name) VALUES(?)", (p["name"],))
                self.clear(); self.categories(); return
            elif kind == "category_rename":
                self.db.execute("UPDATE categories SET name=? WHERE id=?", (p["name"], p["category_id"]))
                self.db.execute("UPDATE expenses SET category=? WHERE category=? COLLATE NOCASE", (p["name"], p["old_name"]))
                self.clear(); self.categories(); return
            elif kind == "payment":
                card = self.db.execute("SELECT * FROM cards WHERE id=? AND active=1 AND due_day IS NOT NULL", (p["card_id"],)).fetchone()
                if not card or self.card_paid(card["name"], p["cycle_month"]):
                    self.clear(); self.send("This card is already paid for that month, or its due day is missing."); return
                bill = self.db.execute("SELECT id FROM bills WHERE card_name=? COLLATE NOCASE AND cycle_month=? ORDER BY id DESC LIMIT 1",
                                       (card["name"], p["cycle_month"])).fetchone()
                if bill:
                    bill_id = bill["id"]
                    self.db.execute("UPDATE bills SET paid_on=? WHERE id=?", (p["paid_on"], bill_id))
                else:
                    month = date.fromisoformat(p["cycle_month"] + "-01")
                    due = monthly_due(card["due_day"], month).isoformat()
                    bill_id = self.db.execute("INSERT INTO bills(card_name,amount,currency,due_on,paid_on,cycle_month,source_update,generated) VALUES(?,?,?,?,?,?,?,1)",
                                              (card["name"], p["amount"], p["currency"], due, p["paid_on"], p["cycle_month"], update_id)).lastrowid
                self.db.execute("INSERT INTO card_payments(bill_id,amount,currency,paid_on,source_update) VALUES(?,?,?,?,?)",
                                (bill_id, p["amount"], p["currency"], p["paid_on"], update_id))
                self.clear(); self.send(f"{card['name']} marked Paid for {p['cycle_month']}: {self.fmt(p['amount'],p['currency'])} recorded. This payment is excluded from spending totals."); return
            elif kind == "subscription":
                self.db.execute("INSERT INTO subscriptions(merchant,amount,currency,day,frequency,first_due_on,auto_from,source_update) VALUES(?,?,?,?,?,?,?,?)",
                                (p["merchant"], p["amount"], p["currency"], p["day"], p["frequency"], p["first_due_on"], p["auto_from"], update_id))
                self.clear(); self.subscriptions(); return
            elif kind == "subscription_edit":
                self.sync_subscriptions()
                self.db.execute("UPDATE subscriptions SET merchant=?,amount=?,currency=?,day=?,frequency=?,first_due_on=?,auto_from=? WHERE id=?",
                                (p["merchant"], p["amount"], p["currency"], p["day"], p["frequency"], p["first_due_on"], p["auto_from"], p["id"]))
                self.clear(); self.subscriptions(); return
            else: return
            self.clear()
            sent = self.send(f"{label} #{expense_id}: {p['merchant']} · {self.fmt(p['amount'],p['currency'])} · {p['category']} · {p['spent_on']}",
                             [("Edit", f"expense:edit:{expense_id}"), ("Undo", f"expense:undo:{expense_id}")])
            self.db.execute("UPDATE expenses SET confirmation_message=? WHERE id=?", (sent["message_id"], expense_id))
        except sqlite3.IntegrityError:
            if kind in ("card_add", "category_add", "category_rename"):
                self.send("That name already exists. Enter a different one.")
                self.set_step("name", p)
            else:
                raise

    def handle_update(self, update: dict) -> None:
        uid = update["update_id"]
        if self.db.execute("SELECT 1 FROM processed_updates WHERE update_id=?", (uid,)).fetchone(): return
        msg, callback = update.get("message"), update.get("callback_query")
        source = msg or (callback or {}).get("message") or {}
        actor = msg.get("from") if msg else (callback or {}).get("from")
        allowed = source.get("chat", {}).get("type") == "private" and (actor or {}).get("id") == self.user_id and source.get("chat", {}).get("id") == self.user_id
        with self.db:
            if allowed:
                if msg:
                    if msg.get("photo"):
                        self.send("Receipt extraction is off. Use /add to enter the purchase.")
                    else: self.handle_text((msg.get("text") or "").strip())
                elif callback: self.handle_callback(callback, uid)
            self.db.execute("INSERT OR IGNORE INTO processed_updates(update_id) VALUES(?)", (uid,))

    def reminders(self) -> None:
        self.sync_subscriptions()
        today = self.today()
        for row in self.db.execute("SELECT * FROM subscriptions WHERE active=1"):
            for offset in (0, 3):
                due = today + timedelta(days=offset)
                if next_renewal(date.fromisoformat(row["first_due_on"]), row["frequency"], due) == due:
                    self.remind("subscription", row["id"], due, today, f"{row['merchant']} renews for {self.fmt(row['amount'],row['currency'])}")
        month = today.replace(day=1)
        next_month = (month + timedelta(days=32)).replace(day=1)
        for card in self.db.execute("SELECT * FROM cards WHERE active=1 AND due_day IS NOT NULL"):
            for cycle in (month, next_month):
                due = monthly_due(card["due_day"], cycle)
                if (due - today).days in (0, card["reminder_days"]) and not self.card_paid(card["name"], cycle.strftime("%Y-%m")):
                    self.remind("card", card["id"], due, today, f"{card['name']} card payment due")

    def remind(self, kind: str, item_id: int, due: date, today: date, label: str) -> None:
        key = (kind, item_id, due.isoformat(), today.isoformat())
        with self.db:
            inserted = self.db.execute("INSERT OR IGNORE INTO reminders VALUES(?,?,?,?)", key).rowcount
        if not inserted: return
        try:
            days = (due - today).days
            self.send(f"Reminder · {label} on {due}" + (" (today)" if days == 0 else f" (in {days} day{'s' if days != 1 else ''})"))
        except Exception:
            with self.db: self.db.execute("DELETE FROM reminders WHERE kind=? AND item_id=? AND due_on=? AND reminder_on=?", key)
            raise


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    required = ("TELEGRAM_BOT_TOKEN", "ALLOWED_TELEGRAM_USER_ID")
    missing = [key for key in required if not os.getenv(key)]
    if missing: sys.exit("Missing environment variables: " + ", ".join(missing))
    db = open_db(os.getenv("DB_PATH", "spendcue.db"))
    bot = SpendCue(db, Telegram(os.environ["TELEGRAM_BOT_TOKEN"]), int(os.environ["ALLOWED_TELEGRAM_USER_ID"]),
                   os.getenv("TIMEZONE", "Asia/Singapore"), os.getenv("DEFAULT_CURRENCY", "SGD"))
    try:
        bot.telegram.call("setMyCommands", {"commands": [
            {"command": "menu", "description": "Open SpendCue"},
            {"command": "add", "description": "Add spending"},
            {"command": "cards", "description": "Credit cards and monthly payments"},
            {"command": "subs", "description": "Subscriptions"},
            {"command": "overview", "description": "Spending and upcoming payments"},
            {"command": "manage", "description": "Categories and edits"},
            {"command": "export", "description": "Download CSV"},
        ]})
    except (urllib.error.URLError, TimeoutError, RuntimeError):
        logging.warning("Could not register slash commands; polling will continue")
    while True:
        try:
            bot.reminders()
            last = db.execute("SELECT MAX(update_id) FROM processed_updates").fetchone()[0]
            updates = bot.telegram.call("getUpdates", {"offset": (last or 0) + 1, "timeout": 30, "allowed_updates": ["message", "callback_query"]})
            for update in updates:
                try: bot.handle_update(update)
                except Exception as exc:
                    logging.error("Update %s failed (%s); will retry", update.get("update_id"), type(exc).__name__)
                    break
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            logging.error("Polling failed (%s); retrying", type(exc).__name__)
            time.sleep(3)


if __name__ == "__main__": main()
