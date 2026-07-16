import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

process.env.KBD_INTERNAL_TASK_SECRET = "i".repeat(48);
process.env.CRON_SECRET = "c".repeat(48);
process.env.VERCEL_DEPLOYMENT_ID = "dpl_recovery_test";
process.env.VERCEL = "1";
process.env.VERCEL_URL = "kbd-current-deployment.vercel.app";
process.env.VERCEL_REGION = "icn1";
process.env.KBD_RESEARCH_QUEUE_TOPIC = "kbd-research";
process.env.KBD_RESEARCH_CONTROL_QUEUE_TOPIC = "kbd-research-control";
const encode = (value) => Buffer.from(JSON.stringify(value)).toString("base64url");
const TEST_OIDC_TOKEN = `${encode({ alg: "RS256", typ: "JWT" })}.${encode({
  exp: 4_102_444_800,
})}.test-signature`;
process.env.VERCEL_OIDC_TOKEN = TEST_OIDC_TOKEN;

const recoveryModule = await import(
  "../../api/queues/kbd-research-recovery.ts"
);
const { GET, runRecovery } = recoveryModule;
const NODE_HANDLER = recoveryModule.default;

test("recovery imports a deployable shared runtime instead of another TS function", async () => {
  const source = await readFile(
    new URL("../../api/queues/kbd-research-recovery.ts", import.meta.url),
    "utf8",
  );
  const shared = await readFile(
    new URL("../../serverless/kbd-research-shared.mjs", import.meta.url),
    "utf8",
  );
  assert.match(source, /from "\.\.\/\.\.\/serverless\/kbd-research-shared\.mjs"/);
  assert.doesNotMatch(source, /from "\.\/kbd-research\.ts"/);
  assert.match(shared, /export async function handleMessage/);
});

const TASK = {
  schema_version: 1,
  research_id: "research_recovery_test",
  stage: "collect_metadata",
  work_id: "metadata-page-1",
  query_fingerprint: "a".repeat(64),
  index_revision: "index-test",
  payload: { page: 1 },
  credential_capability: null,
};

class FakeReceiver {
  constructor(messages) {
    this.messages = [...messages];
    this.calls = [];
    this.directives = [];
  }

  async receive(topic, consumer, handler, options) {
    this.calls.push({ topic, consumer, options });
    const index = this.messages.findIndex(
      (candidate) =>
        (candidate.topic ?? "kbd-research") === topic,
    );
    const item = index < 0 ? undefined : this.messages.splice(index, 1)[0];
    if (!item) {
      return { ok: false, reason: "empty" };
    }
    try {
      await handler(item.message, item.metadata);
    } catch (error) {
      this.directives.push(options.retry(error, item.metadata));
    }
    return { ok: true };
  }
}

function metadata(createdAt, overrides = {}) {
  return {
    messageId: `msg-${createdAt.getTime()}`,
    deliveryCount: 1,
    createdAt,
    expiresAt: new Date(createdAt.getTime() + 86_400_000),
    topicName: "kbd-research",
    consumerGroup: "api_Squeues_Skbd-research_Dts",
    region: "icn1",
    ...overrides,
  };
}

function installInternalFetch() {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (input, init = {}) => {
    const url = input instanceof Request ? input.url : String(input);
    calls.push({ url, init, headers: new Headers(init.headers) });
    if (url.endsWith("/_internal/research/dispatch")) {
      return new Response("{}", { status: 200 });
    }
    throw new Error(`unexpected fetch ${url}`);
  };
  return {
    calls,
    restore() {
      globalThis.fetch = original;
    },
  };
}

test("an available same-group message is recovered immediately", async () => {
  const now = Date.parse("2026-07-14T12:00:00Z");
  const receiver = new FakeReceiver([
    {
      message: TASK,
      metadata: metadata(new Date(now)),
    },
  ]);
  const fake = installInternalFetch();
  try {
    const result = await runRecovery({
      receiver,
      oidcToken: TEST_OIDC_TOKEN,
      deploymentOrigin: "https://kbd-current-deployment.vercel.app",
      now: () => now,
      concurrency: 1,
      maxTasks: 1,
    });
    assert.equal(result.processed, 1);
    assert.equal(result.attempted, 1);
    assert.deepEqual(receiver.directives, []);
    assert.equal(fake.calls.length, 1);
    assert.deepEqual(
      receiver.calls.slice(0, 2).map((call) => call.topic),
      ["kbd-research-control", "kbd-research"],
    );
    const leafCall = receiver.calls.find(
      (call) => call.topic === "kbd-research",
    );
    assert.ok(leafCall);
    assert.equal(
      leafCall.consumer,
      "api_Squeues_Skbd-research_Dts",
    );
    assert.equal(leafCall.options.visibilityTimeoutSeconds, 300);
  } finally {
    fake.restore();
  }
});

test("recovery polls the control topic with its push consumer group", async () => {
  const now = Date.parse("2026-07-14T12:00:00Z");
  const receiver = new FakeReceiver([
    {
      topic: "kbd-research-control",
      message: {
        ...TASK,
        work_id: "phase_barrier:discovery:1",
        payload: { work_kind: "phase_barrier", attempt: 1 },
      },
      metadata: metadata(new Date(now), {
        topicName: "kbd-research-control",
        consumerGroup: "api_Squeues_Skbd-research-control_Dts",
      }),
    },
  ]);
  const fake = installInternalFetch();
  try {
    const result = await runRecovery({
      receiver,
      oidcToken: TEST_OIDC_TOKEN,
      deploymentOrigin: "https://kbd-current-deployment.vercel.app",
      now: () => now,
      concurrency: 1,
      maxTasks: 1,
    });
    assert.equal(result.processed, 1);
    assert.equal(receiver.calls.length, 1);
    assert.equal(receiver.calls[0].topic, "kbd-research-control");
    assert.equal(
      receiver.calls[0].consumer,
      "api_Squeues_Skbd-research-control_Dts",
    );
  } finally {
    fake.restore();
  }
});

test("recovery uses the receipt-check marker and same-deployment identity", async () => {
  const now = Date.parse("2026-07-14T12:00:00Z");
  const receiver = new FakeReceiver([
    {
      message: TASK,
      metadata: metadata(new Date(now - 61_000)),
    },
  ]);
  const fake = installInternalFetch();
  try {
    const result = await runRecovery({
      receiver,
      oidcToken: TEST_OIDC_TOKEN,
      deploymentOrigin: "https://kbd-current-deployment.vercel.app",
      now: () => now,
      concurrency: 1,
      maxTasks: 1,
    });
    assert.equal(result.processed, 1);
    assert.equal(fake.calls.length, 1);
    assert.equal(
      fake.calls[0].url,
      "https://kbd-current-deployment.vercel.app/_internal/research/dispatch",
    );
    assert.equal(
      fake.calls[0].headers.get("x-kbd-recovery-dispatch"),
      "1",
    );
    assert.equal(
      fake.calls[0].headers.get("x-vercel-oidc-token"),
      TEST_OIDC_TOKEN,
    );
  } finally {
    fake.restore();
  }
});

test("one bounded recovery run can advance multiple available messages", async () => {
  const now = Date.parse("2026-07-14T12:00:00Z");
  const child = { ...TASK, work_id: "metadata-page-2" };
  const receiver = new FakeReceiver([
    { message: TASK, metadata: metadata(new Date(now - 61_000)) },
    { message: child, metadata: metadata(new Date(now)) },
  ]);
  const fake = installInternalFetch();
  try {
    const result = await runRecovery({
      receiver,
      oidcToken: TEST_OIDC_TOKEN,
      deploymentOrigin: "https://kbd-current-deployment.vercel.app",
      now: () => now,
      concurrency: 1,
      maxTasks: 2,
    });
    assert.equal(result.processed, 2);
    assert.equal(result.rounds, 2);
    assert.equal(fake.calls.length, 2);
  } finally {
    fake.restore();
  }
});

test("recovery authorization fails closed before queue polling", async () => {
  const missing = await GET(
    new Request("https://example.test/api/queues/kbd-research-recovery"),
  );
  const wrong = await GET(
    new Request("https://example.test/api/queues/kbd-research-recovery", {
      headers: { authorization: "Bearer wrong" },
    }),
  );
  assert.equal(missing.status, 401);
  assert.equal(wrong.status, 401);
  assert.deepEqual(await missing.json(), { ok: false, error: "unauthorized" });
  assert.deepEqual(await wrong.json(), { ok: false, error: "unauthorized" });
});

test("missing cron configuration returns unavailable without queue polling", async () => {
  const secret = process.env.CRON_SECRET;
  delete process.env.CRON_SECRET;
  try {
    const response = await GET(
      new Request("https://example.test/api/queues/kbd-research-recovery"),
    );
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), {
      ok: false,
      error: "recovery_unavailable",
    });
  } finally {
    process.env.CRON_SECRET = secret;
  }
});

test("plain api default export polls with deployment pin and bounded concurrency", async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    const url = input instanceof Request ? input.url : String(input);
    calls.push({ url, method: init.method, headers: new Headers(init.headers) });
    if (url.includes("vercel-queue.com")) {
      return new Response(null, { status: 204 });
    }
    throw new Error(`unexpected fetch ${url}`);
  };
  const result = { status: 0, body: null };
  try {
    await NODE_HANDLER(
      { headers: { authorization: `Bearer ${process.env.CRON_SECRET}` } },
      {
        status(code) {
          result.status = code;
          return this;
        },
        json(body) {
          result.body = body;
        },
      },
    );
    assert.equal(result.status, 200);
    assert.equal(result.body.ok, true);
    assert.equal(result.body.attempted, 0);
    assert.equal(calls.length, 8);
    assert.ok(
      calls.every(
        (call) =>
          call.headers.get("vqs-max-concurrency") === "4" &&
          call.headers.get("vqs-deployment-id") === "dpl_recovery_test",
      ),
    );
    assert.equal(
      calls.filter((call) => call.url.includes("/topic/kbd-research/")).length,
      4,
    );
    assert.equal(
      calls.filter((call) =>
        call.url.includes("/topic/kbd-research-control/")
      ).length,
      4,
    );
  } finally {
    globalThis.fetch = original;
  }
});
