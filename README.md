# Sales & Expense Tracker — SHREE BALAJI ASSOCIATES

A client-server business tracker for a brick manufacturing plant: sales
(with item/quantity/rate), expenses, client advances, partial payments,
and client ledger adjustments — all backed by a real SQLite database on
your Raspberry Pi, usable by several people at once from their own devices.

## Major update: payments, advances & adjustments architecture

This version changed how money is tracked, to support real-world cases
like partial payments, overpayments, advances paid before a sale, and
non-cash adjustments (discounts, TDS, bad debts). Read this section before
deploying if you're upgrading from an earlier version.

### The model
- **A sale is a bill.** `entries` (kind='sale') holds the bill: client,
  item, quantity, rate, and the amount due. It does **not** store whether
  it's "paid" directly anymore.
- **Payments are separate, and there can be several per bill.** Every
  payment against a bill (cash, bank, or using a client's advance balance)
  is its own row in `payments`. A bill's status — Pending / Partial / Paid
  — is always calculated live by summing its payments, so partial payments
  and overpayments are both handled correctly and nothing gets out of
  sync.
- **Advances** (`advances` table): money a client pays *before* any bill
  exists. When a bill is later paid "via Advance," it draws down that
  client's advance balance instead of counting as new income (so it's
  never double-counted).
- **Client Adjustments** (`client_adjustments` table): Discount, Round
  Off, Bad Debt, TDS, or any custom type you name — these reduce what a
  client owes overall without any money moving. They apply against the
  client's total outstanding balance (not tied to one specific bill),
  which keeps the entry simple: pick the client, type, amount, done.
- **Loading/Unloading**: when recording that a sale was paid, you can
  optionally note that you also paid a loading/unloading charge at the
  same time. This automatically creates a matching expense entry (so it
  shows up in your Expenses too) and is annotated in reports next to the
  sale it came from.

### Role-based rules (per your request)
- **Admin** (`admin` / `12346`): no date restrictions anywhere — can
  backdate entries and payments freely, can delete anything, can clear
  all data and restore backups.
- **Data Entry Operator** (`user` / `1234`): entries and payments must be
  dated today or yesterday only, and a payment can never be dated before
  the bill itself was created. Cannot delete, clear data, or restore
  backups.

### Backups now always restore safely, even old ones
Export/Restore now carries **everything** (bills, payments, advances,
adjustments) in one file. Two extra safety nets:
- Restoring a backup made by an **earlier version** of this app (before
  payments/advances existed) still works — old "received" sales are
  automatically converted into a matching payment record so nothing is
  lost or misrepresented.
- Any field a *future* version of this app doesn't recognize yet is kept
  safely in the database (not shown, but not deleted) instead of being
  dropped, so you never lose data just because a newer version changed
  what it displays.

### Clear All Data (admin only)
In the 🗄️ Backup & Restore popup, admins can wipe every entry, payment,
advance, and adjustment (not user accounts) — useful right before
restoring a clean backup. It requires typing `DELETE` to confirm and
cannot be undone, so **always back up first**.

## What's in the app now

- **🏢 Company letterhead** — SHREE BALAJI ASSOCIATES, address, and GSTIN
  appear on the login screen, the app header, and every PDF/Excel report
  (titled "Report of Sales and Expenses").
- **🗄️ / 📄 icon buttons** (admin only, top-right) open Backup/Restore
  and Reports as popups instead of taking up space on the main screen.
- **Sale / Expense / Advance** — a three-way toggle on the entry form.
- **Record Payment** (on any pending/partial bill) — asks amount (not
  assumed to be the full balance; can be less for a partial payment or
  more for an overpayment), date, method (Cash/Bank/Advance), and an
  optional loading/unloading charge.
- **Pending tab** — every client with an outstanding net balance, sorted
  highest first, tap through to their full ledger.
- **Client Ledger** — per client: total billed, gross pending, total
  adjustments, net pending, available advance balance, full bill history
  with payment status, and buttons to add an Advance or an Adjustment
  right there.
- **Dashboard** — Opening/Closing **Cash** only (Bank balance tracking
  was removed since this app doesn't record your full bank ledger, only
  bank *receipts* — see below). Bank Sales/Expenses still show as period
  totals, just not as a running balance.

### Why no more Opening/Closing Bank balance
You mentioned you don't enter your whole bank account's activity here —
just which sales/advances came in via bank. A running "bank balance"
from partial data would be misleading, so that's gone. Cash still gets a
real opening/closing balance because (per your setup) cash-in-hand is
fully tracked here.

## Deploying / upgrading

**Fresh install:** same steps as before —
```bash
cd cash-tracker-server
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python serve.py
```

**Upgrading an existing install:** copy the new `app.py` and
`templates/index.html` over the old ones, then:
```bash
sudo systemctl restart tracker
```
Your database upgrades itself automatically — old "received" sales get a
matching payment record created for them the first time the new app.py
runs, so their status still shows correctly.

**Recommended before any upgrade:** take a backup first (🗄️ → Backup) —
belt and suspenders, even though the migration is designed to be safe.

## Changing passwords

```bash
cd cash-tracker-server
source venv/bin/activate
python3 -c "
import sqlite3
from werkzeug.security import generate_password_hash
conn = sqlite3.connect('tracker.db')
conn.execute('UPDATE users SET password_hash=? WHERE username=?',
             (generate_password_hash('YOUR-NEW-PASSWORD'), 'admin'))
conn.commit()
"
```
(swap `'admin'` for `'user'` for the operator account)

## Making it start on boot

```bash
sudo cp tracker.service /etc/systemd/system/tracker.service
sudo systemctl daemon-reload
sudo systemctl enable tracker --now
journalctl -u tracker -f   # view logs
```
Edit `tracker.service` first if your username isn't `pi`, and set
`TRACKER_SECRET` to a random value (see comments in the file).

## Backing up the real database directly

```bash
scp pi@<pi-ip>:/home/pi/cash-tracker-server/tracker.db ./tracker-backup-$(date +%F).db
```
The in-app Backup/Restore (🗄️ icon, admin only) is a JSON export of
everything and is the easier day-to-day option.

## Internet dependency

PDF/Excel export loads two small libraries from a public CDN (jsPDF,
SheetJS) the first time they're used in a browser session — the Pi needs
internet access at that moment. Everything else works fully offline on
your local network.
