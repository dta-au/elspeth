// ============================================================================
// AppHeader
//
// Thin top-level header. Three regions left-to-right:
//   - ELSPETH brand
//   - HeaderSessionSwitcher
//   - HeaderVersionSelector
//   - UserMenu
// ============================================================================

import { HeaderSessionSwitcher } from "@/components/sessions/HeaderSessionSwitcher";
import { HeaderVersionSelector } from "@/components/header/HeaderVersionSelector";
import { ModelChip } from "@/components/chat/ModelChip";
import { UserMenu } from "@/components/common/UserMenu";
import { WordMark } from "@/components/ui";

interface AppHeaderProps {
  onOpenSettings: () => void;
  onSignOut: () => void;
  /** Forwarded to UserMenu; present only for the env-flagged dev admin. */
  onOpenUserManagement?: () => void;
}

export function AppHeader({
  onOpenSettings,
  onSignOut,
  onOpenUserManagement,
}: AppHeaderProps): JSX.Element {
  return (
    <header className="app-header" role="banner">
      <div className="app-header-left">
        <WordMark as="span" />
        <HeaderSessionSwitcher />
        <span className="app-header-separator" aria-hidden="true" />
        <HeaderVersionSelector />
        {/* Composer-model identity (elspeth-e9f7678de8) — relocated from the
            chat-panel headers (elspeth-8fa71e6d15): identity chrome belongs in
            the identity-chrome region, which has page width and never fights
            the 360px authoring column. Renders nothing when the status
            endpoint reports no model. */}
        <span className="app-header-separator" aria-hidden="true" />
        <ModelChip />
      </div>
      <div className="app-header-right">
        <UserMenu
          onOpenSettings={onOpenSettings}
          onSignOut={onSignOut}
          onOpenUserManagement={onOpenUserManagement}
        />
      </div>
    </header>
  );
}
