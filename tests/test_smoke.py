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
import pathlib
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager

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
                "draft_receipts",
                "customer_rates",
                "scrap_rates",
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
        return self.one("SELECT date('now') AS d")["d"]

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
            re.escape(label) + r"</span>\s*<b>Tk\. ([\d,]+\.\d\d)</b>", html
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
