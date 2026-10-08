// Pure view-model helpers for the HubSpot AI web assistant.
// No DOM access here: app.js turns these models into elements with textContent only,
// so CRM data and user text can never be interpreted as HTML.

const NEEDS_INPUT = new Set([
  "missing_fields",
  "invalid_request",
  "needs_clarification",
  "company_ambiguous",
  "contact_ambiguous",
  "deal_ambiguous",
]);

/** Status treatment for an agent reply: tone drives color, title drives the headline. */
export function statusOf(reply) {
  const result = reply.result || {};
  switch (true) {
    case reply.status === "ok":
      return {
        tone: "success",
        title:
          result.kind === "contact_created"
            ? "Contact created"
            : result.title || "Here's what I found",
      };
    case reply.status === "already_exists":
      return { tone: "info", title: result.title || "Already exists" };
    case reply.status === "duplicate_request":
      return { tone: "info", title: "Already processed" };
    case NEEDS_INPUT.has(reply.status):
      return { tone: "info", title: "More information needed" };
    case reply.status === "pending_confirmation":
      return { tone: "confirm", title: "Confirmation required" };
    case reply.status === "partial":
      return { tone: "warning", title: "Partially completed" };
    default:
      return { tone: "error", title: "Unable to complete request" };
  }
}

/** One-sentence summary shown under the headline, when the backend provides one. */
export function messageOf(reply) {
  const result = reply.result || {};
  if (reply.status === "ok" && result.kind === "contact_created" && result.name) {
    const company = result.company_name ? ` and associated with ${result.company_name}` : "";
    return `${result.name} was successfully added to HubSpot${company}.`;
  }
  return result.message || null;
}

/** Record cards: what was created or linked, without raw IDs leaking into prose. */
export function cardsOf(reply) {
  if (Array.isArray(reply.cards) && reply.cards.length > 0) {
    return reply.cards.map((card) => ({
      kind: card.kind || "record",
      label: KIND_LABELS[card.kind] || "Record",
      title: card.title || "",
      name: card.name || "",
      detail: card.detail || null,
      hubspotId: card.hubspot_id || null,
      hubspotUrl: card.hubspot_url || null,
    }));
  }
  const result = reply.result || {};
  if (reply.status === "ok" && result.kind === "contact_created") {
    return [
      {
        kind: "contact",
        label: KIND_LABELS.contact,
        title: "Contact created",
        name: result.name || "",
        detail: result.email || null,
        hubspotId: result.contact_id || null,
        company: result.company_name || null,
        hubspotUrl: result.hubspot_url || null,
      },
    ];
  }
  return [];
}

/** Structured CRM tables are rendered with textContent by app.js, never as HTML. */
export function tableOf(reply) {
  const table = (reply.result || {}).table;
  if (
    !table ||
    !Array.isArray(table.columns) ||
    !Array.isArray(table.rows) ||
    !table.columns.every((column) => column && typeof column.key === "string" && typeof column.label === "string")
  ) {
    return null;
  }
  return table;
}

const KIND_LABELS = {
  company: "Company",
  contact: "Contact",
  deal: "Deal",
  link: "Association",
};

/** The pending action a confirm button should approve, if any. */
export function confirmationOf(reply) {
  const result = reply.result || {};
  if (reply.status !== "pending_confirmation" || result.kind !== "pending_confirmation") {
    return null;
  }
  if (!/^[a-f0-9]{32}$/.test(result.action_id || "")) {
    return null;
  }
  const isDelete = result.action_type === "delete_contact";
  return {
    actionId: result.action_id,
    verb: isDelete ? "Delete" : "Update",
    destructive: isDelete,
    record: result.record_label || (result.contact_id ? `contact ${result.contact_id}` : "record"),
  };
}

/**
 * Body text to show beneath the headline. Lines already represented elsewhere (record
 * cards, the confirm button, the one-line summary) are removed to keep replies concise.
 */
export function bodyTextOf(reply) {
  const lines = (reply.text || "").split("\n");
  const hasCards = cardsOf(reply).length > 0;
  const kept = lines.filter((line) => {
    const trimmed = line.trim();
    if (reply.status === "pending_confirmation") {
      return !trimmed.startsWith("Action ID:") && !trimmed.startsWith("Reply with");
    }
    if (hasCards && trimmed.startsWith("✅")) return false;
    if (reply.status === "ok" && (reply.result || {}).kind === "contact_created") return false;
    if (messageOf(reply) && reply.status === "ok" && !hasCards) return false;
    return true;
  });
  return kept.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

/**
 * Minimal rich text: lines, "• " bullets, *bold* and `code`. Returns segments, not HTML.
 */
export function parseRichText(text) {
  if (!text) return [];
  return text.split("\n").map((raw) => {
    const bullet = /^\s*[•\-]\s+/.test(raw);
    const content = bullet ? raw.replace(/^\s*[•\-]\s+/, "") : raw;
    const segments = [];
    const pattern = /(\*[^*\n]+\*|`[^`\n]+`)/g;
    let last = 0;
    for (const match of content.matchAll(pattern)) {
      if (match.index > last) segments.push({ text: content.slice(last, match.index) });
      const token = match[0];
      if (token.startsWith("*")) segments.push({ text: token.slice(1, -1), bold: true });
      else segments.push({ text: token.slice(1, -1), code: true });
      last = match.index + token.length;
    }
    if (last < content.length) segments.push({ text: content.slice(last) });
    return { bullet, blank: content.trim() === "", segments };
  });
}

/** Enter sends; Shift+Enter (or IME composition) inserts a new line. */
export function shouldSend(event) {
  return event.key === "Enter" && !event.shiftKey && !event.isComposing;
}

/** Compact relative time for the activity list. */
export function formatRelative(iso, now = new Date()) {
  const then = new Date(iso);
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (Number.isNaN(seconds)) return "";
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d ago`;
  return then.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
