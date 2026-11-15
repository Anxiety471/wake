export type AuthenticatedAgent = {
  id: string;
  name: string;
  // 'mcp' is the single public MCP identity; it can never reach a private bot.
  kind: 'bot' | 'mcp';
};

export function caller(request: {
  agent: AuthenticatedAgent | null;
}): AuthenticatedAgent {
  if (request.agent === null) {
    throw new Error('missing authenticated agent');
  }
  return request.agent;
}
