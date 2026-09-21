import { type JSX, useEffect, useId, useRef, useState } from "react";
import { adminErrorMessage } from "@/api/identityAdmin";
import { isAbort, listPeople, personDisambiguator, personName } from "@/api/people";
import { Button, Input } from "@/components/ui";
import type { IdentityPerson } from "@/types/people";

const PICKER_RESULTS = 8;

interface Props {
  label: string;
  /** Never offered: the person this picker is being used FOR. */
  excludeIdentityId: string;
  selected: IdentityPerson | null;
  onSelect: (person: IdentityPerson | null) => void;
  disabled?: boolean;
}

/**
 * Choose a person by name. Searches the whole authorized directory on the
 * server, so the counterpart need not be on the page the administrator was
 * looking at. Every result carries the line that tells namesakes apart, and
 * the chosen identity travels as an object: no ID is typed or pasted.
 */
export function PersonPicker({ label, excludeIdentityId, selected, onSelect, disabled = false }: Props): JSX.Element {
  const [text, setText] = useState("");
  const [results, setResults] = useState<IdentityPerson[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const statusId = useId();
  const changeRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const needle = text.trim();
    if (selected !== null || needle.length < 2) {
      setResults(null);
      setError(null);
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void listPeople({ q: needle, status: "active", provider: "all", type: "people", offset: 0 }, controller.signal, PICKER_RESULTS + 1).then(
        (page) => {
          setError(null);
          setResults(page.people.filter((person): person is IdentityPerson => person.record_type === "identity" && !person.retired && person.identity.identity_id !== excludeIdentityId));
        },
        (failure: unknown) => { if (!isAbort(failure)) { setResults(null); setError(adminErrorMessage(failure, "Search failed")); } },
      );
    }, 200);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [text, selected, excludeIdentityId, attempt]);

  if (selected !== null) {
    return (
      <div className="people-picker">
        <span className="field-label">{label}</span>
        <div className="people-picker-chosen">
          <span><strong>{personName(selected)}</strong> <span className="people-grant-meta">{personDisambiguator(selected)}</span></span>
          <Button ref={changeRef} compact disabled={disabled} aria-label={`Change ${label.toLowerCase()}: ${personName(selected)}`} onClick={() => { onSelect(null); setText(""); queueMicrotask(() => inputRef.current?.focus()); }}>Change</Button>
        </div>
      </div>
    );
  }

  const shown = results?.slice(0, PICKER_RESULTS) ?? null;
  return (
    <div className="people-picker">
      <Input ref={inputRef} label={label} value={text} disabled={disabled} autoComplete="off" aria-describedby={statusId} hint="Search active people by name, username or email." onChange={(event) => setText(event.target.value)} />
      <div id={statusId} role="status" className="people-grant-purpose">
        {error !== null ? "" : shown === null ? "" : shown.length === 0 ? "No active people match." : `${shown.length}${(results?.length ?? 0) > PICKER_RESULTS ? "+" : ""} matching ${shown.length === 1 ? "person" : "people"}.`}
      </div>
      {error !== null && <div role="alert" className="people-notice people-notice-rejected"><span>{error}</span><Button compact onClick={() => setAttempt((value) => value + 1)}>Retry</Button></div>}
      {shown !== null && shown.length > 0 && (
        <ul className="people-picker-results">
          {shown.map((person) => (
            <li key={person.key}>
              <Button variant="bare" className="people-picker-result" disabled={disabled} onClick={() => { onSelect(person); queueMicrotask(() => changeRef.current?.focus()); }}>
                <strong>{personName(person)}</strong> <span className="people-grant-meta">{personDisambiguator(person)}</span>
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
