/* Billing screen logic: customer search, line items, live totals, save & print.
   All requests go to the local Flask server; no internet needed. */
(function () {
  "use strict";

  const CURRENCY = document.body.dataset.currency || "Rs.";
  const state = { products: [], customer: null, scrapRates: {} };

  const el = {
    search: document.getElementById("customer-search"),
    results: document.getElementById("customer-results"),
    selected: document.getElementById("selected-customer"),
    scName: document.getElementById("sc-name"),
    scMeta: document.getElementById("sc-meta"),
    scDue: document.getElementById("sc-due"),
    scDueLabel: document.getElementById("sc-due-label"),
    clearCustomer: document.getElementById("clear-customer"),
    toggleNew: document.getElementById("toggle-new-customer"),
    cancelNew: document.getElementById("cancel-new-customer"),
    newForm: document.getElementById("new-customer-form"),
    itemsBody: document.getElementById("items-body"),
    addRow: document.getElementById("add-row"),
    billTotal: document.getElementById("bill-total"),
    prevDue: document.getElementById("prev-due"),
    grandTotal: document.getElementById("grand-total"),
    creditUsed: document.getElementById("credit-used"),
    scrapCreditRow: document.getElementById("scrap-credit-row"),
    scrapCredit: document.getElementById("scrap-credit"),
    paid: document.getElementById("paid"),
    newDue: document.getElementById("new-due"),
    changeNote: document.getElementById("change-note"),
    scrapBlack: document.getElementById("scrap-black"),
    scrapWhite: document.getElementById("scrap-white"),
    scrapRateBlack: document.getElementById("scrap-rate-black"),
    scrapRateWhite: document.getElementById("scrap-rate-white"),
    scrapValueBlack: document.getElementById("scrap-value-black"),
    scrapValueWhite: document.getElementById("scrap-value-white"),
    scrapTotal: document.getElementById("scrap-total"),
    scrapDeduct: document.getElementById("scrap-deduct"),
    savePrint: document.getElementById("save-print"),
    status: document.getElementById("save-status"),
    headDue: document.getElementById("head-due"),
  };

  const money = (n) =>
    n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const round2 = (n) => Math.round((n + Number.EPSILON) * 100) / 100;

  function setStatus(message, isError) {
    el.status.textContent = message || "";
    el.status.style.color = isError ? "#c62f3c" : "#147a52";
  }

  /* ---------------- products / rows ---------------- */
  function buildProductSelect(selectedId) {
    const select = document.createElement("select");
    select.className = "input product-select";
    select.required = true;

    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Select product…";
    select.appendChild(placeholder);

    const groups = new Map();
    state.products.forEach((p) => {
      if (!groups.has(p.category)) groups.set(p.category, []);
      groups.get(p.category).push(p);
    });
    groups.forEach((list, category) => {
      const group = document.createElement("optgroup");
      group.label = category;
      list.forEach((p) => {
        const option = document.createElement("option");
        option.value = String(p.id);
        option.dataset.price = String(p.price_per_kg);
        option.textContent = `${p.name} — ${CURRENCY} ${money(p.price_per_kg)}/kg`;
        group.appendChild(option);
      });
      select.appendChild(group);
    });

    if (selectedId) select.value = String(selectedId);
    return select;
  }

  function addRow() {
    if (!state.products.length) {
      setStatus("No products found. Add products on the Products tab first.", true);
      return;
    }
    const tr = document.createElement("tr");

    const tdProduct = document.createElement("td");
    tdProduct.appendChild(buildProductSelect(""));

    const tdWeight = document.createElement("td");
    tdWeight.className = "num";
    const weight = document.createElement("input");
    weight.type = "number";
    weight.className = "input weight-input";
    weight.step = "0.001";
    weight.min = "0";
    weight.placeholder = "0.000";
    weight.setAttribute("aria-label", "Weight in kg");
    tdWeight.appendChild(weight);

    const tdRate = document.createElement("td");
    tdRate.className = "num rate";
    tdRate.textContent = "—";

    const tdTotal = document.createElement("td");
    tdTotal.className = "num line-total";
    tdTotal.textContent = CURRENCY + " 0.00";

    const tdRemove = document.createElement("td");
    tdRemove.className = "actions";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn btn-small btn-ghost btn-danger remove-row";
    remove.title = "Remove line";
    remove.textContent = "✕";
    tdRemove.appendChild(remove);

    tr.append(tdProduct, tdWeight, tdRate, tdTotal, tdRemove);
    el.itemsBody.appendChild(tr);
    weight.focus();
  }

  function rowValues(tr) {
    const select = tr.querySelector(".product-select");
    const weightEl = tr.querySelector(".weight-input");
    const option = select.options[select.selectedIndex];
    const price = option && option.dataset.price ? parseFloat(option.dataset.price) : 0;
    const weight = parseFloat(weightEl.value);
    return {
      productId: select.value ? parseInt(select.value, 10) : null,
      price: isNaN(price) ? 0 : price,
      weight: isNaN(weight) || weight < 0 ? 0 : weight,
      weightRaw: weightEl.value,
    };
  }

  function recalcRow(tr) {
    const { price, weight } = rowValues(tr);
    const rateCell = tr.querySelector(".rate");
    const totalCell = tr.querySelector(".line-total");
    rateCell.textContent = price ? CURRENCY + " " + money(price) : "—";
    totalCell.textContent = CURRENCY + " " + money(round2(price * weight));
  }

  /* ---------------- scrap ---------------- */
  function recalcScrap() {
    let total = 0;
    [
      ["black", el.scrapBlack, el.scrapValueBlack],
      ["white", el.scrapWhite, el.scrapValueWhite],
    ].forEach(([type, input, valueCell]) => {
      const kg = parseFloat(input.value);
      const safeKg = isNaN(kg) || kg < 0 ? 0 : kg;
      const value = round2(safeKg * (state.scrapRates[type] || 0));
      total += value;
      valueCell.textContent = CURRENCY + " " + money(value);
    });
    total = round2(total);
    el.scrapTotal.textContent = CURRENCY + " " + money(total);
    el.scrapDeduct.textContent = CURRENCY + " " + money(total);
    return total;
  }

  function scrapPayload() {
    const raw = {
      black_kg: parseFloat(el.scrapBlack.value),
      white_kg: parseFloat(el.scrapWhite.value),
    };
    return {
      black_kg: isNaN(raw.black_kg) ? 0 : raw.black_kg,
      white_kg: isNaN(raw.white_kg) ? 0 : raw.white_kg,
    };
  }

  async function loadScrapRates() {
    try {
      const res = await fetch("/api/scrap-rates");
      if (!res.ok) throw new Error();
      (await res.json()).forEach((rate) => {
        const type = rate.name.toLowerCase();
        state.scrapRates[type] = rate.price_per_kg;
        const cell = type === "black" ? el.scrapRateBlack : el.scrapRateWhite;
        if (cell) cell.textContent = CURRENCY + " " + money(rate.price_per_kg);
      });
    } catch (err) {
      setStatus("Could not load scrap rates. Is the server running?", true);
    }
  }

  function recalcTotals() {
    let bill = 0;
    el.itemsBody.querySelectorAll("tr").forEach((tr) => {
      const { price, weight } = rowValues(tr);
      bill += price * weight;
    });
    bill = round2(bill);

    const previous = state.customer ? round2(state.customer.balance || 0) : 0;
    const credit = state.customer ? round2(state.customer.credit || 0) : 0;
    const grand = round2(bill + previous);
    // Credit on the account comes off the bill before anything is collected;
    // scrap left over after the bill goes back on the account as credit.
    const creditUsed = round2(Math.min(credit, grand));
    const payable = round2(grand - creditUsed);
    let paid = parseFloat(el.paid.value);
    if (isNaN(paid) || paid < 0) paid = 0;
    const scrap = recalcScrap();
    const scrapApplied = round2(Math.min(scrap, payable));
    const scrapCredit = round2(scrap - scrapApplied);
    const cashNeeded = round2(payable - scrapApplied);
    const cashApplied = round2(Math.min(paid, cashNeeded));
    const remaining = round2(cashNeeded - cashApplied);
    const change = round2(paid - cashApplied);

    el.billTotal.textContent = CURRENCY + " " + money(bill);
    el.prevDue.textContent = CURRENCY + " " + money(previous);
    el.grandTotal.textContent = CURRENCY + " " + money(grand);
    if (el.creditUsed) el.creditUsed.textContent = CURRENCY + " " + money(creditUsed);
    el.newDue.textContent = CURRENCY + " " + money(remaining);

    if (el.scrapCreditRow) {
      el.scrapCreditRow.hidden = scrapCredit <= 0;
      if (el.scrapCredit && scrapCredit > 0) {
        el.scrapCredit.textContent = CURRENCY + " " + money(scrapCredit);
      }
    }

    if (el.changeNote) {
      el.changeNote.hidden = change <= 0;
      el.changeNote.textContent = change > 0
        ? "Change returned: " + CURRENCY + " " + money(change)
        : "";
    }

    if (el.headDue) {
      let amount = "—";
      let label = "previous due";
      if (state.customer) {
        if (previous > 0) {
          amount = CURRENCY + " " + money(previous);
        } else if (credit > 0) {
          amount = CURRENCY + " " + money(credit);
          label = "credit on account";
        } else {
          amount = CURRENCY + " 0.00";
        }
      }
      el.headDue.innerHTML = "<span>" + amount + "</span> " + label;
    }
  }

  /* ---------------- customers ---------------- */
  function showCustomer(customer) {
    state.customer = customer;
    el.search.value = "";
    hideResults();
    el.scName.textContent = customer.name;
    const bits = [customer.shop_name, customer.phone, customer.address].filter(Boolean);
    el.scMeta.textContent = bits.length ? bits.join(" • ") : "No shop details on file";
    const due = round2(customer.balance || 0);
    const credit = round2(customer.credit || 0);
    el.scDue.textContent = CURRENCY + " " + money(due);
    if (el.scDueLabel) el.scDueLabel.textContent = "Previous due";
    el.scDue.classList.toggle("is-credit", credit > 0 && due <= 0);
    if (credit > 0 && due <= 0) {
      el.scDue.textContent = CURRENCY + " " + money(credit);
      if (el.scDueLabel) el.scDueLabel.textContent = "Credit on account";
    }
    el.selected.hidden = false;
    el.newForm.hidden = true;
    el.search.closest(".customer-search").hidden = true;
    recalcTotals();
  }

  function clearCustomer() {
    state.customer = null;
    el.selected.hidden = true;
    el.search.closest(".customer-search").hidden = false;
    el.search.value = "";
    el.search.focus();
    hideResults();
    recalcTotals();
  }

  function hideResults() {
    el.results.hidden = true;
    el.results.innerHTML = "";
  }

  function renderResults(list) {
    el.results.innerHTML = "";
    if (!list.length) {
      const empty = document.createElement("div");
      empty.className = "sr-empty";
      empty.textContent = "No customer found. Use “+ New customer” to add one.";
      el.results.appendChild(empty);
      el.results.hidden = false;
      return;
    }
    list.forEach((c) => {
      const button = document.createElement("button");
      button.type = "button";

      const left = document.createElement("div");
      const name = document.createElement("div");
      name.innerHTML = "";
      name.textContent = c.name;
      name.style.fontWeight = "700";
      left.appendChild(name);
      const meta = document.createElement("div");
      meta.className = "sr-meta";
      meta.textContent = [c.shop_name, c.phone].filter(Boolean).join(" • ") || "—";
      left.appendChild(meta);

      const due = document.createElement("div");
      due.className = "sr-due";
      const credit = round2(c.credit || 0);
      due.textContent = credit > 0
        ? "Credit " + CURRENCY + " " + money(credit)
        : "Due " + CURRENCY + " " + money(round2(c.balance));
      due.classList.toggle("is-credit", credit > 0);

      button.append(left, due);
      button.addEventListener("click", () => showCustomer(c));
      el.results.appendChild(button);
    });
    el.results.hidden = false;
  }

  let searchTimer = null;
  function searchCustomers(term) {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      try {
        const res = await fetch("/api/customers?q=" + encodeURIComponent(term));
        if (!res.ok) throw new Error("search failed");
        renderResults(await res.json());
      } catch (err) {
        setStatus("Could not search customers. Is the server running?", true);
      }
    }, 180);
  }

  async function createCustomer(payload) {
    const res = await fetch("/api/customers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Could not create customer.");
    return data;
  }

  /* ---------------- save & print ---------------- */
  async function saveSale() {
    setStatus("");
    if (!state.customer) {
      setStatus("Select or create a customer first.", true);
      el.search.focus();
      return;
    }

    const items = [];
    let invalid = false;
    el.itemsBody.querySelectorAll("tr").forEach((tr) => {
      const { productId, weight, weightRaw } = rowValues(tr);
      if (!productId && !weightRaw) return; // untouched blank line
      if (!productId || !(weight > 0)) invalid = true;
      items.push({ product_id: productId, weight_kg: weight });
    });

    if (invalid) {
      setStatus("Every line needs a product and a weight above 0 kg.", true);
      return;
    }
    if (!items.length) {
      setStatus("Add at least one product line.", true);
      return;
    }

    const paid = parseFloat(el.paid.value);
    if (isNaN(paid) || paid < 0) {
      setStatus("Amount paid must be 0 or more.", true);
      return;
    }

    const scrap = scrapPayload();
    if (scrap.black_kg < 0 || scrap.white_kg < 0) {
      setStatus("Scrap weight cannot be negative.", true);
      return;
    }

    if (scrap.black_kg > 0 || scrap.white_kg > 0) {
      // Prices may have been edited on the Products tab since this page loaded.
      // Re-read them so the saved valuation matches what is shown here.
      const before = JSON.stringify(state.scrapRates);
      await loadScrapRates();
      if (!Object.keys(state.scrapRates).length) {
        setStatus("Could not load scrap rates. Is the server running?", true);
        return;
      }
      if (JSON.stringify(state.scrapRates) !== before) {
        recalcTotals();
        setStatus("Scrap rates were updated. Check the amounts and save again.", true);
        return;
      }
    }

    el.savePrint.disabled = true;
    setStatus("Saving…");
    // Opened during the click so pop-up blockers allow the receipt tab.
    const printWindow = window.open("", "_blank");
    try {
      const res = await fetch("/api/sales", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          customer_id: state.customer.id,
          items: items,
          scrap: scrap,
          paid: paid,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Could not save the sale.");

      const receiptUrl = data.receipt_url + "?print=1";
      if (printWindow && !printWindow.closed) {
        printWindow.location.href = receiptUrl;
        setStatus(
          "Saved. Receipt " +
            data.receipt_url.split("/").pop().padStart(5, "0") +
            " opened for printing."
        );
      } else {
        el.status.innerHTML = "";
        const link = document.createElement("a");
        link.href = receiptUrl;
        link.target = "_blank";
        link.textContent = "Open the saved receipt";
        el.status.append("Saved. Receipt tab was blocked — ", link);
      }
      resetForm();
    } catch (err) {
      if (printWindow && !printWindow.closed) printWindow.close();
      setStatus(err.message || "Could not save the sale.", true);
    } finally {
      el.savePrint.disabled = false;
    }
  }

  function resetForm() {
    el.itemsBody.innerHTML = "";
    addRow();
    el.paid.value = "0.00";
    el.scrapBlack.value = "";
    el.scrapWhite.value = "";
    clearCustomer();
  }

  /* ---------------- wiring ---------------- */
  el.search.addEventListener("input", () => {
    const term = el.search.value.trim();
    if (!term) {
      hideResults();
      return;
    }
    searchCustomers(term);
  });
  el.search.addEventListener("focus", () => {
    if (el.search.value.trim()) searchCustomers(el.search.value.trim());
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".customer-search")) hideResults();
  });

  el.clearCustomer.addEventListener("click", clearCustomer);

  el.toggleNew.addEventListener("click", () => {
    el.newForm.hidden = !el.newForm.hidden;
    if (!el.newForm.hidden) el.newForm.querySelector("input[name='name']").focus();
  });
  el.cancelNew.addEventListener("click", () => {
    el.newForm.hidden = true;
    el.newForm.reset();
  });

  el.newForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    const payload = {
      name: form.name.value.trim(),
      phone: form.phone.value.trim(),
      shop_name: form.shop_name.value.trim(),
      address: form.address.value.trim(),
    };
    if (!payload.name) {
      setStatus("Customer name is required.", true);
      return;
    }
    try {
      const customer = await createCustomer(payload);
      form.reset();
      showCustomer(customer);
      setStatus("Customer added: " + customer.name);
      el.savePrint.focus();
    } catch (err) {
      setStatus(err.message, true);
    }
  });

  el.itemsBody.addEventListener("input", (event) => {
    const tr = event.target.closest("tr");
    if (!tr) return;
    recalcRow(tr);
    recalcTotals();
  });
  el.itemsBody.addEventListener("change", (event) => {
    const tr = event.target.closest("tr");
    if (!tr) return;
    recalcRow(tr);
    recalcTotals();
    if (event.target.classList.contains("product-select")) {
      tr.querySelector(".weight-input").focus();
    }
  });
  el.itemsBody.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !event.target.classList.contains("weight-input")) return;
    event.preventDefault();
    const tr = event.target.closest("tr");
    const rows = Array.from(el.itemsBody.querySelectorAll("tr"));
    const index = rows.indexOf(tr);
    if (index === rows.length - 1) {
      addRow();
    } else {
      rows[index + 1].querySelector(".weight-input").focus();
    }
  });
  el.itemsBody.addEventListener("click", (event) => {
    if (!event.target.classList.contains("remove-row")) return;
    const tr = event.target.closest("tr");
    if (el.itemsBody.querySelectorAll("tr").length === 1) {
      tr.querySelectorAll("input").forEach((i) => (i.value = ""));
      tr.querySelector(".product-select").value = "";
      recalcRow(tr);
    } else {
      tr.remove();
    }
    recalcTotals();
  });

  el.addRow.addEventListener("click", () => {
    addRow();
    recalcTotals();
  });

  el.paid.addEventListener("input", recalcTotals);
  el.scrapBlack.addEventListener("input", recalcTotals);
  el.scrapWhite.addEventListener("input", recalcTotals);
  el.savePrint.addEventListener("click", saveSale);

  /* ---------------- start ---------------- */
  (async function init() {
    try {
      const res = await fetch("/api/products");
      if (!res.ok) throw new Error();
      state.products = await res.json();
    } catch (err) {
      setStatus("Could not load products. Is the server running?", true);
    }
    await loadScrapRates();
    addRow();
    recalcTotals();
  })();
})();
