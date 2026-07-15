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
} from "../../serverless/kbd-research-shared.mjs";

export {
  currentDeploymentOrigin,
  handleMessage,
  retryDirective,
  validatedOidcToken,
} from "../../serverless/kbd-research-shared.mjs";

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
  visibilityTimeoutSeconds: 600,
  retry: retryDirective,
};

export function isStaleQueueCallbackError(error: unknown): boolean {
  return (
    error instanceof MessageAlreadyProcessedError ||
    error instanceof MessageLockedError ||
    error instanceof MessageNotAvailableError ||
    error instanceof MessageNotFoundError
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
    // Queue callbacks are at-least-once deliveries. A late duplicate can lose
    // its lease because the work was already acknowledged, another consumer
    // owns it, or the message expired. Those SDK outcomes are terminal success
    // for this stale invocation; retrying the callback can only create noise.
    if (isStaleQueueCallbackError(error)) {
      response.status(200).json({ status: "success" });
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
    // A non-success receive-by-ID result means this duplicate callback arrived
    // after the message was processed, expired, or moved to another consumer.
    // PollingQueueClient has not invoked the handler in those cases.
    if (!result.ok) {
      response.status(200).json({ status: "success" });
      return;
    }
    response.status(200).json({ status: "success" });
  } catch (error) {
    if (isStaleQueueCallbackError(error)) {
      response.status(200).json({ status: "success" });
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
