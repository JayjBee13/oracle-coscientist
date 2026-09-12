import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

type Props = {
  children: ReactNode;
  /** Changing this resets the boundary — wire it to the route so navigating
   *  away from a broken screen clears the error. */
  resetKey?: string;
};

type State = { error: Error | null };

/**
 * No white pages. A render that throws lands here with the message, a way back
 * and the stack behind a disclosure — the previous UI showed a blank document
 * when a module failed to load, which is indistinguishable from a hung app.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidUpdate(previous: Props): void {
    if (previous.resetKey !== this.props.resetKey && this.state.error) {
      this.setState({ error: null });
    }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Unhandled error in the interface", error, info.componentStack);
  }

  private reset = (): void => {
    this.setState({ error: null });
  };

  private reload = (): void => {
    window.location.reload();
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="state state--error" role="alert">
        <div className="state__title">This screen stopped working</div>
        <p className="state__body">
          Something in the interface threw an error, so it was taken down rather than left
          half-drawn. Your runs are unaffected — nothing here writes to them.
        </p>
        <div className="row">
          <button type="button" className="btn btn--primary" onClick={this.reset}>
            Try again
          </button>
          <button type="button" className="btn" onClick={this.reload}>
            Reload the app
          </button>
        </div>
        <details>
          <summary className="label" style={{ cursor: "pointer" }}>
            Technical detail
          </summary>
          <pre className="state__detail">{error.stack ?? error.message}</pre>
        </details>
      </div>
    );
  }
}
