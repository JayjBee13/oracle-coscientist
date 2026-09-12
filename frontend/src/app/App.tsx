import { useLocation } from "react-router-dom";

import { advanceWorkspaceRequestEpoch } from "../api/client";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { ErrorState, LoadingPage } from "../components/States";
import { clearToasts } from "../lib/toast";
import { fetchIdentity, onIdentityBoundaryChange, useIdentity } from "../store/identity";
import { resetCapabilitiesStore } from "../store/capabilities";
import { resetRunStore } from "../store/runs";
import { resetModelSettingsStore } from "../store/settings";
import { Toaster } from "../components/Toaster";
import { AppRoutes } from "./routes";
import { TopBar } from "./TopBar";

function clearPrivateWorkspace(): void {
  // Invalidate API continuations first so a response already being parsed
  // cannot refill one of the stores being cleared below.
  advanceWorkspaceRequestEpoch();
  resetRunStore();
  resetModelSettingsStore();
  resetCapabilitiesStore();
  clearToasts();
}

onIdentityBoundaryChange(clearPrivateWorkspace);

/**
 * The shell. Must be rendered inside a router — `main.tsx` supplies
 * `<BrowserRouter>`, tests supply `<MemoryRouter>`.
 *
 * The error boundary sits inside the shell rather than around it, so a screen
 * that throws leaves the top bar, the lane chips and the navigation working:
 * you can always still see whether a run is alive and get back to it.
 */
export function App() {
  const location = useLocation();
  const identity = useIdentity();
  const authenticated = identity.status === "ready" && identity.data !== null;

  return (
    <div className="app">
      <a className="visually-hidden" href="#main">
        Skip to content
      </a>
      {authenticated && identity.data ? (
        <AuthenticatedWorkspace
          key={`${identity.data.source}:${identity.data.username}:${identity.data.is_admin ? "admin" : "user"}`}
          resetKey={location.pathname}
        />
      ) : (
        <>
          <UnauthenticatedHeader />
          <main className="main" id="main">
            {identity.status === "error" ? (
              <GatewayAccessState
                message={identity.error}
                authFailure={identity.errorStatus === 401 || identity.errorStatus === 403}
              />
            ) : (
              <LoadingPage label="Opening your Oracle workspace" />
            )}
          </main>
        </>
      )}
      <Toaster />
    </div>
  );
}

function AuthenticatedWorkspace({ resetKey }: { resetKey: string }) {
  return (
    <>
      <TopBar />
      <main className="main" id="main">
        <ErrorBoundary resetKey={resetKey}>
          <AppRoutes />
        </ErrorBoundary>
      </main>
    </>
  );
}

const GATEWAY_URL = (import.meta.env.VITE_GATEWAY_URL as string | undefined) ?? "/";

function UnauthenticatedHeader() {
  return (
    <header className="topbar topbar--access">
      <a className="wordmark" href={GATEWAY_URL}>
        <span className="wordmark__mark" aria-hidden="true" />
        Oracle
        <span className="wordmark__sub">Private research workspace</span>
      </a>
    </header>
  );
}

function GatewayAccessState({
  message,
  authFailure,
}: {
  message: string | null;
  authFailure: boolean;
}) {
  return (
    <ErrorState
      title={authFailure ? "Sign in through the Oracle gateway" : "Could not open Oracle"}
      message={
        authFailure
          ? "Your workspace session is missing or has expired. Open Oracle through the gateway to sign in; there is no separate Oracle password."
          : (message ?? "Oracle could not verify your workspace session.")
      }
      action={
        <div className="row-wrap state__actions">
          <a className="btn btn--primary" href={GATEWAY_URL}>
            Open Oracle through the gateway
          </a>
          <button
            type="button"
            className="btn"
            onClick={() => void fetchIdentity({ force: true })}
          >
            Try again
          </button>
        </div>
      }
    />
  );
}
