"""
Aluminum Wholesale Shop - POS & Customer Due Ledger
Flask + SQLite. Fully offline: no CDN, no external services.

Run:  python app.py      (then open http://127.0.0.1:5000)
"""

import csv
import io
import json
import os
import sqlite3
import threading
import time
from datetime import datetime

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "shop.db")
# Timestamped snapshots live next to the data folder, never inside it, so a
# backup copy can never be mistaken for the live database.
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
BACKUP_PREFIX = "shop_"
BACKUP_STAMP = "%Y-%m-%d_%H-%M"
BACKUP_EVERY_SECONDS = 60 * 60
# 72 hourly snapshots = three full days of history.
BACKUP_KEEP = 72

# ---------------------------------------------------------------------------
# Shop settings shown on the printed receipt. Edit these to your own shop.
# ---------------------------------------------------------------------------
SHOP = {
    "name": "M/S HASAN ALUMINUM INDUSTRIES",
    "tagline": "Aluminum Wholesale",
    "address": "Mizirpul, Muradpur, Chattogram",
    "phone": "+880 1700-000000",
}
CURRENCY = "Tk."

# Scrap aluminum accepted from customers as payment. Rates are per kg and can
# be edited on the Products tab; they are stored in the scrap_rates table.
SCRAP_TYPES = ("Black", "White")
DEFAULT_SCRAP_RATES = (("Black", 190.00), ("White", 160.00))

app = Flask(__name__)
app.config["SECRET_KEY"] = "local-pos-offline-key"
# Flask 3 sorts JSON keys through app.json, not the old config key.
app.json.sort_keys = False


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL UNIQUE,
    category      TEXT    NOT NULL,
    price_per_kg  REAL    NOT NULL CHECK (price_per_kg >= 0)
);

CREATE TABLE IF NOT EXISTS customers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    phone      TEXT DEFAULT '',
    shop_name  TEXT DEFAULT '',
    address    TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sales (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id   INTEGER NOT NULL REFERENCES customers(id),
    subtotal      REAL    NOT NULL,
    previous_due  REAL    NOT NULL,
    grand_total   REAL    NOT NULL,
    paid          REAL    NOT NULL,
    new_due       REAL    NOT NULL,
    created_at    TEXT    NOT NULL,
    scrap_black_kg   REAL NOT NULL DEFAULT 0,
    scrap_white_kg   REAL NOT NULL DEFAULT 0,
    scrap_black_rate REAL NOT NULL DEFAULT 0,
    scrap_white_rate REAL NOT NULL DEFAULT 0,
    scrap_total      REAL NOT NULL DEFAULT 0,
        credit_used      REAL    NOT NULL DEFAULT 0,
    cash_applied     REAL    NOT NULL DEFAULT 0,
    round_off        REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scrap_rates (
    name         TEXT PRIMARY KEY,
    price_per_kg REAL NOT NULL CHECK (price_per_kg >= 0)
);

CREATE TABLE IF NOT EXISTS sale_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id       INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    product_id    INTEGER REFERENCES products(id) ON DELETE SET NULL,
    product_name  TEXT    NOT NULL,
    weight_kg     REAL    NOT NULL,
    price_per_kg  REAL    NOT NULL,
    line_total    REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    amount       REAL    NOT NULL CHECK (amount > 0),
    note         TEXT    DEFAULT '',
    created_at   TEXT    NOT NULL
);

-- Scrap handed over against an existing due, with no new bill. Rates are
-- snapshotted here so an old entry keeps the price it was valued at.
CREATE TABLE IF NOT EXISTS scrap_payments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    black_kg     REAL    NOT NULL DEFAULT 0 CHECK (black_kg >= 0),
    white_kg     REAL    NOT NULL DEFAULT 0 CHECK (white_kg >= 0),
    black_rate   REAL    NOT NULL DEFAULT 0,
    white_rate   REAL    NOT NULL DEFAULT 0,
    total        REAL    NOT NULL CHECK (total >= 0),
    note         TEXT    DEFAULT '',
    created_at   TEXT    NOT NULL
);

-- Unfinished receipts parked while the shop serves the next customer. A draft
-- is not a sale: nothing here reaches customer due, the ledger or the Daily
-- Sales figures until it is finalized with Save & Print.
CREATE TABLE IF NOT EXISTS draft_receipts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    label            TEXT    NOT NULL DEFAULT '',
    customer_id      INTEGER REFERENCES customers(id) ON DELETE SET NULL,
    customer_name    TEXT    NOT NULL DEFAULT '',
    customer_phone   TEXT    NOT NULL DEFAULT '',
    customer_shop    TEXT    NOT NULL DEFAULT '',
    customer_address TEXT    NOT NULL DEFAULT '',
    items_json       TEXT    NOT NULL DEFAULT '[]',
    paid             REAL    NOT NULL DEFAULT 0,
    waive            INTEGER NOT NULL DEFAULT 0,
    waive_amount     REAL    NOT NULL DEFAULT 0,
    scrap_black_kg   REAL    NOT NULL DEFAULT 0,
    scrap_white_kg   REAL    NOT NULL DEFAULT 0,
    total            REAL    NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sales_customer ON sales(customer_id);
CREATE INDEX IF NOT EXISTS idx_items_sale ON sale_items(sale_id);
CREATE INDEX IF NOT EXISTS idx_payments_customer ON payments(customer_id);
CREATE INDEX IF NOT EXISTS idx_scrap_payments_customer
    ON scrap_payments(customer_id);
CREATE INDEX IF NOT EXISTS idx_drafts_updated ON draft_receipts(updated_at);
"""

# Columns added to databases created before scrap support existed.
SALE_SCRAP_COLUMNS = (
    ("scrap_black_kg", "REAL NOT NULL DEFAULT 0"),
    ("scrap_white_kg", "REAL NOT NULL DEFAULT 0"),
    ("scrap_black_rate", "REAL NOT NULL DEFAULT 0"),
    ("scrap_white_rate", "REAL NOT NULL DEFAULT 0"),
    ("scrap_total", "REAL NOT NULL DEFAULT 0"),
)

# Columns added when scrap/overpayment credit support arrived. `cash_applied`
# is the part of the cash tender that actually went against the bill; the rest
# was change handed back.
SALE_CREDIT_COLUMNS = (
    ("credit_used", "REAL NOT NULL DEFAULT 0"),
    ("cash_applied", "REAL NOT NULL DEFAULT 0"),
)

# A small leftover the shop deliberately waives/rounds off. It settles that
# part of the bill instead of being carried as customer due.
SALE_ROUND_OFF_COLUMNS = (
    ("round_off", "REAL NOT NULL DEFAULT 0"),
)

SEED_PRODUCTS = [
    # Sheets & Coils
    ("Aluminum Sheet 0.5 mm", "Sheets & Coils", 410.00),
    ("Aluminum Sheet 0.8 mm", "Sheets & Coils", 400.00),
    ("Aluminum Sheet 1.0 mm", "Sheets & Coils", 395.00),
    ("Aluminum Sheet 1.5 mm", "Sheets & Coils", 390.00),
    ("Aluminum Sheet 2.0 mm", "Sheets & Coils", 385.00),
    ("Aluminum Coil 0.5 mm", "Sheets & Coils", 405.00),
    ("Aluminum Coil 1.0 mm", "Sheets & Coils", 392.00),
    # Profiles & Sections
    ("Aluminum Sliding Window Profile", "Profiles & Sections", 430.00),
    ("Aluminum Window Section", "Profiles & Sections", 425.00),
    ("Aluminum Door Section", "Profiles & Sections", 440.00),
    ("Aluminum Partition Section", "Profiles & Sections", 415.00),
    ("Aluminum Handle Section", "Profiles & Sections", 465.00),
    # Pipes & Tubes
    ('Aluminum Round Pipe 1"', "Pipes & Tubes", 420.00),
    ('Aluminum Round Pipe 1.5"', "Pipes & Tubes", 415.00),
    ('Aluminum Round Pipe 2"', "Pipes & Tubes", 410.00),
    ('Aluminum Round Pipe 3"', "Pipes & Tubes", 405.00),
    ('Aluminum Square Pipe 1"', "Pipes & Tubes", 425.00),
    ('Aluminum Square Pipe 1.5"', "Pipes & Tubes", 420.00),
    ('Aluminum Square Pipe 2"', "Pipes & Tubes", 415.00),
    ("Aluminum Rectangular Tube 1x2", "Pipes & Tubes", 418.00),
    # Rods & Bars
    ("Aluminum Rod 10 mm", "Rods & Bars", 450.00),
    ("Aluminum Rod 12 mm", "Rods & Bars", 448.00),
    ("Aluminum Rod 16 mm", "Rods & Bars", 445.00),
    ("Aluminum Rod 20 mm", "Rods & Bars", 442.00),
    # Angles & Channels
    ('Aluminum Angle 1"', "Angles & Channels", 435.00),
    ('Aluminum Angle 1.5"', "Angles & Channels", 430.00),
    ('Aluminum Channel 1"', "Angles & Channels", 438.00),
    ('Aluminum Channel 1.5"', "Angles & Channels", 434.00),
    # Foil & Roofing
    ("Aluminum Foil Roll (Household)", "Foil & Roofing", 520.00),
    ("Aluminum Roofing Sheet (Colour Coated)", "Foil & Roofing", 398.00),
]


def connect_db(path=None):
    """A connection set up so a power cut cannot corrupt the shop's data.

    WAL writes changes to a separate log and checkpoints it into the main file,
    so an interrupted commit leaves the database at its last good state instead
    of a half-written page. synchronous=FULL makes each commit wait for the disk
    to confirm the write before the sale is reported as saved.
    """
    con = sqlite3.connect(path or DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = FULL")
    con.execute("PRAGMA busy_timeout = 5000")
    return con


def get_db():
    if "db" not in g:
        os.makedirs(DATA_DIR, exist_ok=True)
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = connect_db()
    con.executescript(SCHEMA)
    if con.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
        con.executemany(
            "INSERT INTO products (name, category, price_per_kg) VALUES (?, ?, ?)",
            SEED_PRODUCTS,
        )

    # Upgrade a database created before scrap/credit support without losing data.
    existing = {row[1] for row in con.execute("PRAGMA table_info(sales)")}
    added = set()
    for name, declaration in (
        SALE_SCRAP_COLUMNS + SALE_CREDIT_COLUMNS + SALE_ROUND_OFF_COLUMNS
    ):
        if name not in existing:
            con.execute(f"ALTER TABLE sales ADD COLUMN {name} {declaration}")
            added.add(name)
    if "cash_applied" in added:
        # Older sales counted the whole cash tender; only the part that was
        # applied (never the change handed back) belongs in the balance.
        con.execute(
            "UPDATE sales SET cash_applied ="
            " MAX(0, MIN(paid, grand_total - scrap_total))"
        )

    con.executemany(
        "INSERT OR IGNORE INTO scrap_rates (name, price_per_kg) VALUES (?, ?)",
        DEFAULT_SCRAP_RATES,
    )
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Backups: timestamped copies of the whole database, made with SQLite's own
# .backup() so a copy is always a complete, consistent file - never a half
# written one, even while the shop keeps billing.
# ---------------------------------------------------------------------------
BACKUP_LOCK = threading.Lock()


def backup_files():
    """Existing backup files, newest first."""
    try:
        names = [
            name
            for name in os.listdir(BACKUP_DIR)
            if name.startswith(BACKUP_PREFIX) and name.endswith(".db")
        ]
    except OSError:
        return []
    # Newest first, by modification time (so two copies made in the same minute
    # still sort in the order they were written).
    entries = []
    for name in names:
        path = os.path.join(BACKUP_DIR, name)
        try:
            entries.append((os.path.getmtime(path), name, path))
        except OSError:
            continue
    entries.sort(reverse=True)
    return [path for _mtime, _name, path in entries]


def newest_backup():
    files = backup_files()
    return files[0] if files else None


def backup_is_due():
    """True when there is no backup yet or the newest one is over an hour old."""
    newest = newest_backup()
    if newest is None:
        return True
    try:
        return time.time() - os.path.getmtime(newest) >= BACKUP_EVERY_SECONDS
    except OSError:
        return True


def prune_backups(keep=BACKUP_KEEP):
    """Delete all but the newest `keep` snapshots so the disk never fills up."""
    for path in backup_files()[keep:]:
        try:
            os.remove(path)
        except OSError as exc:
            print(f"Could not remove old backup {path}: {exc}")


def create_backup(force=False):
    """Write backups/shop_YYYY-MM-DD_HH-MM.db and return its path.

    Returns None when nothing was written: the database does not exist yet, a
    backup newer than an hour already exists (unless `force`), or the copy
    failed. A failed backup never interrupts billing.
    """
    if not os.path.exists(DB_PATH):
        return None
    with BACKUP_LOCK:
        if not force and not backup_is_due():
            return None
        target = None
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            stamp = datetime.now().strftime(BACKUP_STAMP)
            target = os.path.join(BACKUP_DIR, f"{BACKUP_PREFIX}{stamp}.db")
            # Never overwrite: a second copy in the same minute gets a suffix.
            counter = 2
            while os.path.exists(target):
                target = os.path.join(
                    BACKUP_DIR, f"{BACKUP_PREFIX}{stamp}-{counter}.db"
                )
                counter += 1

            source = connect_db()
            try:
                dest = sqlite3.connect(target)
                try:
                    # SQLite's backup API copies page by page inside a read
                    # transaction, so the copy is a consistent snapshot.
                    source.backup(dest)
                finally:
                    dest.close()
            finally:
                source.close()
        except (sqlite3.Error, OSError) as exc:
            print(f"Backup failed: {exc}")
            if target and os.path.exists(target):
                try:
                    os.remove(target)
                except OSError:
                    pass
            return None

        prune_backups()
        return target


def start_backup_thread():
    """Keep taking hourly snapshots for as long as the shop is open."""

    def worker():
        while True:
            time.sleep(60)
            try:
                create_backup()
            except Exception as exc:  # never let a backup kill the thread
                print(f"Backup error: {exc}")

    threading.Thread(target=worker, name="hourly-backup", daemon=True).start()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def money(value):
    """Format a number as 1,234.50 (currency is printed separately)."""
    try:
        return f"{float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "0.00"


app.jinja_env.filters["money"] = money
app.jinja_env.globals["CURRENCY"] = CURRENCY
app.jinja_env.globals["SHOP"] = SHOP


# Single definition of the customer account. Every screen, API call and new
# bill builds on this, so a new kind of payment only has to be added in one
# place and no query can forget it.
#
# net = billed - cash kept at the till - scrap received - rounded-off leftovers
#            - later cash payments - later scrap receipts
#
# Positive net is money owed; negative net is credit on the account. Cash paid
# beyond a bill is change handed back, so `cash_applied` (not the tender) is
# what counts. Scrap beyond a bill is never handed back, so it stays as credit
# and comes off the next bill automatically. A waived remainder is written off
# on that bill, so it never shows up as due here.
NET_BALANCE_SQL = (
    "(COALESCE((SELECT SUM(subtotal) FROM sales WHERE customer_id = {c}), 0)"
    " - COALESCE((SELECT SUM(cash_applied) FROM sales WHERE customer_id = {c}), 0)"
    " - COALESCE((SELECT SUM(scrap_total) FROM sales WHERE customer_id = {c}), 0)"
    " - COALESCE((SELECT SUM(round_off) FROM sales WHERE customer_id = {c}), 0)"
    " - COALESCE((SELECT SUM(amount) FROM payments WHERE customer_id = {c}), 0)"
    " - COALESCE((SELECT SUM(total) FROM scrap_payments"
    "             WHERE customer_id = {c}), 0))"
)


def account_sql(customer_ref):
    """The signed net-balance expression for an id column or parameter."""
    return NET_BALANCE_SQL.format(c=customer_ref)


def customer_account(db, customer_id):
    """Signed account: net (owed if positive), due and credit (never both)."""
    value = db.execute(
        f"SELECT {account_sql(':customer_id')} AS net",
        {"customer_id": customer_id},
    ).fetchone()["net"]
    net = round(value or 0.0, 2)
    return {
        "net": net,
        "due": round(max(0.0, net), 2),
        "credit": round(max(0.0, -net), 2),
    }


def customer_balance(db, customer_id):
    """Amount the customer owes right now (0 when they have credit)."""
    return customer_account(db, customer_id)["due"]


def scrap_rates(db):
    """Current scrap prices per kg, keyed by type name (Black, White)."""
    rows = db.execute(
        "SELECT name, price_per_kg FROM scrap_rates ORDER BY rowid"
    ).fetchall()
    return {row["name"]: row["price_per_kg"] for row in rows}


def load_customer(db, customer_id):
    return db.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()


def note(text, kind="ok"):
    flash(text, kind)


@app.context_processor
def inject_now():
    return {"today": datetime.now().strftime("%d %b %Y")}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.route("/")
def billing():
    """Tab 2: New receipt / billing."""
    return render_template("billing.html", active="billing")


@app.route("/products")
def products():
    """Tab 1: product list with quick price updates."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM products ORDER BY category, name"
    ).fetchall()
    return render_template(
        "products.html",
        active="products",
        products=rows,
        scrap_rates=scrap_rates(db),
        scrap_types=SCRAP_TYPES,
    )


def customers_with_balances(db):
    """Every customer with their due/credit, biggest due first."""
    return db.execute(
        "SELECT t.*, MAX(0.0, t.net) AS balance, MAX(0.0, -t.net) AS credit"
        " FROM ("
        f"SELECT c.*, {account_sql('c.id')} AS net FROM customers c"
        ") AS t ORDER BY balance DESC, t.name COLLATE NOCASE"
    ).fetchall()


@app.route("/customers")
def customers():
    """Tab 3: all customers with due balance."""
    db = get_db()
    rows = customers_with_balances(db)
    total_due = round(sum(r["balance"] for r in rows), 2)
    return render_template(
        "customers.html",
        active="customers",
        customers=rows,
        total_due=total_due,
    )


@app.route("/daily")
def daily_summary():
    """Tab 4: what was sold on one day, ranked by weight or by revenue."""
    db = get_db()
    now = datetime.now()
    today_iso = now.strftime("%Y-%m-%d")

    # Any past day can be reviewed; a missing or malformed date falls back to
    # today instead of showing an error page.
    requested = (request.args.get("date") or "").strip()
    day = now.date()
    if requested:
        try:
            day = datetime.strptime(requested, "%Y-%m-%d").date()
        except ValueError:
            day = now.date()
    day_iso = day.strftime("%Y-%m-%d")
    sort = "revenue" if request.args.get("sort") == "revenue" else "weight"

    totals = db.execute(
        "SELECT COUNT(*) AS receipts,"
        " COALESCE(SUM(subtotal), 0) AS sales,"
        " COALESCE(SUM(cash_applied), 0) AS cash,"
        " COALESCE(SUM(new_due), 0) AS due,"
        " COALESCE(SUM(scrap_total), 0) AS scrap"
        " FROM sales WHERE date(created_at) = ?",
        (day_iso,),
    ).fetchone()

    # One row per product name (the name is snapshotted on each line, so a
    # deleted product still shows up in its own past sales). `sort` is chosen
    # from a fixed pair above, never taken from the URL as raw SQL.
    order_column = "revenue" if sort == "revenue" else "weight"
    products = db.execute(
        "SELECT si.product_name,"
        " SUM(si.weight_kg) AS weight,"
        " SUM(si.line_total) AS revenue,"
        " COUNT(DISTINCT si.sale_id) AS orders"
        " FROM sale_items si JOIN sales s ON s.id = si.sale_id"
        " WHERE date(s.created_at) = ?"
        " GROUP BY si.product_name"
        f" ORDER BY {order_column} DESC, si.product_name COLLATE NOCASE",
        (day_iso,),
    ).fetchall()

    total_weight = round(sum(p["weight"] for p in products), 3)
    avg_bill = round(totals["sales"] / totals["receipts"], 2) if totals["receipts"] else 0.0
    return render_template(
        "daily.html",
        active="daily",
        day_iso=day_iso,
        day_label=day.strftime("%A, %d %b %Y"),
        today_iso=today_iso,
        sort=sort,
        totals=totals,
        products=products,
        total_weight=total_weight,
        avg_bill=avg_bill,
        printed_at=now.strftime("%d %b %Y, %I:%M %p"),
    )


@app.route("/customers/<int:customer_id>")
def customer_detail(customer_id):
    """Customer history + record-a-payment form."""
    db = get_db()
    customer = load_customer(db, customer_id)
    if customer is None:
        abort(404)

    sales = db.execute(
        "SELECT * FROM sales WHERE customer_id = ? ORDER BY id DESC",
        (customer_id,),
    ).fetchall()

    sale_ids = [s["id"] for s in sales]
    items_by_sale = {sid: [] for sid in sale_ids}
    if sale_ids:
        placeholders = ",".join("?" * len(sale_ids))
        for item in db.execute(
            f"SELECT * FROM sale_items WHERE sale_id IN ({placeholders}) ORDER BY id",
            sale_ids,
        ):
            items_by_sale[item["sale_id"]].append(item)

    payments = db.execute(
        "SELECT * FROM payments WHERE customer_id = ? ORDER BY id DESC",
        (customer_id,),
    ).fetchall()

    scrap_entries = db.execute(
        "SELECT * FROM scrap_payments WHERE customer_id = ? ORDER BY id DESC",
        (customer_id,),
    ).fetchall()

    account = customer_account(db, customer_id)
    return render_template(
        "customer_detail.html",
        active="customers",
        customer=customer,
        sales=sales,
        items_by_sale=items_by_sale,
        payments=payments,
        scrap_entries=scrap_entries,
        scrap_rates=scrap_rates(db),
        balance=account["due"],
        credit=account["credit"],
        total_billed=sum(s["subtotal"] for s in sales),
        total_paid=sum(s["cash_applied"] for s in sales)
        + sum(s["scrap_total"] for s in sales)
        + sum(s["round_off"] for s in sales)
        + sum(p["amount"] for p in payments)
        + sum(e["total"] for e in scrap_entries),
    )


@app.get("/backup/download")
def download_backup():
    """One-click copy of the whole database, downloaded to this PC."""
    if not os.path.exists(DB_PATH):
        note("There is no database to back up yet.", "error")
        return redirect(url_for("billing"))
    path = create_backup(force=True)
    if not path:
        note("Could not create the backup file. Check the console for details.", "error")
        return redirect(url_for("billing"))
    return send_file(
        path,
        as_attachment=True,
        download_name=os.path.basename(path),
        mimetype="application/x-sqlite3",
    )


@app.get("/export/customers.csv")
def export_customers_csv():
    """Customer due list as a spreadsheet (opens in Excel / LibreOffice)."""
    db = get_db()
    rows = customers_with_balances(db)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        ["Customer ID", "Name", "Shop", "Phone", "Address", "Due", "Credit"]
    )
    for row in rows:
        writer.writerow(
            [
                row["id"],
                row["name"],
                row["shop_name"] or "",
                row["phone"] or "",
                row["address"] or "",
                f"{row['balance']:.2f}",
                f"{row['credit']:.2f}",
            ]
        )
    filename = (
        "customers_dues_" + datetime.now().strftime(BACKUP_STAMP) + ".csv"
    )
    # The BOM makes Excel read the file as UTF-8 instead of mangling names.
    return Response(
        "\ufeff" + out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/receipt/<int:sale_id>")
def receipt(sale_id):
    db = get_db()
    sale = db.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()
    if sale is None:
        abort(404)
    customer = load_customer(db, sale["customer_id"])
    items = db.execute(
        "SELECT * FROM sale_items WHERE sale_id = ? ORDER BY id", (sale_id,)
    ).fetchall()
    return render_template(
        "receipt.html",
        sale=sale,
        customer=customer,
        items=items,
        autoprint=request.args.get("print") == "1",
        invoice_no=f"INV-{sale_id:05d}",
    )


# ---------------------------------------------------------------------------
# Product actions
# ---------------------------------------------------------------------------
@app.post("/products/add")
def add_product():
    name = (request.form.get("name") or "").strip()
    category = (request.form.get("category") or "").strip() or "Uncategorised"
    try:
        price = round(float(request.form.get("price_per_kg") or 0), 2)
    except ValueError:
        price = -1

    if not name or price < 0:
        note("Enter a product name and a price of 0 or more.", "error")
        return redirect(url_for("products"))

    db = get_db()
    try:
        db.execute(
            "INSERT INTO products (name, category, price_per_kg) VALUES (?, ?, ?)",
            (name, category, price),
        )
        db.commit()
        note(f'Product "{name}" added.')
    except sqlite3.IntegrityError:
        note(f'A product named "{name}" already exists.', "error")
    return redirect(url_for("products"))


@app.post("/products/<int:product_id>/price")
def update_price(product_id):
    db = get_db()
    product = db.execute(
        "SELECT * FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    if product is None:
        abort(404)
    try:
        price = round(float(request.form.get("price_per_kg") or 0), 2)
    except ValueError:
        price = -1
    if price < 0:
        note("Price must be 0 or more.", "error")
        return redirect(url_for("products"))

    db.execute("UPDATE products SET price_per_kg = ? WHERE id = ?", (price, product_id))
    db.commit()
    note(f'Price for "{product["name"]}" updated to {CURRENCY} {money(price)} / kg.')
    return redirect(url_for("products"))


@app.post("/scrap-rates")
def update_scrap_rates():
    """Save the per-kg buying price for each scrap type."""
    db = get_db()
    updates = []
    for name in scrap_rates(db):
        field = f"rate_{name.lower()}"
        if field not in request.form:
            continue
        try:
            price = round(float(request.form.get(field) or 0), 2)
        except ValueError:
            price = -1
        if price < 0:
            note(f'Price for {name} scrap must be 0 or more.', "error")
            return redirect(url_for("products"))
        updates.append((price, name))

    if updates:
        db.executemany(
            "UPDATE scrap_rates SET price_per_kg = ? WHERE name = ?", updates
        )
        db.commit()
        note(
            "Scrap rates updated: "
            + ", ".join(
                f"{name} {CURRENCY} {money(price)}/kg" for price, name in updates
            )
            + "."
        )
    return redirect(url_for("products"))


@app.post("/products/<int:product_id>/delete")
def delete_product(product_id):
    db = get_db()
    product = db.execute(
        "SELECT * FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    if product is None:
        abort(404)
    db.execute("DELETE FROM products WHERE id = ?", (product_id,))
    db.commit()
    note(f'Product "{product["name"]}" removed. Past invoices keep their records.')
    return redirect(url_for("products"))


# ---------------------------------------------------------------------------
# Customer actions
# ---------------------------------------------------------------------------
@app.post("/customers/add")
def add_customer_form():
    name = (request.form.get("name") or "").strip()
    if not name:
        note("Customer name is required.", "error")
        return redirect(url_for("customers"))
    db = get_db()
    cur = db.execute(
        "INSERT INTO customers (name, phone, shop_name, address, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            name,
            (request.form.get("phone") or "").strip(),
            (request.form.get("shop_name") or "").strip(),
            (request.form.get("address") or "").strip(),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    db.commit()
    note(f'Customer "{name}" added.')
    return redirect(url_for("customer_detail", customer_id=cur.lastrowid))


@app.post("/customers/<int:customer_id>/payments")
def record_payment(customer_id):
    db = get_db()
    customer = load_customer(db, customer_id)
    if customer is None:
        abort(404)
    try:
        amount = round(float(request.form.get("amount") or 0), 2)
    except ValueError:
        amount = 0

    if amount <= 0:
        note("Enter a payment amount greater than 0.", "error")
        return redirect(url_for("customer_detail", customer_id=customer_id))

    due_before = customer_balance(db, customer_id)
    if due_before <= 0:
        note(
            f"{customer['name']} has no outstanding due to receive a payment"
            " against. Cash handed over is not carried as credit.",
            "error",
        )
        return redirect(url_for("customer_detail", customer_id=customer_id))

    # Cash is settled on the spot: anything above the due is change, never
    # credit, so only the part that clears the due is recorded.
    recorded = round(min(amount, due_before), 2)
    change = round(amount - recorded, 2)

    db.execute(
        "INSERT INTO payments (customer_id, amount, note, created_at)"
        " VALUES (?, ?, ?, ?)",
        (
            customer_id,
            recorded,
            (request.form.get("note") or "").strip(),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    db.commit()
    message = (
        f"Payment of {CURRENCY} {money(recorded)} recorded for "
        f"{customer['name']}. Remaining due: "
        f"{CURRENCY} {money(customer_balance(db, customer_id))}."
    )
    if change > 0:
        message += f" {CURRENCY} {money(change)} was change handed back."
    note(message)
    # A payment changes due balances: snapshot it if the hour is up.
    create_backup()
    return redirect(url_for("customer_detail", customer_id=customer_id))


@app.post("/customers/<int:customer_id>/scrap")
def record_scrap_payment(customer_id):
    """Customer settles an existing due with scrap, with no new bill."""
    db = get_db()
    customer = load_customer(db, customer_id)
    if customer is None:
        abort(404)

    rates = scrap_rates(db)
    kg = {}
    for name in SCRAP_TYPES:
        try:
            weight = round(float(request.form.get(f"{name.lower()}_kg") or 0), 3)
        except ValueError:
            weight = -1
        if weight < 0:
            note(f"{name} scrap weight must be 0 or more.", "error")
            return redirect(url_for("customer_detail", customer_id=customer_id))
        kg[name] = weight

    if not any(kg.values()):
        note("Enter the weight of at least one scrap type.", "error")
        return redirect(url_for("customer_detail", customer_id=customer_id))

    total = round(sum(kg[name] * rates.get(name, 0) for name in SCRAP_TYPES), 2)

    db.execute(
        "INSERT INTO scrap_payments (customer_id, black_kg, white_kg,"
        " black_rate, white_rate, total, note, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            customer_id,
            kg["Black"],
            kg["White"],
            round(rates.get("Black", 0), 2),
            round(rates.get("White", 0), 2),
            total,
            (request.form.get("note") or "").strip(),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    db.commit()

    parts = [f"{name} {kg[name]:.3f} kg" for name in SCRAP_TYPES if kg[name] > 0]
    account = customer_account(db, customer_id)
    message = (
        f"Scrap received from {customer['name']} ({', '.join(parts)}) worth "
        f"{CURRENCY} {money(total)}. "
    )
    if account["credit"] > 0:
        message += (
            f"Credit on account: {CURRENCY} {money(account['credit'])} "
            "(comes off the next bill)."
        )
    else:
        message += f"Remaining due: {CURRENCY} {money(account['due'])}."
    note(message)
    create_backup()
    return redirect(url_for("customer_detail", customer_id=customer_id))


# ---------------------------------------------------------------------------
# JSON API used by the billing screen
# ---------------------------------------------------------------------------
@app.get("/api/products")
def api_products():
    db = get_db()
    rows = db.execute(
        "SELECT id, name, category, price_per_kg FROM products"
        " ORDER BY category, name COLLATE NOCASE"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/scrap-rates")
def api_scrap_rates():
    db = get_db()
    rates = scrap_rates(db)
    return jsonify(
        [{"name": name, "price_per_kg": rates[name]} for name in SCRAP_TYPES]
    )


@app.get("/api/customers")
def api_customers():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    like = f"%{q}%"
    rows = db.execute(
        "SELECT t.*, MAX(0.0, t.net) AS balance, MAX(0.0, -t.net) AS credit"
        " FROM ("
        "SELECT c.id, c.name, c.phone, c.shop_name, c.address,"
        f" {account_sql('c.id')} AS net"
        " FROM customers c"
        " WHERE c.name LIKE ? OR c.phone LIKE ? OR c.shop_name LIKE ?"
        "    OR c.address LIKE ?"
        ") AS t ORDER BY t.name COLLATE NOCASE LIMIT 20",
        (like, like, like, like),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/customers/<int:customer_id>")
def api_customer(customer_id):
    db = get_db()
    customer = load_customer(db, customer_id)
    if customer is None:
        return jsonify({"error": "not found"}), 404
    data = dict(customer)
    account = customer_account(db, customer_id)
    data["balance"] = account["due"]
    data["credit"] = account["credit"]
    return jsonify(data)


@app.post("/api/customers")
def api_customer_create():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Customer name is required."}), 400

    db = get_db()
    cur = db.execute(
        "INSERT INTO customers (name, phone, shop_name, address, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            name,
            (payload.get("phone") or "").strip(),
            (payload.get("shop_name") or "").strip(),
            (payload.get("address") or "").strip(),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    db.commit()
    data = dict(load_customer(db, cur.lastrowid))
    data["balance"] = 0.0
    data["credit"] = 0.0
    return jsonify(data), 201


# ---------------------------------------------------------------------------
# Held receipts (drafts). An unfinished bill is parked in SQLite so it survives
# a refresh, a closed tab or an app restart. Drafts are read by the billing
# screen only: they never touch customer due, the ledger or Daily Sales.
# ---------------------------------------------------------------------------
DRAFT_LABEL_MAX = 60


def draft_line_items(db, raw_items):
    """Lines for a draft, always priced from the database, never the browser."""
    items = []
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            continue
        try:
            product_id = int(raw.get("product_id"))
            weight = round(float(raw.get("weight_kg")), 3)
        except (TypeError, ValueError):
            continue
        if weight <= 0:
            continue
        product = db.execute(
            "SELECT id, name, price_per_kg FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
        if product is None:
            continue
        items.append(
            {
                "product_id": product["id"],
                "product_name": product["name"],
                "weight_kg": weight,
                "price_per_kg": product["price_per_kg"],
                "line_total": round(weight * product["price_per_kg"], 2),
            }
        )
    return items


def draft_items(row):
    try:
        items = json.loads(row["items_json"] or "[]")
    except (TypeError, ValueError):
        items = []
    return items if isinstance(items, list) else []


def draft_summary(row):
    return {
        "id": row["id"],
        "label": row["label"],
        "customer_id": row["customer_id"],
        "customer_name": row["customer_name"],
        "customer_shop": row["customer_shop"],
        "item_count": len(draft_items(row)),
        "total": row["total"],
        "paid": row["paid"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def draft_detail(row):
    data = draft_summary(row)
    data.update(
        {
            "customer_phone": row["customer_phone"],
            "customer_address": row["customer_address"],
            "items": draft_items(row),
            "waive": bool(row["waive"]),
            "waive_amount": row["waive_amount"],
            "scrap_black_kg": row["scrap_black_kg"],
            "scrap_white_kg": row["scrap_white_kg"],
        }
    )
    return data


@app.get("/api/drafts")
def api_drafts():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM draft_receipts ORDER BY updated_at DESC, id DESC"
    ).fetchall()
    return jsonify([draft_summary(row) for row in rows])


@app.get("/api/drafts/<int:draft_id>")
def api_draft(draft_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM draft_receipts WHERE id = ?", (draft_id,)
    ).fetchone()
    if row is None:
        return jsonify({"error": "That held receipt no longer exists."}), 404
    return jsonify(draft_detail(row))


@app.post("/api/drafts")
def api_draft_save():
    payload = request.get_json(silent=True) or {}
    db = get_db()

    customer_id = payload.get("customer_id")
    try:
        customer_id = int(customer_id) if customer_id else None
    except (TypeError, ValueError):
        customer_id = None
    if customer_id and load_customer(db, customer_id) is None:
        customer_id = None

    items = draft_line_items(db, payload.get("items"))
    if not items:
        return jsonify(
            {"error": "Add at least one product line before holding the receipt."}
        ), 400

    info = payload.get("customer") or {}
    if not isinstance(info, dict):
        info = {}

    def text_field(key, limit=120):
        return str(info.get(key) or "").strip()[:limit]

    label = str(payload.get("label") or "").strip()[:DRAFT_LABEL_MAX]
    if not label:
        label = (text_field("name") or "Held receipt")[:DRAFT_LABEL_MAX]

    def amount(value):
        try:
            return max(0.0, round(float(value or 0), 2))
        except (TypeError, ValueError):
            return 0.0

    def kilos(value):
        try:
            return max(0.0, round(float(value or 0), 3))
        except (TypeError, ValueError):
            return 0.0

    scrap = payload.get("scrap") or {}
    if not isinstance(scrap, dict):
        scrap = {}

    values = (
        label,
        customer_id,
        text_field("name"),
        text_field("phone"),
        text_field("shop_name"),
        text_field("address", 200),
        json.dumps(items),
        amount(payload.get("paid")),
        1 if payload.get("waive") else 0,
        amount(payload.get("waive_amount")),
        kilos(scrap.get("black_kg")),
        kilos(scrap.get("white_kg")),
        round(sum(item["line_total"] for item in items), 2),
    )
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    draft_id = payload.get("id")
    try:
        draft_id = int(draft_id) if draft_id else None
    except (TypeError, ValueError):
        draft_id = None
    existing = None
    if draft_id:
        existing = db.execute(
            "SELECT id FROM draft_receipts WHERE id = ?", (draft_id,)
        ).fetchone()

    if existing:
        db.execute(
            "UPDATE draft_receipts SET label = ?, customer_id = ?,"
            " customer_name = ?, customer_phone = ?, customer_shop = ?,"
            " customer_address = ?, items_json = ?, paid = ?, waive = ?,"
            " waive_amount = ?, scrap_black_kg = ?, scrap_white_kg = ?,"
            " total = ?, updated_at = ? WHERE id = ?",
            values + (now, draft_id),
        )
    else:
        cur = db.execute(
            "INSERT INTO draft_receipts (label, customer_id, customer_name,"
            " customer_phone, customer_shop, customer_address, items_json,"
            " paid, waive, waive_amount, scrap_black_kg, scrap_white_kg, total,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values + (now, now),
        )
        draft_id = cur.lastrowid
    db.commit()

    row = db.execute(
        "SELECT * FROM draft_receipts WHERE id = ?", (draft_id,)
    ).fetchone()
    return jsonify(draft_detail(row)), 201


@app.delete("/api/drafts/<int:draft_id>")
def api_draft_delete(draft_id):
    db = get_db()
    cur = db.execute("DELETE FROM draft_receipts WHERE id = ?", (draft_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "That held receipt no longer exists."}), 404
    return jsonify({"deleted": draft_id})


@app.post("/api/sales")
def api_create_sale():
    payload = request.get_json(silent=True) or {}
    customer_id = payload.get("customer_id")
    raw_items = payload.get("items") or []

    # A receipt resumed from a held draft carries its draft id, so finalizing
    # it can drop the draft in the same transaction.
    try:
        draft_id = int(payload.get("draft_id") or 0)
    except (TypeError, ValueError):
        draft_id = 0

    try:
        paid = round(float(payload.get("paid") or 0), 2)
    except (TypeError, ValueError):
        return jsonify({"error": "Amount paid must be a number."}), 400
    try:
        requested_round_off = round(float(payload.get("round_off") or 0), 2)
    except (TypeError, ValueError):
        return jsonify({"error": "Round-off amount must be a number."}), 400
    if requested_round_off < 0:
        return jsonify({"error": "Round-off amount cannot be negative."}), 400

    if not customer_id:
        return jsonify({"error": "Select or create a customer first."}), 400
    if paid < 0:
        return jsonify({"error": "Amount paid cannot be negative."}), 400
    if not raw_items:
        return jsonify({"error": "Add at least one product line."}), 400

    db = get_db()
    customer = load_customer(db, customer_id)
    if customer is None:
        return jsonify({"error": "Customer not found."}), 400

    # Scrap is valued at the rates stored in the database, never at a price
    # the browser sends, and it counts as payment towards the bill.
    rates = scrap_rates(db)
    raw_scrap = payload.get("scrap") or {}
    scrap = {}
    for name in SCRAP_TYPES:
        field = f"{name.lower()}_kg"
        try:
            kg = round(float(raw_scrap.get(field) or 0), 3)
        except (TypeError, ValueError):
            return jsonify({"error": f"{name} scrap weight must be a number."}), 400
        if kg < 0:
            return jsonify({"error": f"{name} scrap weight cannot be negative."}), 400
        rate = round(float(rates.get(name, 0)), 2)
        scrap[name] = {"kg": kg, "rate": rate, "value": round(kg * rate, 2)}
    scrap_total = round(sum(part["value"] for part in scrap.values()), 2)

    # Rebuild the lines from current database prices - never trust client totals.
    lines = []
    for raw in raw_items:
        try:
            product_id = int(raw.get("product_id"))
            weight = round(float(raw.get("weight_kg")), 3)
        except (TypeError, ValueError, AttributeError):
            return jsonify({"error": "Every line needs a product and a weight."}), 400
        if weight <= 0:
            return jsonify({"error": "Weight must be greater than 0 kg."}), 400

        product = db.execute(
            "SELECT * FROM products WHERE id = ?", (product_id,)
        ).fetchone()
        if product is None:
            return jsonify({"error": "A selected product no longer exists."}), 400

        line_total = round(weight * product["price_per_kg"], 2)
        lines.append(
            {
                "product_id": product["id"],
                "product_name": product["name"],
                "weight_kg": weight,
                "price_per_kg": product["price_per_kg"],
                "line_total": line_total,
            }
        )

    subtotal = round(sum(line["line_total"] for line in lines), 2)
    account = customer_account(db, customer_id)
    previous_due = account["due"]
    credit_before = account["credit"]
    gross_total = round(subtotal + previous_due, 2)
    # Credit already on the account comes off this bill before anything is
    # collected, so it is never forgotten.
    credit_used = round(min(credit_before, gross_total), 2)
    grand_total = round(gross_total - credit_used, 2)
    # Scrap covers what the credit did not. Any scrap left over stays on the
    # account as credit for the next bill instead of being written off.
    scrap_applied = round(min(scrap_total, grand_total), 2)
    scrap_credit = round(scrap_total - scrap_applied, 2)
    cash_needed = round(grand_total - scrap_applied, 2)
    # Cash above the amount needed is change handed back, never credit.
    cash_applied = round(min(paid, cash_needed), 2)
    change = round(paid - cash_applied, 2)
    remainder = round(cash_needed - cash_applied, 2)
    # A small leftover the shop chooses to waive is settled on this bill: it is
    # never allowed to exceed the real remainder and never becomes customer due.
    round_off = round(min(requested_round_off, remainder), 2)
    new_due = round(remainder - round_off, 2)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        db.execute("BEGIN IMMEDIATE")
        cur = db.execute(
            "INSERT INTO sales (customer_id, subtotal, previous_due, grand_total,"
            " paid, new_due, created_at, scrap_black_kg, scrap_white_kg,"
            " scrap_black_rate, scrap_white_rate, scrap_total,"
            " credit_used, cash_applied, round_off)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (customer_id, subtotal, previous_due, grand_total, paid, new_due,
             created_at, scrap["Black"]["kg"], scrap["White"]["kg"],
             scrap["Black"]["rate"], scrap["White"]["rate"], scrap_total,
             credit_used, cash_applied, round_off),
        )
        sale_id = cur.lastrowid
        db.executemany(
            "INSERT INTO sale_items (sale_id, product_id, product_name, weight_kg,"
            " price_per_kg, line_total) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    sale_id,
                    line["product_id"],
                    line["product_name"],
                    line["weight_kg"],
                    line["price_per_kg"],
                    line["line_total"],
                )
                for line in lines
            ],
        )
        # A finalized draft stops being a draft in the same transaction.
        if draft_id:
            db.execute("DELETE FROM draft_receipts WHERE id = ?", (draft_id,))
        db.commit()
    except sqlite3.Error as exc:
        db.rollback()
        return jsonify({"error": f"Could not save the sale: {exc}"}), 500

    # Snapshot right after a receipt is saved, at most once an hour.
    create_backup()

    return (
        jsonify(
            {
                "sale_id": sale_id,
                "receipt_url": url_for("receipt", sale_id=sale_id),
                "subtotal": subtotal,
                "previous_due": previous_due,
                "grand_total": grand_total,
                "paid": paid,
                "scrap_total": scrap_total,
                "new_due": new_due,
                "remainder": remainder,
                "round_off": round_off,
                "credit_used": credit_used,
                "cash_applied": cash_applied,
                "scrap_credit": scrap_credit,
                "change": change,
                "credit_after": round(
                    credit_before - credit_used + scrap_credit, 2
                ),
            }
        ),
        201,
    )


@app.errorhandler(404)
def not_found(_error):
    return render_template("404.html", active=""), 404


# ---------------------------------------------------------------------------
# Start-up
# ---------------------------------------------------------------------------
def start_option(variable, default):
    """Read an override from the environment, ignoring junk or empty values."""
    return (os.environ.get(variable) or "").strip() or default


if __name__ == "__main__":
    init_db()
    # One snapshot on start-up (if the last one is over an hour old), then a
    # fresh one every hour while the shop window stays open.
    start_backup = create_backup()
    start_backup_thread()
    port_text = start_option("POS_PORT", start_option("PORT", "5000"))
    try:
        port = int(port_text)
    except ValueError:
        port = 5000
    if not 1 <= port <= 65535:
        port = 5000
    # Loopback only: this shop has no login, so it must not be reachable from
    # the local network. Set POS_HOST=0.0.0.0 to share it deliberately.
    host = start_option("POS_HOST", "127.0.0.1")
    print(f"Database: {DB_PATH}")
    print(f"Backups : {BACKUP_DIR}  (hourly, newest {BACKUP_KEEP} kept)")
    if start_backup:
        print(f"Backup  : {os.path.basename(start_backup)}")
    print(f"Shop POS running at http://{host}:{port}  (Ctrl+C to stop)")
    app.run(host=host, port=port, debug=False)
