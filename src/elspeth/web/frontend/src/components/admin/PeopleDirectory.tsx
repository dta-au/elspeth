import { type JSX, useId } from "react";
import { PROVIDER_LABEL, personDisambiguator, personName } from "@/api/people";
import { Button, Input } from "@/components/ui";
import type { IdentityProvider } from "@/types/identityAdmin";
import type { PeopleCapabilities, PeopleQuery, PeopleStatusFilter, PeopleTypeFilter, PersonRecord } from "@/types/people";

export type DirectoryLoad =
  | { status: "loading" }
  | { status: "ready"; people: PersonRecord[]; hasMore: boolean }
  | { status: "error"; message: string };

// No "service": a service account is a TYPE, and the Type filter beside this
// one already selects it. Listed here too, it returned nothing under the
// default Type of "People".
const PROVIDERS: IdentityProvider[] = ["local", "oidc", "entra", "vanguard", "google"];

export function personStateLabel(person: PersonRecord): string {
  if (person.record_type === "local_account") return person.access === "not_set_up" ? "Access not set up" : "Local account";
  if (person.retired) return "Retired account";
  return { pending: "Pending access", active: "Active", disabled: "Disabled" }[person.identity.access_state];
}

function personStateKey(person: PersonRecord): string {
  if (person.record_type === "local_account") return person.access;
  return person.retired ? "retired" : person.identity.access_state;
}

interface Props {
  capabilities: PeopleCapabilities;
  query: PeopleQuery;
  draftText: string;
  load: DirectoryLoad;
  selectedKey: string | null;
  pageSize: number;
  onDraftText: (text: string) => void;
  onQuery: (patch: Partial<PeopleQuery>) => void;
  onSelect: (person: PersonRecord) => void;
  onRetry: () => void;
  /** Attached to the row the administrator came from, so "Back to people" can return focus to it. */
  rowRef: (key: string, element: HTMLButtonElement | null) => void;
}

/**
 * The searchable list. Compact on purpose: name, how they sign in, and their
 * access state. Roles, approvers and limits load for ONE selected person, not
 * for every row, so opening the panel costs one request.
 */
export function PeopleDirectory({ capabilities, query, draftText, load, selectedKey, pageSize, onDraftText, onQuery, onSelect, onRetry, rowRef }: Props): JSX.Element {
  const scopeId = useId();
  const identityFilters = capabilities.identity_admin;
  const statusOptions: [PeopleStatusFilter, string][] = [
    ["all", "All"],
    ...(identityFilters ? ([["pending", "Pending access"], ["active", "Active"], ["disabled", "Disabled"]] as [PeopleStatusFilter, string][]) : []),
    ...(identityFilters && capabilities.local_accounts ? ([["not_set_up", "Access not set up"]] as [PeopleStatusFilter, string][]) : []),
  ];
  const filtered = query.q.trim() !== "" || query.status !== "all" || query.provider !== "all" || query.type !== "people";
  const page = Math.floor(query.offset / pageSize) + 1;

  return (
    <section aria-label="People" className="people-directory">
      <form role="search" className="people-filters" onSubmit={(event) => { event.preventDefault(); onQuery({ q: draftText, offset: 0 }); }}>
        <div className="people-search">
          <Input label="Search people" type="search" value={draftText} maxLength={128} autoComplete="off" aria-describedby={scopeId} hint="Name, username or email. Searches everyone, not only this page." onChange={(event) => onDraftText(event.target.value)} />
          <Button type="submit" className="people-search-submit">Search</Button>
        </div>
        {statusOptions.length > 1 && (
          <label className="identity-admin-field">Status
            <select className="input" value={query.status} onChange={(event) => onQuery({ status: event.target.value as PeopleStatusFilter, offset: 0 })}>
              {statusOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
        )}
        {identityFilters && (
          <>
            <label className="identity-admin-field">Sign-in method
              <select className="input" value={query.provider} onChange={(event) => onQuery({ provider: event.target.value as IdentityProvider | "all", offset: 0 })}>
                <option value="all">All</option>
                {PROVIDERS.map((value) => <option key={value} value={value}>{PROVIDER_LABEL[value]}</option>)}
              </select>
            </label>
            <label className="identity-admin-field">Type
              <select className="input" value={query.type} onChange={(event) => onQuery({ type: event.target.value as PeopleTypeFilter, offset: 0 })}>
                <option value="people">People</option>
                <option value="service">Service accounts</option>
                <option value="all">People and service accounts</option>
              </select>
            </label>
          </>
        )}
      </form>
      <p id={scopeId} className="people-grant-purpose">
        {capabilities.identity_admin && capabilities.local_accounts ? "Showing everyone: people with access and local accounts."
          : capabilities.identity_admin ? "Showing people known to access administration. Local sign-in accounts are managed by the local account administrator."
          : "Showing local sign-in accounts only. Access, roles, approvers and limits are managed by an access administrator."}
      </p>

      {load.status === "loading" && <p role="status">Loading people…</p>}
      {load.status === "error" && (
        <div role="alert" className="people-notice people-notice-rejected">
          <span>{load.message} The list may be incomplete, so nothing is shown.</span>
          <Button compact onClick={onRetry}>Retry</Button>
        </div>
      )}
      {load.status === "ready" && load.people.length === 0 && (
        <p role="status">{filtered ? "No people match this search and these filters." : query.offset > 0 ? "No more people." : "There are no people here yet. Use Add person to create the first."}</p>
      )}
      {load.status === "ready" && load.people.length > 0 && (
        <ul className="people-list" aria-label="People results">
          {load.people.map((person) => (
            <li key={person.key}>
              <Button variant="bare" ref={(element) => rowRef(person.key, element)} className="people-row" aria-current={selectedKey === person.key ? "true" : undefined} onClick={() => onSelect(person)}>
                <span className="people-row-name">{personName(person)}</span>
                <span className="people-grant-meta people-wrap">{personDisambiguator(person)}</span>
                <span className={`people-state people-state-${personStateKey(person)}`}>{personStateLabel(person)}</span>
              </Button>
            </li>
          ))}
        </ul>
      )}
      {load.status === "ready" && (query.offset > 0 || load.hasMore) && (
        <nav aria-label="People pages" className="identity-admin-toolbar">
          <Button compact disabled={query.offset === 0} onClick={() => onQuery({ offset: Math.max(0, query.offset - pageSize) })}>Previous</Button>
          <span>Page {page}</span>
          <Button compact disabled={!load.hasMore} onClick={() => onQuery({ offset: query.offset + pageSize })}>Next</Button>
        </nav>
      )}
    </section>
  );
}
