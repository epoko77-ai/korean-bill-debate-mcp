import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const functionsRoot = resolve(
  projectRoot,
  ".vercel/output/functions/api/queues",
);

process.env.KBD_INTERNAL_TASK_SECRET ??= "b".repeat(48);
process.env.VERCEL_DEPLOYMENT_ID ??= "dpl_bundle_verification";
process.env.VERCEL ??= "1";
process.env.VERCEL_REGION ??= "icn1";
process.env.VERCEL_URL ??= "kbd-bundle-verification.vercel.app";

const functionBundles = [
  {
    directory: "kbd-research.func",
    expectedHandler: "api/queues/kbd-research.js",
    expectedTrigger: {
      type: "queue/v2beta",
      topic: "kbd-research",
      retryAfterSeconds: 15,
      initialDelaySeconds: 0,
      maxConcurrency: 32,
      consumer: "api_Squeues_Skbd-research_Dts",
    },
  },
  {
    directory: "kbd-research-control.func",
    expectedHandler: "api/queues/kbd-research-control.js",
    expectedTrigger: {
      type: "queue/v2beta",
      topic: "kbd-research-control",
      retryAfterSeconds: 15,
      initialDelaySeconds: 0,
      maxConcurrency: 8,
      consumer: "api_Squeues_Skbd-research-control_Dts",
    },
  },
  {
    directory: "kbd-research-bulk.func",
    expectedHandler: "api/queues/kbd-research-bulk.js",
    expectedTrigger: {
      type: "queue/v2beta",
      topic: "kbd-research-bulk",
      retryAfterSeconds: 15,
      initialDelaySeconds: 0,
      maxConcurrency: 24,
      consumer: "api_Squeues_Skbd-research-bulk_Dts",
    },
  },
];

for (const { directory, expectedHandler, expectedTrigger } of functionBundles) {
  const bundleRoot = join(functionsRoot, directory);
  const configPath = join(bundleRoot, ".vc-config.json");
  assert.ok(
    existsSync(configPath),
    `${directory} is missing; run vercel build before this verification`,
  );

  const config = JSON.parse(readFileSync(configPath, "utf8"));
  assert.equal(config.handler, expectedHandler);
  assert.deepEqual(config.experimentalTriggers, [expectedTrigger]);

  const handlerPath = join(bundleRoot, config.handler);
  const callbackPath = join(
    bundleRoot,
    "serverless/kbd-research-queue-callback.js",
  );
  const sharedPath = join(bundleRoot, "serverless/kbd-research-shared.mjs");
  for (const requiredPath of [handlerPath, callbackPath, sharedPath]) {
    assert.ok(existsSync(requiredPath), `${requiredPath} is missing from bundle`);
  }

  const deployedSource = [handlerPath, callbackPath]
    .map((path) => readFileSync(path, "utf8"))
    .join("\n");
  assert.doesNotMatch(
    deployedSource,
    /(?:from\s+|import\()["'][^"']+\.ts["']/,
    `${directory} contains a runtime TypeScript import`,
  );

  const module = await import(
    `${pathToFileURL(handlerPath).href}?bundle-check=${Date.now()}`
  );
  assert.equal(typeof module.default, "function");
  assert.equal(typeof module.POST, "function");
  process.stdout.write(`${directory}: runtime import ok\n`);
}
