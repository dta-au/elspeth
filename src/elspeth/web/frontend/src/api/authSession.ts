// Request custody stays outside the auth store to avoid an import cycle.
let generation = 0;
const requestCredentials = new WeakMap<Response, { generation: number; token: string | null }>();

export function advanceAuthGeneration(): number {
  return ++generation;
}

export function isCurrentAuthGeneration(observed: number): boolean {
  return observed === generation;
}

/** Associate the response with the credential sent, before any asynchronous work. */
export async function authFetch(input: string, init?: RequestInit): Promise<Response> {
  const authorization = new Headers(init?.headers).get("Authorization");
  const credential = {
    generation,
    token: authorization?.startsWith("Bearer ") ? authorization.slice(7) : null,
  };
  const response = await fetch(input, init);
  requestCredentials.set(response, credential);
  return response;
}

export function responseOwnsCredential(response: Response, token: string | null): boolean {
  const credential = requestCredentials.get(response);
  return credential !== undefined && credential.token !== null &&
    credential.token === token && credential.token === localStorage.getItem("auth_token") &&
    isCurrentAuthGeneration(credential.generation);
}
