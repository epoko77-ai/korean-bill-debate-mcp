import {
  handleCallback,
  MessageAlreadyProcessedError,
  MessageLockedError,
  MessageNotAvailableError,
  MessageNotFoundError,
  parseRawCallback,
  PollingQueueClient,
  QueueClient,
  type MessageHandler,
  type ParsedCallbackV1,
  type ReceiveResult,
  type VercelRegion,
} from "@vercel/queue";

import {
  currentDeploymentOrigin,
  handleMessage,
  retryDirective,
  validatedOidcToken,
} from "./kbd-research-shared.mjs";

export {
  currentDeploymentOrigin,
  handleMessage,
  retryDirective,
  validatedOidcToken,
} from "./kbd-research-shared.mjs";

const OIDC_HEADER = "x-vercel-oidc-token";

type NodeQueueRequest = {
  method?: string;
  headers: Record<string, string | string[] | undefined>;
  body?: unknown;
};

type NodeQueueResponse = {
  status(code: number): {
    json(data: unknown): void;
    end(): void;
  };
  end(): void;
};

type NodeQueueCallback = (
  request: NodeQueueRequest,
  response: NodeQueueResponse,
) => Promise<void>;

type RoutingQueueReceiver = {
  receive<T = unknown>(
    topicName: string,
    consumerGroup: string,
    handler: MessageHandler<T>,
    options: {
      messageId: string;
      visibilityTimeoutSeconds: number;
      retry: typeof retryDirective;
    },
  ): Promise<ReceiveResult>;
};

const nodeQueueClient = new QueueClient();

function requestOidcToken(request: Request): string {
  return validatedOidcToken(request.headers.get(OIDC_HEADER));
}

function nodeHeader(
  request: NodeQueueRequest,
  name: string,
): string | undefined {
  const value = request.headers[name];
  return Array.isArray(value) ? value[0] : value;
}

function messageHandler(deploymentOrigin: string, oidcToken: string) {
  return async (message: unknown, metadata: { deliveryCount: number }) =>
    handleMessage(message, deploymentOrigin, metadata.deliveryCount, oidcToken);
}

const callbackOptions = {
  // The Python dispatch is bounded to 270 seconds. The SDK renews an active
  // lease, so 300 seconds is enough for healthy work while a crashed or lost
  // callback becomes recoverable within the broad-search SLO.
  visibilityTimeoutSeconds: 300,
  retry: retryDirective,
};

export function isStaleQueueCallbackError(error: unknown): boolean {
  return (
    error instanceof MessageAlreadyProcessedError ||
    error instanceof MessageNotFoundError
  );
}

export function isRetryableQueueCallbackError(error: unknown): boolean {
  return (
    error instanceof MessageLockedError ||
    error instanceof MessageNotAvailableError
  );
}

export async function runNodeQueueCallback(
  callback: NodeQueueCallback,
  request: NodeQueueRequest,
  response: NodeQueueResponse,
): Promise<void> {
  try {
    await callback(request, response);
  } catch (error) {
    // Queue callbacks are at-least-once deliveries. A callback for an already
    // acknowledged or expired message is terminal success; retrying it can
    // only create noise. Active lease conflicts are handled separately below.
    if (isStaleQueueCallbackError(error)) {
      response.status(200).json({ status: "success" });
      return;
    }
    // A lost lease or temporary ticket mismatch is not terminal. Returning a
    // success here strands the unacknowledged message until visibility expiry.
    // Preserve the queue trigger's retry contract instead.
    if (isRetryableQueueCallbackError(error)) {
      response.status(503).json({ error: "queue callback retry required" });
      return;
    }
    response.status(500).json({ error: "queue callback unavailable" });
  }
}

export async function runRoutingOnlyQueueCallback(
  callback: ParsedCallbackV1,
  handler: MessageHandler<unknown>,
  response: NodeQueueResponse,
  receiver?: RoutingQueueReceiver,
): Promise<void> {
  try {
    // Routing-only callbacks do not carry a receipt handle or payload. Using
    // the public receive-by-ID API avoids the SDK Connect adapter's internal
    // catch, which otherwise turns normal stale-delivery outcomes into 500s.
    const activeReceiver = receiver ??
      new PollingQueueClient({
        region: (callback.region ??
          process.env.VERCEL_REGION ??
          "iad1") as VercelRegion,
      });
    const result = await activeReceiver.receive(
      callback.queueName,
      callback.consumerGroup,
      handler,
      {
        messageId: callback.messageId,
        ...callbackOptions,
      },
    );
    // Only terminal absence is safe to acknowledge. `not_available` and
    // `empty` can be a temporary lease/ticket race; acknowledging the callback
    // would leave its message locked until visibility expiry.
    if (!result.ok) {
      if (
        result.reason === "already_processed" ||
        result.reason === "not_found"
      ) {
        response.status(200).json({ status: "success" });
        return;
      }
      response.status(503).json({ error: "queue callback retry required" });
      return;
    }
    response.status(200).json({ status: "success" });
  } catch (error) {
    if (isStaleQueueCallbackError(error)) {
      response.status(200).json({ status: "success" });
      return;
    }
    if (isRetryableQueueCallbackError(error)) {
      response.status(503).json({ error: "queue callback retry required" });
      return;
    }
    response.status(500).json({ error: "queue callback unavailable" });
  }
}

async function queueRoute(request: Request): Promise<Response> {
  const queueCallback = handleCallback<unknown>(
    messageHandler(currentDeploymentOrigin(request), requestOidcToken(request)),
    callbackOptions,
  );
  return queueCallback(request);
}

async function nodeQueueRoute(
  request: NodeQueueRequest,
  response: NodeQueueResponse,
): Promise<void> {
  if (request.method !== "POST") {
    response.status(200).end();
    return;
  }
  let parsedCallback;
  try {
    parsedCallback = parseRawCallback(request.body, request.headers);
  } catch (error) {
    response.status(400).json({
      error: error instanceof Error ? error.message : "invalid queue callback",
    });
    return;
  }
  let handler: MessageHandler<unknown>;
  try {
    handler = messageHandler(
      currentDeploymentOrigin(new Request("https://queue-callback.invalid")),
      validatedOidcToken(nodeHeader(request, OIDC_HEADER)),
    );
  } catch {
    response.status(500).json({ error: "queue callback unavailable" });
    return;
  }
  if (!("receiptHandle" in parsedCallback)) {
    await runRoutingOnlyQueueCallback(parsedCallback, handler, response);
    return;
  }

  // `api/*.ts` is a plain Vercel Node Function, not a framework Web route.
  // Vercel's Queue SDK requires its Connect-style adapter here; exporting a
  // Web `fetch` object builds successfully but is never discovered/invoked by
  // the production queue trigger.
  const callback = nodeQueueClient.handleNodeCallback<unknown>(
    handler,
    callbackOptions,
  );
  await runNodeQueueCallback(callback, request, response);
}

// Keep the Web handler as a named export for contract tests/framework reuse.
// The default Connect-style export is the production `api/*.ts` entry point.
export const POST = queueRoute;
export default nodeQueueRoute;
