// Run with: node --test tests/frontend/*.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

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
} from "../../src/app/web/static/render.js";

const companyCreated = {
  status: "ok",
  text: "✅ Company created: Demo AI Company 001",
  result: {
    kind: "crm_records",
    title: "Company created",
    message: "Demo AI Company 001 was successfully added to HubSpot.",
  },
  cards: [
    { kind: "company", title: "Company created", name: "Demo AI Company 001", hubspot_id: "123" },
  ],
};

test("each status gets a distinct tone and headline", () => {
  const cases = [
    [companyCreated, "success", "Company created"],
    [{ status: "already_exists", result: { title: "Company already exists" } }, "info", "Company already exists"],
    [{ status: "company_ambiguous" }, "info", "More information needed"],
    [{ status: "pending_confirmation" }, "confirm", "Confirmation required"],
    [{ status: "partial" }, "warning", "Partially completed"],
    [{ status: "insufficient_scope" }, "error", "Unable to complete request"],
    [{ status: "unavailable" }, "error", "Unable to complete request"],
  ];
  for (const [reply, tone, title] of cases) {
    assert.deepEqual(statusOf(reply), { tone, title });
  }
});

test("company creation renders a summary and a record card, not the raw step line", () => {
  assert.equal(messageOf(companyCreated), "Demo AI Company 001 was successfully added to HubSpot.");
  assert.deepEqual(cardsOf(companyCreated), [
    {
      kind: "company",
      label: "Company",
      title: "Company created",
      name: "Demo AI Company 001",
      detail: null,
      hubspotId: "123",
      hubspotUrl: null,
    },
  ]);
  assert.equal(bodyTextOf(companyCreated), "");
});

test("deal result tables expose safe structured columns and rows", () => {
  const reply = {
    status: "ok",
    result: {
      table: {
        columns: [{ key: "name", label: "Name" }, { key: "probability", label: "Probability" }],
        rows: [{ name: "Renewal", probability: "80%" }],
      },
    },
  };

  assert.deepEqual(tableOf(reply), reply.result.table);
  assert.equal(tableOf({ result: { table: { columns: "bad", rows: [] } } }), null);
});

test("HubSpot record URLs are retained for record cards and table rows", () => {
  const url = "https://app.hubspot.com/contacts/42/record/0-2/company-1";
  const reply = {
    status: "ok",
    cards: [{ kind: "company", name: "Testing Corp", hubspot_url: url }],
    result: {
      table: {
        columns: [{ key: "view_url", label: "View in HubSpot" }],
        rows: [{ view_url: url }],
      },
    },
  };

  assert.equal(cardsOf(reply)[0].hubspotUrl, url);
  assert.equal(tableOf(reply).rows[0].view_url, url);
});

test("contact creation from the original contact flow becomes a contact card", () => {
  const reply = {
    status: "ok",
    text: "Contact John Smith was created successfully in HubSpot.\n• Contact ID: `9`",
    result: { kind: "contact_created", contact_id: "9", name: "John Smith", email: "john@example.com", company_name: "Acme" },
  };

  assert.equal(statusOf(reply).title, "Contact created");
  assert.equal(messageOf(reply), "John Smith was successfully added to HubSpot and associated with Acme.");
  assert.equal(cardsOf(reply)[0].detail, "john@example.com");
  assert.equal(cardsOf(reply)[0].company, "Acme");
  assert.equal(bodyTextOf(reply), "");
});

test("multi-step replies keep informational and failure lines next to the cards", () => {
  const reply = {
    status: "partial",
    text: "✅ Company created: TechNova\nℹ️ Contact John already exists — used the existing record\n❌ Associating failed: HubSpot returned an error.\n\nThe steps marked ✅ were saved in HubSpot; nothing was rolled back.",
    cards: [{ kind: "company", name: "TechNova", hubspot_id: "1" }],
  };

  const body = bodyTextOf(reply);
  assert.ok(!body.includes("Company created: TechNova"));
  assert.ok(body.includes("already exists"));
  assert.ok(body.includes("❌ Associating failed"));
});

test("confirmation replies expose a validated action and hide typed instructions", () => {
  const reply = {
    status: "pending_confirmation",
    text: "I found a request to delete this HubSpot contact:\n• Contact ID: 9\n\nAction ID: `0123456789abcdef0123456789abcdef`\nReply with `confirm 0123456789abcdef0123456789abcdef` to delete this contact.",
    result: { kind: "pending_confirmation", action_id: "0123456789abcdef0123456789abcdef", action_type: "delete_contact", contact_id: "9" },
  };

  assert.deepEqual(confirmationOf(reply), {
    actionId: "0123456789abcdef0123456789abcdef",
    verb: "Delete",
    destructive: true,
    record: "contact 9",
  });
  assert.ok(!bodyTextOf(reply).includes("Reply with"));
  assert.equal(
    confirmationOf({ ...reply, result: { ...reply.result, action_id: "<script>" } }),
    null,
  );
});

test("rich text is parsed into plain segments, never HTML", () => {
  const lines = parseRichText("*Acme* <img src=x onerror=alert(1)>\n• Domain: `acme.com`");

  assert.deepEqual(lines[0].segments, [
    { text: "Acme", bold: true },
    { text: " <img src=x onerror=alert(1)>" },
  ]);
  assert.equal(lines[1].bullet, true);
  assert.deepEqual(lines[1].segments, [{ text: "Domain: " }, { text: "acme.com", code: true }]);
});

test("Enter sends, Shift+Enter and IME composition do not", () => {
  assert.equal(shouldSend({ key: "Enter", shiftKey: false, isComposing: false }), true);
  assert.equal(shouldSend({ key: "Enter", shiftKey: true, isComposing: false }), false);
  assert.equal(shouldSend({ key: "Enter", shiftKey: false, isComposing: true }), false);
  assert.equal(shouldSend({ key: "a", shiftKey: false, isComposing: false }), false);
});

test("relative times are compact", () => {
  const now = new Date("2026-10-07T12:00:00Z");
  assert.equal(formatRelative("2026-10-07T11:59:40Z", now), "just now");
  assert.equal(formatRelative("2026-10-07T11:45:00Z", now), "15m ago");
  assert.equal(formatRelative("2026-10-07T09:00:00Z", now), "3h ago");
});
