import { useId, useRef, useState } from "react";

import { Button } from "@/components/ui";
import { ModeSwitchButton } from "./guided/ModeSwitchButton";

interface ComposerOptionsProps {
  hasWork: boolean;
  disabledReason?: string;
}

/** Inline disclosure keeps the existing goal and confirmation controls usable at narrow widths. */
export function ComposerOptions({ hasWork, disabledReason }: ComposerOptionsProps): JSX.Element {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();

  return (
    <div
      className="composer-options"
      onKeyDown={(event) => {
        if (event.key === "Escape" && open) {
          event.preventDefault();
          event.stopPropagation();
          setOpen(false);
          triggerRef.current?.focus();
        }
      }}
    >
      <Button
        ref={triggerRef}
        variant="bare"
        className="mode-switch-btn"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((value) => !value)}
      >
        Composer options
      </Button>
      <div id={panelId} hidden={!open}>
        {open && <ModeSwitchButton target="guided" hasWork={hasWork} disabledReason={disabledReason} />}
      </div>
    </div>
  );
}
