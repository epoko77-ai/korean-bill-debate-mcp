export function currentDeploymentOrigin(request: Request): string;

export function validatedOidcToken(raw: string | null | undefined): string;

export function handleMessage(
  message: unknown,
  deploymentOrigin: string,
  deliveryCount: number,
  oidcToken: string,
  recoveryDispatch?: boolean,
  dispatchTimeoutMs?: number,
): Promise<void>;

export function retryDirective(
  error: unknown,
  metadata: { deliveryCount: number },
): { acknowledge: true } | { afterSeconds: number };
