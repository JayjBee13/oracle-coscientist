import { Link } from "react-router-dom";

import { EmptyState } from "../components/States";

export function NotFoundPage() {
  return (
    <EmptyState
      title="Nothing lives at this address"
      action={
        <Link className="btn btn--primary" to="/">
          Back to the runs
        </Link>
      }
    >
      The link may be from an older version of the app, or the run it pointed at was
      deleted.
    </EmptyState>
  );
}
