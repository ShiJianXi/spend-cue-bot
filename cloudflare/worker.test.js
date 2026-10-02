import assert from "node:assert/strict";
import { after, test } from "node:test";
import { fileURLToPath } from "node:url";
import { Miniflare } from "miniflare";
import { addDays, amountInput, clean, monthDue, nextRenewal, todayIn } from "./logic.js";

test("money and calendar boundaries use integer units", () => {
  assert.deepEqual(amountInput("USD 1,234.50"), [123450, "USD"]);
  assert.deepEqual(amountInput("1.234 KWD"), [1234, "KWD"]);
  assert.throws(() => amountInput("1.001 SGD"));
  assert.equal(monthDue(31, "2027-02"), "2027-02-28");
  assert.equal(nextRenewal("2024-02-29", "yearly", "2025-01-01"), "2025-02-28");
  assert.equal(todayIn("Asia/Singapore", new Date("2026-10-01T17:00:00Z")), "2026-10-02");
  assert.equal(clean("Visa 4111/1111/1111/1111"), "[redacted card]");
});

test("invite, private database routing, duplicate updates, and reminders", async () => {
  const sent = [], documents = [];
  let messageId = 100, failNextSend = false, failNextDocument = false;
  const mf = new Miniflare({
    modules: true,
    modulesRules: [{ type: "ESModule", include: ["**/*.js"], fallthrough: true }],
    scriptPath: fileURLToPath(new URL("./worker.js", import.meta.url)),
    compatibilityDate: "2026-07-30",
    durableObjects: { ACCOUNTS: { className: "Account", useSQLite: true } },
    bindings: { TELEGRAM_BOT_TOKEN: "test-token", TELEGRAM_WEBHOOK_SECRET: "secret", OWNER_TELEGRAM_USER_ID: "42", MIGRATION_SECRET: "import-secret", TIMEZONE: "UTC", DEFAULT_CURRENCY: "SGD" },
    serviceBindings: {
      async TELEGRAM_API(request) {
        const path = new URL(request.url).pathname;
        assert.match(path, /^\/bottest-token\/[^/]+$/);
        const method = path.split("/").at(-1);
        if (method === "sendMessage" && failNextSend) {
          failNextSend = false;
          return new Response(JSON.stringify({ ok: false, error_code: 503 }), { status: 503, headers: { "content-type": "application/json" } });
        }
        if (method === "sendMessage") sent.push(await request.json());
        if (method === "sendDocument") {
          if (failNextDocument) {
            failNextDocument = false;
            return new Response(JSON.stringify({ ok: false, error_code: 503 }), { status: 503, headers: { "content-type": "application/json" } });
          }
          const form = await request.formData();
          documents.push({ user: form.get("chat_id"), csv: await form.get("document").text() });
        }
        return new Response(JSON.stringify({ ok: true, result: method === "getMe" ? { username: "spendcue_test_bot" } : { message_id: ++messageId } }), { headers: { "content-type": "application/json" } });
      },
    },
  });
  after(() => mf.dispose());
  let updateId = 0;
  const update = async (user, text, callback, sameId) => {
    const id = sameId ?? ++updateId;
    const entry = callback ? { callback_query: { id: String(id), from: { id: user }, message: { chat: { id: user, type: "private" } }, data: callback } }
      : { message: { from: { id: user }, chat: { id: user, type: "private" }, text } };
    const response = await mf.dispatchFetch("http://localhost/telegram", { method: "POST", headers: { "X-Telegram-Bot-Api-Secret-Token": "secret" }, body: JSON.stringify({ update_id: id, ...entry }) });
    assert.equal(response.status, 200);
    return id;
  };
  const last = (user) => sent.filter((m) => m.chat_id === String(user) || m.chat_id === user).at(-1);
  const click = async (user, label) => {
    const button = last(user).reply_markup.inline_keyboard.flat().find((b) => b.text === label);
    assert.ok(button, `Missing button ${label}`);
    await update(user, "", button.callback_data);
  };
  const invite = async () => {
    await update(42, "/invite");
    return /start=([0-9a-f]{32})/.exec(last(42).text)[1];
  };
  assert.equal((await mf.dispatchFetch("http://localhost/telegram", { method: "POST" })).status, 403);
  await update(42, "/start");
  const first = await invite();
  await update(1001, `/start ${first}`);
  assert.match(last(1001).text, /choose what to do/i);
  await update(1002, `/start ${first}`);
  assert.match(last(1002).text, /invite-only/i);
  const second = await invite();
  await update(1002, `/start ${second}`);
  await update(42, "/announce");
  await update(42, "Card reminders are now available.");
  const announceButton = last(42).reply_markup.inline_keyboard.flat().find((b) => b.text === "Send update");
  const announcementId = await update(42, "", announceButton.callback_data);
  for (const user of [42, 1001, 1002])
    assert.equal(sent.filter((m) => String(m.chat_id) === String(user) && m.text.startsWith("SpendCue update:")).length, 1);
  await update(42, "", announceButton.callback_data, announcementId);
  assert.equal(sent.filter((m) => String(m.chat_id) === "1001" && m.text.startsWith("SpendCue update:")).length, 1);
  failNextSend = true;
  const beforeRetry = sent.length;
  await update(1002, "/help");
  assert.equal(sent.length, beforeRetry);
  const namespace = await mf.getDurableObjectNamespace("ACCOUNTS");
  await namespace.getByName("user:1002").flushOutbox();
  assert.match(last(1002).text, /Use \/add/);

  await update(1001, "/add");
  await click(1001, "Food");
  await update(1001, "10.25");
  await update(1001, "Lunch");
  await click(1001, "Today");
  const savedId = await update(1001, "", last(1001).reply_markup.inline_keyboard.flat().find((b) => b.text === "Save expense").callback_data);
  assert.match(last(1001).text, /Saved expense #1: Lunch/);
  const count = sent.length;
  await update(1001, "", "s:1:action:save", savedId);
  assert.equal(sent.length, count);
  await update(1001, "/overview");
  assert.match(last(1001).text, /Total: SGD 10.25/);
  await update(1002, "/overview");
  assert.match(last(1002).text, /No spending recorded/);
  assert.doesNotMatch(last(1002).text, /Lunch/);
  await update(42, "/overview");
  assert.match(last(42).text, /No spending recorded/);
  await update(1002, "", "expense:edit:1");
  assert.match(last(1002).text, /Expense not found/);
  await update(1002, "/export");
  assert.doesNotMatch(documents.at(-1).csv, /Lunch/);
  await update(1001, "/export");
  assert.match(documents.at(-1).csv, /Lunch/);
  failNextDocument = true;
  const failedExport = await mf.dispatchFetch("http://localhost/telegram", { method: "POST", headers: { "X-Telegram-Bot-Api-Secret-Token": "secret" }, body: JSON.stringify({ update_id: 500000, message: { from: { id: 1001 }, chat: { id: 1001, type: "private" }, text: "/export" } }) });
  assert.equal(failedExport.status, 500);
  const beforeExportRetry = documents.length;
  await update(1001, "/export", null, 500000);
  assert.equal(documents.length, beforeExportRetry + 1);

  await update(1001, "/cards"); await click(1001, "Add card");
  await update(1001, "Visa"); await update(1001, "5"); await click(1001, "Save");
  await click(1001, "Record payment"); await click(1001, "Visa");
  await update(1001, "450"); await click(1001, "Record payment");
  await update(1001, "/overview");
  assert.match(last(1001).text, /Total: SGD 10\.25/);
  assert.match(last(1001).text, /Visa: Paid · SGD 450\.00 recorded/);
  assert.match(last(1001).text, /excluded from spending total/);
  await update(1002, "/cards");
  assert.doesNotMatch(last(1002).text, /Visa/);

  const due = addDays(todayIn("UTC"), 3);
  await update(1001, "/subs"); await click(1001, "Add subscription");
  await update(1001, "Netflix"); await update(1001, "18.99"); await click(1001, "Monthly");
  await update(1001, due); await click(1001, "Save");
  await update(1001, "/subs"); await click(1001, "Add subscription");
  await update(1001, "Cloud storage"); await update(1001, "2.50"); await click(1001, "Monthly");
  await update(1001, todayIn("UTC")); await click(1001, "Save");
  await update(1001, "/overview");
  assert.match(last(1001).text, /Total: SGD 12\.75/);
  assert.match(last(1001).text, /Scheduled subscriptions included above/);
  await update(1002, "/overview");
  assert.match(last(1002).text, /No spending recorded/);
  await namespace.getByName("user:1001").reminders();
  await namespace.getByName("user:1001").reminders();
  assert.equal(sent.filter((m) => m.chat_id === "1001" && m.text.startsWith("Reminder · Netflix")).length, 1);
  assert.equal(sent.filter((m) => m.chat_id === "1002" && m.text.startsWith("Reminder")).length, 0);

  const imported = { categories: [], subscriptions: [], cards: [], expenses: [{ id: 7, amount: 1234, currency: "SGD", merchant: "Owner only", category: "Food", spent_on: todayIn("UTC"), subscription_id: null, scheduled_due: null, deleted: 0 }], card_payments: [], reminders: [] };
  const importRequest = (secret) => mf.dispatchFetch("http://localhost/import", { method: "POST", headers: { "X-SpendCue-Import-Secret": secret }, body: JSON.stringify(imported) });
  assert.equal((await importRequest("wrong")).status, 403);
  assert.equal((await importRequest("import-secret")).status, 200);
  assert.equal((await importRequest("import-secret")).status, 400);
  await update(42, "/overview");
  assert.match(last(42).text, /Total: SGD 12.34/);
  await update(1002, "/overview");
  assert.doesNotMatch(last(1002).text, /Owner only/);

  await update(42, "/users");
  await click(42, "Revoke 1001");
  await click(42, "Revoke access");
  await update(1001, `/start ${first}`);
  assert.match(last(1001).text, /invite-only/i);
  await update(42, "/announce");
  await update(42, "A second update.");
  await click(42, "Send update");
  assert.equal(sent.filter((m) => String(m.chat_id) === "1001" && m.text.startsWith("SpendCue update:")).length, 1);
  assert.equal(sent.filter((m) => String(m.chat_id) === "1002" && m.text.startsWith("SpendCue update:")).length, 2);
});
