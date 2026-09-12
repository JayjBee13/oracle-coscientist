import type { ReactNode } from "react";

/**
 * Empty, loading and error states. The rule from the UX review: an error state
 * replaces the content — it never sits next to a grid of zeros that implies the
 * data loaded and the answer is nothing. Every error names the problem and
 * offers a way forward.
 */

export function EmptyState({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="state">
      <div className="state__title">{title}</div>
      {children ? <p className="state__body">{children}</p> : null}
      {action}
    </div>
  );
}

export function ErrorState({
  title = "That did not load",
  message,
  detail,
  onRetry,
  retryLabel = "Try again",
  action,
}: {
  title?: string;
  message: string;
  detail?: string;
  onRetry?: () => void;
  retryLabel?: string;
  action?: ReactNode;
}) {
  return (
    <div className="state state--error" role="alert">
      <div className="state__title">{title}</div>
      <p className="state__body">{message}</p>
      {detail ? <pre className="state__detail">{detail}</pre> : null}
      {onRetry ? (
        <button type="button" className="btn btn--primary" onClick={onRetry}>
          {retryLabel}
        </button>
      ) : null}
      {action}
    </div>
  );
}

export function SkeletonText({ lines = 3, width }: { lines?: number; width?: string }) {
  return (
    <div aria-hidden="true">
      {Array.from({ length: lines }, (_, index) => (
        <div
          key={index}
          className="skeleton skeleton--text"
          style={{ width: width ?? `${92 - index * 11}%` }}
        />
      ))}
    </div>
  );
}

export function SkeletonBlock({ height }: { height?: number }) {
  return (
    <div
      className="skeleton skeleton--block"
      style={height ? { height } : undefined}
      aria-hidden="true"
    />
  );
}

/** Placeholder body used by the route stubs until their real screens land. */
export function LoadingPage({ label }: { label: string }) {
  return (
    <div className="stack" aria-busy="true" aria-label={label}>
      <div className="skeleton skeleton--title" aria-hidden="true" />
      <SkeletonBlock />
      <SkeletonBlock />
      <span className="visually-hidden">{label}</span>
    </div>
  );
}
