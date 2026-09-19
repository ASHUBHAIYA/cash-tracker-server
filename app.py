import os
import json
import functools
from datetime import date, timedelta

from flask import Flask, request, jsonify, session, render_template
from werkzeug.security import generate_password_hash, check_password_hash
import libsql_client

app = Flask(__name__)
app.secret_key = os.environ.get("TRACKER_SECRET", "please-change-this-secret-key")
app.permanent_session_lifetime = timedelta(days=30)

COMPANY = {
    "name": "SHREE BALAJI ASSOCIATES",
    "address": "Near Om Sai Biofuels Bargawan Odgadi Distt. Singrauli",
    "gstin": "23AEPFS7841N1Z9",
}

# --- Turso Cloud Database Configuration ---
TURSO_DB_URL = os.environ.get(
    "TURSO_DATABASE_URL",
    "https://cash-tracker-ashubhaiya.aws-ap-south-1.turso.io"
)
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")


class RowWrapper:
    """Provides sqlite3.Row-like access (both row['key'] and row.get('key'))"""
    def __init__(self, d):
        self._d = d

    def __getitem__(self, key):
        return self._d[key]

    def get(self, key, default=None):
        return self._d.get(key, default)

    def keys(self):
        return self._d.keys()

    def values(self):
        return self._d.values()

    def items(self):
        return self._d.items()

    def __iter__(self):
        return iter(self._d)

    def __repr__(self):
        return repr(self._d)


class CursorResult:
    def __init__(self, last_insert_rowid=None, rows=None):
        self.lastrowid = last_insert_rowid
        self._rows = rows or []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class TursoDBWrapper:
    """Emulates a sqlite3 connection for libSQL client"""
    def __init__(self, client):
        self.client = client

    def execute(self, sql, params=None):
        params = list(params) if params is not None else []
        res = self.client.execute(sql, params)
        columns = res.columns
        rows = [RowWrapper(dict(zip(columns, row))) for row in res.rows]
        return CursorResult(res.last_insert_rowid, rows)

    def commit(self):
        pass  # Turso auto-commits individual queries

    def rollback(self):
        pass

    def close(self):
        self.client.close()


def get_db():
    client = libsql_client.create_client_sync(
        url=TURSO_DB_URL,
        auth_token=TURSO_AUTH_TOKEN
    )
    return TursoDBWrapper(client)


KNOWN_ENTRY_FIELDS = {
    "date", "kind", "client", "item_type", "itemType", "qty", "rate",
    "expense_type", "expenseType", "amount", "note", "method", "received",
    "received_date", "receivedDate", "linked_sale_id", "linkedSaleId",
    "created_by", "created_at", "id",
}


def init_db():
    conn = get_db()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users(
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            label TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS entries(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            kind TEXT NOT NULL,
            client TEXT,
            item_type TEXT,
            qty REAL,
            rate REAL,
            expense_type TEXT,
            amount REAL NOT NULL,
            note TEXT,
            received INTEGER NOT NULL DEFAULT 0,
            method TEXT,
            received_date TEXT,
            linked_sale_id INTEGER,
            extra_json TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            method TEXT NOT NULL,
            amount REAL NOT NULL,
            loading_unloading REAL NOT NULL DEFAULT 0,
            loading_unloading_expense_id INTEGER,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(entry_id) REFERENCES entries(id) ON DELETE CASCADE
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS advances(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            client TEXT NOT NULL,
            amount REAL NOT NULL,
            method TEXT NOT NULL,
            note TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS client_adjustments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            client TEXT NOT NULL,
            adj_type TEXT NOT NULL,
            amount REAL NOT NULL,
            note TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )

    # --- column check for entries table ---
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(entries)").fetchall()}
    for col, coltype in (
        ("item_type", "TEXT"), ("qty", "REAL"), ("rate", "REAL"),
        ("expense_type", "TEXT"), ("linked_sale_id", "INTEGER"),
        ("extra_json", "TEXT"),
    ):
        if col not in existing_cols:
            try:
                conn.execute(f"ALTER TABLE entries ADD COLUMN {col} {coltype}")
            except Exception:
                pass

    # Create default users if users table is empty
    user_count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    if user_count == 0:
        conn.execute(
            "INSERT INTO users VALUES (?,?,?,?)",
            ("admin", generate_password_hash("12346"), "admin", "Admin"),
        )
        conn.execute(
            "INSERT INTO users VALUES (?,?,?,?)",
            ("user", generate_password_hash("1234"), "operator", "Data Entry Operator"),
        )
        print("First run: created default users admin/12346 and user/1234 on Turso.")

    conn.close()


def login_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if "username" not in session:
            return jsonify({"error": "not authenticated"}), 401
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("role") != "admin":
            return jsonify({"error": "admin only"}), 403
        return f(*args, **kwargs)
    return wrapper


def is_admin():
    return session.get("role") == "admin"


def check_date_window(entry_date, earliest_allowed=None):
    if is_admin():
        return None
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    lo = max(earliest_allowed, yesterday) if earliest_allowed else yesterday
    if entry_date < lo or entry_date > today:
        return f"date must be between {lo} and {today}"
    return None


def split_known_extra(d, known):
    extra = {k: v for k, v in d.items() if k not in known}
    return json.dumps(extra) if extra else None


def merge_extra(row_dict):
    extra = row_dict.pop("extra_json", None)
    if extra:
        try:
            for k, v in json.loads(extra).items():
                if k not in row_dict:
                    row_dict[k] = v
        except (TypeError, ValueError):
            pass
    return row_dict


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/company")
def company_info():
    return jsonify(COMPANY)


# --- auth -------------------------------------------------------------

@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(force=True) or {}
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "invalid credentials"}), 401
    session.permanent = True
    session["username"] = row["username"]
    session["role"] = row["role"]
    return jsonify({"username": row["username"], "role": row["role"], "label": row["label"]})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me")
def me():
    if "username" not in session:
        return jsonify({"error": "not authenticated"}), 401
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username=?", (session["username"],)).fetchone()
    conn.close()
    if not row:
        session.clear()
        return jsonify({"error": "not authenticated"}), 401
    return jsonify({"username": row["username"], "role": row["role"], "label": row["label"]})


# --- lookups for autocomplete ------------------------------------------

@app.route("/api/item-types")
@login_required
def list_item_types():
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT item_type FROM entries WHERE item_type IS NOT NULL AND item_type != '' ORDER BY item_type"
    ).fetchall()
    conn.close()
    return jsonify([r["item_type"] for r in rows])


@app.route("/api/expense-types")
@login_required
def list_expense_types():
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT expense_type FROM entries WHERE expense_type IS NOT NULL AND expense_type != '' ORDER BY expense_type"
    ).fetchall()
    conn.close()
    return jsonify([r["expense_type"] for r in rows])


@app.route("/api/adjustment-types")
@login_required
def list_adjustment_types():
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT adj_type FROM client_adjustments ORDER BY adj_type"
    ).fetchall()
    conn.close()
    defaults = ["Discount", "Round Off", "Bad Debt", "TDS"]
    seen = [r["adj_type"] for r in rows]
    merged = defaults + [s for s in seen if s not in defaults]
    return jsonify(merged)


@app.route("/api/clients")
@login_required
def list_clients():
    conn = get_db()
    names = set()
    for row in conn.execute("SELECT DISTINCT client FROM entries WHERE client IS NOT NULL AND client != ''").fetchall():
        names.add(row["client"])
    for row in conn.execute("SELECT DISTINCT client FROM advances").fetchall():
        names.add(row["client"])
    for row in conn.execute("SELECT DISTINCT client FROM client_adjustments").fetchall():
        names.add(row["client"])
    conn.close()
    return jsonify(sorted(names))


# --- entries (sales & expenses) -----------------------------------------

@app.route("/api/entries", methods=["GET"])
@login_required
def list_entries():
    conn = get_db()
    rows = [merge_extra(dict(r)) for r in conn.execute("SELECT * FROM entries ORDER BY id").fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/entries", methods=["POST"])
@login_required
def add_entry():
    d = request.get_json(force=True) or {}
    kind = d.get("kind")
    entry_date = d.get("date")
    if not entry_date:
        return jsonify({"error": "date is required"}), 400

    err = check_date_window(entry_date)
    if err:
        return jsonify({"error": err}), 400

    if kind == "sale":
        try:
            qty = float(d.get("qty", 0))
            rate = float(d.get("rate", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid quantity or rate"}), 400
        if qty <= 0 or rate <= 0:
            return jsonify({"error": "quantity and rate must be positive"}), 400
        if not (d.get("itemType") or "").strip():
            return jsonify({"error": "item type is required"}), 400
        if not (d.get("client") or "").strip():
            return jsonify({"error": "client is required"}), 400
        amount = round(qty * rate, 2)
        item_type = d.get("itemType", "").strip()
        expense_type = None
    elif kind == "expense":
        qty = None
        rate = None
        item_type = None
        if not (d.get("expenseType") or "").strip():
            return jsonify({"error": "expense type is required"}), 400
        expense_type = d.get("expenseType", "").strip()
        if d.get("method") not in ("cash", "bank"):
            return jsonify({"error": "method must be cash or bank"}), 400
        try:
            amount = float(d.get("amount", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid amount"}), 400
        if amount <= 0:
            return jsonify({"error": "amount must be positive"}), 400
    else:
        return jsonify({"error": "kind must be sale or expense"}), 400

    conn = get_db()
    cur = conn.execute(
        """INSERT INTO entries(date, kind, client, item_type, qty, rate, expense_type, amount, note, method, created_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            entry_date, kind, d.get("client", ""), item_type, qty, rate,
            expense_type, amount, d.get("note", ""),
            d.get("method") if kind == "expense" else None,
            session["username"],
        ),
    )
    new_id = cur.lastrowid

    payment_id = None
    if kind == "sale" and d.get("received"):
        method = d.get("method")
        if method not in ("cash", "bank", "advance"):
            conn.close()
            return jsonify({"error": "method must be cash, bank, or advance"}), 400
        pay_err, payment_id = _record_payment(
            conn, new_id, entry_date, method, amount,
            d.get("loadingUnloadingAmount"), d.get("loadingUnloadingMethod"),
            session["username"],
        )
        if pay_err:
            conn.close()
            return jsonify({"error": pay_err}), 400

    conn.close()
    return jsonify({"id": new_id, "amount": amount, "paymentId": payment_id})


def _client_advance_balance(conn, client):
    row = conn.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM advances WHERE client=?", (client,)
    ).fetchone()
    received = row["s"] if row else 0
    row_used = conn.execute(
        """SELECT COALESCE(SUM(p.amount),0) s FROM payments p
           JOIN entries e ON e.id = p.entry_id
           WHERE e.client=? AND p.method='advance'""",
        (client,),
    ).fetchone()
    used = row_used["s"] if row_used else 0
    return received - used


def _record_payment(conn, entry_id, pay_date, method, amount, loading_amt, loading_method, username):
    entry = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
    if not entry or entry["kind"] != "sale":
        return "sale not found", None
    if amount is None or amount <= 0:
        return "amount must be positive", None
    if method not in ("cash", "bank", "advance"):
        return "method must be cash, bank, or advance", None
    if method == "advance":
        balance = _client_advance_balance(conn, entry["client"])
        if amount > balance + 0.005:
            return f"insufficient advance balance ({balance:.2f} available)", None

    loading_expense_id = None
    loading_amt = float(loading_amt) if loading_amt not in (None, "") else 0
    if loading_amt < 0:
        return "loading/unloading amount cannot be negative", None
    if loading_amt > 0:
        if loading_method not in ("cash", "bank"):
            return "loading/unloading method must be cash or bank", None
        cur = conn.execute(
            """INSERT INTO entries(date, kind, client, expense_type, amount, note, method, linked_sale_id, created_by)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                pay_date, "expense", entry["client"], "Loading/Unloading", loading_amt,
                f"Loading/unloading for sale #{entry_id} ({entry['item_type'] or ''} to {entry['client']})".strip(),
                loading_method, entry_id, username,
            ),
        )
        loading_expense_id = cur.lastrowid

    cur = conn.execute(
        """INSERT INTO payments(entry_id, date, method, amount, loading_unloading, loading_unloading_expense_id, created_by)
           VALUES (?,?,?,?,?,?,?)""",
        (entry_id, pay_date, method, amount, loading_amt, loading_expense_id, username),
    )
    return None, cur.lastrowid


@app.route("/api/entries/<int:eid>/pay", methods=["POST"])
@login_required
def pay_entry(eid):
    d = request.get_json(force=True) or {}
    pay_date = d.get("date")
    if not pay_date:
        return jsonify({"error": "date is required"}), 400

    conn = get_db()
    entry = conn.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
    if not entry or entry["kind"] != "sale":
        conn.close()
        return jsonify({"error": "sale not found"}), 404

    err = check_date_window(pay_date, earliest_allowed=entry["date"])
    if err:
        conn.close()
        return jsonify({"error": err}), 400

    try:
        amount = float(d.get("amount", 0))
    except (TypeError, ValueError):
        conn.close()
        return jsonify({"error": "invalid amount"}), 400

    pay_err, payment_id = _record_payment(
        conn, eid, pay_date, d.get("method"), amount,
        d.get("loadingUnloadingAmount"), d.get("loadingUnloadingMethod"),
        session["username"],
    )
    if pay_err:
        conn.close()
        return jsonify({"error": pay_err}), 400

    conn.close()
    return jsonify({"ok": True, "paymentId": payment_id})


@app.route("/api/payments")
@login_required
def list_payments():
    conn = get_db()
    rows = conn.execute("SELECT * FROM payments ORDER BY id").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/payments/<int:pid>", methods=["DELETE"])
@login_required
@admin_required
def delete_payment(pid):
    conn = get_db()
    row = conn.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404
    if row["loading_unloading_expense_id"]:
        conn.execute("DELETE FROM entries WHERE id=?", (row["loading_unloading_expense_id"],))
    conn.execute("DELETE FROM payments WHERE id=?", (pid,))
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/entries/<int:eid>", methods=["DELETE"])
@login_required
@admin_required
def delete_entry(eid):
    conn = get_db()
    linked = conn.execute("SELECT id FROM entries WHERE linked_sale_id=?", (eid,)).fetchall()
    for row in linked:
        conn.execute("DELETE FROM payments WHERE loading_unloading_expense_id=?", (row["id"],))
        conn.execute("DELETE FROM entries WHERE id=?", (row["id"],))
    conn.execute("DELETE FROM payments WHERE entry_id=?", (eid,))
    conn.execute("DELETE FROM entries WHERE id=?", (eid,))
    conn.close()
    return jsonify({"ok": True})


# --- advances -------------------------------------------------------------

@app.route("/api/advances", methods=["GET"])
@login_required
def list_advances():
    conn = get_db()
    rows = conn.execute("SELECT * FROM advances ORDER BY id").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/advances", methods=["POST"])
@login_required
def add_advance():
    d = request.get_json(force=True) or {}
    if not (d.get("client") or "").strip():
        return jsonify({"error": "client is required"}), 400
    if d.get("method") not in ("cash", "bank"):
        return jsonify({"error": "method must be cash or bank"}), 400
    try:
        amount = float(d.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid amount"}), 400
    if amount <= 0:
        return jsonify({"error": "amount must be positive"}), 400
    adv_date = d.get("date")
    if not adv_date:
        return jsonify({"error": "date is required"}), 400
    err = check_date_window(adv_date)
    if err:
        return jsonify({"error": err}), 400

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO advances(date, client, amount, method, note, created_by) VALUES (?,?,?,?,?,?)",
        (adv_date, d.get("client").strip(), amount, d.get("method"), d.get("note", ""), session["username"]),
    )
    new_id = cur.lastrowid
    conn.close()
    return jsonify({"id": new_id})


@app.route("/api/advances/<int:aid>", methods=["DELETE"])
@login_required
@admin_required
def delete_advance(aid):
    conn = get_db()
    conn.execute("DELETE FROM advances WHERE id=?", (aid,))
    conn.close()
    return jsonify({"ok": True})


# --- client adjustments ---------------------------------------------------

@app.route("/api/client-adjustments", methods=["GET"])
@login_required
def list_adjustments():
    conn = get_db()
    rows = conn.execute("SELECT * FROM client_adjustments ORDER BY id").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/client-adjustments", methods=["POST"])
@login_required
def add_adjustment():
    d = request.get_json(force=True) or {}
    if not (d.get("client") or "").strip():
        return jsonify({"error": "client is required"}), 400
    if not (d.get("adjType") or "").strip():
        return jsonify({"error": "adjustment type is required"}), 400
    try:
        amount = float(d.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid amount"}), 400
    if amount <= 0:
        return jsonify({"error": "amount must be positive"}), 400
    adj_date = d.get("date")
    if not adj_date:
        return jsonify({"error": "date is required"}), 400
    err = check_date_window(adj_date)
    if err:
        return jsonify({"error": err}), 400

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO client_adjustments(date, client, adj_type, amount, note, created_by) VALUES (?,?,?,?,?,?)",
        (adj_date, d.get("client").strip(), d.get("adjType").strip(), amount, d.get("note", ""), session["username"]),
    )
    new_id = cur.lastrowid
    conn.close()
    return jsonify({"id": new_id})


@app.route("/api/client-adjustments/<int:aid>", methods=["DELETE"])
@login_required
@admin_required
def delete_adjustment(aid):
    conn = get_db()
    conn.execute("DELETE FROM client_adjustments WHERE id=?", (aid,))
    conn.close()
    return jsonify({"ok": True})


# --- admin data management -------------------------------------------------

@app.route("/api/export")
@login_required
@admin_required
def export_data():
    conn = get_db()
    entries = [merge_extra(dict(r)) for r in conn.execute("SELECT * FROM entries ORDER BY id").fetchall()]
    payments = [dict(r) for r in conn.execute("SELECT * FROM payments ORDER BY id").fetchall()]
    advances = [dict(r) for r in conn.execute("SELECT * FROM advances ORDER BY id").fetchall()]
    adjustments = [dict(r) for r in conn.execute("SELECT * FROM client_adjustments ORDER BY id").fetchall()]
    conn.close()
    return jsonify({
        "schemaVersion": 2,
        "company": COMPANY,
        "entries": entries,
        "payments": payments,
        "advances": advances,
        "clientAdjustments": adjustments,
    })


@app.route("/api/import", methods=["POST"])
@login_required
@admin_required
def import_data():
    data = request.get_json(force=True)
    if data is None:
        return jsonify({"error": "invalid backup format"}), 400

    if isinstance(data, list):
        entry_rows, payment_rows, advance_rows, adj_rows = data, None, [], []
    else:
        entry_rows = data.get("entries", [])
        payment_rows = data.get("payments")
        advance_rows = data.get("advances", [])
        adj_rows = data.get("clientAdjustments", data.get("client_adjustments", []))

    if not isinstance(entry_rows, list):
        return jsonify({"error": "invalid backup format"}), 400

    conn = get_db()
    conn.execute("DELETE FROM payments")
    conn.execute("DELETE FROM entries")
    conn.execute("DELETE FROM advances")
    conn.execute("DELETE FROM client_adjustments")

    id_map = {}
    for r in entry_rows:
        try:
            amount = float(r.get("amount", 0))
        except (TypeError, ValueError):
            continue
        qty = r.get("qty")
        rate = r.get("rate")
        try:
            qty = float(qty) if qty not in (None, "") else None
            rate = float(rate) if rate not in (None, "") else None
        except (TypeError, ValueError):
            qty, rate = None, None
        old_id = r.get("id")
        extra = split_known_extra(r, KNOWN_ENTRY_FIELDS)
        cur = conn.execute(
            """INSERT INTO entries(date, kind, client, item_type, qty, rate, expense_type, amount, note, method, received, received_date, extra_json, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                r.get("date"), r.get("kind"), r.get("client", ""),
                r.get("item_type") or r.get("itemType"), qty, rate,
                r.get("expense_type") or r.get("expenseType"), amount,
                r.get("note", ""), r.get("method"),
                1 if r.get("received") else 0,
                r.get("received_date") or r.get("receivedDate"),
                extra, r.get("created_by", "import"),
            ),
        )
        if old_id is not None:
            id_map[old_id] = cur.lastrowid
        r["_new_id"] = cur.lastrowid

    for r in entry_rows:
        old_link = r.get("linked_sale_id") or r.get("linkedSaleId")
        if old_link is not None and old_link in id_map:
            conn.execute(
                "UPDATE entries SET linked_sale_id=? WHERE id=?",
                (id_map[old_link], r["_new_id"]),
            )

    if payment_rows is None:
        for r in entry_rows:
            if r.get("kind") == "sale" and r.get("received"):
                conn.execute(
                    """INSERT INTO payments(entry_id, date, method, amount, created_by)
                       VALUES (?,?,?,?,?)""",
                    (
                        r["_new_id"],
                        r.get("received_date") or r.get("receivedDate") or r.get("date"),
                        r.get("method") or "cash",
                        float(r.get("amount", 0)),
                        r.get("created_by", "import"),
                    ),
                )
    else:
        for p in payment_rows:
            old_entry_id = p.get("entry_id")
            new_entry_id = id_map.get(old_entry_id, old_entry_id)
            conn.execute(
                """INSERT INTO payments(entry_id, date, method, amount, loading_unloading, created_by)
                   VALUES (?,?,?,?,?,?)""",
                (
                    new_entry_id, p.get("date"), p.get("method"),
                    float(p.get("amount", 0) or 0), float(p.get("loading_unloading", 0) or 0),
                    p.get("created_by", "import"),
                ),
            )

    for a in advance_rows:
        try:
            amount = float(a.get("amount", 0))
        except (TypeError, ValueError):
            continue
        conn.execute(
            "INSERT INTO advances(date, client, amount, method, note, created_by) VALUES (?,?,?,?,?,?)",
            (a.get("date"), a.get("client", ""), amount, a.get("method", "cash"), a.get("note", ""), a.get("created_by", "import")),
        )

    for adj in adj_rows:
        try:
            amount = float(adj.get("amount", 0))
        except (TypeError, ValueError):
            continue
        conn.execute(
            "INSERT INTO client_adjustments(date, client, adj_type, amount, note, created_by) VALUES (?,?,?,?,?,?)",
            (
                adj.get("date"), adj.get("client", ""),
                adj.get("adj_type") or adj.get("adjType", "Adjustment"),
                amount, adj.get("note", ""), adj.get("created_by", "import"),
            ),
        )

    counts = {
        "entries": len(entry_rows),
        "payments": conn.execute("SELECT COUNT(*) c FROM payments").fetchone()["c"],
        "advances": len(advance_rows),
        "clientAdjustments": len(adj_rows),
    }
    conn.close()
    return jsonify({"ok": True, "counts": counts})


@app.route("/api/clear-all", methods=["POST"])
@login_required
@admin_required
def clear_all():
    d = request.get_json(force=True) or {}
    if d.get("confirm") != "DELETE":
        return jsonify({"error": "confirmation text did not match"}), 400
    conn = get_db()
    conn.execute("DELETE FROM payments")
    conn.execute("DELETE FROM entries")
    conn.execute("DELETE FROM advances")
    conn.execute("DELETE FROM client_adjustments")
    conn.close()
    return jsonify({"ok": True})


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)