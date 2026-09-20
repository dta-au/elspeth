import { useCallback, useEffect, useState } from "react";
import * as admin from "@/api/identityAdmin";
import { fetchPersonLabels, isAbort, personName as nameOf } from "@/api/people";
import { Button, Input } from "@/components/ui";
import type { RelationshipView } from "@/types/identityAdmin";
import type { IdentityPerson, PersonLabel } from "@/types/people";
import { MutationNoticeView } from "./MutationNoticeView";
import { PersonPicker } from "./PersonPicker";
import { formatInstant } from "./peopleFormat";
import { useCancelToTrigger, usePersonMutation, useSubview } from "./peoplePanel";

/** Which way the new edge points, from the selected person's side. */
type Direction = "approver_for_person" | "person_approves_for";
type Form = { type: "assign" } | { type: "revoke"; edge: RelationshipView } | null;

interface Loaded {
  edges: RelationshipView[];
  labels: Record<string, PersonLabel>;
  labelsFailed: boolean;
}

interface Props {
  identityId: string;
  personName: string;
}

async function loadEdges(identityId: string): Promise<Loaded> {
  const { relationships } = await admin.listRelationships(identityId);
  const counterparts = relationships.map((edge) => (edge.from_identity_id === identityId ? edge.to_identity_id : edge.from_identity_id));
  if (counterparts.length === 0) return { edges: relationships, labels: {}, labelsFailed: false };
  try {
    const labels = await fetchPersonLabels(counterparts);
    return { edges: relationships, labels: Object.fromEntries(labels.map((label) => [label.identity_id, label])), labelsFailed: false };
  } catch {
    // The edges are the fact; names are decoration. Show the edges and SAY the names are missing.
    return { edges: relationships, labels: {}, labelsFailed: true };
  }
}

/**
 * Who approves for this person, and whom this person approves for.
 *
 * Direction is the thing administrators get wrong, so it is never implied by
 * field order: each list states it in its heading, the form asks for it in
 * words, and the confirmation sentence is written out before anything is sent.
 */
export function RelationshipsEditor({ identityId, personName }: Props): JSX.Element {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [form, setForm] = useState<Form>(null);
  const [direction, setDirection] = useState<Direction>("approver_for_person");
  const [counterpart, setCounterpart] = useState<IdentityPerson | null>(null);
  const [note, setNote] = useState("");

  const reload = useCallback(async () => {
    setLoaded(await loadEdges(identityId));
    setLoadError(null);
  }, [identityId]);

  const load = useCallback(() => {
    let active = true;
    setLoaded(null);
    setLoadError(null);
    void loadEdges(identityId).then(
      (result) => { if (active) setLoaded(result); },
      (error: unknown) => { if (active && !isAbort(error)) setLoadError(admin.adminErrorMessage(error, "Could not load approvers")); },
    );
    return () => { active = false; };
  }, [identityId]);
  useEffect(() => load(), [load]);

  const mutation = usePersonMutation(reload);
  const closeForm = useCallback(() => { setForm(null); setCounterpart(null); setNote(""); setDirection("approver_for_person"); }, []);
  const { triggerRef, cancel } = useCancelToTrigger<HTMLButtonElement>(closeForm);
  useSubview(`approvers:${identityId}`, form !== null, form?.type === "assign" && (counterpart !== null || note.trim() !== ""), cancel);
  const blocked = mutation.busy || mutation.mustReconcile;

  function label(id: string): JSX.Element {
    const known = loaded?.labels[id];
    if (known === undefined) return <><span>Name unavailable</span> <code className="people-grant-meta">{id}</code></>;
    return <><strong>{known.label}</strong> <span className="people-grant-meta">{known.detail}{known.retired ? " · retired account" : known.access_state !== "active" ? ` · access ${known.access_state}` : ""}</span></>;
  }
  function plainLabel(id: string): string {
    return loaded?.labels[id]?.label ?? "this person";
  }

  function edgeList(edges: RelationshipView[], other: (edge: RelationshipView) => string, empty: string): JSX.Element {
    if (edges.length === 0) return <p>{empty}</p>;
    return (
      <ul className="people-grants">
        {edges.map((edge) => (
          <li key={edge.relationship_id} className="people-grant">
            <div>
              {label(other(edge))}
              {(edge.effective_from !== null || edge.effective_until !== null) && (
                <p className="people-grant-purpose">
                  {edge.effective_from !== null && `Effective from ${formatInstant(edge.effective_from)}. `}
                  {edge.effective_until !== null && `Effective until ${formatInstant(edge.effective_until)}.`}
                </p>
              )}
            </div>
            <Button compact disabled={blocked || form !== null} aria-label={edge.from_identity_id === identityId ? `Stop ${personName} approving for ${plainLabel(edge.to_identity_id)}` : `Remove ${plainLabel(edge.from_identity_id)} as an approver for ${personName}`} onClick={() => { mutation.clear(); setForm({ type: "revoke", edge }); }}>Remove</Button>
          </li>
        ))}
      </ul>
    );
  }

  // Known before a counterpart is chosen when the selected person is the approver.
  const approverHint = direction === "person_approves_for" ? personName : counterpart === null ? null : nameOf(counterpart);
  const approverName = counterpart === null ? null : direction === "approver_for_person" ? nameOf(counterpart) : personName;
  const memberName = counterpart === null ? null : direction === "approver_for_person" ? personName : nameOf(counterpart);

  return (
    <section aria-label={`Approvers for ${personName}`} className="people-section">
      <MutationNoticeView mutation={mutation} />
      {loadError !== null ? (
        <div role="alert" className="people-notice people-notice-rejected"><span>{loadError}</span><Button compact onClick={() => load()}>Retry</Button></div>
      ) : loaded === null ? <p>Loading approvers…</p> : (
        <>
          {loaded.labelsFailed && <p role="status" className="people-notice people-notice-saved_stale">Names could not be loaded, so people are shown by identity ID.</p>}
          <h4>Approvers for {personName}</h4>
          {edgeList(loaded.edges.filter((edge) => edge.to_identity_id === identityId), (edge) => edge.from_identity_id, `Nobody is assigned to approve for ${personName}.`)}
          <h4>People {personName} approves for</h4>
          {edgeList(loaded.edges.filter((edge) => edge.from_identity_id === identityId), (edge) => edge.to_identity_id, `${personName} does not approve for anyone.`)}
        </>
      )}

      {form === null && loaded !== null && <div><Button ref={triggerRef} disabled={blocked} onClick={() => { mutation.clear(); setForm({ type: "assign" }); }}>Assign an approver</Button></div>}

      {form?.type === "revoke" && (
        <form className="identity-admin-form" onSubmit={(event) => {
          event.preventDefault();
          const edge = form.edge;
          void mutation.run(() => admin.revokeRelationship(edge.relationship_id, note.trim() || undefined), "Approver link removed.", "Approver link was not removed").then((ok) => { if (ok) closeForm(); });
        }}>
          <h4>{form.edge.from_identity_id === identityId ? `Stop ${personName} approving for ${plainLabel(form.edge.to_identity_id)}?` : `Remove ${plainLabel(form.edge.from_identity_id)} as an approver for ${personName}?`}</h4>
          <Input label="Note (optional)" value={note} maxLength={512} onChange={(event) => setNote(event.target.value)} />
          <div className="identity-admin-actions"><Button type="submit" variant="danger" disabled={blocked}>Remove approver link</Button><Button disabled={mutation.busy} onClick={cancel}>Cancel</Button></div>
        </form>
      )}

      {form?.type === "assign" && (
        <form className="identity-admin-form" onSubmit={(event) => {
          event.preventDefault();
          if (counterpart === null) return;
          const other = counterpart.identity.identity_id;
          const [from, to] = direction === "approver_for_person" ? [other, identityId] : [identityId, other];
          void mutation.run(
            () => admin.assertRelationship({ from_identity_id: from, to_identity_id: to, relationship_type: "approver", ...(note.trim() ? { note: note.trim() } : {}) }),
            `${approverName} now approves for ${memberName}.`,
            "Approver was not assigned",
          ).then((ok) => { if (ok) closeForm(); });
        }}>
          <h4>Assign an approver</h4>
          <fieldset className="people-direction">
            <legend>Who approves for whom?</legend>
            <label><Input type="radio" name={`direction-${identityId}`} checked={direction === "approver_for_person"} onChange={() => setDirection("approver_for_person")} /> Someone approves for {personName}</label>
            <label><Input type="radio" name={`direction-${identityId}`} checked={direction === "person_approves_for"} onChange={() => setDirection("person_approves_for")} /> {personName} approves for someone</label>
          </fieldset>
          <p className="people-grant-purpose">{approverHint ?? "The person who approves"} must hold the Approver role. If the assignment is refused, open {approverHint ?? "that person"}, choose Roles, add Approver, then assign again.</p>
          <PersonPicker label={direction === "approver_for_person" ? "Approver" : "Person they approve for"} excludeIdentityId={identityId} selected={counterpart} onSelect={setCounterpart} disabled={mutation.busy} />
          <Input label="Note (optional)" value={note} maxLength={512} onChange={(event) => setNote(event.target.value)} />
          <p className="people-confirm-sentence" aria-live="polite">{counterpart === null ? "Choose a person to see what will be assigned." : `Assign ${approverName} to approve for ${memberName}.`}</p>
          <div className="identity-admin-actions"><Button type="submit" variant="primary" disabled={blocked || counterpart === null}>Assign approver</Button><Button disabled={mutation.busy} onClick={cancel}>Cancel</Button></div>
        </form>
      )}
    </section>
  );
}
