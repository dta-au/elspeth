// ============================================================================
// AuditCharacteristicIcon
//
// Single-flag renderer. Its ONE consumer is PluginCard.tsx:199 (sole import
// at :31) — the old header's "and the filter chip strip" was already wrong
// and is not carried forward. Looks up the flag in the centralised metadata
// table. A flag with no metadata renders NOTHING: the Python↔TS parity test
// (tests/unit/web/catalog/test_audit_characteristic_vocabulary_parity.py)
// fails CI on drift, so a fallback chip could only ever have shown a raw
// implementation flag to the user after the gate was already red
// (elspeth-0bfd019f68).
// ============================================================================

import { lookupAuditCharacteristic } from "./auditCharacteristics";

interface AuditCharacteristicIconProps {
  flag: string;
}

export function AuditCharacteristicIcon({ flag }: AuditCharacteristicIconProps) {
  const meta = lookupAuditCharacteristic(flag);
  if (meta === null) return null;
  return (
    <span
      className={`audit-icon audit-icon-${meta.tone}`}
      title={meta.tooltip}
    >
      <span className="audit-icon-label">{meta.label}</span>
    </span>
  );
}
