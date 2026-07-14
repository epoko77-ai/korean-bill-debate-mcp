import { timingSafeEqual } from "node:crypto";

import { getVercelOidcToken } from "@vercel/oidc";
import {
  PollingQueueClient,
  TooManyRequestsError,
  type MessageMetadata,
  type ReceiveOptions,
  type ReceiveResult,
} from "@vercel/queue";

import {
  currentDeploymentOrigin,
  handleMessage,
  retryDirective,
  validatedOidcToken,
} from "../../serverless/kbd-research-shared.mjs";

const DEFAULT_TOPIC = "kbd-research";
// Vercel derives this consumer group from api/queues/kbd-research.ts. Polling
// the same group leases only messages that the primary push consumer has not
// already claimed; a second group would replay every task as another copy.
const DEFAULT_CONSUMER = "api_Squeues_Skbd-research_Dts";
const DEFAULT_CONCURRENCY = 4;
const DEFAULT_MAX_TASKS = 16;
const RECOVERY_DISPATCH_TIMEOUT_MS = 210_000;
const NEW_ROUND_DEADLINE_MS = 60_000;
const VISIBILITY_TIMEOUT_SECONDS = 600;
const NAME_PATTERN = /^[A-Za-z0-9_-]+$/;
const REGION_PATTERN = /^[a-z][a-z0-9-]{1,15}$/;

type RecoveryReceiver = {
  receive<T = unknown>(
    topicName: string,
    consumerGroup: string,
    handler: (message: T, metadata: MessageMetadata) => Promise<void> | void,
    options?: ReceiveOptions,
  ): Promise<ReceiveResult>;
};

type RecoveryOptions = {
  receiver?: RecoveryReceiver;
  oidcToken?: string;
  deploymentOrigin?: string;
  now?: () => number;
  concurrency?: number;
  maxTasks?: number;
};

export type RecoveryResult = {
  attempted: number;
  processed: number;
  empty: number;
  saturated: number;
  rounds: number;
};

function boundedInteger(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  const selected = value ?? fallback;
  if (!Number.isSafeInteger(selected) || selected < minimum || selected > maximum) {
    throw new Error("research recovery limit is invalid");
  }
  return selected;
}

function configuredName(name: string, fallback: string): string {
  const value = (process.env[name] ?? fallback).trim();
  if (!NAME_PATTERN.test(value)) {
    throw new Error("research recovery queue name is invalid");
  }
  return value;
}

function configuredRegion(): string {
  const value = (
    process.env.KBD_RESEARCH_QUEUE_REGION ??
    process.env.VERCEL_REGION ??
    "icn1"
  ).trim();
  if (!REGION_PATTERN.test(value)) {
    throw new Error("research recovery queue region is invalid");
  }
  return value;
}

function createReceiver(concurrency: number): RecoveryReceiver {
  return new PollingQueueClient({
    region: configuredRegion(),
    headers: { "Vqs-Max-Concurrency": String(concurrency) },
  });
}

export async function runRecovery(
  options: RecoveryOptions = {},
): Promise<RecoveryResult> {
  const concurrency = boundedInteger(
    options.concurrency,
    DEFAULT_CONCURRENCY,
    1,
    8,
  );
  const maxTasks = boundedInteger(options.maxTasks, DEFAULT_MAX_TASKS, 1, 32);
  const receiver = options.receiver ?? createReceiver(concurrency);
  const oidcToken = validatedOidcToken(
    options.oidcToken ?? (await getVercelOidcToken()),
  );
  const deploymentOrigin =
    options.deploymentOrigin ??
    currentDeploymentOrigin(new Request("https://recovery.invalid"));
  const now = options.now ?? Date.now;
  const started = now();
  const topic = configuredName("KBD_RESEARCH_QUEUE_TOPIC", DEFAULT_TOPIC);
  const consumer = DEFAULT_CONSUMER;
  const counters: RecoveryResult = {
    attempted: 0,
    processed: 0,
    empty: 0,
    saturated: 0,
    rounds: 0,
  };

  while (
    counters.attempted < maxTasks &&
    now() - started < NEW_ROUND_DEADLINE_MS
  ) {
    counters.rounds += 1;
    const slots = Math.min(concurrency, maxTasks - counters.attempted);
    let roundHandlers = 0;
    const outcomes = await Promise.allSettled(
      Array.from({ length: slots }, async () => {
        try {
          return await receiver.receive(
            topic,
            consumer,
            async (message, metadata) => {
              counters.attempted += 1;
              roundHandlers += 1;
              // A message available in the push consumer's own group is
              // already eligible for recovery. Always check its durable
              // receipt before doing work so an ACK race cannot repeat it.
              await handleMessage(
                message,
                deploymentOrigin,
                metadata.deliveryCount,
                oidcToken,
                true,
                RECOVERY_DISPATCH_TIMEOUT_MS,
              );
              counters.processed += 1;
            },
            {
              limit: 1,
              visibilityTimeoutSeconds: VISIBILITY_TIMEOUT_SECONDS,
              retry: retryDirective,
            },
          );
        } catch (error) {
          if (error instanceof TooManyRequestsError) {
            counters.saturated += 1;
            return { ok: false, reason: "empty" } as const;
          }
          throw error;
        }
      }),
    );
    const failed = outcomes.find(
      (outcome): outcome is PromiseRejectedResult => outcome.status === "rejected",
    );
    if (failed) {
      throw failed.reason;
    }
    for (const outcome of outcomes) {
      if (outcome.status === "fulfilled" && !outcome.value.ok) {
        counters.empty += 1;
      }
    }
    if (roundHandlers === 0 || counters.saturated > 0) {
      break;
    }
  }
  return counters;
}

function configuredCronSecret(): string {
  const value = (process.env.CRON_SECRET ?? "").trim();
  if (value.length < 32 || value.length > 512 || !/^[!-~]+$/.test(value)) {
    throw new Error("research recovery cron is not configured");
  }
  return value;
}

function authorizedCron(header: string | null | undefined, secret: string): boolean {
  const provided = Buffer.from(header ?? "", "utf8");
  const expected = Buffer.from(`Bearer ${secret}`, "utf8");
  return provided.length === expected.length && timingSafeEqual(provided, expected);
}

export async function GET(request: Request): Promise<Response> {
  let secret: string;
  try {
    secret = configuredCronSecret();
  } catch {
    return Response.json({ ok: false, error: "recovery_unavailable" }, { status: 503 });
  }
  if (!authorizedCron(request.headers.get("authorization"), secret)) {
    return Response.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  try {
    return Response.json({ ok: true, ...(await runRecovery()) });
  } catch {
    return Response.json({ ok: false, error: "recovery_failed" }, { status: 503 });
  }
}

type NodeRequest = {
  headers: Record<string, string | string[] | undefined>;
};

type NodeResponse = {
  status(code: number): { json(data: unknown): void };
};

export default async function nodeRecoveryRoute(
  request: NodeRequest,
  response: NodeResponse,
): Promise<void> {
  const raw = request.headers.authorization;
  const authorization = Array.isArray(raw) ? raw[0] : raw;
  const webResponse = await GET(
    new Request("https://recovery.invalid/api/queues/kbd-research-recovery", {
      headers: authorization ? { authorization } : {},
    }),
  );
  const payload: unknown = await webResponse.json();
  response.status(webResponse.status).json(payload);
}
