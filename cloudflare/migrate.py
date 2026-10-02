"""Import the owner's existing SQLite history into the Cloudflare account once."""
import argparse
import json
import os
import sqlite3
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spendcue import SpendCue, open_db


def bundle(source_path: str, owner_id: int, timezone: str, default_currency: str) -> dict:
    if not Path(source_path).is_file():
        raise FileNotFoundError(f"Database not found: {source_path}")
    with tempfile.TemporaryDirectory() as folder:
        snapshot = str(Path(folder) / "snapshot.db")
        source = sqlite3.connect(Path(source_path).resolve().as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(snapshot)
        source.backup(target)
        target.close()
        source.close()
        db = open_db(snapshot)
        SpendCue(db, None, owner_id, timezone, default_currency)
        fields = {
            "categories": "SELECT id,name FROM categories",
            "subscriptions": "SELECT id,merchant,amount,currency,frequency,first_due_on,auto_from,active FROM subscriptions",
            "cards": "SELECT id,name,due_day,reminder_days,created_on,active FROM cards WHERE due_day IS NOT NULL",
            "expenses": "SELECT id,amount,currency,merchant,category,spent_on,subscription_id,scheduled_due,deleted FROM expenses",
            "card_payments": """SELECT c.id card_id,b.cycle_month,p.currency,SUM(p.amount) amount,MAX(p.paid_on) paid_on
                               FROM card_payments p JOIN bills b ON b.id=p.bill_id
                               JOIN cards c ON c.name=b.card_name COLLATE NOCASE
                               GROUP BY c.id,b.cycle_month,p.currency""",
            "reminders": "SELECT kind,item_id,due_on,reminder_on FROM reminders WHERE kind IN ('card','subscription')",
        }
        result = {name: [dict(row) for row in db.execute(query)] for name, query in fields.items()}
        db.close()
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.getenv("DB_PATH", "spendcue.db"))
    parser.add_argument("--url", default=os.getenv("WORKER_URL"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    owner = os.getenv("OWNER_TELEGRAM_USER_ID") or os.getenv("ALLOWED_TELEGRAM_USER_ID")
    if not owner or not owner.isdigit(): parser.error("Set OWNER_TELEGRAM_USER_ID or ALLOWED_TELEGRAM_USER_ID")
    data = bundle(args.db, int(owner), os.getenv("TIMEZONE", "Asia/Singapore"), os.getenv("DEFAULT_CURRENCY", "SGD"))
    counts = {name: len(values) for name, values in data.items()}
    if args.dry_run:
        print("Ready to import:", counts)
        return
    if not args.url or not args.url.startswith("https://") or not os.getenv("MIGRATION_SECRET"):
        parser.error("Set HTTPS WORKER_URL and MIGRATION_SECRET")
    request = urllib.request.Request(args.url.rstrip("/") + "/import", data=json.dumps(data).encode(),
                                     headers={"Content-Type": "application/json", "X-SpendCue-Import-Secret": os.environ["MIGRATION_SECRET"]})
    with urllib.request.urlopen(request, timeout=60) as response:
        print("Imported:", json.load(response))


if __name__ == "__main__":
    main()
