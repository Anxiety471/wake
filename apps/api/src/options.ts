export type RuntimeSecrets = {
  registrationSecret?: string;
  dashboardSecret?: string;
};

export const MIN_DASHBOARD_SECRET_LENGTH = 32;
export const MIN_REGISTRATION_SECRET_LENGTH = 32;

export class ConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ConfigError';
  }
}

function configured(value: string | undefined): value is string {
  return value !== undefined && value !== '';
}

function openRegistrationAllowed(env: NodeJS.ProcessEnv): boolean {
  return env.WAKE_ALLOW_OPEN_REGISTRATION === '1';
}

export function runtimeSecrets(env: NodeJS.ProcessEnv): RuntimeSecrets {
  const registrationSecret = env.REGISTRATION_SECRET;
  const dashboardSecret = env.DASHBOARD_SECRET;
  if (
    configured(dashboardSecret) &&
    dashboardSecret.length < MIN_DASHBOARD_SECRET_LENGTH
  ) {
    throw new ConfigError(
      `DASHBOARD_SECRET must be at least ${MIN_DASHBOARD_SECRET_LENGTH} characters`,
    );
  }
  if (
    configured(registrationSecret) &&
    registrationSecret.length < MIN_REGISTRATION_SECRET_LENGTH
  ) {
    throw new ConfigError(
      `REGISTRATION_SECRET must be at least ${MIN_REGISTRATION_SECRET_LENGTH} characters`,
    );
  }
  if (!configured(registrationSecret) && !openRegistrationAllowed(env)) {
    throw new ConfigError(
      'REGISTRATION_SECRET is not set, so POST /agents would accept anyone; set it, or set WAKE_ALLOW_OPEN_REGISTRATION=1 on a dev box',
    );
  }
  return {
    ...(configured(registrationSecret) ? { registrationSecret } : {}),
    ...(configured(dashboardSecret) ? { dashboardSecret } : {}),
  };
}

export function enforceConfig<T>(read: () => T): T {
  try {
    return read();
  } catch (error) {
    if (error instanceof ConfigError) {
      console.error(error.message);
      process.exit(1);
    }
    throw error;
  }
}

export function enforceRuntimeSecrets(env: NodeJS.ProcessEnv): RuntimeSecrets {
  return enforceConfig(() => runtimeSecrets(env));
}
