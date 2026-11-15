import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { expect, test } from 'bun:test';
import { enforceRuntimeSecrets, runtimeSecrets } from './options.js';

const DESK_SECRET = 'desk-secret-desk-secret-desk-secret';
const REG_SECRET = 'registration-secret-0123456789abcd';

function boot(env: Record<string, string>) {
  const optionsUrl = new URL('./options.ts', import.meta.url);
  const source = `import { enforceRuntimeSecrets } from ${JSON.stringify(optionsUrl.href)};
enforceRuntimeSecrets(${JSON.stringify(env)});
`;
  return spawnSync(
    process.execPath,
    ['--experimental-strip-types', '--input-type=module', '-e', source],
    {
      encoding: 'utf8',
      env: { ...process.env, NODE_NO_WARNINGS: '1' },
    },
  );
}

test('runtime secrets omit blank values and reject a short secret', () => {
  expect(runtimeSecrets({ REGISTRATION_SECRET: REG_SECRET })).toEqual({
    registrationSecret: REG_SECRET,
  });
  expect(
    runtimeSecrets({
      REGISTRATION_SECRET: '',
      DASHBOARD_SECRET: '',
      WAKE_ALLOW_OPEN_REGISTRATION: '1',
    }),
  ).toEqual({});
  expect(
    runtimeSecrets({
      REGISTRATION_SECRET: REG_SECRET,
      DASHBOARD_SECRET: DESK_SECRET,
    }),
  ).toEqual({
    registrationSecret: REG_SECRET,
    dashboardSecret: DESK_SECRET,
  });
  expect(() =>
    runtimeSecrets({
      REGISTRATION_SECRET: REG_SECRET,
      DASHBOARD_SECRET: 'too-short',
    }),
  ).toThrow(/DASHBOARD_SECRET must be at least 32 characters/);
});

test('a short registration secret is refused, so a placeholder cannot boot', () => {
  expect(() => runtimeSecrets({ REGISTRATION_SECRET: 'reg' })).toThrow(
    /REGISTRATION_SECRET must be at least 32 characters/,
  );
  expect(() =>
    runtimeSecrets({
      REGISTRATION_SECRET: 'a'.repeat(31),
    }),
  ).toThrow(/REGISTRATION_SECRET must be at least 32 characters/);
  expect(runtimeSecrets({ REGISTRATION_SECRET: 'a'.repeat(32) })).toEqual({
    registrationSecret: 'a'.repeat(32),
  });
});

test('a missing registration secret is refused unless it is allowed on purpose', () => {
  expect(() => runtimeSecrets({})).toThrow(
    /REGISTRATION_SECRET is not set, so POST \/agents would accept anyone/,
  );
  expect(runtimeSecrets({ WAKE_ALLOW_OPEN_REGISTRATION: '1' })).toEqual({});
  expect(() => runtimeSecrets({ WAKE_ALLOW_OPEN_REGISTRATION: '0' })).toThrow(
    /REGISTRATION_SECRET is not set/,
  );
  expect(() =>
    runtimeSecrets({
      REGISTRATION_SECRET: '',
      WAKE_ALLOW_OPEN_REGISTRATION: 'yes',
    }),
  ).toThrow(/REGISTRATION_SECRET is not set/);
});

test('the placeholder in deploy/README.md is refused at boot', () => {
  const readme = readFileSync(
    new URL('../../../deploy/README.md', import.meta.url),
    'utf8',
  );
  const match = /^REGISTRATION_SECRET=(.*)$/m.exec(readme);
  expect(match).not.toBeNull();
  const placeholder = (match?.[1] ?? '').trim();
  expect(placeholder).not.toBe('');
  expect(placeholder.length).toBeLessThan(32);
  expect(() => runtimeSecrets({ REGISTRATION_SECRET: placeholder })).toThrow(
    /REGISTRATION_SECRET must be at least 32 characters/,
  );
});

test('a boot with a short secret prints one line and exits non-zero', () => {
  const dashboard = boot({ DASHBOARD_SECRET: 'too-short' });
  expect(dashboard.status).toBe(1);
  expect(dashboard.stdout).toBe('');
  expect(dashboard.stderr.trim()).toBe(
    'DASHBOARD_SECRET must be at least 32 characters',
  );

  const shortRegistration = boot({ REGISTRATION_SECRET: 'reg' });
  expect(shortRegistration.status).toBe(1);
  expect(shortRegistration.stdout).toBe('');
  expect(shortRegistration.stderr.trim()).toBe(
    'REGISTRATION_SECRET must be at least 32 characters',
  );

  const missing = boot({});
  expect(missing.status).toBe(1);
  expect(missing.stderr.trim()).toContain(
    'REGISTRATION_SECRET is not set, so POST /agents would accept anyone',
  );

  const allowed = boot({ WAKE_ALLOW_OPEN_REGISTRATION: '1' });
  expect(allowed.status).toBe(0);
  expect(allowed.stderr).toBe('');

  expect(
    enforceRuntimeSecrets({
      DASHBOARD_SECRET: DESK_SECRET,
      WAKE_ALLOW_OPEN_REGISTRATION: '1',
    }),
  ).toEqual({
    dashboardSecret: DESK_SECRET,
  });
});
