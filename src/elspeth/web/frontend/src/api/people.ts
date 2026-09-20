/** Requests to the people directory read facade. Reads only: every write goes
 *  through the identity-administration or dev-admin API that owns it. */
import { authHeaders, parseResponse } from "./client";
import type {
  PeopleCapabilities,
  PeopleListResponse,
  PeopleQuery,
  PersonLabel,
  PersonRecord,
  PersonResponse,
} from "@/types/people";

export const PEOPLE_PAGE_SIZE = 25;
/** The server's bound on one label lookup. */
export const PEOPLE_LABEL_LOOKUP_MAX = 50;
const BASE = "/api/auth/admin/people";

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${BASE}${path}`, { headers: authHeaders(), cache: "no-store", signal });
  return parseResponse<T>(response);
}

export function fetchPeopleCapabilities(signal?: AbortSignal): Promise<PeopleCapabilities> {
  return get("/capabilities", signal);
}

export function listPeople(query: PeopleQuery, signal?: AbortSignal, limit = PEOPLE_PAGE_SIZE): Promise<PeopleListResponse> {
  const params = new URLSearchParams({ status: query.status, type: query.type, limit: String(limit), offset: String(query.offset) });
  const text = query.q.trim();
  if (text !== "") params.set("q", text);
  if (query.provider !== "all") params.set("provider", query.provider);
  return get(`?${params}`, signal);
}

/** Resolve one person directly by their stable key, never by rescanning pages. */
export function fetchPerson(key: string, signal?: AbortSignal): Promise<PersonResponse> {
  const separator = key.indexOf(":");
  const kind = key.slice(0, separator);
  const value = key.slice(separator + 1);
  if (separator < 1 || value === "" || (kind !== "identity" && kind !== "local")) {
    throw new Error("Invalid person key");
  }
  return get(`/${kind}/${encodeURIComponent(value)}`, signal);
}

export async function fetchPersonLabels(identityIds: readonly string[], signal?: AbortSignal): Promise<PersonLabel[]> {
  const unique = [...new Set(identityIds)];
  const labels: PersonLabel[] = [];
  for (let start = 0; start < unique.length; start += PEOPLE_LABEL_LOOKUP_MAX) {
    const params = new URLSearchParams();
    for (const id of unique.slice(start, start + PEOPLE_LABEL_LOOKUP_MAX)) params.append("identity_id", id);
    const page = await get<{ labels: PersonLabel[] }>(`/labels?${params}`, signal);
    labels.push(...page.labels);
  }
  return labels;
}

/** True when the request never produced a server answer, so a WRITE may or
 *  may not have landed. parseResponse rejects with an ApiError record that
 *  carries `status`; anything without one is a transport failure. */
export function isUncertainOutcome(error: unknown): boolean {
  return !(typeof error === "object" && error !== null && "status" in error && typeof error.status === "number");
}

export function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

/** HTTP status of a server refusal, or null for a transport failure. */
export function errorStatus(error: unknown): number | null {
  return typeof error === "object" && error !== null && "status" in error && typeof error.status === "number" ? error.status : null;
}

export function localKey(username: string): string {
  return `local:${username}`;
}

export function personName(person: PersonRecord): string {
  if (person.record_type === "local_account") {
    return person.local_account.display_name.trim() || person.local_account.username;
  }
  const { display_name, username, subject, access_state, activated_at } = person.identity;
  const shown = display_name?.trim() ?? "";
  if (shown !== "") return shown;
  // An identity prepared ahead of its first sign-in has no profile yet. The
  // linked local account's name fills that gap, but NEVER for a never-admitted
  // pending row: that profile is withheld on purpose, and a separately
  // authorized source must not put back what the projection left out.
  const withheld = access_state === "pending" && activated_at === null;
  const accountName = person.local_account?.display_name.trim() ?? "";
  if (!withheld && accountName !== "") return accountName;
  return username || subject;
}

/** The line that tells two people with one name apart: never the name again. */
export function personDisambiguator(person: PersonRecord): string {
  if (person.record_type === "local_account") return `${person.local_account.username} · local`;
  const { username, subject, provider } = person.identity;
  const secondary = username !== null && username !== personName(person) ? username : subject;
  return `${secondary} · ${provider}`;
}
