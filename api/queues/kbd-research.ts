import { handleCallback } from "@vercel/queue";

const INTERNAL_PATH = "/_internal/research/dispatch";
const SECRET_HEADER = "x-kbd-internal-secret";
const DELIVERY_COUNT_HEADER = "x-kbd-delivery-count";
const OIDC_HEADER = "x-vercel-oidc-token";
const TRUSTED_OIDC_HEADER = "x-vercel-trusted-oidc-idp-token";
const MAX_TASK_BYTES = 64 * 1024;
// Leave enough of the 300 second function budget for the Queue SDK to issue
// its acknowledge/reschedule directive after the Python worker returns.
const DISPATCH_TIMEOUT_MS = 270_000;
const MAX_PERMANENT_DELIVERY_ATTEMPTS = 3;

class PermanentDispatchError extends Error {}

function currentDeploymentOrigin(request: Request): string {
  const deploymentHost = (process.env.VERCEL_URL ?? "").trim();
  if (deploymentHost) {
    if (
      deploymentHost.length > 253 ||
      !/^[a-z0-9.-]+$/i.test(deploymentHost) ||
      !deploymentHost.toLowerCase().endsWith(".vercel.app")
    ) {
      throw new Error("research dispatch is not configured");
    }
    return `https://${deploymentHost}`;
  }
  // Production must never derive the secret-bearing internal dispatch target
  // from an inbound Host header. VERCEL_URL is the immutable deployment URL
  // supplied by System Environment Variables. The request fallback exists
  // only for the local Queue SDK test/dev route.
  if (process.env.VERCEL === "1") {
    throw new Error("research dispatch is not configured");
  }
  let url: URL;
  try {
    url = new URL(request.url);
  } catch {
    throw new Error("research dispatch is not configured");
  }
  if (url.protocol !== "https:" || !/^[a-z0-9.-]+$/i.test(url.hostname)) {
    throw new Error("research dispatch is not configured");
  }
  return url.origin;
}

function internalSecret(): string {
  const secret = process.env.KBD_INTERNAL_TASK_SECRET ?? "";
  if (secret.length < 32 || secret.length > 512 || !/^[!-~]+$/.test(secret)) {
    throw new Error("research dispatch is not configured");
  }
  return secret;
}

function requestOidcToken(request: Request): string {
  const token = request.headers.get(OIDC_HEADER)?.trim() ?? "";
  // Vercel workload identity is a compact JWT. Keep the check deliberately
  // shape-only: the Queue SDK and Vercel Queue API remain the trust boundary.
  if (
    token.length < 32 ||
    token.length > 16 * 1024 ||
    !/^[A-Za-z0-9._-]+$/.test(token)
  ) {
    throw new Error("research dispatch identity is unavailable");
  }
  return token;
}

async function dispatchMessage(
  message: unknown,
  deploymentOrigin: string,
  deliveryCount: number,
  oidcToken: string,
): Promise<void> {
  let body: string;
  try {
    const encoded = JSON.stringify(message);
    if (typeof encoded !== "string") {
      throw new Error("invalid serialization result");
    }
    body = encoded;
  } catch {
    throw new PermanentDispatchError("research task serialization failed");
  }
  if (new TextEncoder().encode(body).byteLength > MAX_TASK_BYTES) {
    throw new PermanentDispatchError("research task exceeds dispatch limit");
  }

  let response: Response;
  try {
    response = await fetch(new URL(INTERNAL_PATH, deploymentOrigin), {
      method: "POST",
      headers: {
        "content-type": "application/json",
        [SECRET_HEADER]: internalSecret(),
        [DELIVERY_COUNT_HEADER]: String(deliveryCount),
        // Python publishes follow-up tasks with this request-local identity.
        // The trusted-source form also supports same-project calls when
        // Deployment Protection is enabled.
        [OIDC_HEADER]: oidcToken,
        [TRUSTED_OIDC_HEADER]: oidcToken,
      },
      body,
      redirect: "error",
      signal: AbortSignal.timeout(DISPATCH_TIMEOUT_MS),
    });
  } catch {
    throw new Error("research dispatch request failed");
  }

  if (!response.ok) {
    try {
      await response.body?.cancel();
    } catch {
      // Ignore cancellation errors; only the sanitized HTTP status is surfaced.
    }
    // Throwing leaves the delivery unacknowledged, so Vercel Queues retries it.
    // The response body is intentionally never read or copied into the error.
    const error = `research dispatch failed (${response.status})`;
    if (response.status >= 400 && response.status < 500 && response.status !== 429) {
      throw new PermanentDispatchError(error);
    }
    throw new Error(error);
  }
  try {
    await response.body?.cancel();
  } catch {
    // The task is complete; response cleanup must not turn success into a retry.
  }
}

async function queueRoute(request: Request): Promise<Response> {
  const deploymentOrigin = currentDeploymentOrigin(request);
  const oidcToken = requestOidcToken(request);
  const queueCallback = handleCallback<unknown>(
    async (message, metadata) =>
      dispatchMessage(
        message,
        deploymentOrigin,
        metadata.deliveryCount,
        oidcToken,
      ),
    {
      visibilityTimeoutSeconds: 600,
      retry: (error, metadata) => {
        // Only a proven permanent 4xx/task-shape failure may be discarded by
        // this bridge. Network/429/5xx failures must remain durable: once the
        // Python worker is reachable it records terminal engine failures on its
        // own bounded tenth delivery. A blanket tenth-attempt acknowledge here
        // would silently lose work during a temporary worker outage.
        if (
          error instanceof PermanentDispatchError &&
          metadata.deliveryCount >= MAX_PERMANENT_DELIVERY_ATTEMPTS
        ) {
          return { acknowledge: true };
        }
        return {
          afterSeconds: Math.min(
            300,
            2 ** Math.min(metadata.deliveryCount, 6) * 5,
          ),
        };
      },
    },
  );
  return queueCallback(request);
}

// `POST` follows the official handleCallback route pattern.  The default Web
// fetch export also lets this route run as a standalone TypeScript Vercel
// Function beside the existing Python ASGI function.
export const POST = queueRoute;
export default { fetch: queueRoute };
