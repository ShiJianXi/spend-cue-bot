"""Point an existing BotFather bot at the deployed SpendCue Worker."""
import json
import os
import urllib.request


def call(token: str, method: str, payload: dict):
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                     data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method} failed")
    return result["result"]


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = os.environ["WORKER_URL"].rstrip("/")
    secret = os.environ["TELEGRAM_WEBHOOK_SECRET"]
    if not url.startswith("https://") or not secret:
        raise ValueError("WORKER_URL must be HTTPS and TELEGRAM_WEBHOOK_SECRET must be set")
    call(token, "setMyCommands", {"commands": [
        {"command": "menu", "description": "Open SpendCue"},
        {"command": "add", "description": "Add spending"},
        {"command": "cards", "description": "Cards and monthly payments"},
        {"command": "subs", "description": "Subscriptions"},
        {"command": "overview", "description": "Spending and upcoming payments"},
        {"command": "manage", "description": "Categories and edits"},
        {"command": "export", "description": "Download CSV"},
        {"command": "invite", "description": "Invite a friend (owner only)"},
        {"command": "users", "description": "Invited users (owner only)"},
    ]})
    call(token, "setWebhook", {"url": url + "/telegram", "secret_token": secret,
                               "allowed_updates": ["message", "callback_query"], "drop_pending_updates": True})
    status = call(token, "getWebhookInfo", {})
    if status.get("url") != url + "/telegram": raise RuntimeError("Webhook URL was not confirmed")
    bot = call(token, "getMe", {})
    print(f"Webhook active. Open https://t.me/{bot['username']} and send /start")


if __name__ == "__main__":
    main()
