import { authHeaders, parseResponse } from "./client";
import type { LibraryCuratorAction, LibraryEntry, LibraryFork, LibraryList, LibraryView } from "@/types/library";

const segment = encodeURIComponent;

async function get<T>(url: string): Promise<T> {
  const response = await fetch(url, { headers: authHeaders(), cache: "no-store" });
  return parseResponse<T>(response);
}

async function post<T>(url: string, body?: object): Promise<T> {
  const response = await fetch(url, {
    method: "POST",
    headers: authHeaders(body === undefined ? undefined : "application/json"),
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  return parseResponse<T>(response);
}

export const fetchLibrary = (view: LibraryView): Promise<LibraryList> => get(`/api/library?view=${view}`);

export const publishLibraryEntry = (sessionId: string, title: string): Promise<LibraryEntry> =>
  post(`/api/sessions/${segment(sessionId)}/library/publish`, { title: title.trim() });

export const curateLibraryEntry = (entryId: string, action: LibraryCuratorAction, note: string | null): Promise<LibraryEntry> =>
  post(`/api/library/${segment(entryId)}/${action}`, { note: note?.trim() || null });

export const forkLibraryEntry = (entryId: string): Promise<LibraryFork> =>
  post(`/api/library/${segment(entryId)}/fork`);
