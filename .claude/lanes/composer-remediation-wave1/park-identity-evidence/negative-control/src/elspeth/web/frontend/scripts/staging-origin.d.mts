export function parseStagingOrigin(value: string | undefined): string;
export function configureStagingOrigin(): string;
export function loginToStaging(origin: string, username: string, password: string): Promise<string>;
