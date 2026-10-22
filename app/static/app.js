"use strict";

// All server data is rendered through el()/textContent, never innerHTML.

const state = { products: [], productsById: {}, warehouses: [], countdown: null };

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === false || value == null) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

class ApiError extends Error {
  constructor(status, body) {
    super(typeof body?.detail === "string" ? body.detail : validationMessage(body));
    this.status = status;
    this.plan = body?.plan ?? null;
  }
}

function validationMessage(body) {
  if (Array.isArray(body?.detail)) return body.detail.map((d) => d.msg).join("; ");
  return "Something went wrong. Please try again.";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(response.status, body);
  return body;
}

const productName = (id) => state.productsById[id]?.name ?? id;
const minutes = (m) => `~${Math.round(m)} min`;
const isCold = (product) => product.storage_condition === "COLD_2_8C";
const formatTime = (iso) =>
  new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
const formatWib = (iso) =>
  `${new Date(iso).toLocaleString(undefined, { timeZone: "Asia/Jakarta", weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })} WIB`;

function showAlert(node, kind, message) {
  node.className = `alert ${kind}`;
  node.textContent = message;
  node.hidden = !message;
}

// ---------- navigation ----------

const views = {
  order: async () => {},
  orders: loadOrders,
  stock: loadStock,
};

function route() {
  const tab = location.hash.slice(1) in views ? location.hash.slice(1) : "order";
  for (const name of Object.keys(views)) {
    const link = document.querySelector(`[data-tab="${name}"]`);
    document.getElementById(`view-${name}`).hidden = name !== tab;
    if (name === tab) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  views[tab]().catch((e) => console.error(e));
}

// ---------- plan rendering (shared by new order, order detail, errors) ----------

function renderPlan(plan) {
  const parts = [];
  const tone = !plan.shipments.length ? "error" : plan.fully_fulfilled ? "ok" : "warn";
  parts.push(el("div", { class: `alert ${tone}` }, plan.summary));

  if (plan.metrics) {
    parts.push(
      el(
        "div",
        { class: "metrics" },
        metric(plan.metrics.shipments, "shipments"),
        metric(minutes(plan.metrics.last_arrival_minutes), "last arrival"),
        metric(`${plan.metrics.total_distance_km} km`, "total distance"),
      ),
    );
  }

  if (plan.shipments.length) {
    parts.push(
      el("h3", { class: "section-title" }, "Shipments"),
      el(
        "div",
        { class: "table-wrap" },
        el(
          "table",
          { class: "table" },
          el("thead", {}, el("tr", {}, el("th", {}, "Warehouse"), el("th", { class: "num" }, "Distance"), el("th", { class: "num" }, "ETA"), el("th", {}, "Items"))),
          el(
            "tbody",
            {},
            plan.shipments.map((s) =>
              el(
                "tr",
                {},
                el("td", {}, el("strong", {}, s.warehouse_id), " ", s.city),
                el("td", { class: "num" }, `${s.distance_km} km`),
                el("td", { class: "num" }, minutes(s.eta_minutes), el("span", { class: "eta-basis" }, s.eta_basis)),
                el("td", {}, s.items.map((i) => el("div", {}, `${productName(i.product_id)} × ${i.quantity}`))),
              ),
            ),
          ),
        ),
      ),
    );
  }

  if (plan.backorders.length) {
    parts.push(
      el("h3", { class: "section-title" }, "Backordered"),
      el("ul", { class: "list" }, plan.backorders.map((b) => el("li", {}, `${productName(b.product_id)} × ${b.quantity}: ${b.reason}`))),
    );
  }

  if (plan.alternatives.length) {
    parts.push(
      el("h3", { class: "section-title" }, "Alternatives considered"),
      el("ul", { class: "list" }, plan.alternatives.map((a) => el("li", {}, el("strong", {}, a.warehouse_ids.join(" + ")), `: ${a.comparison}`))),
    );
  }

  if (plan.exclusions.length) {
    parts.push(
      el("h3", { class: "section-title" }, "Warehouses not used"),
      el("ul", { class: "list muted" }, plan.exclusions.map((e) => el("li", {}, el("strong", {}, e.warehouse_id), ` for ${productName(e.product_id)}: ${e.reason}`))),
    );
  }

  parts.push(el("details", {}, el("summary", {}, "Full reasoning"), el("ol", { class: "list" }, plan.reasons.map((r) => el("li", {}, r)))));
  return parts;
}

function metric(value, label) {
  return el("div", { class: "metric" }, el("b", {}, value), el("small", {}, label));
}

function renderOrder(container, order, { onChange } = {}) {
  clearInterval(state.countdown);
  const message = el("div", { class: "alert", hidden: true });
  // A lapsed (EXPIRED) order can still be confirmed if its stock is free, or cancelled.
  const actionable = order.status === "PENDING" || order.status === "EXPIRED";

  const header = el(
    "div",
    { class: "card-head" },
    el("h2", {}, "Order ", el("span", { class: "muted" }, order.id.slice(0, 8))),
    el("div", {}, el("span", { class: `badge ${order.urgency}` }, order.urgency), " ", el("span", { class: `badge ${order.status}` }, order.status)),
  );

  const dispatch = el("p", { class: "muted" }, `Dispatch ${formatWib(order.dispatch_at)}`);
  const timer = el("p", { class: "muted" });
  if (actionable) startCountdown(timer, order.expires_at);
  else if (order.confirmed_at) timer.textContent = `Confirmed ${formatTime(order.confirmed_at)}`;
  else if (order.cancelled_at) timer.textContent = `Cancelled ${formatTime(order.cancelled_at)}`;

  const act = (verb) => async (event) => {
    event.target.disabled = true;
    try {
      const updated = await api(`/orders/${order.id}/${verb}`, { method: "POST" });
      if (updated.status === "CANCELLED") {
        // Nothing left to act on: close the summary instead of leaving a dead plan on screen.
        clearInterval(state.countdown);
        const notice = el("div");
        showAlert(notice, "info", `Order ${order.id.slice(0, 8)} cancelled. Its stock is free again.`);
        container.replaceChildren(notice);
      } else {
        renderOrder(container, updated, { onChange });
      }
      onChange?.();
    } catch (error) {
      showAlert(message, "error", error.message);
      if (error.plan) {
        container.append(el("div", { class: "card" }, el("h3", {}, "Current best plan"), renderPlan(error.plan), el("div", { class: "actions" }, el("button", { class: "btn primary", type: "button", onclick: () => reorder(order) }, "Place again with current stock"))));
      }
      onChange?.();
    }
  };

  container.replaceChildren(
    el(
      "div",
      { class: "card" },
      header,
      dispatch,
      timer,
      message,
      renderPlan(order.plan),
      actionable &&
        el(
          "div",
          { class: "actions" },
          el("button", { class: "btn primary", type: "button", onclick: act("confirm") }, "Confirm order"),
          el("button", { class: "btn ghost", type: "button", onclick: act("cancel") }, "Cancel"),
        ),
    ),
  );
}

function startCountdown(node, expiresAt) {
  const tick = () => {
    const left = Math.max(0, new Date(expiresAt) - Date.now());
    if (left === 0) {
      clearInterval(state.countdown);
      node.className = "alert warn";
      node.textContent = "The reservation has lapsed. Confirming will re-reserve the same stock if it is still free.";
      return;
    }
    const m = Math.floor(left / 60000);
    const s = String(Math.floor((left % 60000) / 1000)).padStart(2, "0");
    node.replaceChildren("Stock reserved for ", el("span", { class: "countdown" }, `${m}:${s}`), ". Confirm or cancel before it lapses.");
  };
  tick();
  state.countdown = setInterval(tick, 1000);
}

async function reorder(order) {
  const result = document.getElementById("order-result");
  location.hash = "order";
  try {
    const created = await api("/orders", {
      method: "POST",
      body: {
        urgency: order.urgency,
        dispatch_at: order.dispatch_at,
        lines: order.lines.map((l) => ({ product_id: l.product_id, quantity: l.quantity })),
      },
    });
    renderOrder(result, created);
  } catch (error) {
    showOrderError(error);
  }
}

// ---------- new order ----------

function addLine(productId = "", quantity = 1) {
  const select = el(
    "select",
    { name: "product", required: true, "aria-label": "Product" },
    el("option", { value: "" }, "Select a product…"),
    state.products.map((p) => el("option", { value: p.id, selected: p.id === productId }, `${p.name}${isCold(p) ? "  ❄ 2–8 °C" : ""}`)),
  );
  const qty = el("input", { name: "quantity", type: "number", min: 1, max: 100000, value: quantity, required: true, "aria-label": "Quantity" });
  const row = el("div", { class: "line" }, select, qty, el("button", { type: "button", class: "btn icon", "aria-label": "Remove product", onclick: () => row.remove() }, "×"));
  document.getElementById("lines").append(row);
}

function showOrderError(error) {
  const box = document.getElementById("order-error");
  showAlert(box, "error", error.message);
  const result = document.getElementById("order-result");
  result.replaceChildren(error.plan ? el("div", { class: "card" }, el("h2", {}, "Why it can't be fulfilled"), renderPlan(error.plan)) : "");
}

async function submitOrder(event) {
  event.preventDefault();
  const form = event.target;
  const button = form.querySelector("button[type=submit]");
  showAlert(document.getElementById("order-error"), "error", "");
  const lines = [...form.querySelectorAll(".line")].map((row) => ({
    product_id: row.querySelector("select").value,
    quantity: Number(row.querySelector("input").value),
  }));
  if (!lines.length) return showAlert(document.getElementById("order-error"), "error", "Add at least one product.");

  button.disabled = true;
  try {
    const body = { urgency: form.urgency.value, lines };
    if (form.dispatch_at.value) body.dispatch_at = form.dispatch_at.value; // read as WIB by the API
    const order = await api("/orders", { method: "POST", body });
    renderOrder(document.getElementById("order-result"), order);
  } catch (error) {
    showOrderError(error);
  } finally {
    button.disabled = false;
  }
}

// ---------- orders ----------

async function loadOrders() {
  const orders = await api("/orders");
  const tbody = document.querySelector("#orders-table tbody");
  document.getElementById("orders-empty").hidden = orders.length > 0;
  tbody.replaceChildren(
    ...orders.map((order) => {
      const row = el(
        "tr",
        { tabindex: 0 },
        el("td", {}, formatTime(order.created_at)),
        el("td", {}, el("span", { class: `badge ${order.status}` }, order.status)),
        el("td", {}, el("span", { class: `badge ${order.urgency}` }, order.urgency)),
        el("td", {}, order.lines.map((l) => el("div", {}, `${productName(l.product_id)} × ${l.quantity}`, l.backordered_quantity ? el("small", {}, `${l.backordered_quantity} backordered`) : null))),
        el("td", {}, order.plan.shipments.map((s) => s.warehouse_id).join(" + ")),
      );
      const open = () => {
        tbody.querySelectorAll("tr").forEach((r) => r.removeAttribute("aria-selected"));
        row.setAttribute("aria-selected", "true");
        renderOrder(document.getElementById("orders-detail"), order, { onChange: loadOrders });
      };
      row.addEventListener("click", open);
      row.addEventListener("keydown", (e) => e.key === "Enter" && open());
      return row;
    }),
  );
}

// ---------- stock ----------

async function loadStock() {
  const [warehouses, deliveryTimes] = await Promise.all([api("/warehouses"), api("/delivery-times")]);
  state.warehouses = warehouses;
  renderDeliveryTimes(deliveryTimes);
  const products = state.products;
  const table = document.getElementById("stock-table");
  table.replaceChildren(
    el(
      "thead",
      {},
      el("tr", {}, el("th", {}, "Warehouse"), el("th", { class: "num" }, "Distance"), el("th", {}, "Cold chain"), products.map((p) => el("th", { class: "num", title: p.name }, p.id, isCold(p) ? " ❄" : ""))),
    ),
    el(
      "tbody",
      {},
      state.warehouses.map((w) => {
        const levels = Object.fromEntries(w.stock.map((s) => [s.product_id, s]));
        return el(
          "tr",
          {},
          el("td", {}, el("strong", {}, w.id), " ", w.city),
          el("td", { class: "num" }, `${w.distance_km} km`, el("small", {}, minutes(w.eta_minutes))),
          el("td", {}, w.can_ship_cold_chain ? el("span", { class: "badge cold" }, "Full") : w.has_cold_storage ? el("span", { class: "muted" }, "Storage only") : el("span", { class: "muted" }, "None")),
          products.map((p) => {
            const level = levels[p.id];
            if (!level) return el("td", { class: "num cell-zero" }, "—");
            return el(
              "td",
              { class: `num ${level.available === 0 ? "cell-zero" : ""}` },
              `${level.available} / ${level.on_hand}`,
              level.reserved ? el("span", { class: "cell-reserved" }, `${level.reserved} reserved`) : null,
            );
          }),
        );
      }),
    ),
  );

  const form = document.getElementById("restock-form");
  const current = { w: form.warehouse.value, p: form.product.value };
  form.warehouse.replaceChildren(...state.warehouses.map((w) => el("option", { value: w.id, selected: w.id === current.w }, `${w.id} — ${w.city}`)));
  form.product.replaceChildren(...products.map((p) => el("option", { value: p.id, selected: p.id === current.p }, p.name)));
}

function renderDeliveryTimes(rows) {
  const periods = rows[0]?.periods ?? [];
  document.getElementById("eta-table").replaceChildren(
    el("thead", {}, el("tr", {}, el("th", {}, "Warehouse"), periods.map((p) => el("th", { class: "num" }, p.label)))),
    el(
      "tbody",
      {},
      rows.map((w) =>
        el(
          "tr",
          {},
          el("td", {}, el("strong", {}, w.warehouse_id), " ", w.city, el("small", {}, `${w.distance_km} km`)),
          w.periods.map((p) => {
            const value = `${Math.round(p.minutes)} min`;
            const shown = {
              WAREHOUSE: () => el("b", {}, value),
              ROUTE_TYPE: () => el("i", { class: "borrowed" }, value),
              DISTANCE: () => el("span", { class: "baseline" }, value),
            }[p.source]();
            return el("td", { class: "num", title: p.basis }, shown, el("small", {}, p.samples ? `n=${p.samples}` : "no data"));
          }),
        ),
      ),
    ),
  );
}

async function submitRestock(event) {
  event.preventDefault();
  const form = event.target;
  const message = document.getElementById("restock-message");
  try {
    const result = await api(`/warehouses/${encodeURIComponent(form.warehouse.value)}/restock`, {
      method: "POST",
      body: { product_id: form.product.value, quantity: Number(form.quantity.value) },
    });
    showAlert(message, "ok", `${result.warehouse_id} now holds ${result.on_hand} × ${productName(result.product_id)}.`);
    await loadStock();
  } catch (error) {
    showAlert(message, "error", error.message);
  }
}

async function resetStock() {
  if (!confirm("Restore baseline stock in every warehouse? Pending orders will be cancelled.")) return;
  const result = await api("/stock/reset", { method: "POST" });
  showAlert(document.getElementById("restock-message"), "info", `Stock restored to baseline. ${result.cancelled_orders} pending order(s) cancelled.`);
  await loadStock();
}

// ---------- boot ----------

async function init() {
  state.products = await api("/products");
  state.productsById = Object.fromEntries(state.products.map((p) => [p.id, p]));

  addLine();
  document.getElementById("add-line").addEventListener("click", () => addLine());
  document.getElementById("order-form").addEventListener("submit", submitOrder);
  document.getElementById("refresh-orders").addEventListener("click", loadOrders);
  document.getElementById("restock-form").addEventListener("submit", submitRestock);
  document.getElementById("reset-stock").addEventListener("click", resetStock);
  window.addEventListener("hashchange", route);
  route();
}

init().catch((error) => {
  document.querySelector("main").prepend(el("div", { class: "alert error" }, `Could not load the app: ${error.message}`));
});
