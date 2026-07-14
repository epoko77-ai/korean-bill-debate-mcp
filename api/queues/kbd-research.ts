import { handleCallback, QueueClient } from "@vercel/queue";

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
  try {
    // `api/*.ts` is a plain Vercel Node Function, not a framework Web route.
    // Vercel's Queue SDK requires its Connect-style adapter here; exporting a
    // Web `fetch` object builds successfully but is never discovered/invoked by
    // the production queue trigger.
    const callback = nodeQueueClient.handleNodeCallback<unknown>(
      messageHandler(
        currentDeploymentOrigin(new Request("https://queue-callback.invalid")),
        validatedOidcToken(nodeHeader(request, OIDC_HEADER)),
      ),
      callbackOptions,
    );
    await callback(request, response);
  } catch {
    response.status(500).json({ error: "queue callback unavailable" });
  }
}

// Keep the Web handler as a named export for contract tests/framework reuse.
// The default Connect-style export is the production `api/*.ts` entry point.
export const POST = queueRoute;
export default nodeQueueRoute;
