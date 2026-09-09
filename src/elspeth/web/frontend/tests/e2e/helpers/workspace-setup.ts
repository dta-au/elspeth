export async function setupWorkspaceScenario<T>(
  create: () => Promise<string>,
  setup: (sessionId: string) => Promise<T>,
  cleanup: (sessionId: string) => Promise<void>,
): Promise<{ sessionId: string; value: T }> {
  const sessionId = await create();
  try {
    const value = await setup(sessionId);
    return { sessionId, value };
  } catch (error) {
    await cleanup(sessionId);
    throw error;
  }
}
