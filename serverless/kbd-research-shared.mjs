const INTERNAL_PATH = "/_internal/research/dispatch";
const SECRET_HEADER = "x-kbd-internal-secret";
const DELIVERY_COUNT_HEADER = "x-kbd-delivery-count";
const RECOVERY_DISPATCH_HEADER = "x-kbd-recovery-dispatch";
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
// Deliveries after the ten normal attempts skip normal work entirely. Their
// lightweight terminal marker preserves most of the function budget for ACK.
const TERMINAL_FAILURE_TIMEOUT_MS = 25_000;
const MAX_NORMAL_DELIVERY_ATTEMPTS = 10;
const MAX_PERMANENT_DELIVERY_ATTEMPTS = 3;
const RECEIPT_SAFE_RETRY_SECONDS = 30;
const MAX_RETRY_SECONDS = 60;

class PermanentDispatchError extends Error {}
class AmbiguousDispatchError extends Error {}
class TerminalFailureDispatchError extends Error {}

/** @param {Request} request */
export function currentDeploymentOrigin(request) {
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
  // from an inbound Host header. The fallback is only for local contract tests.
  if (process.env.VERCEL === "1") {
    throw new Error("research dispatch is not configured");
  }
  let url;
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

function internalSecret() {
  const secret = process.env.KBD_INTERNAL_TASK_SECRET ?? "";
  if (secret.length < 32 || secret.length > 512 || !/^[!-~]+$/.test(secret)) {
    throw new Error("research dispatch is not configured");
  }
  return secret;
}

/** @param {string | null | undefined} raw */
export function validatedOidcToken(raw) {
  const token = raw?.trim() ?? "";
  if (
    token.length < 32 ||
    token.length > 16 * 1024 ||
    !/^[A-Za-z0-9._-]+$/.test(token)
  ) {
    throw new Error("research dispatch identity is unavailable");
  }
  return token;
}

/**
 * @param {unknown} message
 * @param {string} deploymentOrigin
 * @param {number} deliveryCount
 * @param {string} oidcToken
 * @param {{terminalFailure?: boolean, recoveryDispatch?: boolean, timeoutMs?: number}} options
 */
async function dispatchMessage(
  message,
  deploymentOrigin,
  deliveryCount,
  oidcToken,
  options = {},
) {
  let body;
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

  let response;
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
        ...(options.recoveryDispatch
          ? { [RECOVERY_DISPATCH_HEADER]: "1" }
          : {}),
        [OIDC_HEADER]: oidcToken,
        [TRUSTED_OIDC_HEADER]: oidcToken,
      },
      body,
      redirect: "error",
      signal: AbortSignal.timeout(options.timeoutMs ?? DISPATCH_TIMEOUT_MS),
    });
  } catch {
    // The target may still finish after a socket or timeout failure. Delay
    // redelivery so its immutable completion receipt wins any race.
    throw new AmbiguousDispatchError("research dispatch request failed");
  }

  if (!response.ok) {
    try {
      await response.body?.cancel();
    } catch {
      // Only the sanitized status is relevant.
    }
    const error = `research dispatch failed (${response.status})`;
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
    // Response cleanup must not turn completed work into a retry.
  }
}

/**
 * @param {unknown} message
 * @param {string} deploymentOrigin
 * @param {number} deliveryCount
 * @param {string} oidcToken
 * @param {boolean} [recoveryDispatch]
 * @param {number} [dispatchTimeoutMs]
 */
export async function handleMessage(
  message,
  deploymentOrigin,
  deliveryCount,
  oidcToken,
  recoveryDispatch = false,
  dispatchTimeoutMs = DISPATCH_TIMEOUT_MS,
) {
  if (deliveryCount > MAX_NORMAL_DELIVERY_ATTEMPTS) {
    try {
      await dispatchMessage(message, deploymentOrigin, deliveryCount, oidcToken, {
        terminalFailure: true,
        recoveryDispatch,
        timeoutMs: TERMINAL_FAILURE_TIMEOUT_MS,
      });
    } catch (error) {
      if (error instanceof PermanentDispatchError) {
        throw error;
      }
      throw new TerminalFailureDispatchError(
        "research terminal failure dispatch failed",
      );
    }
    return;
  }

  await dispatchMessage(message, deploymentOrigin, deliveryCount, oidcToken, {
    recoveryDispatch,
    timeoutMs: dispatchTimeoutMs,
  });
}

/**
 * @param {unknown} error
 * @param {{deliveryCount: number}} metadata
 * @returns {{acknowledge: true} | {afterSeconds: number}}
 */
export function retryDirective(error, metadata) {
  if (
    error instanceof PermanentDispatchError &&
    metadata.deliveryCount >= MAX_PERMANENT_DELIVERY_ATTEMPTS
  ) {
    return { acknowledge: true };
  }
  if (error instanceof TerminalFailureDispatchError) {
    return { afterSeconds: MAX_RETRY_SECONDS };
  }
  if (error instanceof AmbiguousDispatchError) {
    // The target may have completed after the socket outcome became unknown.
    // Redelivery checks the durable completion receipt before repeating work,
    // so a prompt retry preserves safety without stalling the whole fan-out.
    return { afterSeconds: RECEIPT_SAFE_RETRY_SECONDS };
  }
  if (metadata.deliveryCount === MAX_NORMAL_DELIVERY_ATTEMPTS) {
    // The dispatcher checks the immutable task receipt before doing any work,
    // which also makes the last normal retry safe at this short interval.
    return { afterSeconds: RECEIPT_SAFE_RETRY_SECONDS };
  }
  return {
    afterSeconds: Math.min(
      MAX_RETRY_SECONDS,
      2 ** Math.min(metadata.deliveryCount, 6) * 5,
    ),
  };
}
