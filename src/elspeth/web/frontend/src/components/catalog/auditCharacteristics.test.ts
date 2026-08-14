import { describe, it, expect } from "vitest";
import {
  AUDIT_CHARACTERISTICS,
  lookupAuditCharacteristic,
  KNOWN_AUDIT_FLAGS,
} from "./auditCharacteristics";

// Bidirectional vocabulary parity (Python AuditCharacteristic ↔ this file's
// AUDIT_CHARACTERISTICS) is enforced on the Python side at
// tests/unit/web/catalog/test_audit_characteristic_vocabulary_parity.py
// so adding a member in either language without the other fails CI.

describe("auditCharacteristics metadata", () => {
  it("exposes a metadata entry for io_read", () => {
    const meta = lookupAuditCharacteristic("io_read");
    expect(meta).not.toBeNull();
    expect(meta?.label).toMatch(/i\/?o.read|reads/i);
    expect(meta?.tone).toBe("positive");
  });

  it("exposes a metadata entry for external_call with attention tone", () => {
    const meta = lookupAuditCharacteristic("external_call");
    expect(meta).not.toBeNull();
    expect(meta?.tone).toBe("attention");
    expect(meta?.tooltip).toMatch(/external|network/i);
  });

  it("exposes provenance / retention / quarantine / coerce / signed", () => {
    expect(lookupAuditCharacteristic("provenance")).not.toBeNull();
    expect(lookupAuditCharacteristic("retention")).not.toBeNull();
    expect(lookupAuditCharacteristic("quarantine")).not.toBeNull();
    expect(lookupAuditCharacteristic("coerce")).not.toBeNull();
    expect(lookupAuditCharacteristic("signed")).not.toBeNull();
  });

  it("describes coerce as typed-schema capability, not observed-mode behavior", () => {
    const meta = lookupAuditCharacteristic("coerce");
    expect(meta).not.toBeNull();
    expect(meta?.label).toBe("can coerce types");
    expect(meta?.tooltip).toMatch(/fixed.*flexible.*declared/i);
    expect(meta?.tooltip).toMatch(/observed.*string/i);
  });

  it("io_write has informational tone (not attention)", () => {
    const meta = lookupAuditCharacteristic("io_write");
    expect(meta).not.toBeNull();
    expect(meta?.tone).toBe("informational");
  });

  // elspeth-cfa3faad35: all twelve chips wrap into ONE row at wide viewports,
  // so a lone capitalised label reads as a proper noun or as a more important
  // characteristic than its neighbours. "Network call" was the only offender.
  // Asserted as the constraint rather than as a list of literals: a label's
  // leading alphabetic run must be all-lower (sentence case) or all-upper (an
  // acronym such as "HMAC-signed"), never Mixed.
  it("labels are sentence case, apart from leading acronyms", () => {
    const offenders = AUDIT_CHARACTERISTICS.filter((meta) => {
      const leading = /^[A-Za-z]+/.exec(meta.label)?.[0] ?? "";
      return (
        leading !== leading.toLowerCase() && leading !== leading.toUpperCase()
      );
    }).map((meta) => `${meta.flag}: ${meta.label}`);
    expect(offenders).toEqual([]);
  });

  it("returns null for an unknown flag rather than crashing", () => {
    // Future flags added on the backend without a frontend metadata
    // entry should render as a small grey "unknown" chip, not crash.
    expect(lookupAuditCharacteristic("future_flag_2027")).toBeNull();
  });

  it("KNOWN_AUDIT_FLAGS lists every metadata key", () => {
    expect(KNOWN_AUDIT_FLAGS).toContain("io_read");
    expect(KNOWN_AUDIT_FLAGS).toContain("external_call");
    expect(KNOWN_AUDIT_FLAGS).toContain("quarantine");
  });

  it("AUDIT_CHARACTERISTICS table includes determinism-derived flags", () => {
    // The Phase-7A derivation rules turn Determinism enum values into
    // flag strings verbatim (io_read, io_write, external_call,
    // deterministic, seeded, non_deterministic). The frontend metadata
    // table must cover each so the inferred-flag case has a renderer.
    for (const flag of ["io_read", "io_write", "external_call", "deterministic", "seeded", "non_deterministic"]) {
      expect(lookupAuditCharacteristic(flag)).not.toBeNull();
    }
  });
});
