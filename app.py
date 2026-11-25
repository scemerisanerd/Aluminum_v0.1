"""
Aluminum Wholesale Shop - POS & Customer Due Ledger
Flask + SQLite. Fully offline: no CDN, no external services.

Run:  python app.py      (then open http://127.0.0.1:5000)
"""

import csv
import io
import json
import math
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

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

# How money arrives. Only Cash is in the drawer; bKash and Bank are counted
# separately so the day close reconciles the till, not the phone.
PAYMENT_METHODS = ("Cash", "bKash", "Bank")

app = Flask(__name__)
app.config["SECRET_KEY"] = "local-pos-offline-key"
# Flask 3 sorts JSON keys through app.json, not the old config key.
app.json.sort_keys = False


@app.before_request
def refuse_cross_site_writes():
    """There is no login, so another website must not POST to the till.

    A browser always sends Origin on a cross-site form or fetch; same-origin
    writes carry this app's own origin and pass. Requests without the header
    (the smoke test, curl) are local tools, not a page in a browser.
    """
    if request.method in ("POST", "PUT", "DELETE"):
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            abort(403)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL UNIQUE,
    category      TEXT    NOT NULL,
    price_per_kg  REAL    NOT NULL CHECK (price_per_kg >= 0),
    -- Moving-average purchase cost, weighted by the kilos bought so far.
    -- cost_kg is the weight behind cost_per_kg, never the stock on hand.
    cost_per_kg   REAL    NOT NULL DEFAULT 0,
    cost_kg       REAL    NOT NULL DEFAULT 0
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
    round_off        REAL    NOT NULL DEFAULT 0,
    -- 1 when this bill is an end-of-day master receipt built from challans.
    -- Its weight was already counted on the day the goods were picked up.
    pickup_billed    INTEGER NOT NULL DEFAULT 0,
    -- How the cash on this bill arrived: Cash, bKash or Bank.
    payment_method   TEXT    NOT NULL DEFAULT 'Cash',
    -- A voided bill is never deleted: its row and lines stay exactly as saved
    -- for the audit trail, but it counts nowhere in due, Daily Sales or totals.
    voided_at        TEXT,
    void_reason      TEXT    NOT NULL DEFAULT ''
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
    method       TEXT    NOT NULL DEFAULT 'Cash',
    note         TEXT    DEFAULT '',
    created_at   TEXT    NOT NULL,
    -- A mistyped payment is voided, never deleted: the row stays for the
    -- audit trail and drops out of the due and the day's cash.
    voided_at    TEXT,
    void_reason  TEXT    NOT NULL DEFAULT ''
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
    -- Cash handed to the customer for the scrap instead of credit on the
    -- account: only `total - payout` pays anything off.
    payout       REAL    NOT NULL DEFAULT 0 CHECK (payout >= 0),
    method       TEXT    NOT NULL DEFAULT 'Cash',
    note         TEXT    DEFAULT '',
    created_at   TEXT    NOT NULL,
    voided_at    TEXT,
    void_reason  TEXT    NOT NULL DEFAULT ''
);

-- A due carried over from the paper ledger. It changes what the customer owes
-- and nothing else: no bill, no items, no weight, so Daily Sales stay clean.
-- A positive amount adds due; a negative one corrects a mistake.
CREATE TABLE IF NOT EXISTS ledger_adjustments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    amount      REAL    NOT NULL,
    note        TEXT    DEFAULT '',
    created_at  TEXT    NOT NULL,
    voided_at   TEXT,
    void_reason TEXT    NOT NULL DEFAULT ''
);

-- Credit the shop owed a customer, handed back in cash/bKash/bank with no new
-- bill. It adds to the net balance, so paying out the whole credit leaves the
-- account at exactly zero.
CREATE TABLE IF NOT EXISTS credit_payouts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    amount      REAL    NOT NULL CHECK (amount > 0),
    method      TEXT    NOT NULL DEFAULT 'Cash',
    note        TEXT    DEFAULT '',
    created_at  TEXT    NOT NULL,
    voided_at   TEXT,
    void_reason TEXT    NOT NULL DEFAULT ''
);

-- Special Tk./kg prices agreed with one customer. No row means the shop rate
-- applies, so the table only ever holds genuine exceptions.
CREATE TABLE IF NOT EXISTS customer_rates (
    customer_id  INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    product_id   INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    price_per_kg REAL    NOT NULL CHECK (price_per_kg >= 0),
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (customer_id, product_id)
);

-- Goods that left the shop on a weight-only delivery slip (challan). Priced
-- nothing, billed nothing: the rows wait here until the end-of-day master
-- receipt turns them into one sale (`sale_id` is then set).
CREATE TABLE IF NOT EXISTS pickups (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    challan_no    INTEGER NOT NULL,
    customer_id   INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    product_id    INTEGER REFERENCES products(id) ON DELETE SET NULL,
    product_name  TEXT    NOT NULL,
    weight_kg     REAL    NOT NULL CHECK (weight_kg > 0),
    default_price REAL    NOT NULL DEFAULT 0,
    picked_on     TEXT    NOT NULL,
    picked_at     TEXT    NOT NULL,
    sale_id       INTEGER REFERENCES sales(id) ON DELETE SET NULL,
    cancelled_at  TEXT
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

-- Goods bought in: supplier, kilos and cost per kg. Each row nudges the
-- product's moving-average cost; it never changes a price or a past bill.
CREATE TABLE IF NOT EXISTS purchases (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier     TEXT    NOT NULL DEFAULT '',
    product_id   INTEGER REFERENCES products(id) ON DELETE SET NULL,
    product_name TEXT    NOT NULL,
    weight_kg    REAL    NOT NULL CHECK (weight_kg > 0),
    cost_per_kg  REAL    NOT NULL CHECK (cost_per_kg >= 0),
    total        REAL    NOT NULL,
    created_at   TEXT    NOT NULL
);

-- One close per day: the drawer figure the day was closed on, so a later
-- edit or void shows up as a difference instead of silently moving the total.
CREATE TABLE IF NOT EXISTS day_closes (
    day           TEXT PRIMARY KEY,
    cash_in       REAL NOT NULL DEFAULT 0,
    payouts       REAL NOT NULL DEFAULT 0,
    expected_cash REAL NOT NULL DEFAULT 0,
    counted_cash  REAL NOT NULL DEFAULT 0,
    due_added     REAL NOT NULL DEFAULT 0,
    -- Kilos the day closed on; -1 means "not recorded" on a row from before
    -- this was stored, and is never compared.
    weight        REAL NOT NULL DEFAULT -1,
    note          TEXT NOT NULL DEFAULT '',
    closed_at     TEXT NOT NULL
);

-- Small key/value store for shop-wide settings (the second backup folder).
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_sales_customer ON sales(customer_id);
CREATE INDEX IF NOT EXISTS idx_purchases_created ON purchases(created_at);
CREATE INDEX IF NOT EXISTS idx_items_sale ON sale_items(sale_id);
CREATE INDEX IF NOT EXISTS idx_payments_customer ON payments(customer_id);
CREATE INDEX IF NOT EXISTS idx_scrap_payments_customer
    ON scrap_payments(customer_id);
CREATE INDEX IF NOT EXISTS idx_ledger_adjustments_customer
    ON ledger_adjustments(customer_id);
CREATE INDEX IF NOT EXISTS idx_credit_payouts_customer
    ON credit_payouts(customer_id);
CREATE INDEX IF NOT EXISTS idx_pickups_customer
    ON pickups(customer_id, picked_on);
CREATE INDEX IF NOT EXISTS idx_pickups_open ON pickups(sale_id);
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

# A bill built from weight-only challans adds no weight of its own to Daily
# Sales; this flags which bills those are.
SALE_PICKUP_COLUMNS = (
    ("pickup_billed", "INTEGER NOT NULL DEFAULT 0"),
)

# A voided receipt keeps its row for the audit trail; these two columns are
# what take it out of the due balance and every sales total.
SALE_VOID_COLUMNS = (
    ("voided_at", "TEXT"),
    ("void_reason", "TEXT NOT NULL DEFAULT ''"),
)

# A pickup voided before billing keeps its row for the audit trail but stops
# counting everywhere: pending bills, master receipts and Daily Sales weight.
PICKUP_CANCEL_COLUMNS = (
    ("cancelled_at", "TEXT"),
)

# How a bill's cash arrived, so Daily Sales can split the drawer by method.
SALE_METHOD_COLUMNS = (
    ("payment_method", "TEXT NOT NULL DEFAULT 'Cash'"),
)

# A close remembers the day's kilos, so cancelling a challan afterwards shows
# up as a change instead of quietly rewriting a day that was already counted.
DAY_CLOSE_WEIGHT_COLUMNS = (("weight", "REAL NOT NULL DEFAULT -1"),)

# Cash paid out for scrap instead of left as credit.
SCRAP_PAYOUT_COLUMNS = (
    ("payout", "REAL NOT NULL DEFAULT 0"),
    ("method", "TEXT NOT NULL DEFAULT 'Cash'"),
)

# Voiding a payment / scrap entry / ledger adjustment, exactly like a sale.
VOID_COLUMNS = (
    ("voided_at", "TEXT"),
    ("void_reason", "TEXT NOT NULL DEFAULT ''"),
)
PAYMENT_METHOD_COLUMNS = (("method", "TEXT NOT NULL DEFAULT 'Cash'"),)

# Moving-average cost carried on each product.
PRODUCT_COST_COLUMNS = (
    ("cost_per_kg", "REAL NOT NULL DEFAULT 0"),
    ("cost_kg", "REAL NOT NULL DEFAULT 0"),
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


def add_missing_columns(con, table, columns):
    """Add the columns a database created before a feature is missing."""
    existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    for name, declaration in columns:
        if name not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


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
        SALE_SCRAP_COLUMNS
        + SALE_CREDIT_COLUMNS
        + SALE_ROUND_OFF_COLUMNS
        + SALE_PICKUP_COLUMNS
        + SALE_VOID_COLUMNS
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

    add_missing_columns(con, "sales", SALE_METHOD_COLUMNS)
    add_missing_columns(con, "pickups", PICKUP_CANCEL_COLUMNS)
    add_missing_columns(con, "payments", PAYMENT_METHOD_COLUMNS + VOID_COLUMNS)
    add_missing_columns(
        con, "scrap_payments", SCRAP_PAYOUT_COLUMNS + VOID_COLUMNS
    )
    add_missing_columns(con, "ledger_adjustments", VOID_COLUMNS)
    add_missing_columns(con, "day_closes", DAY_CLOSE_WEIGHT_COLUMNS)
    add_missing_columns(con, "products", PRODUCT_COST_COLUMNS)

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


def backup_files(directory=None):
    """Existing backup files in a folder (default backups/), newest first."""
    folder = directory or BACKUP_DIR
    try:
        names = [
            name
            for name in os.listdir(folder)
            if name.startswith(BACKUP_PREFIX) and name.endswith(".db")
        ]
    except OSError:
        return []
    # Newest first, by modification time (so two copies made in the same minute
    # still sort in the order they were written).
    entries = []
    for name in names:
        path = os.path.join(folder, name)
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


def prune_backups(keep=BACKUP_KEEP, directory=None):
    """Delete all but the newest `keep` snapshots so the disk never fills up."""
    for path in backup_files(directory)[keep:]:
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

        # A copy on another disk is what survives a dead hard drive. If the
        # second folder is unreachable the local backup still stands.
        second = setting_value("backup_second_dir")
        if second and os.path.abspath(second) != os.path.abspath(BACKUP_DIR):
            try:
                os.makedirs(second, exist_ok=True)
                shutil.copy2(target, os.path.join(second, os.path.basename(target)))
                # The other drive rotates on the same budget: an hourly copy kept
                # there for months would otherwise fill that disk.
                prune_backups(directory=second)
            except OSError as exc:
                print(f"Second backup copy failed: {exc}")

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
def finite_number(value, default=None):
    """A finite float from user input, or `default` for junk/infinity/NaN."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


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
#            + opening dues brought over from the paper ledger
#
# Positive net is money owed; negative net is credit on the account. Cash paid
# beyond a bill is change handed back, so `cash_applied` (not the tender) is
# what counts. Scrap beyond a bill is never handed back, so it stays as credit
# and comes off the next bill automatically. A waived remainder is written off
# on that bill, so it never shows up as due here.
# A voided bill is left out of every term, so reversing it puts the account
# back exactly where it stood before that receipt was saved.
NET_BALANCE_SQL = (
    "(COALESCE((SELECT SUM(subtotal) FROM sales"
    " WHERE customer_id = {c} AND voided_at IS NULL), 0)"
    " - COALESCE((SELECT SUM(cash_applied) FROM sales"
    " WHERE customer_id = {c} AND voided_at IS NULL), 0)"
    " - COALESCE((SELECT SUM(scrap_total) FROM sales"
    " WHERE customer_id = {c} AND voided_at IS NULL), 0)"
    " - COALESCE((SELECT SUM(round_off) FROM sales"
    " WHERE customer_id = {c} AND voided_at IS NULL), 0)"
    " - COALESCE((SELECT SUM(amount) FROM payments"
    "             WHERE customer_id = {c} AND voided_at IS NULL), 0)"
    # Cash paid out for scrap never paid the account, so only the part left
    # against the due counts.
    " - COALESCE((SELECT SUM(total - payout) FROM scrap_payments"
    "             WHERE customer_id = {c} AND voided_at IS NULL), 0)"
    # Credit handed back in cash/bKash/bank is no longer owed to the customer,
    # so it raises the net balance and settles a credit account to zero.
    " + COALESCE((SELECT SUM(amount) FROM credit_payouts"
    "             WHERE customer_id = {c} AND voided_at IS NULL), 0)"
    " + COALESCE((SELECT SUM(amount) FROM ledger_adjustments"
    "             WHERE customer_id = {c} AND voided_at IS NULL), 0))"
)

OPENING_DUE_NOTE = "Opening balance from physical ledger"


def account_sql(customer_ref):
    """The signed net-balance expression for an id column or parameter."""
    return NET_BALANCE_SQL.format(c=customer_ref)


def method_of(value):
    """One of PAYMENT_METHODS; anything else is treated as cash."""
    text = (value or "").strip()
    for name in PAYMENT_METHODS:
        if text.casefold() == name.casefold():
            return name
    return PAYMENT_METHODS[0]


def required_reason():
    """The reason typed on a void form, or "" when it was left empty."""
    return (request.form.get("reason") or "").strip()[:200]


def setting_value(key, default=""):
    """A saved shop setting, read on a connection of its own."""
    try:
        con = connect_db()
        try:
            row = con.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row and row[0] else default
        finally:
            con.close()
    except sqlite3.Error:
        return default


def save_setting(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


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


def add_ledger_adjustment(db, customer_id, amount, reason=""):
    """Put an old paper-ledger balance on the account, with no bill behind it."""
    db.execute(
        "INSERT INTO ledger_adjustments (customer_id, amount, note, created_at)"
        " VALUES (?, ?, ?, ?)",
        (
            customer_id,
            round(amount, 2),
            (reason or "").strip() or OPENING_DUE_NOTE,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )


def customer_balance(db, customer_id):
    """Amount the customer owes right now (0 when they have credit)."""
    return customer_account(db, customer_id)["due"]


AGE_BUCKETS = ("0–30", "31–60", "60+")


def due_age(db, customers):
    """Each customer's due split by the age of the bills that built it.

    Payments settle the oldest money first, so what is still owed belongs to
    the newest bills: walk them newest to oldest and take what is left of the
    balance. A carried-over ledger due has no bill behind it and counts as the
    oldest money on the account.
    """
    empty = {bucket: 0.0 for bucket in AGE_BUCKETS}
    ages = {row["id"]: dict(empty) for row in customers}
    remaining = {row["id"]: round(float(row["balance"] or 0), 2) for row in customers}
    now = datetime.now()

    def bucket_for(stamp):
        try:
            days = (now.date() - datetime.strptime(stamp[:10], "%Y-%m-%d").date()).days
        except (TypeError, ValueError):
            return AGE_BUCKETS[0]
        if days <= 30:
            return AGE_BUCKETS[0]
        return AGE_BUCKETS[1] if days <= 60 else AGE_BUCKETS[2]

    bills = db.execute(
        "SELECT customer_id, created_at, new_due - previous_due AS added"
        " FROM sales WHERE voided_at IS NULL"
        " ORDER BY created_at DESC, id DESC"
    ).fetchall()
    for bill in bills:
        customer_id = bill["customer_id"]
        left = remaining.get(customer_id, 0.0)
        added = float(bill["added"] or 0)
        if left <= 0 or added <= 0:
            continue
        taken = min(added, left)
        ages[customer_id][bucket_for(bill["created_at"])] += taken
        remaining[customer_id] = round(left - taken, 2)

    for customer_id, left in remaining.items():
        if left > 0:
            ages[customer_id][AGE_BUCKETS[2]] += left

    return {
        customer_id: {name: round(value, 2) for name, value in buckets.items()}
        for customer_id, buckets in ages.items()
    }


def weight_sold(db, start_iso, end_iso):
    """Kilos that left the shop in a date range: billed lines plus challans.

    A master receipt built from challans adds no weight of its own, so no kilo
    is counted twice.
    """
    billed = db.execute(
        "SELECT COALESCE(SUM(si.weight_kg), 0) FROM sale_items si"
        " JOIN sales s ON s.id = si.sale_id"
        " WHERE date(s.created_at) BETWEEN ? AND ?"
        " AND s.voided_at IS NULL AND s.pickup_billed = 0",
        (start_iso, end_iso),
    ).fetchone()[0]
    picked = db.execute(
        "SELECT COALESCE(SUM(weight_kg), 0) FROM pickups"
        " WHERE picked_on BETWEEN ? AND ? AND cancelled_at IS NULL",
        (start_iso, end_iso),
    ).fetchone()[0]
    return round(float(billed or 0) + float(picked or 0), 3)


def day_cash(db, day_iso):
    """One day's money, per payment method, for the drawer reconciliation.

    Cash on bills and due payments came in; cash paid out for scrap went out.
    Only the Cash method is in the drawer: bKash and Bank are listed beside it
    but never in the expected till figure.
    """
    methods = set(PAYMENT_METHODS)

    def empty():
        return {name: 0.0 for name in PAYMENT_METHODS}

    cash_in = empty()
    for row in db.execute(
        "SELECT method, SUM(amount) AS total FROM ("
        "  SELECT COALESCE(payment_method, 'Cash') AS method,"
        "         cash_applied AS amount FROM sales"
        "   WHERE date(created_at) = ? AND voided_at IS NULL"
        "  UNION ALL"
        "  SELECT COALESCE(method, 'Cash') AS method, amount FROM payments"
        "   WHERE date(created_at) = ? AND voided_at IS NULL"
        ") GROUP BY method",
        (day_iso, day_iso),
    ).fetchall():
        name = row["method"] if row["method"] in methods else "Cash"
        cash_in[name] += float(row["total"] or 0)

    # Cash paid out for scrap and credit handed back to a customer both leave
    # the drawer; bKash and Bank leave the phone/account instead.
    payouts = empty()
    for row in db.execute(
        "SELECT method, SUM(amount) AS total FROM ("
        "  SELECT COALESCE(method, 'Cash') AS method, payout AS amount"
        "    FROM scrap_payments"
        "   WHERE date(created_at) = ? AND voided_at IS NULL"
        "  UNION ALL"
        "  SELECT COALESCE(method, 'Cash') AS method, amount FROM credit_payouts"
        "   WHERE date(created_at) = ? AND voided_at IS NULL"
        ") GROUP BY method",
        (day_iso, day_iso),
    ).fetchall():
        name = row["method"] if row["method"] in methods else "Cash"
        payouts[name] += float(row["total"] or 0)

    due_added = db.execute(
        "SELECT COALESCE(SUM(new_due - previous_due), 0) AS added FROM sales"
        " WHERE date(created_at) = ? AND voided_at IS NULL",
        (day_iso,),
    ).fetchone()["added"]

    rows = [
        {
            "name": name,
            "cash_in": round(cash_in[name], 2),
            "payouts": round(payouts[name], 2),
            "net": round(cash_in[name] - payouts[name], 2),
        }
        for name in PAYMENT_METHODS
    ]
    drawer_in = rows[0]["cash_in"]
    drawer_out = rows[0]["payouts"]
    return {
        "rows": rows,
        "drawer_in": drawer_in,
        "drawer_out": drawer_out,
        "expected": round(drawer_in - drawer_out, 2),
        "due_added": round(float(due_added or 0), 2),
    }


def scrap_rates(db):
    """Current scrap prices per kg, keyed by type name (Black, White)."""
    rows = db.execute(
        "SELECT name, price_per_kg FROM scrap_rates ORDER BY rowid"
    ).fetchall()
    return {row["name"]: row["price_per_kg"] for row in rows}


def customer_rates(db, customer_id):
    """Special Tk./kg prices for one customer, keyed by product id.

    Only genuine exceptions are stored, so a missing key means "shop rate".
    """
    rows = db.execute(
        "SELECT product_id, price_per_kg FROM customer_rates"
        " WHERE customer_id = ?",
        (customer_id,),
    ).fetchall()
    return {row["product_id"]: row["price_per_kg"] for row in rows}


def price_for(customer_prices, product):
    """The customer's special rate for a product, else the shop rate."""
    special = customer_prices.get(product["id"])
    if special is None:
        return round(float(product["price_per_kg"]), 2)
    return round(float(special), 2)


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
        purchases=db.execute(
            "SELECT * FROM purchases ORDER BY id DESC LIMIT 8"
        ).fetchall(),
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
    """Tab 3: all customers with due balance, searchable and due-only."""
    db = get_db()
    rows = customers_with_balances(db)
    # The header total always covers every customer: filtering only narrows
    # the list on screen, it never changes what the shop is owed.
    total_due = round(sum(r["balance"] for r in rows), 2)
    search = (request.args.get("q") or "").strip()
    due_only = request.args.get("due") == "1"
    shown = [r for r in rows if not due_only or r["balance"] > 0]
    if search:
        needle = search.casefold()
        shown = [
            r
            for r in shown
            if needle
            in " ".join(
                (r["name"] or "", r["phone"] or "", r["shop_name"] or "", r["address"] or "")
            ).casefold()
        ]
    # Every customer/day with unbilled challan pickups: the "Pending Daily
    # Bills" list. Nothing here is a sale yet.
    pending_bills = db.execute(
        "SELECT p.customer_id, p.picked_on, c.name, COUNT(*) AS lines,"
        " SUM(p.weight_kg) AS weight"
        " FROM pickups p JOIN customers c ON c.id = p.customer_id"
        " WHERE p.sale_id IS NULL AND p.cancelled_at IS NULL"
        " GROUP BY p.customer_id, p.picked_on"
        " ORDER BY p.picked_on DESC, c.name COLLATE NOCASE"
    ).fetchall()
    return render_template(
        "customers.html",
        active="customers",
        customers=shown,
        ages=due_age(db, rows),
        total_customers=len(rows),
        total_due=total_due,
        search=search,
        due_only=due_only,
        pending_bills=pending_bills,
    )


@app.route("/daily")
def daily_summary():
    """Tab 4: what was sold on one day, ranked by weight or by revenue."""
    db = get_db()
    now = datetime.now()

    # Any past day can be reviewed; a missing or malformed date falls back to
    # today instead of showing an error page. `from`/`to` ask for a span (a
    # month, a week, any two dates); the older `date` link still works.
    today = now.date()

    wants_span = bool(
        (request.args.get("from") or "").strip()
        or (request.args.get("to") or "").strip()
    )
    if wants_span:
        start = parse_day(request.args.get("from")) or parse_day(
            request.args.get("to")
        ) or today
        end = parse_day(request.args.get("to")) or start
        if start > end:
            start, end = end, start
    else:
        start = end = parse_day(request.args.get("date")) or today
    start_iso = start.strftime("%Y-%m-%d")
    end_iso = end.strftime("%Y-%m-%d")
    # One day reads as a date; anything wider reads as a span.
    if start == end:
        range_label = start.strftime("%A, %d %b %Y")
    else:
        range_label = f"{start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')}"
    is_today = start_iso == end_iso == today.strftime("%Y-%m-%d")
    month_start_iso = today.replace(day=1).strftime("%Y-%m-%d")
    sort = "revenue" if request.args.get("sort") == "revenue" else "weight"

    totals = db.execute(
        "SELECT COUNT(*) AS receipts,"
        " COALESCE(SUM(subtotal), 0) AS sales,"
        " COALESCE(SUM(cash_applied), 0) AS cash_on_bills,"
        # new_due is the running account balance, so it already contains the
        # previous due; only the change it made today is due added today.
        " COALESCE(SUM(new_due - previous_due), 0) AS due,"
        " COALESCE(SUM(scrap_total), 0) AS scrap"
        " FROM sales WHERE date(created_at) BETWEEN ? AND ?"
        " AND voided_at IS NULL",
        (start_iso, end_iso),
    ).fetchone()
    # Cash handed over on the customer page to clear an older due lands in the
    # same drawer, so it counts as collected for the same dates.
    payments = db.execute(
        "SELECT COUNT(*) AS count, COALESCE(SUM(amount), 0) AS amount"
        " FROM payments WHERE date(created_at) BETWEEN ? AND ?"
        " AND voided_at IS NULL",
        (start_iso, end_iso),
    ).fetchone()
    due_payments = round(float(payments["amount"] or 0), 2)
    cash_collected = round(float(totals["cash_on_bills"] or 0) + due_payments, 2)
    # Voided bills stay on the record but never in the period's money or weight.
    voided_today = db.execute(
        "SELECT COUNT(*) FROM sales"
        " WHERE date(created_at) BETWEEN ? AND ? AND voided_at IS NOT NULL",
        (start_iso, end_iso),
    ).fetchone()[0]

    # The drawer only exists for one day at a time, so the close and the
    # method split are shown for a single day, never for a span.
    cash_day = day_cash(db, start_iso) if start_iso == end_iso else None
    close = None
    close_changed = False
    if cash_day is not None:
        close = db.execute(
            "SELECT * FROM day_closes WHERE day = ?", (start_iso,)
        ).fetchone()
        if close is not None:
            # Anything voided or edited after the close shows up as a difference
            # here instead of quietly moving a day that was already counted.
            close_changed = any(
                abs(float(close[stored] or 0) - float(cash_day[live] or 0)) >= 0.005
                for stored, live in (
                    ("cash_in", "drawer_in"),
                    ("payouts", "drawer_out"),
                    ("expected_cash", "expected"),
                    ("due_added", "due_added"),
                )
            )
            # Weight moves no cash, so a challan cancelled or restored after the
            # close would otherwise rewrite the day's kilos unseen.
            stored_weight = float(close["weight"])
            if (
                not close_changed
                and stored_weight >= 0
                and abs(stored_weight - weight_sold(db, start_iso, start_iso)) >= 0.0005
            ):
                close_changed = True

    # Weight is counted on the day the goods left the shop: a weight-only
    # challan adds its kg on the pickup day, and the master receipt built from
    # it adds no weight of its own, so no kilo is ever counted twice. Revenue
    # lands on the day the master receipt is finalized.
    billed = db.execute(
        "SELECT si.product_name,"
        " SUM(CASE WHEN s.pickup_billed = 0 THEN si.weight_kg ELSE 0 END)"
        "     AS weight,"
        " SUM(si.line_total) AS revenue"
        " FROM sale_items si JOIN sales s ON s.id = si.sale_id"
        " WHERE date(s.created_at) BETWEEN ? AND ? AND s.voided_at IS NULL"
        " GROUP BY si.product_name",
        (start_iso, end_iso),
    ).fetchall()
    picked = db.execute(
        "SELECT product_name, SUM(weight_kg) AS weight FROM pickups"
        " WHERE picked_on BETWEEN ? AND ? AND cancelled_at IS NULL"
        " GROUP BY product_name",
        (start_iso, end_iso),
    ).fetchall()

    merged = {}
    for row in billed:
        merged[row["product_name"]] = {
            "product_name": row["product_name"],
            "weight": float(row["weight"] or 0),
            "revenue": float(row["revenue"] or 0),
        }
    for row in picked:
        entry = merged.setdefault(
            row["product_name"],
            {"product_name": row["product_name"], "weight": 0.0, "revenue": 0.0},
        )
        entry["weight"] += float(row["weight"] or 0)

    products = [
        {
            "product_name": entry["product_name"],
            "weight": round(entry["weight"], 3),
            "revenue": round(entry["revenue"], 2),
        }
        for entry in merged.values()
    ]
    # `sort` is chosen from a fixed pair above, never raw SQL from the URL.
    products.sort(key=lambda p: p["product_name"].lower())
    products.sort(
        key=lambda p: p["revenue" if sort == "revenue" else "weight"],
        reverse=True,
    )

    total_weight = round(sum(p["weight"] for p in products), 3)
    # Gross margin uses each product's moving-average purchase cost. A product
    # with no purchase behind it costs 0, so its margin reads as the full sale
    # until the first purchase is entered.
    costs = {
        row["name"]: float(row["cost_per_kg"] or 0)
        for row in db.execute("SELECT name, cost_per_kg FROM products")
    }
    for product in products:
        product["cost"] = round(
            product["weight"] * costs.get(product["product_name"], 0.0), 2
        )
        product["margin"] = round(product["revenue"] - product["cost"], 2)
    total_cost = round(sum(p["cost"] for p in products), 2)
    gross_margin = round(float(totals["sales"] or 0) - total_cost, 2)
    avg_bill = round(totals["sales"] / totals["receipts"], 2) if totals["receipts"] else 0.0
    return render_template(
        "daily.html",
        active="daily",
        start_iso=start_iso,
        end_iso=end_iso,
        range_label=range_label,
        today_iso=today.strftime("%Y-%m-%d"),
        month_start_iso=month_start_iso,
        is_today=is_today,
        sort=sort,
        totals=totals,
        cash_collected=cash_collected,
        due_payments=due_payments,
        due_payment_count=payments["count"],
        products=products,
        total_weight=total_weight,
        total_cost=total_cost,
        gross_margin=gross_margin,
        cash_day=cash_day,
        close=close,
        close_changed=close_changed,
        avg_bill=avg_bill,
        voided_today=voided_today,
        printed_at=now.strftime("%d %b %Y, %I:%M %p"),
    )


def parse_day(value):
    """A strict YYYY-MM-DD date, or None."""
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


@app.post("/daily/close")
def close_day():
    """Close a day on the cash actually counted in the drawer."""
    day = parse_day(request.form.get("day"))
    if day is None:
        note("Pick the day being closed.", "error")
        return redirect(url_for("daily_summary"))
    if day > datetime.now().date():
        note("That day has not happened yet, so it cannot be closed.", "error")
        return redirect(url_for("daily_summary"))
    counted = finite_number(request.form.get("counted_cash"))
    if counted is None or counted < 0:
        note("Counted cash must be a number of 0 or more.", "error")
        return redirect(url_for("daily_summary", date=day.strftime("%Y-%m-%d")))

    db = get_db()
    day_iso = day.strftime("%Y-%m-%d")
    cash = day_cash(db, day_iso)
    counted = round(counted, 2)
    difference = round(counted - cash["expected"], 2)
    note_text = (request.form.get("note") or "").strip()[:200]
    db.execute(
        "INSERT INTO day_closes (day, cash_in, payouts, expected_cash,"
        " counted_cash, due_added, weight, note, closed_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(day) DO UPDATE SET cash_in = excluded.cash_in,"
        " payouts = excluded.payouts, expected_cash = excluded.expected_cash,"
        " counted_cash = excluded.counted_cash, due_added = excluded.due_added,"
        " weight = excluded.weight, note = excluded.note,"
        " closed_at = excluded.closed_at",
        (
            day_iso,
            cash["drawer_in"],
            cash["drawer_out"],
            cash["expected"],
            counted,
            cash["due_added"],
            weight_sold(db, day_iso, day_iso),
            note_text,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    db.commit()
    note(
        f"{day.strftime('%d %b %Y')} closed: drawer should hold "
        f"{CURRENCY} {money(cash['expected'])}, counted {CURRENCY} {money(counted)}"
        + (
            f", short {CURRENCY} {money(-difference)}."
            if difference < 0
            else f", over {CURRENCY} {money(difference)}."
            if difference > 0
            else ", matching exactly."
        )
    )
    create_backup()
    return redirect(url_for("daily_summary", date=day_iso))


@app.post("/daily/reopen")
def reopen_day():
    """Forget a close so the day can be corrected and closed again."""
    day = parse_day(request.form.get("day"))
    if day is None:
        note("Pick the day to reopen.", "error")
        return redirect(url_for("daily_summary"))
    db = get_db()
    db.execute("DELETE FROM day_closes WHERE day = ?", (day.strftime("%Y-%m-%d"),))
    db.commit()
    note(f"{day.strftime('%d %b %Y')} reopened — close it again once the day is right.")
    return redirect(url_for("daily_summary", date=day.strftime("%Y-%m-%d")))


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

    payouts = db.execute(
        "SELECT * FROM credit_payouts WHERE customer_id = ? ORDER BY id DESC",
        (customer_id,),
    ).fetchall()

    ledger_entries = db.execute(
        "SELECT * FROM ledger_adjustments WHERE customer_id = ? ORDER BY id DESC",
        (customer_id,),
    ).fetchall()

    products = db.execute(
        "SELECT id, name, category, price_per_kg FROM products"
        " ORDER BY category, name COLLATE NOCASE"
    ).fetchall()

    # Unbilled challan pickups, grouped by the day they left the shop.
    pending = db.execute(
        "SELECT * FROM pickups WHERE customer_id = ? AND sale_id IS NULL"
        " AND cancelled_at IS NULL"
        " ORDER BY picked_on DESC, id",
        (customer_id,),
    ).fetchall()
    pending_days = []
    for row in pending:
        if not pending_days or pending_days[-1]["day"] != row["picked_on"]:
            pending_days.append({"day": row["picked_on"], "rows": [], "weight": 0.0})
        pending_days[-1]["rows"].append(row)
        pending_days[-1]["weight"] = round(
            pending_days[-1]["weight"] + row["weight_kg"], 3
        )

    # Cancelled pickups stay on the ledger for the record, but count nowhere.
    cancelled_pickups = db.execute(
        "SELECT * FROM pickups WHERE customer_id = ? AND sale_id IS NULL"
        " AND cancelled_at IS NOT NULL ORDER BY cancelled_at DESC, id DESC",
        (customer_id,),
    ).fetchall()

    # Totals are for what the customer still owes against: a voided bill is
    # listed below for the record but counted nowhere.
    live_sales = [s for s in sales if s["voided_at"] is None]
    account = customer_account(db, customer_id)
    return render_template(
        "customer_detail.html",
        active="customers",
        customer=customer,
        sales=sales,
        items_by_sale=items_by_sale,
        payments=payments,
        scrap_entries=scrap_entries,
        payouts=payouts,
        ledger_entries=ledger_entries,
        scrap_rates=scrap_rates(db),
        products=products,
        rates=customer_rates(db, customer_id),
        pending_days=pending_days,
        cancelled_pickups=cancelled_pickups,
        balance=account["due"],
        credit=account["credit"],
        total_billed=sum(s["subtotal"] for s in live_sales),
        # Only money that actually settled this account, and only live rows: a
        # voided payment never paid anything, and scrap paid out in cash never
        # paid the due.
        total_paid=sum(s["cash_applied"] for s in live_sales)
        + sum(s["scrap_total"] for s in live_sales)
        + sum(s["round_off"] for s in live_sales)
        + sum(p["amount"] for p in payments if p["voided_at"] is None)
        + sum(
            e["total"] - e["payout"]
            for e in scrap_entries
            if e["voided_at"] is None
        ),
    )


@app.route("/receipts")
def receipts():
    """Find any saved bill by date, customer, amount or voided state."""
    db = get_db()
    today = datetime.now().date()
    start = parse_day(request.args.get("from")) or today - timedelta(days=30)
    end = parse_day(request.args.get("to")) or today
    if start > end:
        start, end = end, start
    start_iso = start.strftime("%Y-%m-%d")
    end_iso = end.strftime("%Y-%m-%d")
    search = (request.args.get("q") or "").strip()
    amount = finite_number(request.args.get("amount"), None)
    status = request.args.get("status") or "all"

    # Built from fixed fragments only: nothing from the URL reaches the SQL.
    query = (
        "SELECT s.*, c.name AS customer_name FROM sales s"
        " JOIN customers c ON c.id = s.customer_id"
        " WHERE date(s.created_at) BETWEEN ? AND ?"
    )
    params = [start_iso, end_iso]
    if search:
        digits = "".join(ch for ch in search if ch.isdigit())
        query += (
            " AND (c.name LIKE ? OR c.shop_name LIKE ? OR c.phone LIKE ?"
            " OR s.id = ?)"
        )
        params += [f"%{search}%", f"%{search}%", f"%{search}%", int(digits or 0)]
    if amount is not None:
        query += " AND ABS(s.subtotal - ?) < 0.005"
        params.append(round(amount, 2))
    if status == "void":
        query += " AND s.voided_at IS NOT NULL"
    elif status == "live":
        query += " AND s.voided_at IS NULL"
    query += " ORDER BY s.id DESC LIMIT 300"

    rows = db.execute(query, params).fetchall()
    return render_template(
        "receipts.html",
        active="receipts",
        receipts=rows,
        start_iso=start_iso,
        end_iso=end_iso,
        search=search,
        amount="" if amount is None else f"{amount:.2f}",
        status=status,
        total_value=round(sum(r["subtotal"] for r in rows if r["voided_at"] is None), 2),
    )


def statement_lines(db, customer_id):
    """Every live money row on an account, oldest first.

    `billed - received` is what that row did to the balance, so adding the
    lines up rebuilds the due without any other rule.
    """
    lines = []
    for sale in db.execute(
        "SELECT * FROM sales WHERE customer_id = ? AND voided_at IS NULL"
        " ORDER BY created_at, id",
        (customer_id,),
    ):
        lines.append(
            {
                "when": sale["created_at"],
                "kind": "Bill",
                "ref": f"INV-{sale['id']:05d}",
                "note": "" if not sale["pickup_billed"] else "Daily pickups",
                "billed": round(float(sale["subtotal"] or 0), 2),
                "received": round(
                    float(sale["cash_applied"] or 0)
                    + float(sale["scrap_total"] or 0)
                    + float(sale["round_off"] or 0),
                    2,
                ),
            }
        )
    for payment in db.execute(
        "SELECT * FROM payments WHERE customer_id = ? AND voided_at IS NULL"
        " ORDER BY created_at, id",
        (customer_id,),
    ):
        lines.append(
            {
                "when": payment["created_at"],
                "kind": "Payment",
                "ref": f"PMT-{payment['id']:05d}",
                "note": " ".join(
                    part
                    for part in (payment["method"], payment["note"] or "")
                    if part
                ),
                "billed": 0.0,
                "received": round(float(payment["amount"] or 0), 2),
            }
        )
    for entry in db.execute(
        "SELECT * FROM scrap_payments WHERE customer_id = ? AND voided_at IS NULL"
        " ORDER BY created_at, id",
        (customer_id,),
    ):
        parts = [
            f"{name} {float(entry[name.lower() + '_kg'] or 0):.3f} kg"
            for name in SCRAP_TYPES
            if float(entry[name.lower() + "_kg"] or 0) > 0
        ]
        if float(entry["payout"] or 0) > 0:
            parts.append(f"{CURRENCY} {money(entry['payout'])} paid out")
        lines.append(
            {
                "when": entry["created_at"],
                "kind": "Scrap",
                "ref": f"SCR-{entry['id']:05d}",
                "note": ", ".join(parts),
                "billed": 0.0,
                "received": round(
                    float(entry["total"] or 0) - float(entry["payout"] or 0), 2
                ),
            }
        )
    for entry in db.execute(
        "SELECT * FROM credit_payouts WHERE customer_id = ? AND voided_at IS NULL"
        " ORDER BY created_at, id",
        (customer_id,),
    ):
        lines.append(
            {
                "when": entry["created_at"],
                "kind": "Credit payout",
                "ref": f"PAY-{entry['id']:05d}",
                "note": " ".join(
                    part
                    for part in (entry["method"], entry["note"] or "")
                    if part
                ),
                # Money handed back raises the balance towards zero, exactly
                # like a new charge does.
                "billed": round(float(entry["amount"] or 0), 2),
                "received": 0.0,
            }
        )
    for entry in db.execute(
        "SELECT * FROM ledger_adjustments WHERE customer_id = ?"
        " AND voided_at IS NULL ORDER BY created_at, id",
        (customer_id,),
    ):
        amount = round(float(entry["amount"] or 0), 2)
        lines.append(
            {
                "when": entry["created_at"],
                "kind": "Adjustment",
                "ref": f"ADJ-{entry['id']:05d}",
                "note": entry["note"] or OPENING_DUE_NOTE,
                "billed": amount if amount > 0 else 0.0,
                "received": -amount if amount < 0 else 0.0,
            }
        )
    lines.sort(key=lambda line: (line["when"], line["kind"]))
    return lines


@app.route("/customers/<int:customer_id>/statement")
def customer_statement(customer_id):
    """A printable account statement for a date range."""
    db = get_db()
    customer = load_customer(db, customer_id)
    if customer is None:
        abort(404)
    today = datetime.now().date()
    start = parse_day(request.args.get("from")) or today.replace(day=1)
    end = parse_day(request.args.get("to")) or today
    if start > end:
        start, end = end, start
    start_iso = start.strftime("%Y-%m-%d")
    end_iso = end.strftime("%Y-%m-%d")

    lines = statement_lines(db, customer_id)
    opening = round(
        sum(
            line["billed"] - line["received"]
            for line in lines
            if line["when"][:10] < start_iso
        ),
        2,
    )
    shown = [line for line in lines if start_iso <= line["when"][:10] <= end_iso]
    running = opening
    for line in shown:
        running = round(running + line["billed"] - line["received"], 2)
        line["balance"] = running

    voided = db.execute(
        "SELECT COUNT(*) FROM sales WHERE customer_id = ? AND voided_at IS NOT NULL",
        (customer_id,),
    ).fetchone()[0]
    return render_template(
        "statement.html",
        active="customers",
        customer=customer,
        lines=shown,
        start_iso=start_iso,
        end_iso=end_iso,
        opening=opening,
        closing=running,
        billed=round(sum(line["billed"] for line in shown), 2),
        received=round(sum(line["received"] for line in shown), 2),
        voided=voided,
        printed_at=datetime.now().strftime("%d %b %Y, %I:%M %p"),
    )


@app.get("/backups")
def backups():
    """Backup, restore and second-copy controls in one place."""
    entries = []
    for path in backup_files()[:15]:
        try:
            entries.append(
                {
                    "name": os.path.basename(path),
                    "size": os.path.getsize(path),
                    "when": datetime.fromtimestamp(os.path.getmtime(path)),
                }
            )
        except OSError:
            continue
    return render_template(
        "backups.html",
        active="",
        backups=entries,
        total_backups=len(backup_files()),
        second_dir=setting_value("backup_second_dir"),
        db_size=os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0,
        db_path=DB_PATH,
    )


@app.post("/backup/now")
def backup_now():
    path = create_backup(force=True)
    if not path:
        note("Could not write a backup. Check the console for details.", "error")
    else:
        note(f"Backup saved as {os.path.basename(path)}.")
    return redirect(url_for("backups"))


@app.post("/backup/second")
def save_backup_second():
    """Remember a second folder that every new backup is copied into."""
    folder = (request.form.get("path") or "").strip()
    if folder:
        try:
            os.makedirs(folder, exist_ok=True)
            probe = os.path.join(folder, ".pos-write-test")
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("ok")
            os.remove(probe)
        except OSError as exc:
            note(f"That folder cannot be written to: {exc}", "error")
            return redirect(url_for("backups"))
    db = get_db()
    save_setting(db, "backup_second_dir", folder)
    db.commit()
    note(
        f"Second backup folder set to {folder}."
        if folder
        else "Second backup folder cleared."
    )
    return redirect(url_for("backups"))


@app.get("/backup/file/<path:name>")
def download_backup_file(name):
    """Download one of the saved snapshots, by name inside backups/."""
    safe = os.path.basename(name)
    path = os.path.join(BACKUP_DIR, safe)
    if safe != name or not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=safe,
                     mimetype="application/x-sqlite3")


@app.post("/backup/restore")
def restore_backup():
    """Replace the live database with an uploaded backup, keeping the old one.

    The upload is copied next to the data, checked with SQLite's own
    integrity check and for the shop tables, and only then copied into the
    live file through the backup API, so a half-written file can never become
    the shop's database.
    """
    upload = request.files.get("backup")
    if upload is None or not upload.filename:
        note("Choose a .db backup file to restore.", "error")
        return redirect(url_for("backups"))
    os.makedirs(DATA_DIR, exist_ok=True)
    probe_path = os.path.join(DATA_DIR, "restore-upload.db")
    try:
        upload.save(probe_path)
        try:
            probe = sqlite3.connect(probe_path)
            try:
                integrity = probe.execute("PRAGMA integrity_check").fetchone()[0]
                tables = {
                    row[0]
                    for row in probe.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            finally:
                probe.close()
        except sqlite3.Error:
            integrity, tables = "not a database", set()

        needed = {"sales", "sale_items", "customers", "products"}
        if integrity != "ok" or not needed <= tables:
            note(
                "That file is not a usable shop backup (it failed SQLite's"
                " integrity check or has no sales tables). Nothing was changed.",
                "error",
            )
            return redirect(url_for("backups"))

        # The database being replaced is kept first, so a restore can be undone.
        previous = create_backup(force=True)
        source = sqlite3.connect(probe_path)
        target = connect_db()
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        # An older backup may be missing newer columns.
        init_db()
        check = connect_db()
        try:
            sales_count = check.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
            customer_count = check.execute(
                "SELECT COUNT(*) FROM customers"
            ).fetchone()[0]
        finally:
            check.close()
        note(
            f"Backup restored: {sales_count} sale"
            f"{'' if sales_count == 1 else 's'} and {customer_count} customer"
            f"{'' if customer_count == 1 else 's'}. "
            + (
                f"The database it replaced is saved as {os.path.basename(previous)}."
                if previous
                else "The previous database is in backups/."
            )
        )
        return redirect(url_for("billing"))
    finally:
        if os.path.exists(probe_path):
            os.remove(probe_path)


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


@app.route("/challan/<int:challan_no>")
def challan(challan_no):
    """80mm weight-only delivery slip: goods listed, no prices, no due."""
    db = get_db()
    items = db.execute(
        "SELECT * FROM pickups WHERE challan_no = ? ORDER BY id", (challan_no,)
    ).fetchall()
    if not items:
        abort(404)
    customer = load_customer(db, items[0]["customer_id"])
    live = [item for item in items if item["cancelled_at"] is None]
    return render_template(
        "challan.html",
        customer=customer,
        items=items,
        challan_no=challan_no,
        total_weight=round(sum(item["weight_kg"] for item in live), 3),
        all_cancelled=not live,
        picked_at=items[0]["picked_at"],
        autoprint=request.args.get("print") == "1",
    )


# ---------------------------------------------------------------------------
# Product actions
# ---------------------------------------------------------------------------
@app.post("/products/add")
def add_product():
    name = (request.form.get("name") or "").strip()
    category = (request.form.get("category") or "").strip() or "Uncategorised"
    price = finite_number(request.form.get("price_per_kg"))
    price = -1 if price is None else round(price, 2)

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
    price = finite_number(request.form.get("price_per_kg"))
    price = -1 if price is None else round(price, 2)
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
        price = finite_number(request.form.get(field))
        price = -1 if price is None else round(price, 2)
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
# Purchases: the buying side, so Daily Sales can show a real gross margin
# ---------------------------------------------------------------------------
@app.post("/purchases")
def add_purchase():
    """Record a delivery and carry its cost into the product's average."""
    db = get_db()
    supplier = (request.form.get("supplier") or "").strip()[:120]
    weight = finite_number(request.form.get("weight_kg"))
    cost = finite_number(request.form.get("cost_per_kg"))
    try:
        product_id = int(request.form.get("product_id") or 0)
    except (TypeError, ValueError):
        product_id = 0

    product = db.execute(
        "SELECT * FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    if product is None:
        note("Pick the product that was bought in.", "error")
        return redirect(url_for("products"))
    if weight is None or round(weight, 3) <= 0:
        note("Weight bought must be greater than 0 kg.", "error")
        return redirect(url_for("products"))
    if cost is None or round(cost, 2) < 0:
        note("Cost per kg must be a number of 0 or more.", "error")
        return redirect(url_for("products"))

    weight = round(weight, 3)
    cost = round(cost, 2)
    old_kg = float(product["cost_kg"] or 0)
    old_cost = float(product["cost_per_kg"] or 0)
    total_kg = round(old_kg + weight, 3)
    average = round((old_cost * old_kg + cost * weight) / total_kg, 2)

    db.execute(
        "INSERT INTO purchases (supplier, product_id, product_name, weight_kg,"
        " cost_per_kg, total, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            supplier,
            product["id"],
            product["name"],
            weight,
            cost,
            round(weight * cost, 2),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    db.execute(
        "UPDATE products SET cost_per_kg = ?, cost_kg = ? WHERE id = ?",
        (average, total_kg, product["id"]),
    )
    db.commit()
    note(
        f"{weight:.3f} kg of {product['name']} bought at {CURRENCY} {money(cost)}"
        f"/kg. Average cost is now {CURRENCY} {money(average)}/kg."
    )
    create_backup()
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
    opening = round(finite_number(request.form.get("opening_due"), 0), 2)
    if opening:
        add_ledger_adjustment(db, cur.lastrowid, opening)
    db.commit()
    note(f'Customer "{name}" added.')
    if opening > 0:
        note(
            f"Opening due of {CURRENCY} {money(opening)} carried onto the account."
        )
    return redirect(url_for("customer_detail", customer_id=cur.lastrowid))


@app.post("/customers/<int:customer_id>/opening-due")
def add_opening_due(customer_id):
    """Carry an old paper-ledger balance onto an existing customer."""
    db = get_db()
    customer = load_customer(db, customer_id)
    if customer is None:
        abort(404)

    amount = round(finite_number(request.form.get("amount"), 0), 2)
    if amount == 0:
        note("Enter the amount owed in the old ledger.", "error")
        return redirect(url_for("customer_detail", customer_id=customer_id))

    add_ledger_adjustment(
        db, customer_id, amount, request.form.get("note") or ""
    )
    db.commit()
    note(
        f"Previous due of {CURRENCY} {money(amount)} added to "
        f"{customer['name']}. Current due: "
        f"{CURRENCY} {money(customer_balance(db, customer_id))}."
    )
    create_backup()
    return redirect(url_for("customer_detail", customer_id=customer_id))


@app.post("/customers/<int:customer_id>/rates")
def save_customer_rates(customer_id):
    """Save the special Tk./kg prices for one customer. Blank = shop rate."""
    db = get_db()
    customer = load_customer(db, customer_id)
    if customer is None:
        abort(404)

    # Validate everything before writing, so a bad row saves nothing at all.
    updates = []
    clears = []
    for product in db.execute("SELECT id FROM products"):
        field = f"rate_{product['id']}"
        if field not in request.form:
            continue
        raw = (request.form.get(field) or "").strip()
        if not raw:
            clears.append((customer_id, product["id"]))
            continue
        price = finite_number(raw)
        if price is None or price < 0:
            note("Special rates must be a number of 0 or more.", "error")
            return redirect(url_for("customer_detail", customer_id=customer_id))
        updates.append((customer_id, product["id"], round(price, 2)))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.executemany(
        "DELETE FROM customer_rates WHERE customer_id = ? AND product_id = ?",
        clears,
    )
    db.executemany(
        "INSERT INTO customer_rates (customer_id, product_id, price_per_kg,"
        " updated_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(customer_id, product_id) DO UPDATE SET"
        " price_per_kg = excluded.price_per_kg,"
        " updated_at = excluded.updated_at",
        [(cid, pid, price, now) for cid, pid, price in updates],
    )
    db.commit()
    note(
        f"Special rates saved for {customer['name']}: {len(updates)} product"
        f"{'' if len(updates) == 1 else 's'}. Blank prices use the shop rate."
    )
    return redirect(url_for("customer_detail", customer_id=customer_id))


@app.post("/pickups/<int:pickup_id>/cancel")
def cancel_pickup(pickup_id):
    """Void a mistaken weight-only pickup before it is billed.

    The row is kept (struck through on the ledger) so the shop can see what was
    voided, but it stops counting in pending bills, master receipts and Daily
    Sales weight.
    """
    db = get_db()
    row = db.execute("SELECT * FROM pickups WHERE id = ?", (pickup_id,)).fetchone()
    if row is None:
        abort(404)
    back = url_for("customer_detail", customer_id=row["customer_id"])
    back += "#pending-pickups"
    if row["sale_id"] is not None:
        note("That pickup is already on a bill and cannot be cancelled.", "error")
        return redirect(back)
    if row["cancelled_at"] is not None:
        note("That pickup was already cancelled.", "error")
        return redirect(back)
    db.execute(
        "UPDATE pickups SET cancelled_at = ?"
        " WHERE id = ? AND sale_id IS NULL AND cancelled_at IS NULL",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), pickup_id),
    )
    db.commit()
    note(
        f"Challan #{row['challan_no']:05d} cancelled: {row['product_name']} "
        f"{row['weight_kg']:.3f} kg no longer counts in the day's weight."
    )
    return redirect(back)


@app.post("/pickups/<int:pickup_id>/restore")
def restore_pickup(pickup_id):
    """Undo a cancellation while the pickup is still unbilled."""
    db = get_db()
    row = db.execute("SELECT * FROM pickups WHERE id = ?", (pickup_id,)).fetchone()
    if row is None:
        abort(404)
    back = url_for("customer_detail", customer_id=row["customer_id"])
    back += "#pending-pickups"
    if row["sale_id"] is not None or row["cancelled_at"] is None:
        note("That pickup is not a cancelled, unbilled pickup.", "error")
        return redirect(back)
    db.execute(
        "UPDATE pickups SET cancelled_at = NULL WHERE id = ? AND sale_id IS NULL",
        (pickup_id,),
    )
    db.commit()
    note(
        f"Challan #{row['challan_no']:05d} restored: {row['product_name']} "
        f"{row['weight_kg']:.3f} kg counts again."
    )
    return redirect(back)


@app.post("/customers/<int:customer_id>/pickups/<day>/bill")
def bill_pickups(customer_id, day):
    """End-of-day master receipt: one day's unbilled pickups become one bill."""
    db = get_db()
    customer = load_customer(db, customer_id)
    if customer is None:
        abort(404)
    try:
        picked_on = datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        abort(404)

    rows = db.execute(
        "SELECT * FROM pickups WHERE customer_id = ? AND picked_on = ?"
        " AND sale_id IS NULL AND cancelled_at IS NULL"
        " ORDER BY product_name COLLATE NOCASE, id",
        (customer_id, picked_on),
    ).fetchall()
    if not rows:
        note("There are no unbilled pickups for that day.", "error")
        return redirect(url_for("customer_detail", customer_id=customer_id))

    paid = round(finite_number(request.form.get("paid"), 0), 2)
    round_off = round(finite_number(request.form.get("round_off"), 0), 2)
    if paid < 0 or round_off < 0:
        note("Amount paid and round-off cannot be negative.", "error")
        return redirect(url_for("customer_detail", customer_id=customer_id))

    # Same weight in one line per product, priced at this customer's special
    # rates. A deleted product falls back to the price snapshotted at pickup.
    special = customer_rates(db, customer_id)
    merged = {}
    for row in rows:
        key = (row["product_id"], row["product_name"])
        line = merged.get(key)
        if line is None:
            price = special.get(row["product_id"])
            if price is None:
                product = (
                    db.execute(
                        "SELECT price_per_kg FROM products WHERE id = ?",
                        (row["product_id"],),
                    ).fetchone()
                    if row["product_id"]
                    else None
                )
                price = (
                    product["price_per_kg"] if product else row["default_price"]
                )
            line = merged[key] = {
                "product_id": row["product_id"],
                "product_name": row["product_name"],
                "weight_kg": 0.0,
                "price_per_kg": round(float(price or 0), 2),
            }
        line["weight_kg"] = round(line["weight_kg"] + row["weight_kg"], 3)

    lines = []
    for line in merged.values():
        line["line_total"] = round(line["weight_kg"] * line["price_per_kg"], 2)
        lines.append(line)

    result = record_sale(
        db,
        customer_id,
        lines,
        paid,
        round_off,
        pickup_billed=True,
        pickup_ids=[row["id"] for row in rows],
        payment_method=request.form.get("method"),
    )
    if result.get("error"):
        note(result["error"], "error")
        return redirect(url_for("customer_detail", customer_id=customer_id))

    note(
        f"Master receipt {result['sale_id']:05d} for {customer['name']}: "
        f"{len(rows)} pickup{'' if len(rows) == 1 else 's'} billed "
        f"{CURRENCY} {money(result['subtotal'])}. Due now "
        f"{CURRENCY} {money(customer_balance(db, customer_id))}."
    )
    return redirect(url_for("receipt", sale_id=result["sale_id"], print=1))


@app.post("/sales/<int:sale_id>/void")
def void_sale(sale_id):
    """Void a saved receipt without deleting anything.

    The sale row and its lines stay exactly as they were saved, so the record
    of what was printed is never lost; `voided_at` takes the bill out of the
    customer's due and out of every sales total. Challans that bill covered go
    back to being unbilled, so a corrected master receipt can be generated
    again.
    """
    db = get_db()
    sale = db.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()
    if sale is None:
        abort(404)
    back = url_for("customer_detail", customer_id=sale["customer_id"])
    if request.form.get("back") == "receipt":
        back = url_for("receipt", sale_id=sale_id)
    if sale["voided_at"] is not None:
        note(f"INV-{sale_id:05d} is already void.", "error")
        return redirect(back)

    reason = (request.form.get("reason") or "").strip()[:200]
    due_before = customer_balance(db, sale["customer_id"])
    released = db.execute(
        "SELECT COUNT(*) FROM pickups WHERE sale_id = ?", (sale_id,)
    ).fetchone()[0]

    db.execute(
        "UPDATE sales SET voided_at = ?, void_reason = ?"
        " WHERE id = ? AND voided_at IS NULL",
        (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            reason or "Voided from the ledger",
            sale_id,
        ),
    )
    # The challans this bill covered are unbilled again: re-generate the master
    # receipt after fixing whatever was wrong.
    db.execute("UPDATE pickups SET sale_id = NULL WHERE sale_id = ?", (sale_id,))
    db.commit()

    due_after = customer_balance(db, sale["customer_id"])
    message = (
        f"INV-{sale_id:05d} voided: {CURRENCY} {money(sale['subtotal'])} bill"
        f" removed from the account. Due went from "
        f"{CURRENCY} {money(due_before)} to {CURRENCY} {money(due_after)}."
    )
    if released:
        message += (
            f" {released} challan pickup{'' if released == 1 else 's'} back in"
            " pending daily bills."
        )
    note(message)
    # Voiding moves money: snapshot it if the hour is up.
    create_backup()
    return redirect(back)


# The tables a customer profile can void a money row in. Names are constants,
# never anything from the request.
VOIDABLE_ROWS = {
    "payments": "payment",
    "scrap_payments": "scrap entry",
    "ledger_adjustments": "ledger adjustment",
    "credit_payouts": "credit payout",
}


def void_money_row(table, row_id, label):
    """Void one payment / scrap entry / ledger adjustment, with a reason.

    Same shape as voiding a receipt: the row is kept exactly as it was saved
    and `voided_at` takes it out of the due, the day's cash and the totals.
    """
    db = get_db()
    row = db.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        abort(404)
    back = url_for("customer_detail", customer_id=row["customer_id"])
    if row["voided_at"] is not None:
        note(f"That {label} is already void.", "error")
        return redirect(back)
    reason = required_reason()
    if not reason:
        note(f"Type why this {label} is being voided.", "error")
        return redirect(back)

    due_before = customer_balance(db, row["customer_id"])
    db.execute(
        f"UPDATE {table} SET voided_at = ?, void_reason = ?"
        " WHERE id = ? AND voided_at IS NULL",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), reason, row_id),
    )
    db.commit()
    due_after = customer_balance(db, row["customer_id"])
    note(
        f"{label.capitalize()} voided: {reason}. Due went from "
        f"{CURRENCY} {money(due_before)} to {CURRENCY} {money(due_after)}."
    )
    create_backup()
    return redirect(back)


@app.post("/payments/<int:payment_id>/void")
def void_payment(payment_id):
    return void_money_row("payments", payment_id, "payment")


@app.post("/scrap-payments/<int:entry_id>/void")
def void_scrap_payment(entry_id):
    return void_money_row("scrap_payments", entry_id, "scrap entry")


@app.post("/ledger-adjustments/<int:entry_id>/void")
def void_ledger_adjustment(entry_id):
    return void_money_row("ledger_adjustments", entry_id, "ledger adjustment")


@app.post("/credit-payouts/<int:payout_id>/void")
def void_credit_payout(payout_id):
    return void_money_row("credit_payouts", payout_id, "credit payout")


@app.post("/customers/<int:customer_id>/payouts")
def record_credit_payout(customer_id):
    """Hand a customer's credit back as cash/bKash/bank, with no new bill."""
    db = get_db()
    customer = load_customer(db, customer_id)
    if customer is None:
        abort(404)

    credit = customer_account(db, customer_id)["credit"]
    if credit <= 0:
        note(
            f"{customer['name']} has no credit balance to pay out.",
            "error",
        )
        return redirect(url_for("customer_detail", customer_id=customer_id))

    amount = round(finite_number(request.form.get("amount"), 0), 2)
    if amount <= 0:
        note("Enter a payout amount greater than 0.", "error")
        return redirect(url_for("customer_detail", customer_id=customer_id))
    # Handing back more than the credit would flip the account into due for
    # money that never bought anything, so the credit is the ceiling.
    if amount > credit + 0.005:
        note(
            f"Only {CURRENCY} {money(credit)} credit is on the account.",
            "error",
        )
        return redirect(url_for("customer_detail", customer_id=customer_id))
    amount = round(min(amount, credit), 2)

    method = method_of(request.form.get("method"))
    cursor = db.execute(
        "INSERT INTO credit_payouts (customer_id, amount, method, note, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            customer_id,
            amount,
            method,
            (request.form.get("note") or "").strip(),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    db.commit()
    payout_id = cursor.lastrowid
    left = customer_account(db, customer_id)["credit"]
    note(
        f"Credit paid out: {CURRENCY} {money(amount)} ({method}) to "
        f"{customer['name']}. "
        + (
            f"Credit left: {CURRENCY} {money(left)}."
            if left > 0
            else "Credit is now clear."
        )
    )
    create_backup()
    return redirect(
        url_for("customer_detail", customer_id=customer_id, slip=payout_id)
    )


@app.get("/credit-payouts/<int:payout_id>/slip")
def payout_slip(payout_id):
    """80mm customer + shop copy of a credit payout."""
    db = get_db()
    payout = db.execute(
        "SELECT * FROM credit_payouts WHERE id = ?", (payout_id,)
    ).fetchone()
    if payout is None:
        abort(404)
    return render_template(
        "payout_slip.html",
        payout=payout,
        customer=load_customer(db, payout["customer_id"]),
        slip_no=f"PAY-{payout_id:05d}",
        credit_left=customer_account(db, payout["customer_id"])["credit"],
        autoprint=request.args.get("print") == "1",
    )


@app.post("/customers/<int:customer_id>/payments")
def record_payment(customer_id):
    db = get_db()
    customer = load_customer(db, customer_id)
    if customer is None:
        abort(404)
    amount = round(finite_number(request.form.get("amount"), 0), 2)

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

    method = method_of(request.form.get("method"))
    db.execute(
        "INSERT INTO payments (customer_id, amount, method, note, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            customer_id,
            recorded,
            method,
            (request.form.get("note") or "").strip(),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    db.commit()
    message = (
        f"Payment of {CURRENCY} {money(recorded)} ({method}) recorded for "
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
        # A scrap type left off the form means none of it was handed over.
        raw = (request.form.get(f"{name.lower()}_kg") or "").strip()
        weight = 0.0 if not raw else finite_number(raw)
        weight = -1 if weight is None else round(weight, 3)
        if weight < 0:
            note(f"{name} scrap weight must be 0 or more.", "error")
            return redirect(url_for("customer_detail", customer_id=customer_id))
        kg[name] = weight

    if not any(kg.values()):
        note("Enter the weight of at least one scrap type.", "error")
        return redirect(url_for("customer_detail", customer_id=customer_id))

    total = round(sum(kg[name] * rates.get(name, 0) for name in SCRAP_TYPES), 2)
    # Cash handed over for the scrap comes out of the drawer and never pays the
    # account; 0 keeps the whole value as credit against the due.
    payout = round(finite_number(request.form.get("payout"), 0), 2)
    if payout < 0 or payout > total:
        note(
            f"Cash paid out must be between 0 and the scrap value "
            f"({CURRENCY} {money(total)}).",
            "error",
        )
        return redirect(url_for("customer_detail", customer_id=customer_id))
    method = method_of(request.form.get("method"))

    db.execute(
        "INSERT INTO scrap_payments (customer_id, black_kg, white_kg,"
        " black_rate, white_rate, total, payout, method, note, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            customer_id,
            kg["Black"],
            kg["White"],
            round(rates.get("Black", 0), 2),
            round(rates.get("White", 0), 2),
            total,
            payout,
            method,
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
    if payout > 0:
        message += f"{CURRENCY} {money(payout)} paid out by {method}. "
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
    data["rates"] = customer_rates(db, customer_id)
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
    opening = round(finite_number(payload.get("opening_due"), 0), 2)
    if opening:
        add_ledger_adjustment(db, cur.lastrowid, opening)
    db.commit()
    data = dict(load_customer(db, cur.lastrowid))
    account = customer_account(db, cur.lastrowid)
    data["balance"] = account["due"]
    data["credit"] = account["credit"]
    return jsonify(data), 201


@app.post("/api/challans")
def api_create_challan():
    """Weight-only slip: log what left the shop, bill nothing yet."""
    payload = request.get_json(silent=True) or {}
    customer_id = payload.get("customer_id")
    raw_items = payload.get("items") or []
    if not customer_id:
        return jsonify({"error": "Select or create a customer first."}), 400
    if not raw_items:
        return jsonify({"error": "Add at least one product line."}), 400

    db = get_db()
    customer = load_customer(db, customer_id)
    if customer is None:
        return jsonify({"error": "Customer not found."}), 400

    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            return jsonify({"error": "Every line needs a product and a weight."}), 400
        try:
            product_id = int(raw.get("product_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "Every line needs a product and a weight."}), 400
        weight = finite_number(raw.get("weight_kg"))
        if weight is None or round(weight, 3) <= 0:
            return jsonify({"error": "Weight must be greater than 0 kg."}), 400
        product = db.execute(
            "SELECT id, name, price_per_kg FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
        if product is None:
            return jsonify({"error": "A selected product no longer exists."}), 400
        items.append(
            {
                "product_id": product["id"],
                "product_name": product["name"],
                "weight_kg": round(weight, 3),
                "default_price": round(float(product["price_per_kg"]), 2),
            }
        )

    now = datetime.now()
    picked_on = now.strftime("%Y-%m-%d")
    picked_at = now.strftime("%Y-%m-%d %H:%M:%S")
    try:
        db.execute("BEGIN IMMEDIATE")
        # Next slip number, read inside the write transaction so two slips can
        # never share a number.
        challan_no = db.execute(
            "SELECT COALESCE(MAX(challan_no), 0) + 1 AS next FROM pickups"
        ).fetchone()["next"]
        db.executemany(
            "INSERT INTO pickups (challan_no, customer_id, product_id,"
            " product_name, weight_kg, default_price, picked_on, picked_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    challan_no,
                    customer_id,
                    item["product_id"],
                    item["product_name"],
                    item["weight_kg"],
                    item["default_price"],
                    picked_on,
                    picked_at,
                )
                for item in items
            ],
        )
        db.commit()
    except sqlite3.Error as exc:
        db.rollback()
        return jsonify({"error": f"Could not save the slip: {exc}"}), 500

    return (
        jsonify(
            {
                "challan_no": challan_no,
                "challan_url": url_for("challan", challan_no=challan_no),
                "total_weight": round(
                    sum(item["weight_kg"] for item in items), 3
                ),
                "picked_on": picked_on,
            }
        ),
        201,
    )


# ---------------------------------------------------------------------------
# Held receipts (drafts). An unfinished bill is parked in SQLite so it survives
# a refresh, a closed tab or an app restart. Drafts are read by the billing
# screen only: they never touch customer due, the ledger or Daily Sales.
# ---------------------------------------------------------------------------
DRAFT_LABEL_MAX = 60


def draft_line_items(db, raw_items):
    """Lines for a draft, always priced from the database, never the browser.

    Returns (items, error). A line with only some of its fields filled in is
    reported, never quietly dropped, so holding cannot lose part of a bill.
    Fully blank lines are ignored.
    """
    items = []
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            continue
        product_raw = raw.get("product_id")
        weight_raw = raw.get("weight_kg")
        if product_raw in (None, "") and weight_raw in (None, ""):
            continue
        try:
            product_id = int(product_raw)
        except (TypeError, ValueError):
            return None, "Every line needs a product. Pick one or remove the line."
        weight = finite_number(weight_raw)
        if weight is None:
            return None, "Every line needs a weight in kg. Enter it or remove the line."
        weight = round(weight, 3)
        if weight <= 0:
            return None, "Weight must be greater than 0 kg. Fix the line or remove it."
        product = db.execute(
            "SELECT id, name, price_per_kg FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
        if product is None:
            return None, (
                "A product on this receipt no longer exists."
                " Pick another product or remove the line."
            )
        items.append(
            {
                "product_id": product["id"],
                "product_name": product["name"],
                "weight_kg": weight,
                "price_per_kg": product["price_per_kg"],
                "line_total": round(weight * product["price_per_kg"], 2),
            }
        )
    return items, None


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

    items, error = draft_line_items(db, payload.get("items"))
    if error:
        return jsonify({"error": error}), 400
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
        return max(0.0, round(finite_number(value, 0.0), 2))

    def kilos(value):
        return max(0.0, round(finite_number(value, 0.0), 3))

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


def record_sale(
    db,
    customer_id,
    lines,
    paid,
    requested_round_off,
    scrap=None,
    draft_id=0,
    pickup_billed=False,
    pickup_ids=(),
    payment_method=None,
):
    """Write one bill and its lines: the single place the due math lives.

    Save & Print and the end-of-day master receipt both come through here, so
    previous due, credit on account, scrap, round-off and change behave the
    same. Returns the saved figures, or {"error": ...} if SQLite refuses.
    """
    scrap = scrap or {}
    subtotal = round(sum(line["line_total"] for line in lines), 2)
    scrap_total = round(
        sum(float(part.get("value") or 0) for part in scrap.values()), 2
    )

    black = scrap.get("Black", {})
    white = scrap.get("White", {})

    try:
        db.execute("BEGIN IMMEDIATE")
        # The duplicate check belongs inside this write transaction. BEGIN
        # IMMEDIATE takes the write lock before the read, so two copies of one
        # submit arriving at the same moment are serialised: the first inserts,
        # the second waits for it to commit, then finds it here and writes
        # nothing. A check made before the transaction could miss it and book
        # the bill twice.
        if not pickup_billed:
            repeated = repeat_of_recent_sale(
                db, customer_id, subtotal, paid, scrap_total
            )
            if repeated is not None:
                # Still tidy up the draft the repeat carried along.
                if draft_id:
                    db.execute(
                        "DELETE FROM draft_receipts WHERE id = ?", (draft_id,)
                    )
                db.commit()
                answer = sale_response(repeated)
                answer["duplicate"] = True
                return answer

        # The account is read here, inside the write transaction, for the same
        # reason. A second bill for this customer saved at the same moment
        # waits for this transaction to commit, then takes its previous due
        # from the balance this bill left behind. Read before the lock, both
        # bills would see the same old due and one of them would be lost.
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
        # A small leftover the shop chooses to waive is settled on this bill: it
        # is never allowed to exceed the real remainder and never becomes due.
        round_off = round(min(max(0.0, requested_round_off), remainder), 2)
        new_due = round(remainder - round_off, 2)
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cur = db.execute(
            "INSERT INTO sales (customer_id, subtotal, previous_due, grand_total,"
            " paid, new_due, created_at, scrap_black_kg, scrap_white_kg,"
            " scrap_black_rate, scrap_white_rate, scrap_total,"
            " credit_used, cash_applied, round_off, pickup_billed,"
            " payment_method)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (customer_id, subtotal, previous_due, grand_total, paid, new_due,
             created_at, black.get("kg", 0), white.get("kg", 0),
             black.get("rate", 0), white.get("rate", 0), scrap_total,
             credit_used, cash_applied, round_off,
             1 if pickup_billed else 0, method_of(payment_method)),
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
        # The challans this bill covers stop being unbilled right here.
        if pickup_ids:
            marks = ",".join("?" * len(pickup_ids))
            db.execute(
                f"UPDATE pickups SET sale_id = ? WHERE id IN ({marks})",
                (sale_id, *pickup_ids),
            )
        db.commit()
    except sqlite3.Error as exc:
        db.rollback()
        return {"error": f"Could not save the sale: {exc}"}

    # Snapshot right after a receipt is saved, at most once an hour.
    create_backup()

    return {
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
        "payment_method": method_of(payment_method),
        "scrap_credit": scrap_credit,
        "change": change,
        "credit_after": round(credit_before - credit_used + scrap_credit, 2),
    }


# The flag in billing.js stops most double-clicks, but the save endpoint must
# not depend on the browser for that. The same bill for the same customer
# arriving again within this many seconds is one click that reached the server
# twice, not two real sales.
DUPLICATE_SAVE_WINDOW_SECONDS = 10


def sale_response(row):
    """The JSON the billing screen reads, rebuilt from a saved sale row."""
    return {
        "sale_id": row["id"],
        "receipt_url": url_for("receipt", sale_id=row["id"]),
        "subtotal": row["subtotal"],
        "previous_due": row["previous_due"],
        "grand_total": row["grand_total"],
        "paid": row["paid"],
        "scrap_total": row["scrap_total"],
        "new_due": row["new_due"],
        "round_off": row["round_off"],
        "credit_used": row["credit_used"],
        "cash_applied": row["cash_applied"],
        "payment_method": row["payment_method"],
    }


def repeat_of_recent_sale(db, customer_id, subtotal, paid, scrap_total):
    """The bill this request already saved, when it is a repeat submit.

    Customer, subtotal, tendered cash and scrap value all have to match, and
    cannot differ between two copies of one submit, so a second identical
    request is answered with the receipt the first one produced instead of a
    second bill. A genuinely different bill never matches.
    """
    cutoff = f"-{DUPLICATE_SAVE_WINDOW_SECONDS} seconds"
    return db.execute(
        "SELECT * FROM sales"
        " WHERE customer_id = ? AND subtotal = ? AND paid = ?"
        " AND scrap_total = ? AND pickup_billed = 0 AND voided_at IS NULL"
        " AND created_at >= datetime('now', 'localtime', ?)"
        " ORDER BY id DESC LIMIT 1",
        (customer_id, subtotal, paid, scrap_total, cutoff),
    ).fetchone()


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

    paid = finite_number(payload.get("paid") or 0)
    if paid is None:
        return jsonify({"error": "Amount paid must be a number."}), 400
    paid = round(paid, 2)
    requested_round_off = finite_number(payload.get("round_off") or 0)
    if requested_round_off is None:
        return jsonify({"error": "Round-off amount must be a number."}), 400
    requested_round_off = round(requested_round_off, 2)
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
    if not isinstance(raw_scrap, dict):
        return jsonify({"error": "Scrap weights must be numbers."}), 400
    scrap = {}
    for name in SCRAP_TYPES:
        field = f"{name.lower()}_kg"
        kg = finite_number(raw_scrap.get(field) or 0)
        if kg is None:
            return jsonify({"error": f"{name} scrap weight must be a number."}), 400
        kg = round(kg, 3)
        if kg < 0:
            return jsonify({"error": f"{name} scrap weight cannot be negative."}), 400
        rate = round(float(rates.get(name, 0)), 2)
        scrap[name] = {"kg": kg, "rate": rate, "value": round(kg * rate, 2)}

    # Rebuild the lines from this customer's special rates, else the shop
    # prices - never trust client totals or client prices.
    special_rates = customer_rates(db, customer_id)
    lines = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            return jsonify({"error": "Every line needs a product and a weight."}), 400
        try:
            product_id = int(raw.get("product_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "Every line needs a product and a weight."}), 400
        weight = finite_number(raw.get("weight_kg"))
        if weight is None:
            return jsonify({"error": "Every line needs a product and a weight."}), 400
        weight = round(weight, 3)
        if weight <= 0:
            return jsonify({"error": "Weight must be greater than 0 kg."}), 400

        product = db.execute(
            "SELECT * FROM products WHERE id = ?", (product_id,)
        ).fetchone()
        if product is None:
            return jsonify({"error": "A selected product no longer exists."}), 400

        price = price_for(special_rates, product)
        lines.append(
            {
                "product_id": product["id"],
                "product_name": product["name"],
                "weight_kg": weight,
                "price_per_kg": price,
                "line_total": round(weight * price, 2),
            }
        )

    # record_sale hands back the first receipt instead of a second bill when
    # this is a repeat submit, checked inside its write transaction.
    result = record_sale(
        db,
        customer_id,
        lines,
        paid,
        requested_round_off,
        scrap=scrap,
        draft_id=draft_id,
        payment_method=payload.get("payment_method"),
    )
    if result.get("error"):
        return jsonify({"error": result["error"]}), 500
    if result.get("duplicate"):
        return jsonify(result), 200
    result["duplicate"] = False
    return jsonify(result), 201


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
