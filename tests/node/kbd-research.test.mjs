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
const NODE_HANDLER = queueModule.default;
const FETCH = POST;

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

function nodeCallback(deliveryCount) {
  const request = callbackRequest(deliveryCount);
  const result = { statusCode: 0, body: null };
  return {
    request: {
      method: "POST",
      headers: Object.fromEntries(request.headers.entries()),
      body: TASK,
    },
    response: {
      status(code) {
        result.statusCode = code;
        return this;
      },
      json(body) {
        result.body = body;
      },
      end() {},
    },
    result,
  };
}

function installFetch(internalResponses) {
  const calls = [];
  const responses = Array.isArray(internalResponses)
    ? [...internalResponses]
    : [internalResponses];
  const original = globalThis.fetch;
  globalThis.fetch = async (input, init = {}) => {
    const url = input instanceof Request ? input.url : String(input);
    const method = init.method ?? (input instanceof Request ? input.method : "GET");
    const headers = new Headers(init.headers);
    calls.push({ url, method, headers, body: init.body });
    if (url.endsWith("/_internal/research/dispatch")) {
      const outcome = responses.shift();
      if (outcome instanceof Error) {
        throw outcome;
      }
      if (typeof outcome === "number") {
        return new Response("{}", { status: outcome });
      }
      assert.equal(typeof outcome?.status, "number", "missing internal fetch response");
      return new Response("{}", {
        status: outcome.status,
        headers: outcome.headers,
      });
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

function assertVisibilityChange(calls, seconds) {
  const changes = calls.filter(
    (call) =>
      call.url.includes("vercel-queue.com") && call.method === "PATCH",
  );
  assert.equal(changes.length, 1);
  assert.deepEqual(JSON.parse(changes[0].body), {
    visibilityTimeoutSeconds: seconds,
  });
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
    assert.equal(internal.headers.get("x-kbd-terminal-failure"), null);
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

test("plain api default export consumes the production Node callback shape", async () => {
  const fake = installFetch(200);
  const callback = nodeCallback(1);
  try {
    await NODE_HANDLER(callback.request, callback.response);
    assert.equal(callback.result.statusCode, 200);
    assert.deepEqual(callback.result.body, { status: "success" });
    const internal = fake.calls.find((call) =>
      call.url.endsWith("/_internal/research/dispatch"),
    );
    assert.ok(internal);
    assert.equal(internal.headers.get("x-kbd-delivery-count"), "1");
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

test("reschedules a transient worker outage before the final delivery", async () => {
  const fake = installFetch(503);
  try {
    const response = await POST(callbackRequest(9));
    assert.equal(response.status, 200);
    assert.equal(
      fake.calls.filter((call) =>
        call.url.endsWith("/_internal/research/dispatch"),
      ).length,
      1,
    );
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

for (const [name, ambiguousFailure] of [
  ["network failure", new TypeError("simulated network failure")],
  ["dispatch timeout", new DOMException("timed out", "TimeoutError")],
]) {
  test(`delivery one waits 600 seconds after ambiguous ${name}`, async () => {
    const fake = installFetch(ambiguousFailure);
    try {
      const response = await POST(callbackRequest(1));
      assert.equal(response.status, 200);
      assertVisibilityChange(fake.calls, 600);
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
}

test("delivery one keeps exponential backoff for an explicit worker 5xx", async () => {
  const fake = installFetch(503);
  try {
    const response = await POST(callbackRequest(1));
    assert.equal(response.status, 200);
    assertVisibilityChange(fake.calls, 10);
  } finally {
    fake.restore();
  }
});

test("acknowledges successful work on delivery ten without a terminal marker", async () => {
  const fake = installFetch(200);
  try {
    const response = await POST(callbackRequest(10));
    assert.equal(response.status, 200);
    const internal = fake.calls.filter((call) =>
      call.url.endsWith("/_internal/research/dispatch"),
    );
    assert.equal(internal.length, 1);
    assert.equal(internal[0].headers.get("x-kbd-terminal-failure"), null);
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

for (const [name, transientFailure] of [
  ["network failure", new TypeError("simulated network failure")],
  ["rate limit", 429],
  ["worker 5xx", 503],
  ["dispatch timeout", new DOMException("timed out", "TimeoutError")],
]) {
  test(`reschedules delivery ten after ${name} without a terminal marker`, async () => {
    const fake = installFetch(transientFailure);
    try {
      const response = await POST(callbackRequest(10));
      assert.equal(response.status, 200);
      const internal = fake.calls.filter((call) =>
        call.url.endsWith("/_internal/research/dispatch"),
      );
      assert.equal(internal.length, 1);
      assert.equal(internal[0].headers.get("x-kbd-terminal-failure"), null);
      assertVisibilityChange(fake.calls, 600);
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
}

test("delivery eleven is marker-only and acknowledges its durable success", async () => {
  const fake = installFetch(200);
  try {
    const response = await POST(callbackRequest(11));
    assert.equal(response.status, 200);
    const internal = fake.calls.filter((call) =>
      call.url.endsWith("/_internal/research/dispatch"),
    );
    assert.equal(internal.length, 1);
    assert.equal(
      internal[0].headers.get("x-kbd-terminal-failure"),
      "task_retry_budget_exhausted",
    );
    assert.equal(internal[0].headers.get("x-kbd-delivery-count"), "11");
    assert.equal(
      internal[0].headers.get("x-vercel-oidc-token"),
      "header.queue.request.token.value",
    );
    assert.equal(
      internal[0].headers.get("x-vercel-trusted-oidc-idp-token"),
      "header.queue.request.token.value",
    );
    assert.deepEqual(JSON.parse(internal[0].body), TASK);
    assert.ok(
      fake.calls.some(
        (call) =>
          call.url.includes("vercel-queue.com") && call.method === "DELETE",
      ),
    );
    assert.ok(
      !fake.calls.some(
        (call) =>
          call.url.includes("vercel-queue.com") && call.method === "PATCH",
      ),
    );
  } finally {
    fake.restore();
  }
});

test("delivery eleven acknowledges a boundary-proven permanent poison task", async () => {
  const fake = installFetch({
    status: 400,
    headers: { "x-kbd-dispatch-error-class": "permanent-task" },
  });
  try {
    const response = await POST(callbackRequest(11));
    assert.equal(response.status, 200);
    const internal = fake.calls.filter((call) =>
      call.url.endsWith("/_internal/research/dispatch"),
    );
    assert.equal(internal.length, 1);
    assert.equal(
      internal[0].headers.get("x-kbd-terminal-failure"),
      "task_retry_budget_exhausted",
    );
    assert.ok(
      fake.calls.some(
        (call) =>
          call.url.includes("vercel-queue.com") && call.method === "DELETE",
      ),
    );
    assert.ok(
      !fake.calls.some(
        (call) =>
          call.url.includes("vercel-queue.com") && call.method === "PATCH",
      ),
    );
  } finally {
    fake.restore();
  }
});

for (const markerFailure of [
  400,
  503,
  new TypeError("simulated terminal marker network failure"),
]) {
  test(`delivery twelve remains marker-only when its marker fails (${String(markerFailure)})`, async () => {
    const fake = installFetch(markerFailure);
    try {
      const response = await POST(callbackRequest(12));
      assert.equal(response.status, 200);
      const internal = fake.calls.filter((call) =>
        call.url.endsWith("/_internal/research/dispatch"),
      );
      assert.equal(internal.length, 1);
      assert.equal(
        internal[0].headers.get("x-kbd-terminal-failure"),
        "task_retry_budget_exhausted",
      );
      assert.equal(internal[0].headers.get("x-kbd-delivery-count"), "12");
      assertVisibilityChange(fake.calls, 300);
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
}

for (const retryableStatus of [400, 401, 403, 404, 408, 413]) {
  test(`does not discard auth/deploy/transient-like HTTP ${retryableStatus}`, async () => {
    const fake = installFetch(retryableStatus);
    try {
      const response = await POST(callbackRequest(3));
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
}

for (const permanentStatus of [400, 413]) {
  test(`acknowledges proven permanent task HTTP ${permanentStatus} after bounded retries`, async () => {
    const fake = installFetch({
      status: permanentStatus,
      headers: { "x-kbd-dispatch-error-class": "permanent-task" },
    });
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
}

test("does not discard a 400 response with an untrusted error class", async () => {
  const fake = installFetch({
    status: 400,
    headers: { "x-kbd-dispatch-error-class": "proxy-error" },
  });
  try {
    const response = await POST(callbackRequest(3));
    assert.equal(response.status, 200);
    assertVisibilityChange(fake.calls, 40);
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
