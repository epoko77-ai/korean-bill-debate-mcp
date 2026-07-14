import { handleCallback } from "@vercel/queue";

const INTERNAL_PATH = "/_internal/research/dispatch";
const SECRET_HEADER = "x-kbd-internal-secret";
const DELIVERY_COUNT_HEADER = "x-kbd-delivery-count";
const TERMINAL_FAILURE_HEADER = "x-kbd-terminal-failure";
const TERMINAL_FAILURE_CODE = "task_retry_budget_exhausted";
const ERROR_CLASS_HEADER = "x-kbd-dispatch-error-class";
const PERMANENT_TASK_ERROR_CLASS = "permanent-task";
const OIDC_HEADER = "x-vercel-oidc-token";
const TRUSTED_OIDC_HEADER = "x-vercel-trusted-oidc-idp-token";
const MAX_TASK_BYTES = 64 * 1024;
// Leave enough of the 300 second function budget for the Queue SDK to issue
// its acknowledge/reschedule directive after the Python worker returns.
const DISPATCH_TIMEOUT_MS = 270_000;
// Deliveries after the ten normal attempts skip normal work entirely.  Their
// lightweight terminal marker leaves 275 seconds of the function budget for
// cold start, response handling, and the Queue SDK acknowledge/reschedule call.
const TERMINAL_FAILURE_TIMEOUT_MS = 25_000;
const MAX_NORMAL_DELIVERY_ATTEMPTS = 10;
const MAX_PERMANENT_DELIVERY_ATTEMPTS = 3;

class PermanentDispatchError extends Error {}
class AmbiguousDispatchError extends Error {}
class TerminalFailureDispatchError extends Error {}

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
  options: {
    terminalFailure?: boolean;
    timeoutMs?: number;
  } = {},
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
        ...(options.terminalFailure
          ? { [TERMINAL_FAILURE_HEADER]: TERMINAL_FAILURE_CODE }
          : {}),
        // Python publishes follow-up tasks with this request-local identity.
        // The trusted-source form also supports same-project calls when
        // Deployment Protection is enabled.
        [OIDC_HEADER]: oidcToken,
        [TRUSTED_OIDC_HEADER]: oidcToken,
      },
      body,
      redirect: "error",
      signal: AbortSignal.timeout(options.timeoutMs ?? DISPATCH_TIMEOUT_MS),
    });
  } catch {
    // DNS/socket/AbortSignal failures do not prove whether Python accepted the
    // task.  Give a potentially still-running 300s target time to finish and
    // write its receipt before any normal redelivery.
    throw new AmbiguousDispatchError("research dispatch request failed");
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
    // These are the only statuses emitted by the private Python boundary for
    // a proven malformed/oversized task body.  Auth, routing/deployment, HTTP
    // timeout, conflict, and rate-limit responses remain retryable.
    if (
      (response.status === 400 || response.status === 413) &&
      response.headers.get(ERROR_CLASS_HEADER) === PERMANENT_TASK_ERROR_CLASS
    ) {
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

async function handleMessage(
  message: unknown,
  deploymentOrigin: string,
  deliveryCount: number,
  oidcToken: string,
): Promise<void> {
  if (deliveryCount > MAX_NORMAL_DELIVERY_ATTEMPTS) {
    // Never repeat the potentially expensive/ambiguous task after its ten
    // normal attempts.  This avoids a timeout-then-late-success racing with a
    // second normal execution.  The Python marker checks durable task state
    // before deciding whether fail_task is still required.
    try {
      await dispatchMessage(message, deploymentOrigin, deliveryCount, oidcToken, {
        terminalFailure: true,
        timeoutMs: TERMINAL_FAILURE_TIMEOUT_MS,
      });
    } catch (error) {
      // Preserve a boundary-proven malformed/oversized task classification.
      // Collapsing it into the transient marker error below would reschedule a
      // poison message every five minutes until Queue retention expired.
      if (error instanceof PermanentDispatchError) {
        throw error;
      }
      throw new TerminalFailureDispatchError(
        "research terminal failure dispatch failed",
      );
    }
    return;
  }

  // Attempts 1..10 are normal dispatches only.  Any transient failure is
  // thrown to the Queue SDK and rescheduled; the terminal marker begins on the
  // following delivery, never inside an aborted normal invocation.
  await dispatchMessage(message, deploymentOrigin, deliveryCount, oidcToken);
}

async function queueRoute(request: Request): Promise<Response> {
  const deploymentOrigin = currentDeploymentOrigin(request);
  const oidcToken = requestOidcToken(request);
  const queueCallback = handleCallback<unknown>(
    async (message, metadata) =>
      handleMessage(
        message,
        deploymentOrigin,
        metadata.deliveryCount,
        oidcToken,
      ),
    {
      visibilityTimeoutSeconds: 600,
      retry: (error, metadata) => {
        // Only a proven permanent 4xx/task-shape failure may be discarded by
        // this bridge.  Transient failures are acknowledged by normal handler
        // completion only after the dedicated terminal marker succeeds.
        if (
          error instanceof PermanentDispatchError &&
          metadata.deliveryCount >= MAX_PERMANENT_DELIVERY_ATTEMPTS
        ) {
          return { acknowledge: true };
        }
        if (error instanceof TerminalFailureDispatchError) {
          return { afterSeconds: 300 };
        }
        if (error instanceof AmbiguousDispatchError) {
          return { afterSeconds: 600 };
        }
        if (metadata.deliveryCount === MAX_NORMAL_DELIVERY_ATTEMPTS) {
          // The bridge can lose its response immediately after Python accepts
          // the tenth task.  Wait beyond that target's 300s max duration so its
          // write-once completion receipt cannot race the marker-only delivery.
          return { afterSeconds: 600 };
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
