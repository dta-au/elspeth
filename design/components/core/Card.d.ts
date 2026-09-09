import * as React from "react";

/**
 * Surface container. Default uses the workspace surface; `paper` switches to
 * the warm-neutral inspection family for right-rail / modal panels.
 *
 * @startingPoint section="Core" subtitle="Surface card — workspace or paper" viewport="700x200"
 */
export interface CardProps extends React.HTMLAttributes<HTMLDivElement> {
  /** Use the warm-neutral inspection (paper) surface. @default false */
  paper?: boolean;
  /** Apply default 16px padding. Set false for full-bleed media. @default true */
  pad?: boolean;
}

export function Card(props: CardProps): React.ReactElement;

export interface CardHeaderProps {
  title: React.ReactNode;
  /** Right-aligned actions (buttons, menu). */
  actions?: React.ReactNode;
  /** Small uppercase label above the title. */
  eyebrow?: React.ReactNode;
}

export function CardHeader(props: CardHeaderProps): React.ReactElement;
