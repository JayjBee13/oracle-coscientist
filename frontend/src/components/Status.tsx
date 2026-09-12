import type { ReactNode } from "react";

import type { Labelled, StatusDescriptor, Tone } from "../lib/status";

/**
 * Status shown the way the design system defines it: colour *and* shape, with
 * the label always present. Never render a lifecycle string directly — pass a
 * descriptor from `lib/status.ts`.
 */
export function StatusLabel({
  status,
  className,
}: {
  status: StatusDescriptor;
  className?: string;
}) {
  return (
    <span
      className={className ? `status ${className}` : "status"}
      data-tone={status.tone}
      title={status.hint || undefined}
    >
      <span className="status-mark" data-mark={status.mark} aria-hidden="true" />
      {status.label}
    </span>
  );
}

/** Status as a self-contained chip — used in the top bar and in list rows. */
export function StatusChip({
  status,
  className = "chip--quiet",
}: {
  status: StatusDescriptor;
  className?: string;
}) {
  return (
    <span
      className={`chip ${className}`}
      data-tone={status.tone}
      title={status.hint || undefined}
    >
      <span className="status-mark" data-mark={status.mark} aria-hidden="true" />
      {status.label}
    </span>
  );
}

export function Chip({
  tone = "neutral",
  title,
  className,
  children,
}: {
  tone?: Tone;
  title?: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <span
      className={className ? `chip ${className}` : "chip"}
      data-tone={tone}
      title={title}
    >
      {children}
    </span>
  );
}

export function LabelChip({
  value,
  title,
  className,
}: {
  value: Labelled;
  title?: string;
  className?: string;
}) {
  return (
    <Chip tone={value.tone} title={title} className={className}>
      {value.label}
    </Chip>
  );
}
