import type { SVGProps } from "react";

// Sizing contract (D3 wave ruling, elspeth-2b88e3ca6d): geometry is defined
// once in the 24-unit viewBox with a 1.8-unit stroke; consumers choose the
// RENDERED size (width/height props, or a CSS class — CSS width/height beat
// the 16px attribute defaults) and the effective stroke scales with it:
// 1.2px at the 16px default, 1.5px at the chat family's 20px
// (.chat-input-icon, chat.css). No per-size stroke compensation — pass a
// larger strokeWidth only if a design explicitly calls for heavier line work.
//
// "more" is drawn as three FILLED circles, never zero-length round-cap
// strokes: at 20px a 1.5px round cap rasterises as a hard square carrying an
// order of magnitude less ink than sibling glyphs, so the control reads as
// disabled (elspeth-b720e0b932; geometry matches ChatInput's shipped dots so
// the ChatInputIcon retirement swap is pixel-identical).
export type IconName =
  | "assistant"
  | "check"
  | "chevron-down"
  | "chevron-up"
  | "chevrons-left"
  | "chevrons-right"
  | "cross"
  | "download"
  | "eye"
  | "folder"
  | "key"
  | "more"
  | "pipeline"
  | "play"
  | "status-error"
  | "status-pending"
  | "status-ready"
  | "status-unknown"
  | "trash"
  | "upload"
  | "user";

export interface IconProps extends Omit<SVGProps<SVGSVGElement>, "name"> {
  name: IconName;
}

export function Icon({ name, className, ...props }: IconProps) {
  return (
    <svg
      aria-hidden="true"
      className={["ui-icon", className].filter(Boolean).join(" ")}
      data-icon={name}
      fill="none"
      focusable="false"
      height="16"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.8"
      viewBox="0 0 24 24"
      width="16"
      {...props}
    >
      {renderIcon(name)}
    </svg>
  );
}

function renderIcon(name: IconName) {
  switch (name) {
    case "assistant":
      return (
        <>
          <rect x="5" y="8" width="14" height="10" rx="3" />
          <path d="M9 8V5h6v3" />
          <path d="M9 13h.01" />
          <path d="M15 13h.01" />
          <path d="M10 17h4" />
        </>
      );
    case "check":
      // The plain (uncircled) sibling of status-ready's inner mark, scaled to
      // the full canvas at the same arm proportions (elspeth-0febf2e71b).
      return <path d="m5 13 4.5 4.5 9.5-10" />;
    case "chevron-down":
      return <path d="m6 9.5 6 6 6-6" />;
    case "chevron-up":
      return <path d="m6 14.5 6-6 6 6" />;
    // Doubled chevrons: the sidebar collapse/expand vocabulary (points the
    // way the pane moves). 5.5-unit arms (slightly tighter than the single
    // chevrons' 6) so the pair centers on the canvas without crowding it.
    case "chevrons-left":
      return (
        <>
          <path d="m11 17.5-5.5-5.5 5.5-5.5" />
          <path d="m18.5 17.5-5.5-5.5 5.5-5.5" />
        </>
      );
    case "chevrons-right":
      return (
        <>
          <path d="m5.5 17.5 5.5-5.5-5.5-5.5" />
          <path d="m13 17.5 5.5-5.5-5.5-5.5" />
        </>
      );
    case "cross":
      return (
        <>
          <path d="m6.5 6.5 11 11" />
          <path d="m17.5 6.5-11 11" />
        </>
      );
    case "download":
      return (
        <>
          <path d="M12 4v11" />
          <path d="m7 10 5 5 5-5" />
          <path d="M5 20h14" />
        </>
      );
    case "eye":
      return (
        <>
          <path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z" />
          <circle cx="12" cy="12" r="2.75" />
        </>
      );
    case "folder":
      // Geometry matches ChatInputIcon's shipped folder (ChatInput.tsx) so the
      // elspeth-2b88e3ca6d retirement swap is pixel-identical.
      return <path d="M3 6.5h6l1.5 2H21v9.5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6.5Zm0 3h18" />;
    case "key":
      // Geometry matches ChatInputIcon's shipped key (ChatInput.tsx).
      return (
        <path d="M14.5 9.5a4 4 0 1 0-3.2 3.9L9 15.7V18H6.7L5 19.7 3.3 18l6.3-6.3a4 4 0 0 0 4.9-2.2Zm.5-2h.01" />
      );
    case "more":
      // FILLED circles, never zero-length round-cap strokes — see the header
      // comment and elspeth-b720e0b932. fill="currentColor" is a presentation
      // attribute ON each circle, so it beats the fill="none" inherited from
      // the root; the inherited 1.8-unit stroke keeps the dots' weight
      // relationship to sibling glyphs. Centres/radius match ChatInputIcon.
      return (
        <>
          <circle cx="5" cy="12" r="1.4" fill="currentColor" />
          <circle cx="12" cy="12" r="1.4" fill="currentColor" />
          <circle cx="19" cy="12" r="1.4" fill="currentColor" />
        </>
      );
    case "pipeline":
      return (
        <>
          <circle cx="6" cy="7" r="2" />
          <circle cx="18" cy="7" r="2" />
          <circle cx="12" cy="17" r="2" />
          <path d="M8 8.5 11 15" />
          <path d="M16 8.5 13 15" />
        </>
      );
    case "play":
      return <path d="m8 5 11 7-11 7Z" />;
    case "status-error":
      return (
        <>
          <path d="M12 4 3 20h18L12 4Z" />
          <path d="M12 9v4" />
          <path d="M12 17h.01" />
        </>
      );
    case "status-pending":
      return (
        <>
          <circle cx="12" cy="12" r="8" />
          <path d="M12 8v4l3 2" />
        </>
      );
    case "status-ready":
      return (
        <>
          <circle cx="12" cy="12" r="8" />
          <path d="m8.5 12.5 2.25 2.25 4.75-5" />
        </>
      );
    case "status-unknown":
      return (
        <>
          <circle cx="12" cy="12" r="8" />
          <path d="M9.75 9.75a2.5 2.5 0 1 1 3.6 2.25c-.8.42-1.35 1.05-1.35 2" />
          <path d="M12 17h.01" />
        </>
      );
    case "trash":
      return (
        <>
          <path d="M4 7h16" />
          <path d="M10 11v6" />
          <path d="M14 11v6" />
          <path d="M6 7l1 13h10l1-13" />
          <path d="M9 7V4h6v3" />
        </>
      );
    case "upload":
      // Up-arrow-out-of-tray; geometry matches ChatInputIcon's shipped upload
      // (ChatInput.tsx). Deliberately NOT a mirror of "download", whose
      // baseline form points the opposite way (M_chat_root request).
      return <path d="M12 16V4m0 0 4 4m-4-4-4 4M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3" />;
    case "user":
      return (
        <>
          <circle cx="12" cy="8" r="3" />
          <path d="M5 20a7 7 0 0 1 14 0" />
        </>
      );
  }
}
