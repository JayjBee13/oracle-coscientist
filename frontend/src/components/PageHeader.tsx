import type { ReactNode } from "react";

export function PageHeader({
  eyebrow,
  title,
  children,
  actions,
}: {
  eyebrow?: string;
  title: string;
  children?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header
      className="stack"
      style={{ gap: "var(--space-2)", marginBottom: "var(--space-6)" }}
    >
      {eyebrow ? <span className="label">{eyebrow}</span> : null}
      <div className="row-wrap">
        <h1 style={{ minWidth: 0 }}>{title}</h1>
        <span className="spacer" />
        {actions}
      </div>
      {children ? <p className="muted">{children}</p> : null}
    </header>
  );
}
