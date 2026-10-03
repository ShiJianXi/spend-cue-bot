export const CATEGORIES = ["Food", "Transport", "Shopping", "Bills", "Other"];
const ZERO = new Set("BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX UYI VND VUV XAF XOF XPF".split(" "));
const THREE = new Set("BHD IQD JOD KWD LYD OMR TND".split(" "));

export const places = (code) => ZERO.has(code) ? 0 : THREE.has(code) ? 3 : 2;
export function currency(code, fallback = "SGD") {
  const value = (code || fallback).trim().toUpperCase();
  if (!/^[A-Z]{3}$/.test(value)) throw new Error("Use a three-letter currency code");
  return value;
}
export function amountInput(text, fallback = "SGD") {
  const match = /^\s*(?:([A-Za-z]{3})\s*)?\$?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*([A-Za-z]{3})?\s*$/.exec(text);
  if (!match || (match[1] && match[3] && match[1].toUpperCase() !== match[3].toUpperCase()))
    throw new Error("Enter an amount such as 24.80 or USD 24.80");
  const code = currency(match[1] || match[3], fallback);
  const [whole, fraction = ""] = match[2].replaceAll(",", "").split(".");
  const digits = places(code);
  if (fraction.length > digits) throw new Error("Too many decimal places for this currency");
  const amount = BigInt(whole) * 10n ** BigInt(digits) + BigInt((fraction || "").padEnd(digits, "0") || "0");
  if (amount <= 0n || amount > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("Enter a positive, smaller amount");
  return [Number(amount), code];
}
export function formatMoney(amount, code) {
  const factor = 10 ** places(code);
  const whole = Math.floor(amount / factor);
  const fraction = places(code) ? `.${String(amount % factor).padStart(places(code), "0")}` : "";
  return `${code} ${whole}${fraction}`;
}
export function clean(text, limit = 100) {
  const value = String(text || "").trim();
  return (value.replace(/\D/g, "").length >= 12 ? "[redacted card]" : value).slice(0, limit);
}
export function parseDate(text) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) throw new Error("Enter a valid date as YYYY-MM-DD");
  const date = new Date(`${text}T00:00:00Z`);
  if (Number.isNaN(date.getTime()) || date.toISOString().slice(0, 10) !== text) throw new Error("Enter a valid date as YYYY-MM-DD");
  return text;
}
export function todayIn(timeZone, clock = Date.now()) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" })
    .formatToParts(clock).filter((p) => p.type !== "literal").map((p) => [p.type, p.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}
export function addDays(day, count) {
  const result = new Date(`${parseDate(day)}T00:00:00Z`);
  result.setUTCDate(result.getUTCDate() + count);
  return result.toISOString().slice(0, 10);
}
export function monthDue(day, month) {
  const first = parseDate(`${month.slice(0, 7)}-01`);
  const [year, number] = first.split("-").map(Number);
  const last = new Date(Date.UTC(year, number, 0)).getUTCDate();
  return `${first.slice(0, 8)}${String(Math.min(day, last)).padStart(2, "0")}`;
}
export function nextRenewal(first, frequency, onOrAfter) {
  parseDate(first); parseDate(onOrAfter);
  const step = { monthly: 1, quarterly: 3, yearly: 12 }[frequency];
  if (!step) throw new Error("Choose monthly, quarterly, or yearly");
  if (onOrAfter <= first) return first;
  const [year, month, day] = first.split("-").map(Number);
  let count = Math.max(0, Math.floor(((Number(onOrAfter.slice(0, 4)) - year) * 12 + Number(onOrAfter.slice(5, 7)) - month) / step));
  for (;;) {
    const d = new Date(Date.UTC(year, month - 1 + count * step, 1));
    const due = monthDue(day, d.toISOString().slice(0, 7));
    if (due >= onOrAfter) return due;
    count++;
  }
}
export function subscriptionAnchor(value, frequency, today) {
  // Leap-year anchor preserves the chosen day after a shortened renewal month.
  value = value.trim();
  let first;
  if (frequency === "monthly") {
    if (!/^\d{1,2}$/.test(value) || Number(value) < 1 || Number(value) > 31) throw new Error("Enter a day from 1 to 31");
    first = `2000-01-${value.padStart(2, "0")}`;
  } else if (frequency === "quarterly") {
    const match = /^(\d{1,2})-(\d{1,2})$/.exec(value);
    if (!match) throw new Error("Enter a month and day as MM-DD");
    try { first = parseDate(`2000-${match[1].padStart(2, "0")}-${match[2].padStart(2, "0")}`); }
    catch { throw new Error("Enter a valid month and day as MM-DD"); }
  } else if (frequency === "yearly") {
    first = parseDate(value);
    if (first < today) throw new Error("Enter today or a future payment date");
  } else throw new Error("Choose monthly, quarterly, or yearly");
  return first;
}
export const csvSafe = (value) => /^[=+\-@]/.test(String(value).trimStart()) ? `'${value}` : value;
