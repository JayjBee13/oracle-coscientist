import { useToasts } from "../lib/toast";

/**
 * The single live region for transient messages. Danger toasts announce
 * assertively; everything else waits its turn so a screen reader is not
 * interrupted mid-sentence by a success message.
 */
export function Toaster() {
  const { toasts, dismiss } = useToasts();

  return (
    <div className="toaster" aria-live="polite" aria-atomic="false">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className="toast"
          data-tone={toast.tone}
          role={toast.tone === "danger" ? "alert" : "status"}
        >
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="toast__title">{toast.title}</div>
            {toast.message ? <div className="toast__message">{toast.message}</div> : null}
          </div>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => dismiss(toast.id)}
            aria-label={`Dismiss: ${toast.title}`}
          >
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}
