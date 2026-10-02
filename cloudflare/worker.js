import { DurableObject } from "cloudflare:workers";
import { CATEGORIES, addDays, amountInput, clean, csvSafe, currency, formatMoney, monthDue, nextRenewal, parseDate, todayIn } from "./logic.js";

const row = (sql, query, ...args) => sql.exec(query, ...args).toArray()[0];
const rows = (sql, query, ...args) => sql.exec(query, ...args).toArray();
const minor = (amount) => Number.isSafeInteger(amount) && amount > 0;
const nextMonth = (month) => `${new Date(Date.UTC(Number(month.slice(0, 4)), Number(month.slice(5, 7)), 1)).toISOString().slice(0, 7)}`;
const monthStart = (day) => `${day.slice(0, 7)}-01`;
const monthEnd = (day) => monthDue(31, day);
const csvCell = (value) => `"${String(csvSafe(value ?? "")).replaceAll('"', '""')}"`;

async function telegram(env, method, data) {
  const apiFetch = env.TELEGRAM_API ? env.TELEGRAM_API.fetch.bind(env.TELEGRAM_API) : fetch;
  const response = await apiFetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok || !result.ok) throw new Error(`Telegram ${method} failed: ${result.error_code || response.status}`);
  return result.result;
}
async function send(env, userId, text, buttons, prompt = false) {
  const data = { chat_id: userId, text: text.slice(0, 4096) };
  if (buttons?.length) {
    const keys = buttons.map(([label, action]) => ({ text: label, callback_data: action }));
    data.reply_markup = { inline_keyboard: keys.length > 3 ? keys.map((key) => [key]) : [keys] };
  } else if (prompt) data.reply_markup = { force_reply: true };
  return telegram(env, "sendMessage", data);
}

export class Account extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS processed_updates (update_id INTEGER PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, step TEXT NOT NULL, payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, active INTEGER NOT NULL DEFAULT 1);
      CREATE TABLE IF NOT EXISTS expenses (id INTEGER PRIMARY KEY, amount INTEGER NOT NULL, currency TEXT NOT NULL, merchant TEXT NOT NULL, category TEXT NOT NULL, spent_on TEXT NOT NULL, subscription_id INTEGER, scheduled_due TEXT, deleted INTEGER NOT NULL DEFAULT 0, UNIQUE(subscription_id, scheduled_due));
      CREATE TABLE IF NOT EXISTS cards (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, due_day INTEGER NOT NULL, reminder_days INTEGER NOT NULL DEFAULT 7, created_on TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
      CREATE TABLE IF NOT EXISTS card_payments (id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL, cycle_month TEXT NOT NULL, amount INTEGER NOT NULL, currency TEXT NOT NULL, paid_on TEXT NOT NULL, UNIQUE(card_id, cycle_month, currency));
      CREATE TABLE IF NOT EXISTS subscriptions (id INTEGER PRIMARY KEY, merchant TEXT NOT NULL, amount INTEGER NOT NULL, currency TEXT NOT NULL, frequency TEXT NOT NULL, first_due_on TEXT NOT NULL, auto_from TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
      CREATE TABLE IF NOT EXISTS reminders (kind TEXT NOT NULL, item_id INTEGER NOT NULL, due_on TEXT NOT NULL, reminder_on TEXT NOT NULL, PRIMARY KEY(kind,item_id,due_on,reminder_on));
      CREATE TABLE IF NOT EXISTS invites (code TEXT PRIMARY KEY, expires_at INTEGER NOT NULL, claimed_by TEXT);
      CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
    `);
    for (const name of CATEGORIES) this.sql.exec("INSERT OR IGNORE INTO categories(name) VALUES(?)", name);
  }

  get(key) { return row(this.sql, "SELECT value FROM meta WHERE key=?", key)?.value; }
  set(key, value) { this.sql.exec("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, String(value)); }
  user() { return this.get("user_id"); }
  today() { return todayIn(this.env.TIMEZONE || "Asia/Singapore"); }
  code() { return currency(this.env.DEFAULT_CURRENCY, "SGD"); }
  fmt(amount, code) { return formatMoney(amount, code); }
  async say(text, buttons, prompt) {
    this.sql.exec("INSERT INTO outbox(payload) VALUES(?)", JSON.stringify({ text, buttons, prompt }));
    await this.flushOutbox();
  }
  async flushOutbox() {
    if (this.flushPromise) return this.flushPromise;
    this.flushPromise = (async () => {
      for (;;) {
        const pending = row(this.sql, "SELECT * FROM outbox ORDER BY id LIMIT 1");
        if (!pending) return;
        const { text, buttons, prompt } = JSON.parse(pending.payload);
        try { await send(this.env, this.user(), text, buttons, prompt); }
        catch {
          await this.ctx.storage.setAlarm(Date.now() + 5 * 60000);
          return;
        }
        this.sql.exec("DELETE FROM outbox WHERE id=?", pending.id);
      }
    })();
    try { await this.flushPromise; } finally { this.flushPromise = null; }
  }

  authorize(userId) {
    if (!/^\d+$/.test(String(userId))) throw new Error("Invalid user ID");
    const existing = this.user();
    if (existing && existing !== String(userId)) throw new Error("Account mismatch");
    this.set("user_id", userId);
    this.set("authorized", "1");
  }
  async revoke() {
    this.set("authorized", "0");
    this.sql.exec("DELETE FROM outbox");
    await this.ctx.storage.deleteAlarm();
  }

  createInvite() {
    if (this.user() !== String(this.env.OWNER_TELEGRAM_USER_ID)) throw new Error("Owner only");
    const code = crypto.randomUUID().replaceAll("-", "");
    this.sql.exec("INSERT INTO invites(code,expires_at) VALUES(?,?)", code, Date.now() + 7 * 86400000);
    return code;
  }
  claimInvite(code, userId) {
    if (this.user() !== String(this.env.OWNER_TELEGRAM_USER_ID)) return false;
    const invite = row(this.sql, "SELECT * FROM invites WHERE code=?", code);
    if (!invite || invite.expires_at < Date.now()) return false;
    if (invite.claimed_by && invite.claimed_by !== String(userId)) return false;
    this.sql.exec("UPDATE invites SET claimed_by=? WHERE code=?", String(userId), code);
    return true;
  }
  invitedUsers() { return rows(this.sql, "SELECT DISTINCT claimed_by FROM invites WHERE claimed_by IS NOT NULL AND expires_at<>0").map((r) => r.claimed_by); }

  importData(data) {
    if (this.user() !== String(this.env.OWNER_TELEGRAM_USER_ID)) throw new Error("Owner only");
    if (this.get("imported") || ["expenses", "cards", "subscriptions", "card_payments"].some((table) => row(this.sql, `SELECT 1 ok FROM ${table} LIMIT 1`)))
      throw new Error("Owner account already has data; import refused");
    const names = ["categories", "subscriptions", "cards", "expenses", "card_payments", "reminders"];
    if (!data || names.some((name) => !Array.isArray(data[name]) || data[name].length > 10000)) throw new Error("Invalid import bundle");
    const validId = (value) => Number.isSafeInteger(value) && value > 0;
    const validName = (value) => typeof value === "string" && value.trim() && value.length <= 100 && clean(value) === value;
    const validDate = (value) => { try { return parseDate(value) === value; } catch { return false; } };
    const validMoney = (item) => minor(item.amount) && /^[A-Z]{3}$/.test(item.currency);
    for (const c of data.categories) if (!validId(c.id) || !validName(c.name)) throw new Error("Invalid category");
    for (const s of data.subscriptions) if (!validId(s.id) || !validName(s.merchant) || !validMoney(s) || !["monthly", "quarterly", "yearly"].includes(s.frequency) || !validDate(s.first_due_on) || !validDate(s.auto_from) || ![0, 1].includes(s.active)) throw new Error("Invalid subscription");
    for (const c of data.cards) if (!validId(c.id) || !validName(c.name) || !Number.isInteger(c.due_day) || c.due_day < 1 || c.due_day > 31 || !Number.isInteger(c.reminder_days) || c.reminder_days < 0 || c.reminder_days > 31 || !validDate(c.created_on) || ![0, 1].includes(c.active)) throw new Error("Invalid card");
    for (const e of data.expenses) if (!validId(e.id) || !validMoney(e) || !validName(e.merchant) || !validName(e.category) || !validDate(e.spent_on) || (e.subscription_id != null && !validId(e.subscription_id)) || (e.scheduled_due != null && !validDate(e.scheduled_due)) || ![0, 1].includes(e.deleted)) throw new Error("Invalid expense");
    for (const p of data.card_payments) if (!validId(p.card_id) || !validMoney(p) || !/^\d{4}-\d{2}$/.test(p.cycle_month) || !validDate(`${p.cycle_month}-01`) || !validDate(p.paid_on)) throw new Error("Invalid card payment");
    for (const r of data.reminders) if (!["card", "subscription"].includes(r.kind) || !validId(r.item_id) || !validDate(r.due_on) || !validDate(r.reminder_on)) throw new Error("Invalid reminder");
    this.ctx.storage.transactionSync(() => {
      for (const c of data.categories) this.sql.exec("INSERT OR IGNORE INTO categories(name) VALUES(?)", c.name);
      for (const s of data.subscriptions) this.sql.exec("INSERT INTO subscriptions(id,merchant,amount,currency,frequency,first_due_on,auto_from,active) VALUES(?,?,?,?,?,?,?,?)", s.id, s.merchant, s.amount, s.currency, s.frequency, s.first_due_on, s.auto_from, s.active);
      for (const c of data.cards) this.sql.exec("INSERT INTO cards(id,name,due_day,reminder_days,created_on,active) VALUES(?,?,?,?,?,?)", c.id, c.name, c.due_day, c.reminder_days, c.created_on, c.active);
      for (const e of data.expenses) this.sql.exec("INSERT INTO expenses(id,amount,currency,merchant,category,spent_on,subscription_id,scheduled_due,deleted) VALUES(?,?,?,?,?,?,?,?,?)", e.id, e.amount, e.currency, e.merchant, e.category, e.spent_on, e.subscription_id, e.scheduled_due, e.deleted);
      for (const p of data.card_payments) this.sql.exec("INSERT INTO card_payments(card_id,cycle_month,amount,currency,paid_on) VALUES(?,?,?,?,?)", p.card_id, p.cycle_month, p.amount, p.currency, p.paid_on);
      for (const r of data.reminders) this.sql.exec("INSERT OR IGNORE INTO reminders VALUES(?,?,?,?)", r.kind, r.item_id, r.due_on, r.reminder_on);
      this.set("imported", "1");
    });
    return Object.fromEntries(names.map((name) => [name, data[name].length]));
  }

  session() { return row(this.sql, "SELECT * FROM sessions ORDER BY id DESC LIMIT 1"); }
  clear() { this.sql.exec("DELETE FROM sessions"); }
  async start(kind, step, payload = {}) {
    this.clear();
    this.sql.exec("INSERT INTO sessions(kind,step,payload) VALUES(?,?,?)", kind, step, JSON.stringify(payload));
    await this.prompt();
  }
  async step(step, payload) {
    this.sql.exec("UPDATE sessions SET step=?,payload=? WHERE id=?", step, JSON.stringify(payload), this.session().id);
    await this.prompt();
  }
  choices(session, entries, prefix) { return entries.map(([label, value]) => [String(label), `s:${session.id}:${prefix}:${value}`]); }

  async prompt() {
    const session = this.session();
    if (!session) return;
    const { kind, step } = session;
    const p = JSON.parse(session.payload);
    const choices = (entries, prefix) => this.choices(session, entries, prefix);
    if (kind === "expense" || kind === "expense_edit") {
      if (step === "category") await this.say("Choose a spending category:", choices(rows(this.sql, "SELECT id,name FROM categories WHERE active=1 ORDER BY id").map((r) => [r.name, r.id]), "cat"));
      else if (step === "amount") await this.say("Enter the amount, for example 24.80 or USD 24.80:", null, true);
      else if (step === "merchant") await this.say("What was this for? Enter a merchant or short note:", null, true);
      else if (step === "date") await this.say("When was it spent?", choices([["Today", "today"], ["Yesterday", "yesterday"], ["Enter date", "custom"]], "date"));
      else if (step === "date_input") await this.say("Enter the spending date as YYYY-MM-DD:", null, true);
      else if (step === "edit_field") await this.say("What would you like to change?", choices([["Category", "category"], ["Amount", "amount"], ["Merchant", "merchant"], ["Date", "date"]], "field"));
      else if (step === "review") await this.say(`Review · ${p.merchant} · ${this.fmt(p.amount, p.currency)} · ${p.category} · ${p.spent_on}`, choices([[kind === "expense" ? "Save expense" : "Save changes", "save"], ["Edit", "edit"], ["Cancel", "cancel"]], "action"));
    } else if (kind === "expense_undo") await this.say("Delete this expense from spending totals?", choices([["Undo expense", "save"], ["Cancel", "cancel"]], "action"));
    else if (["card_add", "card_due", "category_add", "category_rename"].includes(kind)) {
      if (step === "name") await this.say(kind === "card_add" ? "Enter a card nickname (never a card number):" : "Enter the category name:", null, true);
      else if (step === "due_day") await this.say("What day of each month is this card due? Enter 1–31:", null, true);
      else if (step === "review") await this.say(`Review · ${p.name}${p.due_day ? ` · due day ${p.due_day}` : ""}`, choices([["Save", "save"], ["Cancel", "cancel"]], "action"));
    } else if (kind === "payment") {
      if (step === "card") {
        const month = this.today().slice(0, 7);
        const cards = rows(this.sql, "SELECT * FROM cards WHERE active=1 ORDER BY name").filter((r) => !this.cardPaid(r.id, month));
        await this.say(`Which card did you pay for ${month}?`, choices(cards.map((r) => [r.name, r.id]), "card"));
      } else if (step === "amount") await this.say("How much did you pay? For example 450 or USD 450:", null, true);
      else if (step === "review") await this.say(`Review · ${p.card_name} · ${this.fmt(p.amount, p.currency)} paid ${p.paid_on} for ${p.cycle_month}. This marks the month Paid.`, choices([["Record payment", "save"], ["Cancel", "cancel"]], "action"));
    } else if (kind === "subscription" || kind === "subscription_edit") {
      if (step === "name") await this.say("Enter the subscription name:", null, true);
      else if (step === "amount") await this.say("Enter the amount per renewal:", null, true);
      else if (step === "frequency") await this.say("How often is it charged?", choices([["Monthly", "monthly"], ["Quarterly", "quarterly"], ["Yearly", "yearly"]], "frequency"));
      else if (step === "due_on") await this.say("Enter the next payment date as YYYY-MM-DD (today or later):", null, true);
      else if (step === "edit_field") await this.say("What would you like to change?", choices([["Name", "name"], ["Amount", "amount"], ["Schedule", "frequency"]], "field"));
      else if (step === "review") await this.say(`Review subscription · ${p.merchant} · ${this.fmt(p.amount, p.currency)} ${p.frequency} · next payment ${nextRenewal(p.first_due_on, p.frequency, this.today())}. Scheduled charges are added to spending automatically.`, choices([["Save", "save"], ["Edit", "edit"], ["Cancel", "cancel"]], "action"));
    } else if (kind === "overview_range") await this.say(`Enter the ${step} date as YYYY-MM-DD:`, null, true);
  }

  cardPaid(id, month) { return row(this.sql, "SELECT currency,SUM(amount) amount,MAX(paid_on) paid_on FROM card_payments WHERE card_id=? AND cycle_month=? GROUP BY currency", id, month); }
  syncSubscriptions() {
    const today = this.today();
    for (const sub of rows(this.sql, "SELECT * FROM subscriptions WHERE active=1")) {
      let due = nextRenewal(sub.first_due_on, sub.frequency, sub.auto_from);
      while (due <= today) {
        this.sql.exec("INSERT OR IGNORE INTO expenses(amount,currency,merchant,category,spent_on,subscription_id,scheduled_due) VALUES(?,?,?,?,?,?,?)", sub.amount, sub.currency, sub.merchant, "Subscriptions", due, sub.id, due);
        due = nextRenewal(sub.first_due_on, sub.frequency, addDays(due, 1));
      }
    }
  }

  async home() { await this.say("SpendCue · choose what to do:", [["Add spending", "nav:add"], ["Credit cards", "nav:cards"], ["Subscriptions", "nav:subs"], ["Overview", "nav:overview"], ["Manage", "nav:manage"]]); }
  async cards() {
    const today = this.today(), month = today.slice(0, 7);
    const cards = rows(this.sql, "SELECT * FROM cards WHERE active=1 ORDER BY name");
    const lines = [`Credit cards · ${month}`];
    for (const card of cards) {
      const paid = this.cardPaid(card.id, month), due = monthDue(card.due_day, month);
      const status = paid ? "Paid" : due < today ? "Unpaid · overdue" : "Unpaid";
      lines.push(`${card.name}: ${status} · due ${due} · reminder ${card.reminder_days ? `${card.reminder_days} days before` : "due day only"}${paid ? ` · ${this.fmt(paid.amount, paid.currency)} recorded` : ""}`);
    }
    if (!cards.length) lines.push("No cards yet.");
    await this.say(lines.join("\n"), [["Add card", "cards:add"], ["Record payment", "cards:pay"], ...cards.slice(0, 8).map((r) => [`Settings · ${r.name.slice(0, 20)}`, `cards:view:${r.id}`]), ["Back", "nav:home"]]);
  }
  async subscriptions() {
    this.syncSubscriptions();
    const subs = rows(this.sql, "SELECT * FROM subscriptions ORDER BY merchant");
    const lines = ["Subscriptions", ...subs.map((r) => `#${r.id} ${r.merchant} · ${this.fmt(r.amount, r.currency)} ${r.frequency} · next ${nextRenewal(r.first_due_on, r.frequency, this.today())} · ${r.active ? "Active" : "Cancelled"}`)];
    if (!subs.length) lines.push("None yet.");
    await this.say(lines.join("\n"), [["Add subscription", "subs:add"], ...subs.slice(0, 8).map((r) => [`Edit ${r.merchant.slice(0, 20)}`, `subs:view:${r.id}`]), ["Back", "nav:home"]]);
  }
  async manage() { await this.say("Manage your records:", [["Categories", "manage:categories"], ["Edit spending", "manage:expenses"], ["Export CSV", "nav:export"], ["Back", "nav:home"]]); }
  async categories() {
    const list = rows(this.sql, "SELECT name FROM categories WHERE active=1 ORDER BY id").map((r) => r.name).join(", ");
    await this.say(`Categories: ${list}`, [["Add category", "category:add"], ["Rename category", "category:rename"], ["Back", "nav:manage"]]);
  }
  async expensesToEdit() {
    const list = rows(this.sql, "SELECT * FROM expenses WHERE deleted=0 ORDER BY id DESC LIMIT 8");
    if (!list.length) return this.say("No expenses to edit.", [["Back", "nav:manage"]]);
    await this.say("Choose an expense:", [...list.map((r) => [`#${r.id} ${r.merchant.slice(0, 20)} ${this.fmt(r.amount, r.currency)}`, `expense:edit:${r.id}`]), ["Back", "nav:manage"]]);
  }
  async months() {
    const months = [];
    let day = monthStart(this.today());
    for (let i = 0; i < 12; i++) {
      const label = new Intl.DateTimeFormat("en-US", { month: "short", year: "numeric", timeZone: "UTC" }).format(new Date(`${day}T00:00:00Z`));
      months.push([label, `overview:month:${day.slice(0, 7)}`]);
      day = monthStart(addDays(day, -1));
    }
    await this.say("Choose a month. Older records remain available through Date range.", [...months, ["Older date range", "overview:range"], ["Back", "nav:overview"]]);
  }
  async overview(start = monthStart(this.today()), end = this.today()) {
    this.syncSubscriptions();
    const summary = rows(this.sql, "SELECT currency,category,SUM(amount) total FROM expenses WHERE deleted=0 AND spent_on BETWEEN ? AND ? GROUP BY currency,category ORDER BY currency,category", start, end);
    const lines = [`Spending · ${start} to ${end}`], totals = new Map();
    for (const r of summary) { totals.set(r.currency, (totals.get(r.currency) || 0) + r.total); lines.push(`${r.category}: ${this.fmt(r.total, r.currency)}`); }
    for (const [code, total] of [...totals].sort()) lines.push(`Total: ${this.fmt(total, code)}`);
    if (!summary.length) lines.push("No spending recorded.");
    const scheduled = rows(this.sql, "SELECT currency,SUM(amount) total FROM expenses WHERE deleted=0 AND subscription_id IS NOT NULL AND spent_on BETWEEN ? AND ? GROUP BY currency", start, end);
    if (scheduled.length) lines.push("Scheduled subscriptions included above (payment not verified): " + scheduled.map((r) => this.fmt(r.total, r.currency)).join(", "));
    const month = end.slice(0, 7), cards = rows(this.sql, "SELECT * FROM cards WHERE active=1 AND created_on<=? ORDER BY name", monthEnd(end));
    lines.push(`Card payments · ${month} (excluded from spending total to avoid double counting):`);
    for (const card of cards) {
      const paid = this.cardPaid(card.id, month);
      lines.push(`${card.name}: ${paid ? `Paid · ${this.fmt(paid.amount, paid.currency)} recorded` : "Unpaid"} · due ${monthDue(card.due_day, month)}`);
    }
    if (!cards.length) lines.push("No cards for this month.");
    await this.say(lines.join("\n"), [["This week", "overview:week"], ["This month", "overview:month"], ["Recent months", "overview:months"], ["Date range", "overview:range"], ["Upcoming payments", "overview:upcoming"], ["Back", "nav:home"]]);
  }
  async upcoming() {
    this.syncSubscriptions();
    const today = this.today(), end = addDays(today, 30), items = [];
    for (const sub of rows(this.sql, "SELECT * FROM subscriptions WHERE active=1")) {
      const due = nextRenewal(sub.first_due_on, sub.frequency, today);
      if (due <= end) items.push([due, `${sub.merchant} · ${this.fmt(sub.amount, sub.currency)} expected`]);
    }
    for (const card of rows(this.sql, "SELECT * FROM cards WHERE active=1")) {
      for (const month of [today.slice(0, 7), nextMonth(today.slice(0, 7))]) {
        const due = monthDue(card.due_day, month);
        if (due <= end && !this.cardPaid(card.id, month)) items.push([due, `${card.name} · unpaid card payment`]);
      }
    }
    items.sort(([a], [b]) => a.localeCompare(b));
    await this.say(items.length ? `Upcoming payments:\n${items.map(([d, v]) => `${d}: ${v}`).join("\n")}` : "No upcoming payments in the next 30 days.", [["Back", "nav:overview"]]);
  }
  async exportCsv() {
    const out = [["type", "id", "name", "amount_minor", "currency", "date_or_day", "category_or_status"]];
    for (const r of rows(this.sql, "SELECT * FROM categories ORDER BY id")) out.push(["category", r.id, r.name, "", "", "", r.active ? "active" : "inactive"]);
    for (const r of rows(this.sql, "SELECT * FROM expenses WHERE deleted=0 ORDER BY id")) out.push(["expense", r.id, r.merchant, r.amount, r.currency, r.spent_on, r.category]);
    for (const r of rows(this.sql, "SELECT * FROM subscriptions ORDER BY id")) out.push([`subscription_${r.frequency}`, r.id, r.merchant, r.amount, r.currency, r.first_due_on, r.active ? "active" : "paused"]);
    for (const r of rows(this.sql, "SELECT * FROM cards ORDER BY id")) out.push(["card", r.id, r.name, "", "", r.due_day, r.active ? "active" : "inactive"]);
    for (const r of rows(this.sql, "SELECT p.*,c.name FROM card_payments p JOIN cards c ON c.id=p.card_id ORDER BY p.id")) out.push(["card_payment", r.id, r.name, r.amount, r.currency, r.paid_on, "transfer"]);
    const form = new FormData();
    form.set("chat_id", this.user());
    form.set("document", new Blob(["\ufeff" + out.map((record) => record.map(csvCell).join(",")).join("\r\n")], { type: "text/csv" }), `spendcue-${this.today()}.csv`);
    const apiFetch = this.env.TELEGRAM_API ? this.env.TELEGRAM_API.fetch.bind(this.env.TELEGRAM_API) : fetch;
    const response = await apiFetch(`https://api.telegram.org/bot${this.env.TELEGRAM_BOT_TOKEN}/sendDocument`, { method: "POST", body: form });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error("CSV send failed");
  }

  async navigate(where) {
    if (where === "home") await this.home();
    else if (where === "add") await this.start("expense", "category");
    else if (where === "cards") await this.cards();
    else if (where === "subs") await this.subscriptions();
    else if (where === "overview") await this.overview();
    else if (where === "manage") await this.manage();
    else if (where === "export") await this.exportCsv();
  }
  async handleText(text) {
    const raw = String(text || "").trim().toLowerCase();
    const command = raw.startsWith("/") ? raw.split(/\s+/, 1)[0].split("@")[0] : raw;
    const routes = { "/start": "home", "/menu": "home", menu: "home", "/add": "add", add: "add", "/cards": "cards", "/cc": "cards", "/creditcard": "cards", "/subs": "subs", "/subscriptions": "subs", "/overview": "overview", "/manage": "manage", "/export": "export" };
    if (command === "/cancel" || command === "cancel") { this.clear(); await this.home(); return; }
    if (command === "/help") { await this.say("Use /add, /cards, /subs, /overview, or /manage. /menu shows buttons; /cancel stops the current form. /export sends a CSV."); return; }
    if (this.user() === String(this.env.OWNER_TELEGRAM_USER_ID) && command === "/invite") {
      const code = this.createInvite();
      const bot = await telegram(this.env, "getMe", {});
      await this.say(`One-use invite (expires in 7 days):\nhttps://t.me/${bot.username}?start=${code}\nShare it privately with one friend.`);
      return;
    }
    if (this.user() === String(this.env.OWNER_TELEGRAM_USER_ID) && command === "/users") { await this.users(); return; }
    if (routes[command]) { this.clear(); await this.navigate(routes[command]); return; }
    const session = this.session();
    if (!session) { await this.say("Choose a menu option or send /help.", [["Open menu", "nav:home"]]); return; }
    const { kind, step } = session;
    const p = JSON.parse(session.payload);
    try {
      if (["expense", "expense_edit"].includes(kind) && ["amount", "merchant", "date_input"].includes(step)) {
        if (step === "amount") {
          [p.amount, p.currency] = amountInput(text, p.currency || this.code());
          await this.step(p.spent_on ? "review" : "merchant", p);
        } else if (step === "merchant") {
          p.merchant = clean(text);
          if (!p.merchant) throw new Error("Enter a merchant or note");
          await this.step(p.spent_on ? "review" : "date", p);
        } else { p.spent_on = parseDate(text.trim()); await this.step("review", p); }
      } else if (["card_add", "category_add", "category_rename"].includes(kind) && step === "name") {
        p.name = clean(text, 40);
        if (!p.name || p.name === "[redacted card]") throw new Error("Enter a short name, not a card number");
        await this.step(kind === "card_add" ? "due_day" : "review", p);
      } else if (["card_add", "card_due"].includes(kind) && step === "due_day") {
        if (!/^\d{1,2}$/.test(text.trim()) || Number(text) < 1 || Number(text) > 31) throw new Error("Enter a day from 1 to 31");
        p.due_day = Number(text); await this.step("review", p);
      } else if (kind === "payment" && step === "amount") {
        [p.amount, p.currency] = amountInput(text, this.code());
        p.paid_on = this.today(); await this.step("review", p);
      } else if (["subscription", "subscription_edit"].includes(kind) && ["name", "amount", "due_on"].includes(step)) {
        if (step === "name") {
          p.merchant = clean(text);
          if (!p.merchant) throw new Error("Enter a subscription name");
          await this.step(p.first_due_on ? "review" : "amount", p);
        } else if (step === "amount") {
          [p.amount, p.currency] = amountInput(text, p.currency || this.code());
          await this.step(p.first_due_on ? "review" : "frequency", p);
        } else {
          const due = parseDate(text.trim());
          if (due < this.today()) throw new Error("Enter today or a future payment date");
          p.first_due_on = due; p.auto_from = this.today();
          await this.step("review", p);
        }
      } else if (kind === "overview_range" && ["start", "end"].includes(step)) {
        const selected = parseDate(text.trim());
        if (step === "start") { p.start = selected; await this.step("end", p); }
        else {
          if (selected < p.start) throw new Error("End date is before start date");
          this.clear(); await this.overview(p.start, selected);
        }
      } else await this.say("Use the buttons shown above, or /cancel to stop.");
    } catch (error) {
      if (!(error instanceof Error) || !/^(Enter|Use|Too many)/.test(error.message)) throw error;
      await this.say(error.message); await this.prompt();
    }
  }

  async users() {
    const ids = this.invitedUsers();
    await this.say(ids.length ? `Invited Telegram users:\n${ids.join("\n")}` : "No friends have claimed an invite yet.", [...ids.map((id) => [`Revoke ${id}`, `admin:revokeask:${id}`]), ["Back", "nav:home"]]);
  }
  async callback(callback, updateId) {
    try { await telegram(this.env, "answerCallbackQuery", { callback_query_id: callback.id }); } catch { /* Old buttons can still be processed. */ }
    const data = callback.data || "";
    if (data.startsWith("nav:")) { this.clear(); await this.navigate(data.slice(4)); return; }
    if (data.startsWith("admin:") && this.user() === String(this.env.OWNER_TELEGRAM_USER_ID)) {
      const match = /^admin:(revokeask|revoke):(\d+)$/.exec(data);
      if (!match || !this.invitedUsers().includes(match[2])) return;
      if (match[1] === "revokeask") await this.say(`Revoke access for Telegram user ${match[2]}? Their records stay stored but the bot stops accepting their messages.`, [["Revoke access", `admin:revoke:${match[2]}`], ["Cancel", "nav:home"]]);
      else {
        const target = this.env.ACCOUNTS.getByName(`user:${match[2]}`);
        await target.revoke();
        this.sql.exec("UPDATE invites SET expires_at=0 WHERE claimed_by=?", match[2]);
        await this.users();
      }
      return;
    }
    if (data.startsWith("cards:")) {
      const action = data.slice(6);
      if (action === "add") await this.start("card_add", "name");
      else if (action === "pay") {
        const unpaid = rows(this.sql, "SELECT id FROM cards WHERE active=1").some((r) => !this.cardPaid(r.id, this.today().slice(0, 7)));
        if (unpaid) await this.start("payment", "card"); else await this.say("No unpaid cards this month.");
      } else {
        const view = /^view:(\d+)$/.exec(action), due = /^due:(\d+)$/.exec(action), alerts = /^alerts:(\d+)$/.exec(action), alert = /^alert:(\d+):(0|1|3|7|14)$/.exec(action);
        const id = Number((view || due || alerts || alert || [])[1]);
        const card = id && row(this.sql, "SELECT * FROM cards WHERE id=? AND active=1", id);
        if (!card) return;
        if (view) await this.say(`${card.name} · due day ${card.due_day} · remind ${card.reminder_days ? `${card.reminder_days} days before` : "due day only"}`, [["Change due day", `cards:due:${id}`], ["Reminder timing", `cards:alerts:${id}`], ["Back", "nav:cards"]]);
        else if (due) await this.start("card_due", "due_day", { id, name: card.name });
        else if (alerts) await this.say(`Remind me before ${card.name} is due:`, [["Due day only", `cards:alert:${id}:0`], ["1 day", `cards:alert:${id}:1`], ["3 days", `cards:alert:${id}:3`], ["1 week", `cards:alert:${id}:7`], ["2 weeks", `cards:alert:${id}:14`]]);
        else if (alert) { this.sql.exec("UPDATE cards SET reminder_days=? WHERE id=?", Number(alert[2]), id); await this.cards(); }
      }
      return;
    }
    if (data.startsWith("subs:")) {
      const action = data.slice(5), match = /^(view|edit|toggle):(\d+)$/.exec(action);
      if (action === "add") await this.start("subscription", "name");
      else if (match) {
        const sub = row(this.sql, "SELECT * FROM subscriptions WHERE id=?", Number(match[2]));
        if (!sub) return;
        if (match[1] === "view") await this.say(`${sub.merchant} · ${this.fmt(sub.amount, sub.currency)} ${sub.frequency} · next ${nextRenewal(sub.first_due_on, sub.frequency, this.today())}`, [["Edit", `subs:edit:${sub.id}`], [sub.active ? "Cancel renewals" : "Reactivate", `subs:toggle:${sub.id}`], ["Back", "nav:subs"]]);
        else if (match[1] === "edit") await this.start("subscription_edit", "edit_field", sub);
        else {
          if (sub.active) this.syncSubscriptions();
          this.sql.exec("UPDATE subscriptions SET active=1-active,auto_from=? WHERE id=?", sub.active ? sub.auto_from : this.today(), sub.id);
          await this.subscriptions();
        }
      }
      return;
    }
    if (data === "manage:categories") { await this.categories(); return; }
    if (data === "manage:expenses") { await this.expensesToEdit(); return; }
    if (data === "category:add") { await this.start("category_add", "name"); return; }
    if (data === "category:rename") {
      await this.start("category_rename", "choose");
      const session = this.session();
      await this.say("Choose a category to rename:", this.choices(session, rows(this.sql, "SELECT id,name FROM categories WHERE active=1 ORDER BY id").map((r) => [r.name, r.id]), "category"));
      return;
    }
    if (data.startsWith("expense:")) {
      const match = /^expense:(edit|undo):(\d+)$/.exec(data);
      if (!match) return;
      const expense = row(this.sql, "SELECT * FROM expenses WHERE id=? AND deleted=0", Number(match[2]));
      if (!expense) { await this.say("Expense not found."); return; }
      if (match[1] === "edit") await this.start("expense_edit", "edit_field", expense);
      else await this.start("expense_undo", "review", { expense_id: expense.id });
      return;
    }
    if (data.startsWith("overview:")) {
      const action = data.slice(9), today = this.today();
      if (action === "week") {
        const weekday = new Date(`${today}T00:00:00Z`).getUTCDay();
        await this.overview(addDays(today, -(weekday + 6) % 7), today);
      } else if (action === "month") await this.overview();
      else if (action === "months") await this.months();
      else if (action === "range") await this.start("overview_range", "start");
      else if (action === "upcoming") await this.upcoming();
      else if (/^month:\d{4}-\d{2}$/.test(action)) {
        try {
          const start = parseDate(`${action.slice(6)}-01`);
          if (start <= monthStart(today)) await this.overview(start, monthEnd(start) > today ? today : monthEnd(start));
        } catch { /* Invalid month button. */ }
      }
      return;
    }
    const match = /^s:(\d+):(.+)$/.exec(data);
    if (!match) return;
    const session = this.session();
    if (!session || session.id !== Number(match[1])) { await this.say("That form has expired. Open /menu to start again."); return; }
    await this.sessionAction(session, match[2], updateId);
  }

  async sessionAction(session, action, updateId) {
    const { kind, step } = session, p = JSON.parse(session.payload);
    if (action === "action:cancel") { this.clear(); await this.home(); return; }
    if (step === "review") {
      if (action === "action:save") await this.saveSession(kind, p, updateId);
      else if (action === "action:edit") {
        if (["expense", "expense_edit", "subscription", "subscription_edit"].includes(kind)) await this.step("edit_field", p);
        else await this.say("Use Cancel and start again to change this entry.");
      }
      return;
    }
    if (["expense", "expense_edit"].includes(kind)) {
      if (step === "category" && /^cat:\d+$/.test(action)) {
        const category = row(this.sql, "SELECT name FROM categories WHERE id=? AND active=1", Number(action.slice(4)));
        if (category) { p.category = category.name; await this.step(kind === "expense_edit" || p.amount ? "review" : "amount", p); }
      } else if (step === "edit_field" && /^(field:)(category|amount|merchant|date)$/.test(action)) await this.step(action.slice(6), p);
      else if (step === "date" && action.startsWith("date:")) {
        const choice = action.slice(5);
        if (choice === "custom") await this.step("date_input", p);
        else if (choice === "today" || choice === "yesterday") { p.spent_on = choice === "today" ? this.today() : addDays(this.today(), -1); await this.step("review", p); }
      }
    } else if (kind === "category_rename" && step === "choose" && /^category:\d+$/.test(action)) {
      const category = row(this.sql, "SELECT * FROM categories WHERE id=? AND active=1", Number(action.slice(9)));
      if (category) { p.category_id = category.id; p.old_name = category.name; await this.step("name", p); }
    } else if (kind === "payment" && step === "card" && /^card:\d+$/.test(action)) {
      const card = row(this.sql, "SELECT * FROM cards WHERE id=? AND active=1", Number(action.slice(5)));
      const month = this.today().slice(0, 7);
      if (card && !this.cardPaid(card.id, month)) {
        Object.assign(p, { card_id: card.id, card_name: card.name, cycle_month: month });
        await this.step("amount", p);
      }
    } else if (["subscription", "subscription_edit"].includes(kind)) {
      if (step === "edit_field" && /^(field:)(name|amount|frequency)$/.test(action)) await this.step(action.slice(6), p);
      else if (step === "frequency" && /^frequency:(monthly|quarterly|yearly)$/.test(action)) { p.frequency = action.slice(10); await this.step("due_on", p); }
    }
  }

  async saveSession(kind, p) {
    try {
      let expenseId, label;
      if (kind === "expense") {
        this.sql.exec("INSERT INTO expenses(amount,currency,merchant,category,spent_on) VALUES(?,?,?,?,?)", p.amount, p.currency, p.merchant, p.category, p.spent_on);
        expenseId = row(this.sql, "SELECT last_insert_rowid() id").id; label = "Saved expense";
      } else if (kind === "expense_edit") {
        this.sql.exec("UPDATE expenses SET amount=?,currency=?,merchant=?,category=?,spent_on=? WHERE id=? AND deleted=0", p.amount, p.currency, p.merchant, p.category, p.spent_on, p.id);
        expenseId = p.id; label = "Updated expense";
      } else if (kind === "expense_undo") {
        this.sql.exec("UPDATE expenses SET deleted=1 WHERE id=?", p.expense_id);
        this.clear(); await this.say("Expense removed from spending totals."); return;
      } else if (kind === "card_add") {
        this.sql.exec("INSERT INTO cards(name,due_day,created_on) VALUES(?,?,?)", p.name, p.due_day, this.today());
        this.clear(); await this.cards(); return;
      } else if (kind === "card_due") {
        this.sql.exec("UPDATE cards SET due_day=? WHERE id=? AND active=1", p.due_day, p.id);
        this.clear(); await this.cards(); return;
      } else if (kind === "category_add") {
        this.sql.exec("INSERT INTO categories(name) VALUES(?)", p.name);
        this.clear(); await this.categories(); return;
      } else if (kind === "category_rename") {
        this.sql.exec("UPDATE categories SET name=? WHERE id=?", p.name, p.category_id);
        this.sql.exec("UPDATE expenses SET category=? WHERE category=? COLLATE NOCASE", p.name, p.old_name);
        this.clear(); await this.categories(); return;
      } else if (kind === "payment") {
        const card = row(this.sql, "SELECT * FROM cards WHERE id=? AND active=1", p.card_id);
        if (!card || this.cardPaid(p.card_id, p.cycle_month) || p.cycle_month !== this.today().slice(0, 7)) {
          this.clear(); await this.say("That card is already paid, or the month changed. Open /cards and start again."); return;
        }
        this.sql.exec("INSERT INTO card_payments(card_id,cycle_month,amount,currency,paid_on) VALUES(?,?,?,?,?)", card.id, p.cycle_month, p.amount, p.currency, this.today());
        this.clear(); await this.say(`${card.name} marked Paid for ${p.cycle_month}: ${this.fmt(p.amount, p.currency)} recorded. This payment is excluded from spending totals.`); return;
      } else if (kind === "subscription") {
        this.sql.exec("INSERT INTO subscriptions(merchant,amount,currency,frequency,first_due_on,auto_from) VALUES(?,?,?,?,?,?)", p.merchant, p.amount, p.currency, p.frequency, p.first_due_on, p.auto_from);
        this.clear(); await this.subscriptions(); return;
      } else if (kind === "subscription_edit") {
        this.syncSubscriptions();
        this.sql.exec("UPDATE subscriptions SET merchant=?,amount=?,currency=?,frequency=?,first_due_on=?,auto_from=? WHERE id=?", p.merchant, p.amount, p.currency, p.frequency, p.first_due_on, p.auto_from, p.id);
        this.clear(); await this.subscriptions(); return;
      } else return;
      this.clear();
      await this.say(`${label} #${expenseId}: ${p.merchant} · ${this.fmt(p.amount, p.currency)} · ${p.category} · ${p.spent_on}`, [["Edit", `expense:edit:${expenseId}`], ["Undo", `expense:undo:${expenseId}`]]);
    } catch (error) {
      if (!/UNIQUE constraint failed/i.test(String(error)) || !["card_add", "category_add", "category_rename"].includes(kind)) throw error;
      await this.say("That name already exists. Enter a different one.");
      await this.step("name", p);
    }
  }

  async ensureAlarm() {
    if (await this.ctx.storage.getAlarm() == null) await this.ctx.storage.setAlarm(Date.now() + 6 * 3600000);
  }
  async remind(kind, id, due, today, label) {
    if (row(this.sql, "SELECT 1 ok FROM reminders WHERE kind=? AND item_id=? AND due_on=? AND reminder_on=?", kind, id, due, today)) return;
    this.sql.exec("INSERT OR IGNORE INTO reminders VALUES(?,?,?,?)", kind, id, due, today);
    const days = Math.round((Date.parse(`${due}T00:00:00Z`) - Date.parse(`${today}T00:00:00Z`)) / 86400000);
    await this.say(`Reminder · ${label} on ${due}${days === 0 ? " (today)" : ` (in ${days} day${days === 1 ? "" : "s"})`}`);
  }
  async reminders() {
    if (this.get("authorized") !== "1") return;
    this.syncSubscriptions();
    const today = this.today();
    for (const sub of rows(this.sql, "SELECT * FROM subscriptions WHERE active=1")) {
      for (const offset of [0, 3]) {
        const due = addDays(today, offset);
        if (nextRenewal(sub.first_due_on, sub.frequency, due) === due)
          await this.remind("subscription", sub.id, due, today, `${sub.merchant} renews for ${this.fmt(sub.amount, sub.currency)}`);
      }
    }
    for (const card of rows(this.sql, "SELECT * FROM cards WHERE active=1")) {
      for (const month of [today.slice(0, 7), nextMonth(today.slice(0, 7))]) {
        const due = monthDue(card.due_day, month);
        const days = Math.round((Date.parse(`${due}T00:00:00Z`) - Date.parse(`${today}T00:00:00Z`)) / 86400000);
        if ((days === 0 || days === card.reminder_days) && !this.cardPaid(card.id, month))
          await this.remind("card", card.id, due, today, `${card.name} card payment due`);
      }
    }
  }
  async alarm() {
    await this.ctx.storage.setAlarm(Date.now() + 6 * 3600000);
    await this.flushOutbox();
    await this.reminders();
  }
  async handle(update, userId) {
    if (this.user() !== String(userId) || this.get("authorized") !== "1") return false;
    if (!Number.isSafeInteger(update.update_id)) return false;
    if (row(this.sql, "SELECT 1 ok FROM processed_updates WHERE update_id=?", update.update_id)) return true;
    this.sql.exec("INSERT INTO processed_updates(update_id) VALUES(?)", update.update_id);
    try {
      await this.flushOutbox();
      if (update.message?.photo?.length) await this.say("Receipt extraction is off. Use /add to enter the purchase.");
      else if (update.message) await this.handleText(update.message.text || "");
      else if (update.callback_query) await this.callback(update.callback_query, update.update_id);
      await this.ensureAlarm();
    } catch (error) {
      this.sql.exec("DELETE FROM processed_updates WHERE update_id=?", update.update_id);
      throw error;
    }
    return true;
  }
}

export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (request.method === "GET" && path === "/") return new Response("SpendCue is running");
    if (request.method === "POST" && path === "/import") {
      if (!env.MIGRATION_SECRET || request.headers.get("X-SpendCue-Import-Secret") !== env.MIGRATION_SECRET) return new Response("Forbidden", { status: 403 });
      try {
        const body = await request.text();
        if (body.length > 2_000_000) return new Response("Import too large", { status: 413 });
        const ownerId = String(env.OWNER_TELEGRAM_USER_ID), owner = env.ACCOUNTS.getByName(`user:${ownerId}`);
        await owner.authorize(ownerId);
        const result = await owner.importData(JSON.parse(body));
        return Response.json(result);
      } catch (error) {
        console.error("SpendCue import failed", error?.name || "Error");
        return new Response("Import rejected", { status: 400 });
      }
    }
    if (request.method !== "POST" || path !== "/telegram") return new Response("Not found", { status: 404 });
    if (!env.TELEGRAM_WEBHOOK_SECRET || request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.TELEGRAM_WEBHOOK_SECRET)
      return new Response("Forbidden", { status: 403 });
    let update;
    try { update = await request.json(); } catch { return new Response("Bad JSON", { status: 400 }); }
    const message = update.message || update.callback_query?.message;
    const actor = update.message?.from || update.callback_query?.from;
    if (!Number.isSafeInteger(update.update_id) || !actor?.id || message?.chat?.type !== "private" || actor.id !== message.chat.id)
      return new Response("OK");
    const id = String(actor.id), ownerId = String(env.OWNER_TELEGRAM_USER_ID);
    const account = env.ACCOUNTS.getByName(`user:${id}`);
    try {
      if (id === ownerId) await account.authorize(id);
      else {
        const raw = update.message?.text?.trim() || "";
        const invite = /^\/start(?:@\w+)?\s+([0-9a-f]{32})$/.exec(raw)?.[1];
        if (invite) {
          const owner = env.ACCOUNTS.getByName(`user:${ownerId}`);
          await owner.authorize(ownerId);
          if (await owner.claimInvite(invite, id)) await account.authorize(id);
        }
      }
      if (!await account.handle(update, id) && update.message?.text?.startsWith("/start"))
        await send(env, id, "This bot is invite-only. Ask its owner for a fresh invite link.");
      return new Response("OK");
    } catch (error) {
      console.error("SpendCue update failed", error?.name || "Error");
      return new Response("Retry", { status: 500 });
    }
  },
};
