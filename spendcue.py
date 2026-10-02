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
        db.execute("INSERT OR IGNORE INTO card_payments(bill_id,amount,currency,paid_on,legacy_transfer_id) SELECT bill_id,amount,currency,paid_on,id FROM transfers")
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
        elif kind in ("card_add", "category_add", "category_rename"):
            if step == "name":
                self.send("Enter a card nickname (never a card number):" if kind == "card_add" else "Enter the category name:", prompt=True)
            elif step == "review":
                self.send(f"Review · {p['name']}", self.choices(session, [("Save", "save"), ("Cancel", "cancel")], "action"))
        elif kind in ("bill", "bill_edit"):
            if step == "card":
                rows = self.db.execute("SELECT id,name FROM cards WHERE active=1 ORDER BY name").fetchall()
                self.send("Choose the card for this bill:", self.choices(session, [(r["name"], r["id"]) for r in rows], "card"))
            elif step == "amount": self.send("Enter the statement amount:", prompt=True)
            elif step == "date": self.send("Choose the exact payment due date:", self.choices(session, [("Today", "today"), ("Tomorrow", "tomorrow"), ("Enter date", "custom")], "date"))
            elif step == "date_input": self.send("Enter the due date as YYYY-MM-DD:", prompt=True)
            elif step == "edit_field": self.send("What would you like to change?", self.choices(session, [("Amount", "amount"), ("Due date", "date")], "field"))
            elif step == "review":
                self.send(f"Review bill · {p['card_name']} · {self.fmt(p['amount'], p['currency'])} due {p['due_on']}",
                          self.choices(session, [("Save changes" if kind == "bill_edit" else "Save bill", "save"), ("Edit", "edit"), ("Cancel", "cancel")], "action"))
        elif kind == "payment":
            if step == "bill":
                rows = self.unpaid_bills()
                self.send("Choose the bill you paid:", self.choices(session, [(f"{r['card_name']} · {r['due_on']} · {self.fmt(r['remaining'], r['currency'])} left", r["id"]) for r in rows[:10]], "bill"))
            elif step == "amount": self.send(f"Enter the payment amount. Remaining: {self.fmt(p['remaining'], p['currency'])}", prompt=True)
            elif step == "date": self.send("When did you pay it?", self.choices(session, [("Today", "today"), ("Yesterday", "yesterday"), ("Enter date", "custom")], "date"))
            elif step == "date_input": self.send("Enter the payment date as YYYY-MM-DD:", prompt=True)
            elif step == "review":
                self.send(f"Review card payment · {p['card_name']} · {self.fmt(p['amount'], p['currency'])} on {p['paid_on']}. It will appear separately in the monthly overview.",
                          self.choices(session, [("Record payment", "save"), ("Cancel", "cancel")], "action"))
        elif kind in ("subscription", "subscription_edit"):
            if step == "name": self.send("Enter the subscription name:", prompt=True)
            elif step == "amount": self.send("Enter the amount per renewal:", prompt=True)
            elif step == "frequency": self.send("How often is it charged?", self.choices(session, [("Monthly", "monthly"), ("Quarterly", "quarterly"), ("Yearly", "yearly")], "frequency"))
            elif step == "due_on": self.send("Enter the next payment date as YYYY-MM-DD (today or later):", prompt=True)
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
            bill = self.db.execute("SELECT * FROM bills WHERE card_name=? COLLATE NOCASE AND cycle_month=? ORDER BY id DESC LIMIT 1", (card["name"], month)).fetchone()
            if bill:
                paid = self.paid_amount(bill["id"])
                status = "Paid" if bill["paid_on"] or paid >= bill["amount"] else "Part paid" if paid else "Unpaid"
                lines.append(f"{card['name']}: {status} · {self.fmt(bill['amount'], bill['currency'])} · due {bill['due_on']}" + (f" · {self.fmt(paid, bill['currency'])} paid" if paid else ""))
            else:
                lines.append(f"{card['name']}: No bill entered for this due month")
        older = [r for r in self.unpaid_bills() if r["cycle_month"] < month]
        for bill in older:
            lines.append(f"OVERDUE · {bill['card_name']} · {self.fmt(bill['remaining'],bill['currency'])} remaining · due {bill['due_on']}")
        if not rows: lines.append("No cards yet.")
        self.send("\n".join(lines), [("Add card", "cards:add"), ("Add bill", "cards:bill"), ("Record payment", "cards:pay"), ("Edit bill", "cards:edit"), ("Back", "nav:home")])

    def paid_amount(self, bill_id: int) -> int:
        return self.db.execute("SELECT COALESCE(SUM(amount),0) FROM card_payments WHERE bill_id=?", (bill_id,)).fetchone()[0]

    def unpaid_bills(self):
        rows = self.db.execute("SELECT * FROM bills WHERE paid_on IS NULL ORDER BY due_on,id").fetchall()
        return [dict(row, remaining=row["amount"] - self.paid_amount(row["id"])) for row in rows if row["amount"] > self.paid_amount(row["id"])]

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
        payments = self.db.execute("SELECT currency,SUM(amount) total FROM card_payments WHERE paid_on BETWEEN ? AND ? GROUP BY currency ORDER BY currency", (start.isoformat(), end.isoformat())).fetchall()
        lines.append("Card payments recorded (separate from spending): " + (", ".join(self.fmt(r['total'], r['currency']) for r in payments) if payments else "none"))
        lines.append(f"Unpaid or part-paid card bills now: {len(self.unpaid_bills())}")
        self.send("\n".join(lines), [("This week", "overview:week"), ("This month", "overview:month"), ("Recent months", "overview:months"), ("Date range", "overview:range"), ("Upcoming payments", "overview:upcoming"), ("Back", "nav:home")])

    def upcoming(self) -> None:
        self.sync_subscriptions()
        today = self.today()
        end = today + timedelta(days=30)
        items = []
        for row in self.db.execute("SELECT * FROM subscriptions WHERE active=1"):
            due = next_renewal(date.fromisoformat(row["first_due_on"]), row["frequency"], today)
            if due <= end: items.append((due, f"{row['merchant']} · {self.fmt(row['amount'],row['currency'])} expected"))
        for row in self.unpaid_bills():
            due = date.fromisoformat(row["due_on"])
            if due <= end: items.append((due, f"{row['card_name']} · {self.fmt(row['remaining'],row['currency'])} remaining"))
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
        for r in self.db.execute("SELECT * FROM bills ORDER BY id"):
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
                next_step = "review"
            elif kind in ("bill", "bill_edit") and step in ("amount", "date_input"):
                if step == "amount":
                    p["amount"], p["currency"] = amount_input(text, p.get("currency", self.default_currency))
                    next_step = "review" if "due_on" in p else "date"
                else:
                    p["due_on"] = resolve_date(text, self.today()).isoformat(); next_step = "review"
            elif kind == "payment" and step in ("amount", "date_input"):
                if step == "amount":
                    amount, curr = amount_input(text, p["currency"])
                    if curr != p["currency"]: raise ValueError("Payment currency must match the bill")
                    if amount > p["remaining"]: raise ValueError("Payment exceeds the remaining bill")
                    p["amount"] = amount; next_step = "date"
                else:
                    p["paid_on"] = resolve_date(text, self.today()).isoformat(); next_step = "review"
            elif kind in ("subscription", "subscription_edit") and step in ("name", "amount", "due_on"):
                if step == "name":
                    p["merchant"] = clean(text)
                    if not p["merchant"]: raise ValueError("Enter a subscription name")
                    next_step = "review" if "first_due_on" in p else "amount"
                elif step == "amount":
                    p["amount"], p["currency"] = amount_input(text, p.get("currency", self.default_currency))
                    next_step = "review" if "first_due_on" in p else "frequency"
                else:
                    due = resolve_date(text, self.today())
                    if due < self.today(): raise ValueError("Enter today or a future payment date")
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
            elif action == "bill":
                if self.db.execute("SELECT 1 FROM cards WHERE active=1").fetchone(): self.start("bill", "card")
                else: self.send("Add a card nickname first.", [("Add card", "cards:add")])
            elif action == "pay":
                if self.unpaid_bills(): self.start("payment", "bill")
                else: self.send("No unpaid card bills found.")
            elif action == "edit":
                rows = self.db.execute("SELECT id,card_name,amount,currency,due_on FROM bills ORDER BY id DESC LIMIT 8").fetchall()
                self.send("Choose a bill to edit:", [(f"{r['card_name']} · {r['due_on']} · {self.fmt(r['amount'],r['currency'])}", f"cards:edit:{r['id']}") for r in rows] + [("Back", "nav:cards")]) if rows else self.send("No bills to edit.")
            elif action.startswith("edit:") and action[5:].isdigit():
                row = self.db.execute("SELECT * FROM bills WHERE id=?", (int(action[5:]),)).fetchone()
                if row: self.start("bill_edit", "edit_field", dict(row))
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
                self.set_step("edit_field", p) if kind in ("expense", "expense_edit", "subscription", "subscription_edit", "bill", "bill_edit") else self.send("Use Cancel and start again to change this entry.")
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
        elif kind in ("bill", "bill_edit"):
            if step == "card" and action.startswith("card:") and action[5:].isdigit():
                row = self.db.execute("SELECT name FROM cards WHERE id=? AND active=1", (int(action[5:]),)).fetchone()
                if row: p["card_name"] = row["name"]; self.set_step("amount", p)
            elif step == "date" and action.startswith("date:"):
                choice = action[5:]
                if choice == "custom": self.set_step("date_input", p)
                elif choice in ("today", "tomorrow"):
                    p["due_on"] = resolve_date(choice, self.today()).isoformat(); self.set_step("review", p)
            elif step == "edit_field" and action in ("field:amount", "field:date"):
                self.set_step(action[6:], p)
        elif kind == "payment":
            if step == "bill" and action.startswith("bill:") and action[5:].isdigit():
                row = self.db.execute("SELECT * FROM bills WHERE id=? AND paid_on IS NULL", (int(action[5:]),)).fetchone()
                if row:
                    p.update({"bill_id": row["id"], "card_name": row["card_name"], "currency": row["currency"], "remaining": row["amount"] - self.paid_amount(row["id"])})
                    self.set_step("amount", p)
            elif step == "date" and action.startswith("date:"):
                choice = action[5:]
                if choice == "custom": self.set_step("date_input", p)
                elif choice in ("today", "yesterday"):
                    p["paid_on"] = resolve_date(choice, self.today()).isoformat(); self.set_step("review", p)
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
                self.db.execute("INSERT INTO cards(name) VALUES(?)", (p["name"],))
                self.clear(); self.cards(); return
            elif kind == "category_add":
                self.db.execute("INSERT INTO categories(name) VALUES(?)", (p["name"],))
                self.clear(); self.categories(); return
            elif kind == "category_rename":
                self.db.execute("UPDATE categories SET name=? WHERE id=?", (p["name"], p["category_id"]))
                self.db.execute("UPDATE expenses SET category=? WHERE category=? COLLATE NOCASE", (p["name"], p["old_name"]))
                self.clear(); self.categories(); return
            elif kind == "bill":
                cycle = p["due_on"][:7]
                if self.db.execute("SELECT 1 FROM bills WHERE card_name=? COLLATE NOCASE AND cycle_month=?", (p["card_name"], cycle)).fetchone():
                    self.send(f"A {p['card_name']} bill already exists for {cycle}. Cancel this form and start again if the due month is wrong."); return
                self.db.execute("INSERT INTO bills(card_name,amount,currency,due_on,cycle_month,source_update) VALUES(?,?,?,?,?,?)",
                                (p["card_name"], p["amount"], p["currency"], p["due_on"], cycle, update_id))
                self.clear(); self.cards(); return
            elif kind == "bill_edit":
                bill = self.db.execute("SELECT * FROM bills WHERE id=?", (p["id"],)).fetchone()
                if not bill: self.send("Bill not found."); return
                paid = self.paid_amount(bill["id"])
                if paid > p["amount"] or (paid and p["currency"] != bill["currency"]):
                    self.send("The revised amount must cover recorded payments, and their currency cannot change."); return
                cycle = p["due_on"][:7]
                if self.db.execute("SELECT 1 FROM bills WHERE id<>? AND card_name=? COLLATE NOCASE AND cycle_month=?", (bill["id"], bill["card_name"], cycle)).fetchone():
                    self.send("Another bill for this card already uses that due month."); return
                paid_on = self.db.execute("SELECT MAX(paid_on) FROM card_payments WHERE bill_id=?", (bill["id"],)).fetchone()[0] if paid == p["amount"] else None
                self.db.execute("UPDATE bills SET amount=?,currency=?,due_on=?,cycle_month=?,paid_on=? WHERE id=?",
                                (p["amount"], p["currency"], p["due_on"], cycle, paid_on, bill["id"]))
                self.clear(); self.cards(); return
            elif kind == "payment":
                bill = self.db.execute("SELECT * FROM bills WHERE id=? AND paid_on IS NULL", (p["bill_id"],)).fetchone()
                if not bill: self.send("This bill is already paid."); return
                remaining = bill["amount"] - self.paid_amount(bill["id"])
                if p["currency"] != bill["currency"] or not 0 < p["amount"] <= remaining:
                    self.send("Payment no longer matches the outstanding bill. Start again from /cards."); return
                self.db.execute("INSERT INTO card_payments(bill_id,amount,currency,paid_on,source_update) VALUES(?,?,?,?,?)",
                                (bill["id"], p["amount"], p["currency"], p["paid_on"], update_id))
                if p["amount"] == remaining:
                    self.db.execute("UPDATE bills SET paid_on=? WHERE id=?", (p["paid_on"], bill["id"]))
                self.clear(); self.send("Card payment recorded in the monthly overview. " + ("Bill paid." if p["amount"] == remaining else f"{self.fmt(remaining-p['amount'],p['currency'])} remains.")); return
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
        for row in self.unpaid_bills():
            due = date.fromisoformat(row["due_on"])
            if (due - today).days in (0, 3):
                self.remind("bill", row["id"], due, today, f"{row['card_name']} bill: {self.fmt(row['remaining'],row['currency'])} remaining")

    def remind(self, kind: str, item_id: int, due: date, today: date, label: str) -> None:
        key = (kind, item_id, due.isoformat(), today.isoformat())
        with self.db:
            inserted = self.db.execute("INSERT OR IGNORE INTO reminders VALUES(?,?,?,?)", key).rowcount
        if not inserted: return
        try:
            self.send(f"Reminder · {label} on {due}" + (" (today)" if due == today else " (in 3 days)"))
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
            {"command": "cards", "description": "Credit card bills and payments"},
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
