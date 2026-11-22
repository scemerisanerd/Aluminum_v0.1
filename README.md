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
- The receipt opens in a new browser tab and the print dialog appears by itself.
- In the dialog choose your printer; **A4** is the default page size. For an 80 mm
  thermal printer, choose that paper size (the receipt is a single narrow column).
- If the dialog does not appear, press `Ctrl + P` on the receipt page, or use the
  **Print receipt** button.

---

## Backup and moving to another PC
Everything is inside the `data` folder.

- **Backup:** copy `data\shop.db` to a USB drive / cloud folder (do it while the app is
  stopped, or just after a save).
- **Move PC:** install Python on the new PC, copy the project folder including `data`,
  run `run.bat`.
- **Reset / start fresh:** stop the app, delete `data\shop.db`, start again — the 30 starter
  products are re-created (customers and invoices are gone).

---

## Customising

| What | Where |
| --- | --- |
| Shop name, address, phone on receipts | `app.py` → `SHOP = {...}` at the top |
| Currency symbol (`Rs.` by default) | `app.py` → `CURRENCY = "Rs."` |
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
data\shop.db                Created automatically — your data (back this up)
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
