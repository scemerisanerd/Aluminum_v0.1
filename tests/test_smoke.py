"""Smoke test for the Aluminum POS — the money paths that must not regress.

Covers, in one file: saving a receipt (the double-click guard in front of it,
and the server's own refusal to book one bill twice, including two submits
fired at the same moment), weight-only challans and the end-of-day master receipt built from them,
held drafts, due payments from the customer page, and the arithmetic that ties
them together (previous due, credit, scrap, round-off, change).

It never touches the shop's real database: on start it copies data/shop.db into
a temporary folder with sqlite3's backup API, points the app at that copy and
uses a throwaway backups folder. Every test then wipes the copy's transactions
and seeds two known products and one customer, so the figures asserted here are
exact instead of depending on what the shop sold today.

Run it from the project root:

    ./.venv/Scripts/python.exe tests/test_smoke.py          (Windows)
    ./.venv/bin/python tests/test_smoke.py                  (macOS / Linux)

Every test is independent; the last one checks the real database file was
never written to.
"""

import hashlib
import io
import pathlib
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as pos  # noqa: E402  (path set up above so `import app` works)

# Captured before the app is pointed at the copy.
REAL_DB = pathlib.Path(pos.DB_PATH)
REAL_BACKUP_DIR = pathlib.Path(pos.BACKUP_DIR)
TMP = None
REAL_DB_HASH = None


def file_hash(path):
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def setUpModule():
    global TMP, REAL_DB_HASH
    TMP = pathlib.Path(tempfile.mkdtemp(prefix="pos-smoke-"))
    REAL_DB_HASH = file_hash(REAL_DB)

    copy = TMP / "shop.db"
    if REAL_DB.exists():
        # A real copy (schema, settings, price history), never the live file.
        source = sqlite3.connect(f"file:{REAL_DB.as_posix()}?mode=ro", uri=True)
        target = sqlite3.connect(copy)
        source.backup(target)
        target.close()
        source.close()
    pos.DB_PATH = str(copy)
    pos.BACKUP_DIR = str(TMP / "backups")
    # The restore probe file is written next to the data folder, so send that
    # to the temp directory too.
    pos.DATA_DIR = str(TMP)
    pos.init_db()


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class SmokeTest(unittest.TestCase):
    """One shared fixture; each test resets the copy to a known state."""

    def setUp(self):
        self.client = pos.app.test_client()
        self.product = {}
        self.customer_id = 0
        self.reset()

    # --- fixtures / tiny helpers -------------------------------------------

    @contextmanager
    def sql(self):
        con = sqlite3.connect(pos.DB_PATH, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def rows(self, query, params=()):
        with self.sql() as con:
            return con.execute(query, params).fetchall()

    def one(self, query, params=()):
        found = self.rows(query, params)
        return found[0] if found else None

    def scalar(self, query, params=()):
        with self.sql() as con:
            return con.execute(query, params).fetchone()[0]

    def reset(self):
        """Empty every transaction and seed two products and one customer."""
        with self.sql() as con:
            for table in (
                "sale_items",
                "sales",
                "pickups",
                "payments",
                "scrap_payments",
                "ledger_adjustments",
                "credit_payouts",
                "draft_receipts",
                "customer_rates",
                "scrap_rates",
                "purchases",
                "day_closes",
                "products",
                "customers",
            ):
                con.execute(f"DELETE FROM {table}")
            con.executemany(
                "INSERT INTO products (name, category, price_per_kg)"
                " VALUES (?, ?, ?)",
                [("Smoke Sheet", "Sheet", 300.0), ("Smoke Pipe", "Pipe", 250.0)],
            )
            con.executemany(
                "INSERT INTO scrap_rates (name, price_per_kg) VALUES (?, ?)",
                [("Black", 190.0), ("White", 160.0)],
            )
            con.execute(
                "INSERT INTO customers (name, phone, shop_name, address, created_at)"
                " VALUES ('Smoke Customer', '', 'Smoke Shop', '',"
                " '2026-01-01 09:00:00')"
            )
        for row in self.rows("SELECT id, name FROM products"):
            self.product[row["name"]] = row["id"]
        self.customer_id = self.scalar("SELECT id FROM customers")

    def save(self, items, paid, **extra):
        payload = {
            "customer_id": self.customer_id,
            "items": items,
            "paid": paid,
        }
        payload.update(extra)
        return self.client.post("/api/sales", json=payload)

    def account(self):
        with self.sql() as con:
            return pos.customer_account(con, self.customer_id)

    def sale(self, sale_id):
        return self.one("SELECT * FROM sales WHERE id = ?", (sale_id,))

    def today(self):
        # The app stamps rows with the local clock, so the test must read the
        # same clock. SQLite's date('now') is UTC and drags a day behind after
        # local midnight.
        return datetime.now().strftime("%Y-%m-%d")

    def daily(self, query=""):
        response = self.client.get("/daily" + query)
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def daily_weight(self):
        """Total kilos on the Daily Sales card (flash text is not parsed)."""
        html = self.daily()
        found = re.search(r"Total aluminum sold</span>\s*<b>([\d.]+) kg</b>", html)
        self.assertIsNotNone(found, "no weight total on the Daily Sales page")
        return float(found.group(1))

    def money_after(self, html, label):
        """The Tk. figure rendered on the Daily Sales card for `label`."""
        found = re.search(
            re.escape(label) + r"</span>\s*<b>Tk\. (-?[\d,]+\.\d\d)</b>", html
        )
        self.assertIsNotNone(found, f"no {label} figure on the Daily Sales page")
        return float(found.group(1).replace(",", ""))

    # --- saving a receipt --------------------------------------------------

    def test_saved_receipt_money_maths(self):
        response = self.save([{"product_id": self.product["Smoke Sheet"], "weight_kg": 10}], 1000)
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        figures = response.get_json()

        self.assertAlmostEqual(figures["subtotal"], 3000.00, places=2)
        self.assertAlmostEqual(figures["previous_due"], 0.00, places=2)
        self.assertAlmostEqual(figures["grand_total"], 3000.00, places=2)
        self.assertAlmostEqual(figures["cash_applied"], 1000.00, places=2)
        self.assertAlmostEqual(figures["change"], 0.00, places=2)
        self.assertAlmostEqual(figures["new_due"], 2000.00, places=2)

        stored = self.sale(figures["sale_id"])
        self.assertAlmostEqual(stored["subtotal"], 3000.00, places=2)
        self.assertAlmostEqual(stored["paid"], 1000.00, places=2)
        self.assertAlmostEqual(stored["cash_applied"], 1000.00, places=2)
        self.assertAlmostEqual(stored["new_due"], 2000.00, places=2)
        self.assertIsNone(stored["voided_at"])

        line = self.one("SELECT * FROM sale_items WHERE sale_id = ?", (figures["sale_id"],))
        self.assertAlmostEqual(line["weight_kg"], 10.000, places=3)
        self.assertAlmostEqual(line["price_per_kg"], 300.00, places=2)
        self.assertAlmostEqual(line["line_total"], 3000.00, places=2)

        self.assertAlmostEqual(self.account()["due"], 2000.00, places=2)

        receipt = self.client.get(f"/receipt/{figures['sale_id']}")
        self.assertEqual(receipt.status_code, 200)

        html = self.daily()
        self.assertEqual(self.money_after(html, "Cash collected"), 1000.00)
        self.assertEqual(self.money_after(html, "Total sales value"), 3000.00)
        self.assertEqual(self.money_after(html, "New due"), 2000.00)

    def test_repeat_submit_is_refused_by_the_server(self):
        """A second identical POST books nothing, with no browser involved.

        billing.js stops most double-clicks, but the endpoint must hold on its
        own: the repeat is answered with the first receipt, so the due moves
        once and only one sale exists.
        """
        items = [{"product_id": self.product["Smoke Sheet"], "weight_kg": 3}]
        first = self.save(items, 500)
        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        self.assertFalse(first.get_json()["duplicate"])

        repeat = self.save(items, 500)
        self.assertEqual(repeat.status_code, 200, "the repeat must not be saved again")
        self.assertTrue(repeat.get_json()["duplicate"])
        self.assertEqual(
            repeat.get_json()["sale_id"], first.get_json()["sale_id"],
            "the repeat must answer with the first receipt",
        )
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sales"), 1)
        self.assertAlmostEqual(self.account()["due"], 400.00, places=2)  # 900 - 500
        self.assertEqual(self.money_after(self.daily(), "Total sales value"), 900.00)

        # A different bill straight afterwards is a real second sale.
        other = self.save(
            [{"product_id": self.product["Smoke Sheet"], "weight_kg": 4}], 500
        )
        self.assertEqual(other.status_code, 201, other.get_data(as_text=True))
        self.assertFalse(other.get_json()["duplicate"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sales"), 2)

    def test_two_identical_submits_at_once_book_one_sale(self):
        """Two copies of one bill fired at the same moment make one sale.

        This only holds if the duplicate check runs inside the write
        transaction: checked before it, both requests could read "no such bill
        yet" and both go on to insert one. The barrier releases both threads
        together so the requests are in the server at the same time.
        """
        payload = {
            "customer_id": self.customer_id,
            "items": [{"product_id": self.product["Smoke Sheet"], "weight_kg": 5}],
            "paid": 500,
        }
        barrier = threading.Barrier(2, timeout=15)
        answers = []
        failures = []
        lock = threading.Lock()

        def submit():
            client = pos.app.test_client()
            try:
                barrier.wait()
                response = client.post("/api/sales", json=payload)
                with lock:
                    answers.append(response)
            except BaseException as exc:  # noqa: BLE001 - reported below
                with lock:
                    failures.append(exc)

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(failures, [])
        self.assertEqual(len(answers), 2, "both requests must have answered")
        self.assertEqual(
            sorted(response.status_code for response in answers),
            [200, 201],
            "one request saves, the other is answered with the first receipt",
        )

        bodies = [response.get_json() for response in answers]
        self.assertEqual(
            len({body["sale_id"] for body in bodies}),
            1,
            "both answers must point at the same receipt",
        )
        self.assertEqual(sum(1 for body in bodies if body["duplicate"]), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sales"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sale_items"), 1)
        # One bill, so the due moved once: 1500 - 500.
        self.assertAlmostEqual(self.account()["due"], 1000.00, places=2)
        self.assertEqual(self.money_after(self.daily(), "Total sales value"), 1500.00)

    def test_two_different_bills_at_once_keep_both_dues(self):
        """Two bills for one customer saved at once lose neither balance.

        Each bill carries the due the other one left behind, so the account
        after both is the sum of both, and the second bill's grand total
        includes the first bill's due. That only holds if the account is read
        inside the write transaction; read before it, both bills would take the
        same old due and one of them would be lost.
        """
        sheet = {
            "customer_id": self.customer_id,
            "items": [{"product_id": self.product["Smoke Sheet"], "weight_kg": 5}],
            "paid": 500,
        }
        pipe = {
            "customer_id": self.customer_id,
            "items": [{"product_id": self.product["Smoke Pipe"], "weight_kg": 2}],
            "paid": 0,
        }
        barrier = threading.Barrier(2, timeout=15)
        answers = []
        failures = []
        lock = threading.Lock()

        def submit(payload):
            client = pos.app.test_client()
            try:
                barrier.wait()
                response = client.post("/api/sales", json=payload)
                with lock:
                    answers.append(response)
            except BaseException as exc:  # noqa: BLE001 - reported below
                with lock:
                    failures.append(exc)

        threads = [
            threading.Thread(target=submit, args=(payload,))
            for payload in (sheet, pipe)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(failures, [])
        self.assertEqual(len(answers), 2, "both requests must have answered")
        self.assertEqual(
            [response.status_code for response in answers],
            [201, 201],
            "both bills must save: "
            + " | ".join(response.get_data(as_text=True) for response in answers),
        )

        sales = self.rows("SELECT * FROM sales ORDER BY id")
        self.assertEqual(len(sales), 2)
        # The first bill opened on a clean account, whichever bill it was; the
        # second carries the due the first one left behind.
        first, second = sales
        self.assertEqual(round(first["previous_due"], 2), 0.00)
        self.assertAlmostEqual(
            second["previous_due"],
            first["new_due"],
            places=2,
            msg="the second bill must carry the first bill's due",
        )
        for row in sales:
            self.assertAlmostEqual(
                row["grand_total"],
                round(row["subtotal"] + row["previous_due"], 2),
                places=2,
                msg="each bill must carry the due the other left behind",
            )

        # Neither due was dropped: 1000 from the sheet bill, 500 from the pipe.
        self.assertAlmostEqual(self.account()["due"], 1500.00, places=2)
        self.assertEqual(self.money_after(self.daily(), "Total sales value"), 2000.00)

    def test_prices_come_from_the_shop_not_the_browser(self):
        response = self.client.post(
            "/api/sales",
            json={
                "customer_id": self.customer_id,
                "paid": 0,
                "items": [
                    {
                        "product_id": self.product["Smoke Sheet"],
                        "weight_kg": 2,
                        "price_per_kg": 1.0,
                        "line_total": 2.0,
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertAlmostEqual(response.get_json()["subtotal"], 600.00, places=2)

    def test_save_double_click_guard_is_still_in_the_billing_script(self):
        """The browser must still refuse the second click of a double-click.

        The server-side copy of this protection is tested below; this one pins
        the guard in static/js/billing.js, which a Python test cannot click: a
        flag is raised synchronously ahead of the first `await` and cleared
        afterwards, and the Save button is disabled while the save runs.
        """
        js = (ROOT / "static" / "js" / "billing.js").read_text(encoding="utf-8")
        early_return = js.find("if (state.saving) return;")
        raised = js.find("state.saving = true;")
        cleared = js.find("state.saving = false;")
        self.assertNotEqual(early_return, -1, "the double-click guard was removed")
        self.assertNotEqual(raised, -1, "the save flag is never raised")
        self.assertNotEqual(cleared, -1, "the save flag is never cleared")
        self.assertLess(early_return, raised, "the guard must run before the save")
        self.assertLess(raised, cleared, "the flag must be cleared after the save")

        disabled = js.find("el.savePrint.disabled = true;")
        posted = js.find('fetch("/api/sales"')
        re_enabled = js.find("el.savePrint.disabled = false;")
        self.assertNotEqual(disabled, -1, "the Save button is never disabled")
        self.assertLess(disabled, posted, "disable the button before the POST")
        self.assertGreater(re_enabled, posted, "the button must be usable again")

        # Creating a customer has its own guard against a double submit.
        self.assertIn('if (form.dataset.busy === "1") return;', js)

    # --- money that is not a plain cash sale -------------------------------

    def test_overpayment_is_change_and_never_credit(self):
        figures = self.save(
            [{"product_id": self.product["Smoke Pipe"], "weight_kg": 4}], 5000
        ).get_json()
        self.assertAlmostEqual(figures["subtotal"], 1000.00, places=2)
        self.assertAlmostEqual(figures["cash_applied"], 1000.00, places=2)
        self.assertAlmostEqual(figures["change"], 4000.00, places=2)
        self.assertAlmostEqual(figures["new_due"], 0.00, places=2)
        account = self.account()
        self.assertAlmostEqual(account["due"], 0.00, places=2)
        self.assertAlmostEqual(account["credit"], 0.00, places=2)

    def test_round_off_waives_only_what_is_actually_left(self):
        figures = self.save(
            [{"product_id": self.product["Smoke Sheet"], "weight_kg": 4}],
            1000,
            round_off=999,
        ).get_json()
        self.assertAlmostEqual(figures["grand_total"], 1200.00, places=2)
        self.assertAlmostEqual(figures["round_off"], 200.00, places=2)
        self.assertAlmostEqual(figures["new_due"], 0.00, places=2)
        self.assertAlmostEqual(self.account()["due"], 0.00, places=2)

        refused = self.save(
            [{"product_id": self.product["Smoke Sheet"], "weight_kg": 4}], 0, round_off=-5
        )
        self.assertEqual(refused.status_code, 400, "a negative round-off must be refused")

    def test_previous_due_is_carried_onto_the_next_bill(self):
        self.save([{"product_id": self.product["Smoke Sheet"], "weight_kg": 1}], 100)
        second = self.save(
            [{"product_id": self.product["Smoke Sheet"], "weight_kg": 1}], 0
        ).get_json()
        self.assertAlmostEqual(second["previous_due"], 200.00, places=2)
        self.assertAlmostEqual(second["grand_total"], 500.00, places=2)
        self.assertAlmostEqual(second["new_due"], 500.00, places=2)
        self.assertAlmostEqual(self.account()["due"], 500.00, places=2)

    def test_scrap_pays_the_bill(self):
        # 20 kg x 300 = 6000 bill, 5 kg x 190 = 950 of scrap handed over.
        figures = self.save(
            [{"product_id": self.product["Smoke Sheet"], "weight_kg": 20}],
            0,
            scrap={"black_kg": 5},
        ).get_json()
        self.assertAlmostEqual(figures["grand_total"], 6000.00, places=2)
        self.assertAlmostEqual(figures["scrap_total"], 950.00, places=2)
        self.assertAlmostEqual(figures["cash_applied"], 0.00, places=2)
        self.assertAlmostEqual(figures["credit_used"], 0.00, places=2)
        self.assertAlmostEqual(figures["new_due"], 5050.00, places=2)
        self.assertAlmostEqual(self.account()["due"], 5050.00, places=2)

    def test_spare_scrap_stays_as_credit_not_a_refund(self):
        # 2 kg x 300 = 600 bill settled by 5 kg x 190 = 950 of scrap.
        figures = self.save(
            [{"product_id": self.product["Smoke Sheet"], "weight_kg": 2}],
            0,
            scrap={"black_kg": 5},
        ).get_json()
        self.assertAlmostEqual(figures["scrap_total"], 950.00, places=2)
        self.assertAlmostEqual(figures["scrap_credit"], 350.00, places=2)
        self.assertAlmostEqual(figures["change"], 0.00, places=2)
        self.assertAlmostEqual(figures["new_due"], 0.00, places=2)
        account = self.account()
        self.assertAlmostEqual(account["due"], 0.00, places=2)
        self.assertAlmostEqual(account["credit"], 350.00, places=2)

    # --- voiding a saved receipt -------------------------------------------

    def test_void_removes_the_bill_from_due_and_totals(self):
        figures = self.save(
            [{"product_id": self.product["Smoke Sheet"], "weight_kg": 10}], 1000
        ).get_json()
        sale_id = figures["sale_id"]
        self.assertAlmostEqual(self.account()["due"], 2000.00, places=2)

        voided = self.client.post(
            f"/sales/{sale_id}/void", data={"reason": "wrong weight"}
        )
        self.assertEqual(voided.status_code, 302)

        stored = self.sale(sale_id)
        self.assertIsNotNone(stored["voided_at"], "the void must be recorded")
        self.assertEqual(stored["void_reason"], "wrong weight")
        # The audit trail stays: row and lines are never deleted or rewritten.
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sales"), 1)
        line = self.one("SELECT * FROM sale_items WHERE sale_id = ?", (sale_id,))
        self.assertIsNotNone(line)
        self.assertAlmostEqual(line["line_total"], 3000.00, places=2)
        self.assertAlmostEqual(stored["subtotal"], 3000.00, places=2)

        self.assertAlmostEqual(self.account()["due"], 0.00, places=2)
        html = self.daily()
        self.assertEqual(self.money_after(html, "Cash collected"), 0.00)
        self.assertEqual(self.money_after(html, "Total sales value"), 0.00)
        self.assertEqual(self.money_after(html, "New due"), 0.00)
        self.assertIn("1 voided receipt", html)

        receipt = self.client.get(f"/receipt/{sale_id}").get_data(as_text=True)
        self.assertIn("** VOID **", receipt)
        self.assertIn("wrong weight", receipt)
        self.assertNotIn("receipt-void-form", receipt, "a void receipt must not offer voiding again")

    def test_void_is_post_only_and_happens_once(self):
        sale_id = self.save(
            [{"product_id": self.product["Smoke Pipe"], "weight_kg": 2}], 500
        ).get_json()["sale_id"]

        self.assertEqual(
            self.client.get(f"/sales/{sale_id}/void").status_code,
            405,
            "voiding a receipt must not be a link anyone can click",
        )

        self.client.post(f"/sales/{sale_id}/void", data={"reason": "first reason"})
        self.client.post(f"/sales/{sale_id}/void", data={"reason": "second reason"})
        self.assertEqual(self.sale(sale_id)["void_reason"], "first reason")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sales"), 1)
        self.assertAlmostEqual(self.account()["due"], 0.00, places=2)

    def test_void_master_receipt_returns_its_challans_to_pending(self):
        self.client.post(
            "/api/challans",
            json={
                "customer_id": self.customer_id,
                "items": [{"product_id": self.product["Smoke Sheet"], "weight_kg": 4}],
            },
        )
        day = self.today()
        self.client.post(
            f"/customers/{self.customer_id}/pickups/{day}/bill", data={"paid": "500"}
        )
        sale = self.one("SELECT * FROM sales")
        self.assertAlmostEqual(self.account()["due"], 700.00, places=2)  # 1200 - 500

        self.client.post(f"/sales/{sale['id']}/void", data={"reason": "wrong price"})

        self.assertAlmostEqual(self.account()["due"], 0.00, places=2)
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM pickups WHERE sale_id IS NOT NULL"),
            0,
            "the challans must be unbilled again",
        )
        # The kilos are back on the day's weight, with no revenue behind them.
        self.assertAlmostEqual(self.daily_weight(), 4.000, places=3)
        self.assertEqual(self.money_after(self.daily(), "Total sales value"), 0.00)

        # A corrected master receipt can now be built from the same challans.
        again = self.client.post(
            f"/customers/{self.customer_id}/pickups/{day}/bill", data={"paid": "0"}
        )
        self.assertEqual(again.status_code, 302)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sales"), 2)
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM sales WHERE voided_at IS NULL"), 1
        )
        self.assertAlmostEqual(self.account()["due"], 1200.00, places=2)

    # --- challans and the end-of-day master receipt ------------------------

    def test_challan_weights_once_when_the_master_receipt_is_billed(self):
        slip = self.client.post(
            "/api/challans",
            json={
                "customer_id": self.customer_id,
                "items": [
                    {"product_id": self.product["Smoke Sheet"], "weight_kg": 4},
                    {"product_id": self.product["Smoke Pipe"], "weight_kg": 2},
                ],
            },
        )
        self.assertEqual(slip.status_code, 201, slip.get_data(as_text=True))
        self.assertAlmostEqual(slip.get_json()["total_weight"], 6.000, places=3)

        # A challan is not a sale: no money, no due, no receipt.
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sales"), 0)
        self.assertAlmostEqual(self.account()["due"], 0.00, places=2)
        self.assertEqual(self.money_after(self.daily(), "Total sales value"), 0.00)

        day = self.today()
        billed = self.client.post(
            f"/customers/{self.customer_id}/pickups/{day}/bill",
            data={"paid": "1000"},
        )
        self.assertEqual(billed.status_code, 302)

        sale = self.one("SELECT * FROM sales")
        self.assertEqual(sale["pickup_billed"], 1)
        self.assertAlmostEqual(sale["subtotal"], 1700.00, places=2)  # 4x300 + 2x250
        self.assertAlmostEqual(sale["cash_applied"], 1000.00, places=2)
        self.assertAlmostEqual(sale["new_due"], 700.00, places=2)
        self.assertAlmostEqual(self.account()["due"], 700.00, places=2)

        unbilled = self.scalar(
            "SELECT COUNT(*) FROM pickups WHERE sale_id IS NULL"
        )
        self.assertEqual(unbilled, 0, "the master receipt must claim its challans")

        # Weight is counted once: from the challans, never again from the bill.
        html = self.daily()
        self.assertIn("6.000", html)
        self.assertNotIn("12.000", html)
        self.assertEqual(self.money_after(html, "Total sales value"), 1700.00)

        # Billing the same day twice finds nothing left to bill.
        again = self.client.post(
            f"/customers/{self.customer_id}/pickups/{day}/bill", data={"paid": "0"}
        )
        self.assertEqual(again.status_code, 302)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sales"), 1)

    def test_cancelled_challan_leaves_the_days_weight(self):
        self.client.post(
            "/api/challans",
            json={
                "customer_id": self.customer_id,
                "items": [{"product_id": self.product["Smoke Sheet"], "weight_kg": 4}],
            },
        )
        pickup = self.one("SELECT * FROM pickups")
        self.assertAlmostEqual(self.daily_weight(), 4.000, places=3)

        cancelled = self.client.post(f"/pickups/{pickup['id']}/cancel")
        self.assertEqual(cancelled.status_code, 302)
        self.assertAlmostEqual(
            self.daily_weight(), 0.000, places=3
        )
        self.assertIsNotNone(self.one("SELECT cancelled_at FROM pickups")["cancelled_at"])

        self.client.post(f"/pickups/{pickup['id']}/restore")
        self.assertAlmostEqual(self.daily_weight(), 4.000, places=3)
        self.assertIsNone(self.one("SELECT cancelled_at FROM pickups")["cancelled_at"])

    # --- held drafts -------------------------------------------------------

    def test_held_draft_touches_no_money_until_it_is_saved(self):
        held = self.client.post(
            "/api/drafts",
            json={
                "customer_id": self.customer_id,
                "label": "Smoke hold",
                "paid": 100,
                "items": [{"product_id": self.product["Smoke Pipe"], "weight_kg": 2}],
            },
        )
        self.assertEqual(held.status_code, 201, held.get_data(as_text=True))
        draft = held.get_json()
        self.assertAlmostEqual(draft["total"], 500.00, places=2)
        self.assertEqual(draft["item_count"], 1)

        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sales"), 0)
        self.assertAlmostEqual(self.account()["due"], 0.00, places=2)
        self.assertEqual(self.money_after(self.daily(), "Cash collected"), 0.00)
        self.assertEqual(len(self.client.get("/api/drafts").get_json()), 1)

        resumed = self.save(
            [{"product_id": self.product["Smoke Pipe"], "weight_kg": 2}],
            100,
            draft_id=draft["id"],
        )
        self.assertEqual(resumed.status_code, 201, resumed.get_data(as_text=True))
        self.assertAlmostEqual(resumed.get_json()["new_due"], 400.00, places=2)
        self.assertAlmostEqual(self.account()["due"], 400.00, places=2)
        self.assertEqual(
            self.client.get("/api/drafts").get_json(), [], "the draft must be consumed"
        )

    def test_draft_refuses_a_half_filled_line(self):
        response = self.client.post(
            "/api/drafts",
            json={
                "customer_id": self.customer_id,
                "items": [{"product_id": self.product["Smoke Sheet"]}],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM draft_receipts"), 0)

    # --- payments taken on the customer page -------------------------------

    def test_due_payment_reduces_due_and_overpayment_is_capped(self):
        self.save([{"product_id": self.product["Smoke Sheet"], "weight_kg": 10}], 1000)
        self.assertAlmostEqual(self.account()["due"], 2000.00, places=2)

        paid = self.client.post(
            f"/customers/{self.customer_id}/payments", data={"amount": "500"}
        )
        self.assertEqual(paid.status_code, 302)
        self.assertAlmostEqual(self.account()["due"], 1500.00, places=2)

        # More cash than the due clears the due; the rest is change, not credit.
        self.client.post(
            f"/customers/{self.customer_id}/payments", data={"amount": "99999"}
        )
        self.assertAlmostEqual(self.account()["due"], 0.00, places=2)
        self.assertAlmostEqual(self.account()["credit"], 0.00, places=2)
        self.assertAlmostEqual(
            self.scalar("SELECT COALESCE(SUM(amount), 0) FROM payments"),
            2000.00,
            places=2,
        )

        # With nothing due there is nothing to receive.
        refused = self.client.post(
            f"/customers/{self.customer_id}/payments", data={"amount": "100"}
        )
        self.assertEqual(refused.status_code, 302)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM payments"), 2)
        self.assertAlmostEqual(self.account()["due"], 0.00, places=2)

        # A payment is not a sale: Daily Sales counts it as cash, never revenue.
        # 1000 cash on the bill + 2000 of due payments = 3000 in the drawer.
        html = self.daily()
        self.assertEqual(self.money_after(html, "Cash collected"), 3000.00)
        self.assertEqual(self.money_after(html, "Total sales value"), 3000.00)

    # --- voiding money that is not a bill ----------------------------------

    def test_void_payment_needs_a_reason_and_restores_the_due(self):
        self.save([{"product_id": self.product["Smoke Sheet"], "weight_kg": 10}], 1000)
        self.client.post(
            f"/customers/{self.customer_id}/payments",
            data={"amount": "500", "method": "bKash"},
        )
        self.assertAlmostEqual(self.account()["due"], 1500.00, places=2)
        payment = self.one("SELECT * FROM payments")
        self.assertEqual(payment["method"], "bKash", "the method must be stored")
        self.assertIsNone(payment["voided_at"])

        # No reason, no void.
        self.client.post(f"/payments/{payment['id']}/void", data={"reason": "  "})
        self.assertIsNone(
            self.one("SELECT voided_at FROM payments WHERE id = ?", (payment["id"],))[
                "voided_at"
            ],
            "a void without a reason must be refused",
        )
        self.assertAlmostEqual(self.account()["due"], 1500.00, places=2)

        self.client.post(f"/payments/{payment['id']}/void", data={"reason": "typo"})
        voided = self.one("SELECT * FROM payments WHERE id = ?", (payment["id"],))
        self.assertIsNotNone(voided["voided_at"])
        self.assertEqual(voided["void_reason"], "typo")
        self.assertAlmostEqual(voided["amount"], 500.00, places=2, msg="the row stays")
        self.assertAlmostEqual(self.account()["due"], 2000.00, places=2)
        # It leaves the day's cash too: only the 1000 on the bill remains.
        self.assertEqual(
            self.money_after(self.daily(), "Cash collected"), 1000.00
        )

    def test_void_scrap_entry_restores_the_account(self):
        self.save([{"product_id": self.product["Smoke Sheet"], "weight_kg": 10}], 0)
        self.assertAlmostEqual(self.account()["due"], 3000.00, places=2)
        self.client.post(
            f"/customers/{self.customer_id}/scrap",
            data={"black_kg": "5", "payout": "0", "method": "Cash"},
        )
        self.assertAlmostEqual(self.account()["due"], 2050.00, places=2)  # 3000-950
        entry = self.one("SELECT * FROM scrap_payments")

        self.client.post(f"/scrap-payments/{entry['id']}/void", data={"reason": "wrong kg"})
        self.assertAlmostEqual(self.account()["due"], 3000.00, places=2)
        self.assertEqual(
            self.one("SELECT void_reason FROM scrap_payments")["void_reason"], "wrong kg"
        )

    def test_scrap_cash_payout_leaves_the_drawer_and_not_the_due_maths(self):
        self.save([{"product_id": self.product["Smoke Sheet"], "weight_kg": 10}], 0)
        # 5 kg black scrap = 950; 200 of it paid out in cash, so 750 pays the due.
        self.client.post(
            f"/customers/{self.customer_id}/scrap",
            data={"black_kg": "5", "payout": "200", "method": "Cash"},
        )
        self.assertAlmostEqual(self.account()["due"], 2250.00, places=2)
        self.assertEqual(
            self.money_after(self.daily(), "Expected in drawer"), -200.00
        )
        # More cash than the scrap is worth is refused outright.
        self.client.post(
            f"/customers/{self.customer_id}/scrap",
            data={"black_kg": "1", "payout": "5000"},
        )
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM scrap_payments"), 1)

    def test_void_ledger_adjustment_removes_the_carried_due(self):
        self.client.post(
            f"/customers/{self.customer_id}/opening-due",
            data={"amount": "1200", "note": "old notebook"},
        )
        self.assertAlmostEqual(self.account()["due"], 1200.00, places=2)
        entry = self.one("SELECT * FROM ledger_adjustments")
        self.client.post(
            f"/ledger-adjustments/{entry['id']}/void", data={"reason": "already paid"}
        )
        self.assertAlmostEqual(self.account()["due"], 0.00, places=2)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM ledger_adjustments"), 1)

    def test_credit_payout_clears_the_credit_and_is_not_drawer_cash(self):
        self.save([{"product_id": self.product["Smoke Sheet"], "weight_kg": 10}], 3000)
        # Nothing is due, so there is no credit to hand back either.
        self.client.post(
            f"/customers/{self.customer_id}/payouts", data={"amount": "100"}
        )
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM credit_payouts"), 0)

        # 5 kg black scrap on a settled bill leaves 950 as credit.
        self.client.post(
            f"/customers/{self.customer_id}/scrap", data={"black_kg": "5"}
        )
        self.assertAlmostEqual(self.account()["credit"], 950.00, places=2)

        # More than the credit is refused: no payout becomes new due.
        self.client.post(
            f"/customers/{self.customer_id}/payouts",
            data={"amount": "1200", "method": "Cash"},
        )
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM credit_payouts"), 0)

        self.client.post(
            f"/customers/{self.customer_id}/payouts",
            data={"amount": "950", "method": "bKash", "note": "collecting"},
        )
        payout = self.one("SELECT * FROM credit_payouts")
        self.assertEqual(payout["method"], "bKash")
        self.assertAlmostEqual(payout["amount"], 950.00, places=2)
        account = self.account()
        self.assertAlmostEqual(account["credit"], 0.00, places=2, msg="full credit clears")
        self.assertAlmostEqual(account["due"], 0.00, places=2)

        # bKash leaves the till alone: the 3,000 from the bill is still there.
        self.assertEqual(self.money_after(self.daily(), "Expected in drawer"), 3000.00)

        statement = self.client.get(
            f"/customers/{self.customer_id}/statement"
            f"?from={self.today()}&to={self.today()}"
        ).get_data(as_text=True)
        self.assertIn(f"PAY-{payout['id']:05d}", statement)
        self.assertIn("Credit payout", statement)

    def test_cash_credit_payout_comes_out_of_the_drawer(self):
        self.save([{"product_id": self.product["Smoke Sheet"], "weight_kg": 10}], 3000)
        self.client.post(
            f"/customers/{self.customer_id}/scrap", data={"black_kg": "5"}
        )
        self.client.post(
            f"/customers/{self.customer_id}/payouts",
            data={"amount": "400", "method": "Cash", "note": "part"},
        )
        payout = self.one("SELECT * FROM credit_payouts")

        # 3,000 in on the bill, 400 handed back: the drawer holds 2,600.
        self.assertEqual(self.money_after(self.daily(), "Expected in drawer"), 2600.00)
        self.assertAlmostEqual(self.account()["credit"], 550.00, places=2)

        page = self.client.get(f"/customers/{self.customer_id}").get_data(as_text=True)
        self.assertIn("Credit paid out", page)
        self.assertIn("Use full credit", page)

        slip = self.client.get(f"/credit-payouts/{payout['id']}/slip")
        self.assertEqual(slip.status_code, 200)
        html = slip.get_data(as_text=True)
        self.assertIn("CUSTOMER COPY", html)
        self.assertIn("SHOP COPY", html)
        self.assertIn("400.00", html)
        self.assertIn("Received by (customer)", html)

    def test_void_credit_payout_needs_a_reason_and_restores_the_credit(self):
        self.save([{"product_id": self.product["Smoke Sheet"], "weight_kg": 10}], 3000)
        self.client.post(
            f"/customers/{self.customer_id}/scrap", data={"black_kg": "5"}
        )
        self.client.post(
            f"/customers/{self.customer_id}/payouts",
            data={"amount": "950", "method": "Cash"},
        )
        self.assertAlmostEqual(self.account()["credit"], 0.00, places=2)
        payout = self.one("SELECT * FROM credit_payouts")

        # No reason, no void.
        self.client.post(f"/credit-payouts/{payout['id']}/void", data={"reason": "  "})
        self.assertIsNone(
            self.one("SELECT voided_at FROM credit_payouts WHERE id = ?", (payout["id"],))[
                "voided_at"
            ],
            "a void without a reason must be refused",
        )

        self.client.post(
            f"/credit-payouts/{payout['id']}/void", data={"reason": "wrong customer"}
        )
        voided = self.one("SELECT * FROM credit_payouts WHERE id = ?", (payout["id"],))
        self.assertIsNotNone(voided["voided_at"])
        self.assertEqual(voided["void_reason"], "wrong customer")
        self.assertAlmostEqual(voided["amount"], 950.00, places=2, msg="the row stays")
        self.assertAlmostEqual(self.account()["credit"], 950.00, places=2)
        # Its cash is back in the drawer: only the bill remains.
        self.assertEqual(self.money_after(self.daily(), "Expected in drawer"), 3000.00)

    # --- payment methods and the drawer ------------------------------------

    def test_payment_methods_are_kept_apart_in_the_drawer(self):
        figures = self.save(
            [{"product_id": self.product["Smoke Sheet"], "weight_kg": 10}],
            1000,
            payment_method="bKash",
        ).get_json()
        self.assertEqual(figures["payment_method"], "bKash")
        self.assertEqual(
            self.sale(figures["sale_id"])["payment_method"], "bKash"
        )
        self.client.post(
            f"/customers/{self.customer_id}/payments",
            data={"amount": "500", "method": "Bank"},
        )

        html = self.daily()
        # Nothing reached the till: the drawer expects 0 and the other two
        # methods carry the 1500.
        self.assertEqual(self.money_after(html, "Expected in drawer"), 0.00)
        self.assertIn("<td>bKash</td>", html)
        self.assertIn("<td>Bank</td>", html)
        self.assertEqual(self.money_after(html, "Cash collected"), 1500.00)

    def test_day_close_snapshots_and_flags_a_later_void(self):
        sale_id = self.save(
            [{"product_id": self.product["Smoke Sheet"], "weight_kg": 10}], 1000
        ).get_json()["sale_id"]
        day = self.today()

        self.client.post(
            "/daily/close",
            data={"day": day, "counted_cash": "950", "note": "short 50"},
        )
        close = self.one("SELECT * FROM day_closes WHERE day = ?", (day,))
        self.assertAlmostEqual(close["expected_cash"], 1000.00, places=2)
        self.assertAlmostEqual(close["counted_cash"], 950.00, places=2)
        self.assertAlmostEqual(close["cash_in"], 1000.00, places=2)
        html = self.daily()
        self.assertIn("Closed", html)
        self.assertIn("is-short", html, "the short drawer must be marked")
        self.assertNotIn("Changed since close", html)

        self.client.post(f"/sales/{sale_id}/void", data={"reason": "wrong weight"})
        html = self.daily()
        self.assertIn("Changed since close", html, "a later void must flag the day")
        # The snapshot is untouched: the close still remembers what was counted.
        self.assertAlmostEqual(
            self.one("SELECT counted_cash FROM day_closes WHERE day = ?", (day,))[
                "counted_cash"
            ],
            950.00,
            places=2,
        )

        self.client.post("/daily/reopen", data={"day": day})
        self.assertIsNone(self.one("SELECT * FROM day_closes WHERE day = ?", (day,)))
        self.assertNotIn("Changed since close", self.daily())

    # --- receipts, ageing and statements -----------------------------------

    def test_receipts_page_filters_by_customer_amount_and_status(self):
        first = self.save(
            [{"product_id": self.product["Smoke Sheet"], "weight_kg": 10}], 1000
        ).get_json()["sale_id"]
        second = self.save(
            [{"product_id": self.product["Smoke Pipe"], "weight_kg": 2}], 0
        ).get_json()["sale_id"]
        page = self.client.get(f"/receipts?q=Smoke+Customer&status=live")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn(f"INV-{first:05d}", html)
        self.assertIn(f"INV-{second:05d}", html)

        # Amount filter: the pipe bill is 2 x 250.00.
        by_amount = self.client.get("/receipts?amount=500").get_data(as_text=True)
        self.assertIn(f"INV-{second:05d}", by_amount)
        self.assertNotIn(f"INV-{first:05d}", by_amount)

        self.client.post(f"/sales/{first}/void", data={"reason": "wrong weight"})
        voided = self.client.get("/receipts?status=void").get_data(as_text=True)
        self.assertIn(f"INV-{first:05d}", voided)
        self.assertNotIn(f"INV-{second:05d}", voided)
        # By invoice number, the way a customer quotes it.
        self.assertIn(
            f"INV-{first:05d}",
            self.client.get(f"/receipts?q=INV-{first:05d}").get_data(as_text=True),
        )

    def test_due_ageing_splits_by_the_age_of_the_bill(self):
        self.save([{"product_id": self.product["Smoke Sheet"], "weight_kg": 5}], 0)
        old_id = self.scalar("SELECT id FROM sales")
        with self.sql() as con:
            con.execute(
                "UPDATE sales SET created_at = datetime('now', 'localtime', '-90 days')"
                " WHERE id = ?",
                (old_id,),
            )
        self.save([{"product_id": self.product["Smoke Sheet"], "weight_kg": 1}], 0)
        self.assertAlmostEqual(self.account()["due"], 1800.00, places=2)

        html = self.client.get("/customers").get_data(as_text=True)
        # The newest 300 is fresh; the 1500 behind it is over 60 days old.
        self.assertIn("0–30 <b>300.00</b>", html)
        self.assertIn("60+ <b>1,500.00</b>", html)

    def test_statement_shows_bills_payments_scrap_and_closing_due(self):
        sale_id = self.save(
            [{"product_id": self.product["Smoke Sheet"], "weight_kg": 10}], 1000
        ).get_json()["sale_id"]
        self.client.post(
            f"/customers/{self.customer_id}/payments",
            data={"amount": "500", "method": "Cash"},
        )
        self.client.post(
            f"/customers/{self.customer_id}/scrap", data={"black_kg": "2"}
        )
        page = self.client.get(f"/customers/{self.customer_id}/statement")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("Account Statement", html)
        self.assertIn(f"INV-{sale_id:05d}", html)
        self.assertIn("PMT-", html)
        self.assertIn("SCR-", html)
        # 3000 billed - 1000 cash - 500 payment - 380 scrap = 1120.
        self.assertIn("1,120.00", html)

    # --- purchases and margin ----------------------------------------------

    def test_purchases_carry_a_moving_average_cost_into_the_margin(self):
        for cost in ("200", "300"):
            bought = self.client.post(
                "/purchases",
                data={
                    "supplier": "Smoke Supplier",
                    "product_id": str(self.product["Smoke Sheet"]),
                    "weight_kg": "10",
                    "cost_per_kg": cost,
                },
            )
            self.assertEqual(bought.status_code, 302)
        self.assertAlmostEqual(
            self.scalar(
                "SELECT cost_per_kg FROM products WHERE id = ?",
                (self.product["Smoke Sheet"],),
            ),
            250.00,
            places=2,
        )
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM purchases"), 2)

        # 10 kg sold at 300 = 3000, cost 10 x 250 = 2500, margin 500.
        self.save([{"product_id": self.product["Smoke Sheet"], "weight_kg": 10}], 3000)
        html = self.daily()
        self.assertEqual(self.money_after(html, "Cost of goods"), 2500.00)
        self.assertEqual(self.money_after(html, "Gross margin"), 500.00)

    def test_restore_refuses_a_file_that_is_not_a_backup(self):
        self.save([{"product_id": self.product["Smoke Sheet"], "weight_kg": 2}], 0)
        before = self.scalar("SELECT COUNT(*) FROM sales")

        junk = self.client.post(
            "/backup/restore",
            data={"backup": (io.BytesIO(b"not a database"), "notes.db")},
            content_type="multipart/form-data",
        )
        self.assertEqual(junk.status_code, 302)
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM sales"),
            before,
            "a junk file must never replace the database",
        )
        self.assertFalse(
            (pathlib.Path(pos.DATA_DIR) / "restore-upload.db").exists(),
            "the uploaded copy must always be cleaned up",
        )

        # A real snapshot goes back in, and the live database still works.
        backup = pos.create_backup(force=True)
        self.assertIsNotNone(backup)
        with open(backup, "rb") as handle:
            payload = handle.read()
        restored = self.client.post(
            "/backup/restore",
            data={"backup": (io.BytesIO(payload), "shop_backup.db")},
            content_type="multipart/form-data",
        )
        self.assertEqual(restored.status_code, 302)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sales"), before)
        self.assertEqual(
            self.client.post("/api/sales", json={
                "customer_id": self.customer_id,
                "items": [{"product_id": self.product["Smoke Pipe"], "weight_kg": 1}],
                "paid": 0,
            }).status_code,
            201,
            "the restored database must accept a new bill",
        )

    def test_second_backup_folder_gets_a_copy(self):
        second = pathlib.Path(pos.BACKUP_DIR).parent / "second-copy"
        saved = self.client.post("/backup/second", data={"path": str(second)})
        self.assertEqual(saved.status_code, 302)
        self.assertEqual(pos.setting_value("backup_second_dir"), str(second))
        self.client.post("/backup/now")
        copies = list(second.glob("*.db"))
        self.assertTrue(copies, "the new snapshot must be copied to the second folder")
        self.assertTrue(
            (pathlib.Path(pos.BACKUP_DIR) / copies[0].name).exists(),
            "and still exist in the normal backups folder",
        )

    # --- the real shop file ------------------------------------------------

    def test_real_database_is_never_written(self):
        self.assertNotEqual(pos.DB_PATH, str(REAL_DB), "the app points at the real database")
        self.assertTrue(str(pos.BACKUP_DIR).startswith(str(TMP)), "backups went outside the temp folder")
        self.assertEqual(
            file_hash(REAL_DB), REAL_DB_HASH, "the real shop.db changed during the test run"
        )
        # Saving a receipt writes a snapshot into the temp backups folder only.
        self.save([{"product_id": self.product["Smoke Sheet"], "weight_kg": 1}], 0)
        outside = [
            path
            for path in pathlib.Path(pos.BACKUP_DIR).glob("*.db")
            if str(path).startswith(str(REAL_BACKUP_DIR))
        ]
        self.assertEqual(outside, [], "a backup was written into the real backups folder")


if __name__ == "__main__":
    unittest.main(verbosity=2)
