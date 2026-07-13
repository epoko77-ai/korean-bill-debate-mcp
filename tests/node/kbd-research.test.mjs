import assert from "node:assert/strict";
import test from "node:test";

process.env.KBD_INTERNAL_TASK_SECRET = "i".repeat(48);
process.env.VERCEL_DEPLOYMENT_ID = "dpl_queue_test";
process.env.VERCEL = "1";
process.env.VERCEL_URL = "kbd-current-deployment.vercel.app";
process.env.VERCEL_REGION = "icn1";
const encode = (value) => Buffer.from(JSON.stringify(value)).toString("base64url");
const TEST_OIDC_TOKEN = `${encode({ alg: "RS256", typ: "JWT" })}.${encode({
  exp: 4_102_444_800,
})}.test-signature`;
process.env.VERCEL_OIDC_TOKEN = TEST_OIDC_TOKEN;

const queueModule = await import("../../api/queues/kbd-research.ts");
const { POST } = queueModule;
const FETCH = queueModule.default.fetch;

const TASK = {
  schema_version: 1,
  research_id: "research_queue_test",
  stage: "collect_metadata",
  work_id: "metadata-page-1",
  query_fingerprint: "a".repeat(64),
  index_revision: "index-test",
  payload: { page: 1 },
  credential_capability: null,
};

function callbackRequest(deliveryCount) {
  return new Request(
    "https://kbd-test-deployment.vercel.app/api/queues/kbd-research",
    {
      method: "POST",
      headers: {
        "ce-type": "com.vercel.queue.v2beta",
        "ce-vqsqueuename": "kbd-research",
        "ce-vqsconsumergroup": "kbd-workers",
        "ce-vqsmessageid": `msg-${deliveryCount}`,
        "ce-vqsregion": "icn1",
        "ce-vqsreceipthandle": `receipt-${deliveryCount}`,
        "ce-vqsdeliverycount": String(deliveryCount),
        "ce-vqscreatedat": "2026-07-13T00:00:00.000Z",
        "ce-vqsexpiresat": "2026-07-14T00:00:00.000Z",
        "ce-vqsvisibilitydeadline": "2099-07-13T00:10:00.000Z",
        "content-type": "application/json",
        "x-vercel-oidc-token": "header.queue.request.token.value",
      },
      body: JSON.stringify(TASK),
    },
  );
}

function installFetch(internalStatus) {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (input, init = {}) => {
    const url = input instanceof Request ? input.url : String(input);
    const method = init.method ?? (input instanceof Request ? input.method : "GET");
    const headers = new Headers(init.headers);
    calls.push({ url, method, headers, body: init.body });
    if (url.endsWith("/_internal/research/dispatch")) {
      return new Response("{}", { status: internalStatus });
    }
    if (url.includes("vercel-queue.com")) {
      return new Response(null, { status: 204 });
    }
    throw new Error(`unexpected fetch: ${method} ${url}`);
  };
  return {
    calls,
    restore() {
      globalThis.fetch = original;
    },
  };
}

test("forwards request OIDC identity and acknowledges successful Python work", async () => {
  const fake = installFetch(200);
  try {
    // A framework can invoke the named POST export, while the raw `/api/*.ts`
    // Vercel Function runtime uses the default Web `fetch` export.
    const response = await FETCH(callbackRequest(1));
    assert.equal(response.status, 200);
    const internal = fake.calls.find((call) =>
      call.url.endsWith("/_internal/research/dispatch"),
    );
    assert.equal(
      internal.url,
      "https://kbd-current-deployment.vercel.app/_internal/research/dispatch",
    );
    assert.ok(internal);
    assert.equal(internal.headers.get("x-kbd-delivery-count"), "1");
    assert.equal(
      internal.headers.get("x-vercel-oidc-token"),
      "header.queue.request.token.value",
    );
    assert.equal(
      internal.headers.get("x-vercel-trusted-oidc-idp-token"),
      "header.queue.request.token.value",
    );
    assert.deepEqual(JSON.parse(internal.body), TASK);
    assert.ok(
      fake.calls.some(
        (call) =>
          call.url.includes("vercel-queue.com") && call.method === "DELETE",
      ),
    );
  } finally {
    fake.restore();
  }
});

test("never acknowledges a transient worker outage, even on delivery ten", async () => {
  const fake = installFetch(503);
  try {
    const response = await POST(callbackRequest(10));
    assert.equal(response.status, 200);
    assert.ok(
      fake.calls.some(
        (call) =>
          call.url.includes("vercel-queue.com") && call.method === "PATCH",
      ),
    );
    assert.ok(
      !fake.calls.some(
        (call) =>
          call.url.includes("vercel-queue.com") && call.method === "DELETE",
      ),
    );
  } finally {
    fake.restore();
  }
});

test("acknowledges a proven permanent task failure after bounded retries", async () => {
  const fake = installFetch(400);
  try {
    const response = await POST(callbackRequest(3));
    assert.equal(response.status, 200);
    assert.ok(
      fake.calls.some(
        (call) =>
          call.url.includes("vercel-queue.com") && call.method === "DELETE",
      ),
    );
  } finally {
    fake.restore();
  }
});
