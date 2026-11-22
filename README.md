# Aluminum Wholesale Shop — POS & Customer Due Ledger

A small, complete shop application that runs **entirely on your own PC**. No internet,
no accounts, no monthly fees — the data lives in one SQLite file on your computer.

Built with **Python (Flask) + SQLite** on the backend and plain **HTML / CSS / JavaScript**
on the frontend (no CDN, no build step, no frameworks).

---

## What it does

### 1. Products tab (`/products`)
- 30 aluminum products pre-loaded (sheets, coils, profiles, pipes, rods, angles, channels, foil, roofing)
  with **Name, Category and Price per kg**.
- **Quick price update:** type a new price in the row and press `Update` (or `Enter`).
- Add new products, remove products. Old invoices keep their original names and rates.

### 2. New Receipt / Billing tab (`/`)
- **Search a customer** by name, phone or shop; or press **+ New customer** to create one
  (Name, Phone, Shop name, Address).
- The customer's **Previous Due** appears automatically after selection.
- Add as many **line items** as needed: pick a product, type **Weight (kg)** — the rate and
  line total calculate instantly.
- Bottom panel shows **Current Bill Total**, **Previous Due**, **Grand Total**,
  **Amount Paid Today**, and auto-calculated **New Remaining Due**.

### 3. Receipt printing
- **Save & Print Receipt** saves the invoice, reduces/increases the customer's due balance
  and opens a clean receipt page (shop header, invoice no., date, customer details,
  itemised weights & rates, previous due, paid, remaining due, signature lines).
- The print dialog opens automatically; the layout is controlled by `@media print` CSS in
  `static/css/receipt.css`, so paper output is clean (navigation and buttons are hidden).

### 4. Customers & Due tab (`/customers`)
- Every customer with their **current total due** and a shop-wide outstanding total.
- Click a customer → full **purchase history** (each invoice with its items, paid amount and
  due carried forward) plus every **due payment** received.
- **Record Due Payment** form: enter an amount (cash/bKash etc.) and the balance drops
  immediately; the remaining due updates live as you type.

---

## Install & run on Windows (step by step)

### Step 1 — Install Python (once)
1. Go to <https://www.python.org/downloads/windows/> and download **Python 3.10 or newer**
   (64-bit installer).
2. Run the installer and **tick the checkbox “Add python.exe to PATH”** on the first screen.
   This is important.
3. Click *Install Now* and wait for it to finish.

Check it worked — open **Command Prompt** (press `Win`, type `cmd`, hit Enter) and type:

```bat
python --version
```

You should see something like `Python 3.12.4`. If you get an error, reinstall Python and
make sure the “Add to PATH” box is ticked.

### Step 2 — Put the app on your PC
Copy this whole project folder somewhere permanent, for example:

```
C:\ShopApp\
```

The folder should contain `app.py`, `requirements.txt`, `run.bat`, `templates\` and `static\`.

### Step 3 — Start the app
**Easiest way:** double-click **`run.bat`**.

The first time, it creates a private Python environment (`.venv`) inside the folder,
installs Flask into it, and starts the shop. Later runs take a few seconds.

**Manual way** (identical result) — in Command Prompt:

```bat
cd C:\ShopApp
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

> If `python` is not recognised, try `py -3` instead of `python` in each command.

### Step 4 — Open the shop
In your browser (Chrome, Edge or Firefox) open:

```
http://127.0.0.1:5000
```

That's it. The database file `data\shop.db` is created automatically with the 30 starter
products on the first run.

### Step 5 — Stop the shop
Press `Ctrl + C` in the black command window (or just close it). Restart any time with
`run.bat` — all products, customers and dues are still there.

---

## Everyday use

1. **Products** tab → update prices when rates change.
2. **New Receipt** tab → search/select the customer → add lines (product + kg) →
   enter *Amount Paid Today* → **Save & Print Receipt**.
3. **Customers & Due** tab → click a customer → **Record Due Payment** when they pay
   an old balance.

### Printing tips
- The receipt opens in a new browser tab and prints itself in **two jobs**: the
  **Customer Copy** first, then — after you close the first dialog — the **Shop
  Copy**. Closing the dialog between jobs is what lets the printer cut the paper.
- The page is set up for an **80 mm roll** (`@page { size: 80mm auto }`), 72 mm of
  printable width. Pick your thermal printer in the dialog and leave margins off.
- Only the **Shop Copy** carries the customer signature line, and only when money
  is still owed after the bill.
- If the dialogs do not appear, press `Ctrl + P` — that prints both copies in a
  single job with a tear line between them.

---

## Backup, restore and moving to another PC

The shop backs itself up — there is nothing to switch on.

### Hourly snapshots in the `backups` folder
- The live database is still `data\shop.db`.
- A full copy is written to `backups\shop_YYYY-MM-DD_HH-MM.db`, for example
  `backups\shop_2026-11-22_14-00.db`.
- A snapshot is taken:
  - when the app starts, **if** the newest one is more than an hour old,
  - every hour while the app window stays open,
  - right after a receipt or a due payment is saved (again, at most once an hour).
- Copies are made with SQLite's own `.backup()` command, so a file is always a
  complete, working database — never half-written, even while you keep billing.
- **The newest 72 files are kept** (3 full days of hourly snapshots). Older ones are
  deleted automatically, so the folder never fills the drive.
- An existing file is never overwritten: a second copy in the same minute is saved as
  `shop_2026-11-22_14-00-2.db`.

The database itself runs in SQLite's **WAL** mode with `synchronous = FULL`, so a power
cut or forced shutdown leaves it at the last good save instead of corrupting it. The
hourly files in `backups` are the second line of defence.

### Buttons in the top bar
- **Download Backup (.db)** — makes a fresh snapshot immediately and saves it to your
  Downloads folder. Use it before moving the PC, before reinstalling Windows, or to
  copy the shop onto a USB stick.
- **Export Customers & Dues (.csv)** — saves every customer with their Due and Credit
  (`customers_dues_2026-11-22_14-00.csv`). It opens directly in Excel. This is a list
  for reading, printing or archiving — it is not re-imported into the app.

### Restoring an older backup
If `data\shop.db` is ever lost, deleted or damaged:

1. **Close the shop app** first (close the black command window, or press `Ctrl + C`).
   Restoring while it is running will not work — the app keeps the file open.
2. Open the `backups` folder in the project and pick the newest `shop_....db` from before
   the loss. You can double-click it with SQLite/DB Browser to confirm the customers and
   dues look right.
3. Rename the broken `data\shop.db` to `data\shop.db.broken` (or delete it), so it cannot
   be confused with the good copy. If `data\shop.db-wal` or `data\shop.db-shm` exist,
   delete those too — they belong to the old file.
4. **Copy** (do not move) the backup file into the `data` folder and rename it to
   `shop.db`, so the path is exactly `data\shop.db`.
5. Start the shop with `run.bat` and check the **Customers & Due** tab. The restored
   database is a normal shop database — new bills, payments and hourly backups continue
   from there.

A snapshot is only as recent as its file name: everything saved **after** that time is not
in it, so pick the newest one you have.

### Moving to another PC / reset
- **Move PC:** install Python on the new PC, copy the whole project folder — including
  `data` (and `backups` if you want the history) — then run `run.bat`. Or move just the
  newest backup file and restore it as above.
- **Copy to a USB drive / cloud folder:** download a backup with the button, or copy any
  file from the `backups` folder. Do this while the app is closed for the safest copy.
- **Reset / start fresh:** stop the app, delete `data\shop.db` (and any `shop.db-wal` /
  `shop.db-shm`), start again — the 30 starter products are re-created (customers and
  invoices are gone). The files in `backups` are left alone.

---

## Customising

| What | Where |
| --- | --- |
| Shop name, address, phone on receipts | `app.py` → `SHOP = {...}` at the top |
| Currency symbol (`Tk.` by default) | `app.py` → `CURRENCY = "Tk."` |
| Starter product list | `app.py` → `SEED_PRODUCTS` (only used when the database is empty) |
| Colours / look | `static/css/style.css`, `static/css/receipt.css` |
| Port (5000 by default) | set an environment variable `PORT`, e.g. `set PORT=8080` before `python app.py` |

---

## Project structure

```
app.py                      Flask app: routes, JSON API, SQLite schema & helpers
requirements.txt            Only dependency: Flask
run.bat                     One-click Windows launcher
templates\
  base.html                 Header, tabs, flash messages
  billing.html              New Receipt tab
  products.html             Products tab
  customers.html            Customers & Due list
  customer_detail.html      Ledger: history + record payment
  receipt.html              Printable receipt
  404.html                  Not found page
static\
  css\style.css             App styling
  css\receipt.css           Receipt styling + @media print rules
  js\billing.js             Billing screen logic (search, totals, save & print)
data\shop.db                Created automatically — your live data
data\shop.db-wal / -shm     SQLite WAL files (normal; crash protection while running)
backups\shop_*.db          Hourly snapshots, newest 72 kept — restore from here
```

---

## Running on macOS / Linux (optional)

```bash
cd /path/to/project
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open <http://127.0.0.1:5000>.

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `python` is not recognised | Reinstall Python with “Add python.exe to PATH”, or use `py -3`. |
| `pip` is not recognised | Use `python -m pip install -r requirements.txt`. |
| `Address already in use` | Another app is using port 5000: close it, or change the port in `app.py`. |
| Page won't open at 127.0.0.1:5000 | Make sure the black command window is still running and shows “Running on http://127.0.0.1:5000”. |
| Print preview shows the buttons | Make sure you print the receipt tab (not the main shop tab) — buttons are hidden only on the receipt by the print CSS. |
| Changed prices but old receipts differ | Correct behaviour: each invoice stores the rate used at the time of sale. |
| Antivirus blocks `run.bat` | Use the manual commands in Step 3, or allow the file. |
