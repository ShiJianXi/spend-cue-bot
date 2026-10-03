import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from spendcue import SpendCue, amount_input, clean, money, monthly_due, next_renewal, open_db, subscription_anchor


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.next_id = 100

    def send(self, chat, text, buttons=None, *, prompt=False):
        self.next_id += 1
        self.sent.append((text, buttons, prompt))
        return {"message_id": self.next_id}

    def call(self, method, data=None):
        if method == "answerCallbackQuery": return True
        raise AssertionError(method)

    def csv(self, chat, filename, content):
        self.sent.append((filename, content, False))


class SpendCueTests(unittest.TestCase):
    def setUp(self):
        self.db = open_db(":memory:")
        self.telegram = FakeTelegram()
        self.bot = SpendCue(self.db, self.telegram, 42, clock=lambda: datetime(2026, 10, 2, 2, tzinfo=ZoneInfo("UTC")))
        self.uid = 0

    def message(self, text="", *, from_id=42, chat_type="private", photo=None):
        self.uid += 1
        msg = {"message_id": self.uid, "chat": {"id": 42 if chat_type == "private" else -5, "type": chat_type},
               "from": {"id": from_id}, "text": text}
        if photo is not None: msg["photo"] = photo
        update = {"update_id": self.uid, "message": msg}
        self.bot.handle_update(update)
        return update

    def callback(self, action):
        self.uid += 1
        self.bot.handle_update({"update_id": self.uid, "callback_query": {"id": str(self.uid), "from": {"id": 42},
                                "message": {"chat": {"id": 42, "type": "private"}}, "data": action}})

    def action(self, suffix):
        return f"s:{self.bot.session()['id']}:{suffix}"

    def add_expense(self, amount="24.80", merchant="Lunch", category="Food"):
        self.message("/add")
        category_id = self.db.execute("SELECT id FROM categories WHERE name=?", (category,)).fetchone()[0]
        self.callback(self.action(f"cat:{category_id}"))
        self.message(amount)
        self.message(merchant)
        self.callback(self.action("date:yesterday"))
        self.callback(self.action("action:save"))

    def add_card(self, due_day="5"):
        self.message("/cards")
        self.callback("cards:add")
        self.message("Visa")
        self.message(due_day)
        self.callback(self.action("action:save"))

    def pay_card(self, amount):
        self.callback("cards:pay")
        card_id = self.db.execute("SELECT id FROM cards WHERE name='Visa'").fetchone()[0]
        self.callback(self.action(f"card:{card_id}"))
        self.message(amount)
        self.callback(self.action("action:save"))

    def add_subscription(self, name="Netflix", amount="18.99", frequency="monthly", due="5"):
        self.callback("subs:add")
        self.message(name)
        self.message(amount)
        self.callback(self.action(f"frequency:{frequency}"))
        self.message(due)
        self.callback(self.action("action:save"))

    def test_money_precision_and_local_dates(self):
        self.assertEqual(money("24.80"), 2480)
        self.assertEqual(amount_input("USD 1,234.50", "SGD"), (123450, "USD"))
        self.assertEqual(money("1000", "JPY"), 1000)
        self.assertEqual(money("1.234", "KWD"), 1234)
        with self.assertRaises(ValueError): money("24.801")
        with self.assertRaises(ValueError): amount_input("1,2", "SGD")
        self.assertEqual(self.bot.today(), date(2026, 10, 2))
        self.assertEqual(next_renewal(date(2026, 1, 31), "monthly", date(2026, 2, 1)), date(2026, 2, 28))
        self.assertEqual(next_renewal(date(2024, 2, 29), "yearly", date(2025, 1, 1)), date(2025, 2, 28))
        self.assertEqual(next_renewal(date(2026, 10, 31), "quarterly", date(2027, 1, 1)), date(2027, 1, 31))
        self.assertEqual(monthly_due(31, date(2027, 2, 1)), date(2027, 2, 28))
        self.assertNotIn("1234", clean("Visa 1234.5678.9012.3456"))

    def test_subscription_day_and_month_day_inputs(self):
        today = date(2026, 10, 2)
        monthly = subscription_anchor("31", "monthly", today)
        self.assertEqual(next_renewal(subscription_anchor("1", "monthly", today), "monthly", today), date(2026, 11, 1))
        self.assertEqual(next_renewal(monthly, "monthly", date(2027, 2, 1)), date(2027, 2, 28))
        self.assertEqual(next_renewal(monthly, "monthly", date(2027, 3, 1)), date(2027, 3, 31))
        self.assertEqual(next_renewal(subscription_anchor("01-15", "quarterly", today), "quarterly", today), date(2026, 10, 15))
        self.assertEqual(next_renewal(subscription_anchor("10-01", "quarterly", today), "quarterly", today), date(2027, 1, 1))
        leap = subscription_anchor("02-29", "quarterly", today)
        self.assertEqual(next_renewal(leap, "quarterly", date(2027, 2, 1)), date(2027, 2, 28))
        self.assertEqual(next_renewal(leap, "quarterly", date(2027, 3, 1)), date(2027, 5, 29))
        self.assertEqual(subscription_anchor("2026-10-15", "yearly", today), date(2026, 10, 15))
        for value, frequency in (("32", "monthly"), ("2026-10-15", "monthly"), ("02-30", "quarterly"), ("2026-10-01", "yearly")):
            with self.subTest(value=value, frequency=frequency), self.assertRaises(ValueError):
                subscription_anchor(value, frequency, today)

    def test_expense_menu_edit_undo_and_replay(self):
        self.add_expense()
        row = self.db.execute("SELECT * FROM expenses").fetchone()
        self.assertEqual((row["amount"], row["spent_on"], row["category"]), (2480, "2026-10-01", "Food"))
        self.assertEqual([b[0] for b in self.telegram.sent[-1][1]], ["Edit", "Undo"])
        self.callback(f"expense:edit:{row['id']}")
        self.callback(self.action("field:amount"))
        self.message("25.01")
        self.callback(self.action("action:save"))
        self.assertEqual(self.db.execute("SELECT amount FROM expenses").fetchone()[0], 2501)
        self.callback(f"expense:undo:{row['id']}")
        self.callback(self.action("action:save"))
        self.assertEqual(self.db.execute("SELECT deleted FROM expenses").fetchone()[0], 1)
        self.bot.handle_update({"update_id": self.uid, "callback_query": {"id": "replay", "from": {"id": 42},
                                "message": {"chat": {"id": 42, "type": "private"}}, "data": "nav:add"}})
        self.assertIsNone(self.bot.session())

    def test_old_buttons_cannot_modify_new_form(self):
        self.message("/add")
        old_action = self.action("cat:1")
        self.message("/cancel")
        self.message("/add")
        self.callback(old_action)
        self.assertEqual(self.bot.session()["step"], "category")
        self.assertIn("expired", self.telegram.sent[-1][0])

    def test_card_one_payment_marks_month_paid_and_resets_next_month(self):
        self.add_card()
        self.callback("cards:alert:1:3")
        self.bot.reminders(); self.bot.reminders()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reminders WHERE kind='card'").fetchone()[0], 1)
        self.callback("nav:cards")
        self.assertIn("Unpaid", self.telegram.sent[-1][0])
        self.pay_card("200")
        self.bot.overview()
        self.assertIn("Card payments · 2026-10 (excluded from spending total to avoid double counting):", self.telegram.sent[-1][0])
        self.assertIn("Visa: Paid · SGD 200.00 recorded · due 2026-10-05", self.telegram.sent[-1][0])
        self.assertNotIn("Total: SGD 200.00", self.telegram.sent[-1][0])
        self.callback("nav:cards")
        self.assertIn("Paid", self.telegram.sent[-1][0])
        self.callback("cards:pay")
        self.assertIn("No unpaid cards", self.telegram.sent[-1][0])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM card_payments").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0], 0)
        self.bot.clock = lambda: datetime(2026, 11, 2, 2, tzinfo=ZoneInfo("UTC"))
        self.callback("nav:cards")
        self.assertIn("Unpaid · due 2026-11-05", self.telegram.sent[-1][0])
        self.assertEqual(self.db.execute("SELECT paid_on FROM bills").fetchone()[0], "2026-10-02")

    def test_card_due_day_can_change_without_changing_payment_history(self):
        self.callback("cards:add")
        self.message("Visa")
        self.message("0")
        self.assertIn("day from 1 to 31", self.telegram.sent[-2][0])
        self.message("5")
        self.callback(self.action("action:save"))
        self.pay_card("200")
        self.callback("cards:due:1")
        self.message("15")
        self.callback(self.action("action:save"))
        self.assertEqual(self.db.execute("SELECT due_day FROM cards").fetchone()[0], 15)
        self.assertEqual(self.db.execute("SELECT amount FROM card_payments").fetchone()[0], 20000)
        self.assertIn("Paid · due 2026-10-15", self.telegram.sent[-1][0])

    def test_card_reminder_crosses_month_and_stops_after_payment(self):
        self.bot.clock = lambda: datetime(2026, 9, 24, 2, tzinfo=ZoneInfo("UTC"))
        self.add_card(due_day="1")
        self.bot.reminders(); self.bot.reminders()
        self.assertEqual(self.db.execute("SELECT due_on,reminder_on FROM reminders WHERE kind='card'").fetchone()[:],
                         ("2026-10-01", "2026-09-24"))
        self.assertIn("in 7 days", self.telegram.sent[-1][0])
        self.bot.clock = lambda: datetime(2026, 10, 1, 2, tzinfo=ZoneInfo("UTC"))
        self.bot.reminders(); self.bot.reminders()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reminders WHERE kind='card'").fetchone()[0], 2)
        self.assertIn("(today)", self.telegram.sent[-1][0])
        self.pay_card("123.45")
        self.bot.reminders()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reminders WHERE kind='card'").fetchone()[0], 2)

    def test_subscription_schedule_cancel_and_reminder_deduplication(self):
        self.add_subscription()
        self.bot.reminders(); self.bot.reminders()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 1)
        self.assertEqual(sum("Reminder" in item[0] for item in self.telegram.sent), 1)
        self.callback("subs:edit:1")
        self.callback(self.action("field:frequency"))
        self.callback(self.action("frequency:yearly"))
        self.message("2026-10-15")
        self.callback(self.action("action:save"))
        self.assertEqual(self.db.execute("SELECT day FROM subscriptions").fetchone()[0], 15)
        self.assertEqual(self.db.execute("SELECT frequency FROM subscriptions").fetchone()[0], "yearly")
        self.callback("subs:toggle:1")
        self.assertEqual(self.db.execute("SELECT active FROM subscriptions").fetchone()[0], 0)

    def test_scheduled_subscription_spending_catchup_cancel_and_undo(self):
        self.add_subscription(frequency="quarterly", due="10-02")
        self.bot.sync_subscriptions(); self.bot.sync_subscriptions()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM expenses WHERE subscription_id=1").fetchone()[0], 1)
        self.bot.overview()
        self.assertIn("Subscriptions: SGD 18.99", self.telegram.sent[-1][0])
        self.assertIn("payment not verified", self.telegram.sent[-1][0])
        self.bot.clock = lambda: datetime(2027, 1, 2, 2, tzinfo=ZoneInfo("UTC"))
        self.bot.sync_subscriptions()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM expenses WHERE subscription_id=1").fetchone()[0], 2)
        self.callback("subs:toggle:1")
        self.bot.clock = lambda: datetime(2027, 4, 2, 2, tzinfo=ZoneInfo("UTC"))
        self.bot.sync_subscriptions()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM expenses WHERE subscription_id=1").fetchone()[0], 2)
        self.callback("subs:toggle:1")
        self.bot.sync_subscriptions()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM expenses WHERE subscription_id=1").fetchone()[0], 3)
        self.callback("expense:undo:1")
        self.callback(self.action("action:save"))
        self.bot.sync_subscriptions()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM expenses WHERE subscription_id=1 AND deleted=0").fetchone()[0], 2)

    def test_card_purchases_and_payments_have_separate_totals_and_overdue_status(self):
        self.add_expense(amount="10")
        self.add_subscription(due="2")
        self.add_card(due_day="1")
        self.pay_card("200")
        self.bot.overview()
        summary = self.telegram.sent[-1][0]
        self.assertIn("Food: SGD 10.00", summary)
        self.assertIn("Subscriptions: SGD 18.99", summary)
        self.assertIn("Total: SGD 28.99", summary)
        self.assertIn("Visa: Paid · SGD 200.00 recorded · due 2026-10-01", summary)
        self.assertNotIn("Total: SGD 228.99", summary)
        self.bot.clock = lambda: datetime(2026, 11, 2, 2, tzinfo=ZoneInfo("UTC"))
        self.bot.cards()
        self.assertIn("Visa: Unpaid · overdue · due 2026-11-01", self.telegram.sent[-1][0])
        self.bot.overview()
        self.assertIn("Visa: Unpaid · due 2026-11-01", self.telegram.sent[-1][0])
        self.bot.overview(date(2026, 10, 1), date(2026, 10, 31))
        self.assertIn("Visa: Paid · SGD 200.00 recorded · due 2026-10-01", self.telegram.sent[-1][0])

    def test_recent_month_picker_and_older_history(self):
        with self.db:
            for amount, spent_on in ((1000, "2026-08-31"), (2000, "2026-09-01"),
                                     (3000, "2026-09-30"), (4000, "2026-10-01"),
                                     (5000, "2024-01-15")):
                self.db.execute("INSERT INTO expenses(amount,currency,merchant,category,spent_on) VALUES(?,'SGD','Test','Food',?)", (amount, spent_on))
            self.db.execute("INSERT INTO cards(name,due_day,created_on) VALUES('Visa',30,'2026-09-01')")
            bill = self.db.execute("INSERT INTO bills(card_name,amount,currency,due_on,cycle_month) VALUES('Visa',5000,'SGD','2026-09-30','2026-09')")
            self.db.execute("INSERT INTO card_payments(bill_id,amount,currency,paid_on) VALUES(?,5000,'SGD','2026-09-30')", (bill.lastrowid,))
        self.callback("overview:months")
        labels = [label for label, _ in self.telegram.sent[-1][1]]
        self.assertEqual(labels[:2], ["Oct 2026", "Sep 2026"])
        self.assertEqual(len(labels), 14)  # 12 months, older dates, back
        self.callback("overview:month:2026-09")
        answer = self.telegram.sent[-1][0]
        self.assertIn("2026-09-01 to 2026-09-30", answer)
        self.assertIn("Total: SGD 50.00", answer)
        self.assertIn("Visa: Paid · SGD 50.00 recorded · due 2026-09-30", answer)
        self.assertNotIn("SGD 40.00", answer)
        self.callback("overview:range")
        self.message("2024-01-01")
        self.message("2024-01-31")
        self.assertIn("Total: SGD 50.00", self.telegram.sent[-1][0])
        self.assertIn("No cards for this month.", self.telegram.sent[-1][0])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM expenses WHERE deleted=0").fetchone()[0], 5)

    def test_categories_overview_currencies_and_private_only(self):
        self.add_expense()
        self.callback("category:rename")
        food = self.db.execute("SELECT id FROM categories WHERE name='Food'").fetchone()[0]
        self.callback(self.action(f"category:{food}"))
        self.message("Meals")
        self.callback(self.action("action:save"))
        self.assertEqual(self.db.execute("SELECT category FROM expenses").fetchone()[0], "Meals")
        self.callback("category:add")
        self.message("Dining")
        self.callback(self.action("action:save"))
        self.assertTrue(self.db.execute("SELECT 1 FROM categories WHERE name='Dining'").fetchone())
        with self.db:
            self.db.execute("INSERT INTO expenses(amount,currency,merchant,category,spent_on) VALUES(2000,'USD','Store','Shopping','2026-10-01')")
        self.message("/overview")
        answer = self.telegram.sent[-1][0]
        self.assertIn("SGD 24.80", answer)
        self.assertIn("USD 20.00", answer)
        before = len(self.telegram.sent)
        self.message("/add", from_id=99)
        self.message("/add", chat_type="group")
        self.assertEqual(len(self.telegram.sent), before)

    def test_photo_is_ignored_without_ai(self):
        self.message("", photo=[{"file_id": "receipt"}])
        self.assertIn("extraction is off", self.telegram.sent[-1][0])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0], 0)

    def test_form_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            db = open_db(folder + "/spendcue.db")
            bot = SpendCue(db, self.telegram, 42, clock=self.bot.clock)
            bot.handle_update({"update_id": 1, "message": {"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": "/add"}})
            form = bot.session()
            bot.handle_update({"update_id": 2, "callback_query": {"id": "2", "from": {"id": 42},
                               "message": {"chat": {"id": 42, "type": "private"}}, "data": f"s:{form['id']}:cat:1"}})
            db.close()
            db = open_db(folder + "/spendcue.db")
            bot = SpendCue(db, self.telegram, 42, clock=self.bot.clock)
            self.assertEqual(bot.session()["step"], "amount")
            bot.handle_update({"update_id": 3, "message": {"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": "12.50"}})
            self.assertEqual(bot.session()["step"], "merchant")
            db.close()

    def test_legacy_saved_data_survives_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = folder + "/spendcue.db"
            old = sqlite3.connect(path)
            old.executescript("""
                CREATE TABLE expenses (id INTEGER PRIMARY KEY, amount INTEGER NOT NULL, currency TEXT NOT NULL, merchant TEXT NOT NULL, category TEXT NOT NULL, spent_on TEXT NOT NULL, source_update INTEGER UNIQUE, receipt_hash TEXT, confirmation_message INTEGER, deleted INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE bills (id INTEGER PRIMARY KEY, card_name TEXT NOT NULL, amount INTEGER NOT NULL, currency TEXT NOT NULL, due_on TEXT NOT NULL, paid_on TEXT, source_update INTEGER UNIQUE);
                CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, merchant TEXT NOT NULL, amount INTEGER NOT NULL, currency TEXT NOT NULL, day INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, source_update INTEGER UNIQUE);
                CREATE TABLE transfers (id INTEGER PRIMARY KEY, bill_id INTEGER NOT NULL UNIQUE REFERENCES bills(id), amount INTEGER NOT NULL, currency TEXT NOT NULL, paid_on TEXT NOT NULL);
                INSERT INTO expenses(amount,currency,merchant,category,spent_on) VALUES(1000,'SGD','Cafe','groceries','2026-10-01');
                INSERT INTO bills(card_name,amount,currency,due_on,paid_on) VALUES('Visa',45000,'SGD','2026-10-05','2026-10-02');
                INSERT INTO subscriptions(merchant,amount,currency,day) VALUES('Old service',999,'SGD',31);
                INSERT INTO transfers(bill_id,amount,currency,paid_on) VALUES(1,20000,'SGD','2026-10-02');
            """)
            old.close()
            db = open_db(path)
            self.assertEqual(db.execute("SELECT cycle_month FROM bills").fetchone()[0], "2026-10")
            self.assertEqual(db.execute("SELECT amount FROM card_payments").fetchone()[0], 20000)
            self.assertTrue(db.execute("SELECT 1 FROM categories WHERE name='groceries'").fetchone())
            self.assertTrue(db.execute("SELECT 1 FROM cards WHERE name='Visa'").fetchone())
            self.assertEqual(db.execute("SELECT due_day FROM cards WHERE name='Visa'").fetchone()[0], 5)
            self.assertEqual(db.execute("SELECT created_on FROM cards WHERE name='Visa'").fetchone()[0], "2026-10-05")
            bot = SpendCue(db, self.telegram, 42, clock=self.bot.clock)
            bot.cards()
            self.assertIn("Visa: Paid · due 2026-10-05 · reminder 7 days before · SGD 200.00 recorded", self.telegram.sent[-1][0])
            bot.sync_subscriptions()
            self.assertEqual(db.execute("SELECT frequency,first_due_on,auto_from FROM subscriptions").fetchone()[:],
                             ("monthly", "2000-01-31", "2026-10-02"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM expenses WHERE subscription_id=1").fetchone()[0], 0)
            db.close()
            db = open_db(path)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM card_payments").fetchone()[0], 1)
            db.close()


if __name__ == "__main__": unittest.main()
