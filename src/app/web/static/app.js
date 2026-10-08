// HubSpot AI web assistant. Talks only to this origin's /api/v1/assistant endpoints;
// every request is answered by the same backend agent that serves Slack.
import {
  bodyTextOf,
  cardsOf,
  confirmationOf,
  formatRelative,
  messageOf,
  parseRichText,
  shouldSend,
  statusOf,
  tableOf,
} from "./render.js";

const API = "/api/v1/assistant";
const SVG_NS = "http://www.w3.org/2000/svg";

// Static, trusted icon paths (never built from data).
const ICONS = {
  spark: { fill: true, d: "M12 2.5l1.9 6.2 6.1 2-6.1 2-1.9 6.3-1.9-6.3-6.1-2 6.1-2z" },
  success: { d: "M5 12.5l4.2 4.2L19 7" },
  info: { d: "M12 11v5.5M12 7.8v.2" },
  warning: { d: "M12 7.5v5.5M12 16.2v.2" },
  error: { d: "M12 7.5v5.5M12 16.2v.2" },
  confirm: { d: "M12 3l7 3v6c0 4.5-3 7.8-7 9-4-1.2-7-4.5-7-9V6z" },
  pending: { d: "M12 7v5l3 2" },
  link: { d: "M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1" },
};

// --------------------------------------------------------------- DOM helpers

function el(tag, { className, text, attrs } = {}, children = []) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  if (attrs) for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  for (const child of children) if (child) node.append(child);
  return node;
}

function icon(name) {
  const spec = ICONS[name];
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", spec.d);
  if (spec.fill) {
    svg.style.fill = "currentColor";
    svg.style.stroke = "none";
  }
  svg.append(path);
  return svg;
}

function aiLabel() {
  return el("div", { className: "ai-label" }, [
    el("span", { className: "ai-mark" }, [icon("spark")]),
    el("span", { text: "AI Assistant" }),
  ]);
}

function richText(text) {
  const container = el("div", { className: "body" });
  for (const line of parseRichText(text)) {
    const paragraph = el("p", { className: line.bullet ? "bullet" : undefined });
    for (const segment of line.segments) {
      if (segment.bold) paragraph.append(el("strong", { text: segment.text }));
      else if (segment.code) paragraph.append(el("code", { text: segment.text }));
      else paragraph.append(document.createTextNode(segment.text));
    }
    container.append(paragraph);
  }
  return container;
}

// ----------------------------------------------------------------- renderers

export function renderUserMessage(text) {
  return el("div", { className: "message message--user" }, [
    el("div", { className: "bubble", text }),
  ]);
}

export function renderThinking() {
  return el("div", { className: "message message--ai", attrs: { "aria-label": "HubSpot AI is thinking" } }, [
    aiLabel(),
    el("div", { className: "typing" }, [el("span"), el("span"), el("span")]),
  ]);
}

function renderRecord(card) {
  if (card.kind === "link") {
    return el("div", { className: "record record--link" }, [
      icon("link"),
      el("span", { className: "record-name", text: card.name }),
    ]);
  }
  const meta = card.hubspotId
    ? el("div", { className: "record-meta" }, [
        el("span", { text: "HubSpot ID" }),
        el("code", { text: card.hubspotId }),
      ])
    : null;
  const company = card.company
    ? el("div", { className: "record-detail", text: `Company · ${card.company}` })
    : null;
  const hubspotLink = safeHubSpotLink(card.hubspotUrl);
  return el("div", { className: "record" }, [
    el("div", { className: "record-kind", text: card.label }),
    el("div", { className: "record-name", text: card.name }),
    card.detail ? el("div", { className: "record-detail", text: card.detail }) : null,
    company,
    meta,
    hubspotLink,
  ]);
}

function safeHubSpotLink(url) {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || parsed.hostname !== "app.hubspot.com") return null;
    return el("a", {
      className: "hubspot-record-link",
      text: "View in HubSpot",
      attrs: { href: parsed.href, target: "_blank", rel: "noopener noreferrer" },
    });
  } catch {
    return null;
  }
}

function renderTable(tableModel) {
  const isCompanyTable = tableModel.columns.some((column) => column.key === "employees");
  const table = el("table", {
    className: `crm-table${isCompanyTable ? " crm-table--company" : ""}`,
  });
  const head = el("thead");
  const header = el("tr");
  for (const column of tableModel.columns) {
    header.append(el("th", {
      className: column.key === "employees" ? "crm-table__employees" : "",
      text: column.label,
      attrs: { scope: "col" },
    }));
  }
  head.append(header);
  table.append(head);

  const body = el("tbody");
  for (const row of tableModel.rows) {
    const tr = el("tr");
    for (const column of tableModel.columns) {
      const cell = el("td", {
        className: column.key === "employees" ? "crm-table__employees" : "",
      });
      if (column.key === "view_url") {
        const link = safeHubSpotLink(row[column.key]);
        if (link) cell.append(link);
        else cell.textContent = "—";
      } else {
        cell.textContent = row[column.key] == null ? "" : String(row[column.key]);
      }
      tr.append(cell);
    }
    body.append(tr);
  }
  table.append(body);
  return el("div", { className: "table-wrap", attrs: { role: "region", "aria-label": "CRM deal results", tabindex: "0" } }, [table]);
}

export function renderReply(reply, { onConfirm } = {}) {
  const status = statusOf(reply);
  const card = el("article", { className: "card", attrs: { "data-tone": status.tone } }, [
    el("div", { className: "status" }, [
      el("span", { className: "status-icon" }, [icon(status.tone)]),
      el("span", { text: status.title }),
    ]),
  ]);

  const summary = messageOf(reply);
  if (summary) card.append(el("p", { className: "summary", text: summary }));

  const body = bodyTextOf(reply);
  if (body) card.append(richText(body));

  const records = cardsOf(reply);
  if (records.length) card.append(el("div", { className: "records" }, records.map(renderRecord)));

  const table = tableOf(reply);
  if (table) card.append(renderTable(table));
  else if (!records.length) {
    const resultLink = safeHubSpotLink((reply.result || {}).hubspot_url);
    if (resultLink) card.append(resultLink);
  }

  const confirmation = confirmationOf(reply);
  if (confirmation && onConfirm) {
    const button = el("button", {
      className: `button ${confirmation.destructive ? "button--danger" : "button--primary"}`,
      text: `Confirm ${confirmation.verb.toLowerCase()}`,
      attrs: { type: "button" },
    });
    button.addEventListener("click", () => {
      button.disabled = true;
      onConfirm(confirmation);
    });
    card.append(
      el("div", { className: "actions" }, [
        button,
        el("span", { className: "hint", text: "Expires in 5 minutes" }),
      ]),
    );
  }

  return el("div", { className: "message message--ai" }, [aiLabel(), card]);
}

function renderActivity(items) {
  const list = document.getElementById("activity-list");
  list.replaceChildren();
  if (!items.length) {
    list.append(el("li", { className: "activity-empty", text: "Your CRM changes will appear here." }));
    return;
  }
  const toneColors = {
    success: "var(--success)",
    info: "var(--info)",
    warning: "var(--warning)",
    error: "var(--error)",
    pending: "var(--confirm)",
  };
  for (const item of items.slice(0, 6)) {
    const badge = el("span", { className: "activity-icon" }, [icon(item.tone === "pending" ? "pending" : item.tone)]);
    badge.style.color = toneColors[item.tone] || "var(--muted)";
    badge.style.background = `color-mix(in srgb, ${toneColors[item.tone] || "var(--muted)"} 14%, transparent)`;
    const meta = [item.detail, formatRelative(item.created_at)].filter(Boolean).join(" · ");
    list.append(
      el("li", { className: "activity-item" }, [
        badge,
        el("div", {}, [
          el("span", { className: "activity-title", text: item.title }),
          el("span", { className: "activity-meta", text: meta, attrs: { title: meta } }),
        ]),
      ]),
    );
  }
}

// ---------------------------------------------------------------- behaviour

function setConnection(state, text) {
  const node = document.getElementById("connection");
  node.dataset.state = state;
  document.getElementById("connection-text").textContent = text;
}

function showGate(expired) {
  document.getElementById("gate").hidden = false;
  if (expired) {
    document.getElementById("gate-title").textContent = "This sign-in link has expired";
    document.getElementById("gate-text").textContent =
      "Sign-in links from Slack are valid for a short time. Open the HubSpot AI Agent app in Slack and choose Open HubSpot AI again.";
  }
}

async function loadSession({ linkExpired = false } = {}) {
  try {
    const response = await fetch(`${API}/session`, { credentials: "same-origin" });
    if (response.status === 401) {
      showGate(linkExpired);
      setConnection("disconnected", "Signed out");
      return false;
    }
    if (!response.ok) throw new Error(`session ${response.status}`);
    const session = await response.json();
    setConnection(
      session.hubspot_connected ? "connected" : "disconnected",
      session.hubspot_connected ? "Connected" : "HubSpot not connected",
    );
    renderActivity(session.recent || []);
    return true;
  } catch {
    setConnection("disconnected", "Offline");
    return false;
  }
}

function boot() {
  const thread = document.getElementById("thread");
  const emptyState = document.getElementById("empty-state");
  const conversation = document.getElementById("conversation");
  const form = document.getElementById("composer");
  const input = document.getElementById("composer-input");
  const sendButton = document.getElementById("send-button");
  let pending = false;

  const scrollToEnd = () => {
    conversation.scrollTop = conversation.scrollHeight;
  };

  const resize = () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
  };

  const syncSend = () => {
    sendButton.disabled = pending || input.value.trim() === "";
  };

  const closeNav = () => {
    document.body.classList.remove("nav-open");
    document.getElementById("scrim").hidden = true;
  };

  async function send(message, display = message) {
    if (pending || !message.trim()) return;
    pending = true;
    syncSend();
    emptyState.hidden = true;
    thread.append(renderUserMessage(display));
    const thinking = renderThinking();
    thread.append(thinking);
    scrollToEnd();

    let reply;
    try {
      const response = await fetch(`${API}/messages`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, client_message_id: crypto.randomUUID() }),
      });
      if (response.status === 401) {
        showGate(false);
        reply = { status: "unauthenticated", text: "Your session has ended. Open HubSpot AI from Slack to continue." };
      } else if (!response.ok) {
        reply = { status: "unavailable", text: "HubSpot AI couldn't process that request. Please try again." };
      } else {
        reply = await response.json();
      }
    } catch {
      reply = { status: "unavailable", text: "You appear to be offline. Check your connection and try again." };
    }

    thinking.replaceWith(
      renderReply(reply, {
        onConfirm: (confirmation) =>
          send(`confirm ${confirmation.actionId}`, `Confirm ${confirmation.verb.toLowerCase()} of ${confirmation.record}`),
      }),
    );
    pending = false;
    syncSend();
    scrollToEnd();
    loadSession();
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const message = input.value.trim();
    if (!message || pending) return;
    input.value = "";
    resize();
    send(message);
  });

  input.addEventListener("keydown", (event) => {
    if (shouldSend(event)) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  input.addEventListener("input", () => {
    resize();
    syncSend();
  });

  for (const button of document.querySelectorAll("[data-prompt]")) {
    button.addEventListener("click", () => {
      input.value = button.dataset.prompt;
      resize();
      syncSend();
      closeNav();
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    });
  }

  document.getElementById("menu-button").addEventListener("click", () => {
    document.body.classList.add("nav-open");
    document.getElementById("scrim").hidden = false;
  });
  document.getElementById("scrim").addEventListener("click", closeNav);

  const linkExpired = new URLSearchParams(location.search).get("link") === "expired";
  if (linkExpired) {
    history.replaceState(null, "", "/assistant");
  }
  loadSession({ linkExpired });
  input.focus();
}

if (document.body?.dataset.app === "assistant") {
  boot();
}
