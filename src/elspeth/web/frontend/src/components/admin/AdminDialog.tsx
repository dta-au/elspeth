import { useEffect, useRef, useState } from "react";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { Button } from "@/components/ui";
import { IdentitiesTable } from "./IdentitiesTable";
import { RolesEditor } from "./RolesEditor";
import { RelationshipsEditor } from "./RelationshipsEditor";
import "./admin.css";

type Tab = "identities" | "roles" | "relationships";
const TABS: readonly Tab[] = ["identities", "roles", "relationships"];

/** Mounted by App only while a live admin role is present in the mailbox summary.
 * Every backend request independently checks that role again. */
export function AdminDialog({ onClose, currentIdentityId }: { onClose: () => void; currentIdentityId: string }): JSX.Element {
  const modalRef = useRef<HTMLDivElement>(null);
  const [tab, setTab] = useState<Tab>("identities");
  useFocusTrap(modalRef, true);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) { if (event.key === "Escape") onClose(); }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return <>
    <div role="presentation" className="app-dialog-backdrop" onClick={onClose} />
    <div ref={modalRef} role="dialog" aria-modal="true" aria-labelledby="identity-admin-title" className="app-dialog settings-dialog settings-dialog-wide identity-admin-dialog">
      <div className="secrets-panel-header"><h2 id="identity-admin-title" className="secrets-panel-title">Identity administration</h2><Button variant="bare" aria-label="Close identity administration" className="dialog-close" onClick={onClose}>×</Button></div>
      <div className="secrets-panel-body">
        <div role="tablist" aria-label="Identity administration sections" className="identity-admin-tabs" onKeyDown={(event) => {
          const index = TABS.indexOf(tab);
          const next = event.key === "ArrowRight" ? (index + 1) % TABS.length : event.key === "ArrowLeft" ? (index - 1 + TABS.length) % TABS.length : event.key === "Home" ? 0 : event.key === "End" ? TABS.length - 1 : -1;
          if (next < 0) return;
          event.preventDefault();
          setTab(TABS[next]);
          event.currentTarget.querySelectorAll<HTMLButtonElement>("[role=tab]")[next].focus();
        }}>
          {TABS.map((value) => <Button key={value} id={`identity-admin-tab-${value}`} role="tab" tabIndex={tab === value ? 0 : -1} aria-selected={tab === value} aria-controls={`identity-admin-${value}`} variant="bare" className="identity-admin-tab" onClick={() => setTab(value)}>{value[0].toUpperCase() + value.slice(1)}</Button>)}
        </div>
        <div id={`identity-admin-${tab}`} role="tabpanel" aria-labelledby={`identity-admin-tab-${tab}`}>
          {tab === "identities" ? <IdentitiesTable currentIdentityId={currentIdentityId} /> : tab === "roles" ? <RolesEditor /> : <RelationshipsEditor />}
        </div>
      </div>
    </div>
  </>;
}
