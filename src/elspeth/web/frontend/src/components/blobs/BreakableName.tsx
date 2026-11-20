import { Fragment, type JSX } from "react";

/**
 * A file or step name that may wrap at its own separators (`_`, `.`, `-`,
 * `/`) instead of mid-word: long generated names otherwise break at an
 * arbitrary letter ("…revised_v7_us / er_prompt.txt") or overflow their box.
 * The full name stays in the text; `<wbr>` adds no characters, so copy,
 * search and assistive tech read it unchanged.
 */
export function BreakableName({ name }: { name: string }): JSX.Element {
  const parts = name.split(/(?<=[_./-])/);
  return <>{parts.map((part, index) => <Fragment key={index}>{part}{index < parts.length - 1 && <wbr />}</Fragment>)}</>;
}
