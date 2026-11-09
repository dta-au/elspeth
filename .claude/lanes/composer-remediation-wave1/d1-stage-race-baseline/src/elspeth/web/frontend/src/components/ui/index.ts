// ============================================================================
// ui/ primitive library — FULL-MIGRATION policy (operator decision 2026-08-14)
//
// The previous adopt-as-touched policy (elspeth-e1c5ad0b53) did not move the
// number: at approval time the census stood at 217 raw <button> against 10
// <Button>. On 2026-08-14 the operator approved a one-time full migration of
// every raw <button>/<input> outside components/ui/ onto these primitives,
// executed as the D3 wave. The mechanical rule the wave applied — and the
// rule for any control written from now on:
//
//   - className "btn" / "btn …" -> <Button>, variant mapped from
//     btn-primary/-danger/-ghost, compact from btn-compact; mapped classes
//     stripped, the rest kept. type defaults to "button", so every button
//     inside a <form> carries an explicit type decision.
//   - any other (bespoke) className -> <Button variant="bare" className=…>
//     verbatim. Bespoke recipes were NOT collapsed onto .btn in this wave.
//   - <input> -> <Input>; the non-text types {checkbox, radio, range, file,
//     color} omit the .input base class by the type rule, and a text-like
//     field with a complete bespoke recipe takes `bare` (className verbatim,
//     no .input chrome — Button's variant="bare" for inputs).
//
// primitiveCensus.test.ts (this directory) enforces the end state from now
// on: zero raw <button> AND zero raw <input> outside components/ui/, both
// gated LIVE. The input half briefly shipped staged (six bespoke-recipe text
// inputs waited on the `bare` escape); it went live the same wave once
// Input grew `bare` and the six sites migrated. Do not add new raw sites.
//
// Unused primitives still get retired: Card, Tabs, and Textarea were deleted
// in wave 3 of the 2026-07-02 UX remediation because they had no consumer
// and no mechanical adoption site (see epic elspeth-6cf0e1e188 notes). No
// new primitive without a concrete first consumer.
// ============================================================================

export { Button } from "./Button";
export type { ButtonProps } from "./Button";

export { TypeBadge } from "./TypeBadge";
export type { TypeBadgeProps } from "./TypeBadge";

export { StatusBadge } from "./StatusBadge";
export type { StatusBadgeProps } from "./StatusBadge";

export { AlertBanner } from "./AlertBanner";
export type { AlertBannerProps } from "./AlertBanner";

export { Input } from "./Input";
export type { InputProps } from "./Input";

export { WordMark } from "./WordMark";
export type { WordMarkProps } from "./WordMark";

export { Icon } from "./Icon";
export type { IconName, IconProps } from "./Icon";

export { StructuredJsonPreview } from "./StructuredJsonPreview";

export { PreviewTable } from "./PreviewTable";
export type { PreviewTableModel } from "./PreviewTable";
