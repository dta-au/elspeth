/**
 * Shared inspection owns its URL even when the token cannot be decoded.
 *
 * Distinguish malformed share links from ordinary workspace navigation so
 * a refusal never displays the recipient's active composition instead.
 *
 * This hook is a peer of `useHashRouter` and runs BEFORE the regular
 * composer route is mounted. The intended call site is the App
 * component, which short-circuits to `SharedInspectView` when this
 * hook returns a shared route. The regular `useHashRouter`'s session
 * handling does not fire in shared-route mode because App.tsx renders a
 * different tree.
 *
 * The token is URL-encoded in transit (the share_url is the raw token
 * appended to `/#/shared/`); we decode here so callers receive the
 * canonical token bytes.
 */

import { useEffect, useState } from "react";
import { parseSharedRoute, type SharedRoute } from "@/utils/hashRoutes";

export function useSharedRoute(): SharedRoute {
  const [route, setRoute] = useState<SharedRoute>(() =>
    parseSharedRoute(typeof window !== "undefined" ? window.location.hash : ""),
  );

  useEffect(() => {
    function handleHashChange() {
      setRoute(parseSharedRoute(window.location.hash));
    }
    window.addEventListener("hashchange", handleHashChange);
    window.addEventListener("popstate", handleHashChange);
    return () => {
      window.removeEventListener("hashchange", handleHashChange);
      window.removeEventListener("popstate", handleHashChange);
    };
  }, []);

  return route;
}
