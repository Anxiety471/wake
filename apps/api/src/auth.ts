export type AuthenticatedAgent = {
  id: string;
  name: string;
};

export function caller(request: {
  agent: AuthenticatedAgent | null;
}): AuthenticatedAgent {
  if (request.agent === null) {
    throw new Error('missing authenticated agent');
  }
  return request.agent;
}
