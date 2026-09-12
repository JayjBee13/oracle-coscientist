import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import * as api from "../api/client";
import type { ModelsPayload, RoleModelRow } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { AdaptiveWorkflowDiagram } from "../components/AdaptiveWorkflowDiagram";
import { ErrorState, SkeletonBlock } from "../components/States";
import { withLabels } from "../components/graph/graphModel";
import { EXAMPLE_TRACE, buildIdeaTrace, type IdeaTrace } from "../lib/ideaTrace";
import {
  applyOverrides,
  isRoleOverridden,
  modelLabel,
  tablesFor,
  type RoleChoice,
} from "../lib/models";
import { describeEffort, describeModelTier } from "../lib/status";
import { refreshCapabilities, useCapabilities } from "../store/capabilities";
import { useModelSettings } from "../store/settings";
import { CircuitBoard } from "./circuit/CircuitBoard";
import { IdeaTraceLane } from "./circuit/IdeaTraceLane";
import { InspectorRail, type StationModelLine } from "./circuit/InspectorRail";
import { LEGEND, stationById, type StationId } from "./circuit/stations";
import "../styles/circuit.css";

/**
 * The page you send someone to when they ask "but what is it actually doing?".
 *
 * Every other view of a run shows the loop *mid-flight* — a phase name in the
 * trust line, a round bar, an event stream — and none of them shows its shape.
 * This one does, twice: once as a circuit you can interrogate station by
 * station, and once as the biography of a single idea that went round it. The
 * first is the machine; the second is the evidence that the machine ran.
 *
 * The board and the trace lane are inset panels — `--surface-inset` inside a
 * card — and they theme with the rest of the app. Nothing on this page names a
 * colour: the five wire semantics are the tone tokens the whole system already
 * uses, so "cyan carries ideas, red kills them" holds in either palette because
 * `--accent` and `--danger` hold in either palette.
 *
 * Both live things on the page are read from endpoints that already exist. The
 * model beside a station is the role table from `/api/capabilities` laid under
 * your saved Models settings, and the tier switch previews the other tiers
 * without touching what is saved. The trace is derived client-side from the most
 * recent completed run's genealogy — no endpoint was added for it, and when this
 * machine has never finished a run the lane says *example* on itself.
 */
export function HowItWorksPage() {
  const { data, error } = useCapabilities();
  const models = data?.models ?? null;
  const settings = useModelSettings().saved;

  const [selected, setSelected] = useState<StationId | null>(null);
  const [chosenTier, setChosenTier] = useState<string | null>(null);

  const provider = settings?.provider ?? null;
  const tables = tablesFor(models, provider);
  const tiers = Object.keys(tables);
  const savedTier = settings?.tier && tables[settings.tier] ? settings.tier : null;
  const tier =
    (chosenTier && tables[chosenTier] ? chosenTier : null) ??
    savedTier ??
    (models?.default_tier && tables[models.default_tier] ? models.default_tier : null);

  const overrides = settings?.overrides ?? {};
  const rows = applyOverrides(tier ? (tables[tier] ?? []) : [], overrides);

  const station = stationById(selected);
  const model = modelLine(station?.role ?? null, rows, models, overrides);

  const trace = useLatestTrace();

  return (
    <div className="stack" style={{ gap: "var(--space-6)" }}>
      <PageHeader
        eyebrow="How it works"
        title="Explore widely. Test the ideas. Build a stronger answer."
        actions={
          <Link className="btn btn--primary" to="/new">
            Start a run
          </Link>
        }
      />

      <div className="card">
        <AdaptiveWorkflowDiagram
          models={models}
          provider={provider}
          tier={tier}
          overrides={overrides}
        />
      </div>

      <details className="stack">
        <summary>Earlier tournament workflow — retained for historical runs</summary>
        <section className="card">
          <div className="row-wrap" style={{ marginBottom: "var(--space-4)" }}>
            <div>
              <h2 style={{ margin: 0 }}>The round circuit</h2>
              <p
                className="muted"
                style={{ margin: "var(--space-1) 0 0", maxWidth: "68ch" }}
              >
                Solid cyan carries ideas. Blue carries judgments. Dotted amber is steering
                — it never carries an idea, only instructions about the next ones. Red
                edges are the only two ways an idea dies.
              </p>
            </div>
            <span className="spacer" />
            {tiers.length > 1 && tier ? (
              <div className="row" role="group" aria-label="Model tier to describe">
                <span className="label">Tier</span>
                {tiers.map((name) => (
                  <button
                    key={name}
                    type="button"
                    className={`btn btn--sm${name === tier ? " btn--primary" : ""}`}
                    aria-pressed={name === tier}
                    onClick={() => setChosenTier(name)}
                  >
                    {describeModelTier(name).label}
                  </button>
                ))}
              </div>
            ) : null}
          </div>

          {tier ? (
            <p className="footnote" style={{ marginBottom: "var(--space-4)" }}>
              Stations show the <strong>{describeModelTier(tier).label}</strong> tier
              {savedTier && tier !== savedTier
                ? ` — a preview. Your saved default is ${describeModelTier(savedTier).label}, and nothing here changes it.`
                : savedTier
                  ? " — your saved default."
                  : "."}
            </p>
          ) : null}

          {error && !models ? (
            <ErrorState
              message="The model table could not be loaded, so the stations below would have no models on them."
              detail={error}
              onRetry={() => void refreshCapabilities()}
            />
          ) : null}

          <div className="stage">
            <CircuitBoard selected={selected} onSelect={setSelected} />
            <InspectorRail station={station} model={model} />
          </div>

          <ul className="legend">
            {LEGEND.map((entry) => (
              <li key={entry.kind}>
                <i
                  className={`legend__swatch legend__swatch--${entry.kind}`}
                  aria-hidden="true"
                />
                {entry.text}
              </li>
            ))}
          </ul>
        </section>

        <section className="card">
          <h2 style={{ margin: 0 }}>
            The life of one idea
            {trace.status === "ready"
              ? ` — ${trace.trace.hid}, from the last completed run`
              : ""}
          </h2>
          <p
            className="muted"
            style={{ margin: "var(--space-1) 0 var(--space-4)", maxWidth: "68ch" }}
          >
            {trace.status === "ready" ? (
              <>
                A real record
                {trace.trace.runTitle ? ` from “${trace.trace.runTitle}”` : ""}. Every
                step below is a column of that run&rsquo;s genealogy — nothing here is
                written for the occasion.
              </>
            ) : trace.status === "loading" ? (
              "Reading the most recent completed run."
            ) : (
              "This machine has not finished a run yet, so the lane below is a worked example from an earlier one — marked as such, because a page about keeping the record cannot pass an illustration off as one."
            )}
          </p>

          {trace.status === "loading" ? (
            <SkeletonBlock height={220} />
          ) : (
            <>
              {trace.status === "example" ? (
                <p className="trace__badge">Example — not from a run on this machine</p>
              ) : null}
              <IdeaTraceLane
                trace={trace.status === "ready" ? trace.trace : EXAMPLE_TRACE}
              />
            </>
          )}
        </section>
      </details>
      <p className="footnote">
        Every station&rsquo;s model and thinking effort is read live from{" "}
        <code className="mono">GET /api/capabilities</code> under your saved Models
        settings. What a run spends is wall-clock time and your plan&rsquo;s rate limits —
        the stations that fan out do so to buy back wall clock, and the one marked
        arithmetic costs no call at all.
      </p>
    </div>
  );
}

/** What the rail says about the selected station's model. */
function modelLine(
  role: string | null,
  rows: readonly RoleModelRow[],
  models: ModelsPayload | null,
  overrides: Readonly<Record<string, RoleChoice>>,
): StationModelLine {
  if (!role) return { kind: "none" };
  const row = rows.find((entry) => entry.role === role);
  if (!models || !row) return { kind: "pending" };
  return {
    kind: "ready",
    model: modelLabel(row.model, models.catalog) ?? row.model,
    effort: describeEffort(row.effort),
    overridden: isRoleOverridden(overrides, role),
    note: row.note,
  };
}

type TraceState =
  { status: "loading" } | { status: "ready"; trace: IdeaTrace } | { status: "example" };

/**
 * The most recent completed run's best-told idea, or nothing.
 *
 * Two requests the app already makes elsewhere — the runs list and one run's
 * graph — and no new endpoint. Every failure lands in the same place: there is
 * no trace to show, and the page falls back to the labelled example. That is
 * deliberate. A page explaining the machine must not itself become an error
 * screen because the machine has never been run.
 */
function useLatestTrace(): TraceState {
  const [state, setState] = useState<TraceState>({ status: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;

    async function load(): Promise<void> {
      try {
        const page = await api.listRuns(
          { status: "completed", sort: "recent", page_size: 1 },
          controller.signal,
        );
        const run = page.items[0];
        if (!run) throw new Error("no completed run");
        const graph = await api.getRunGraph(run.id, controller.signal);
        const derived = buildIdeaTrace(withLabels(graph));
        if (cancelled) return;
        if (!derived) {
          setState({ status: "example" });
          return;
        }
        // The run we asked for, not the id echoed back inside the graph: the
        // link under the lane must point at the run this page actually read.
        setState({
          status: "ready",
          trace: { ...derived, runId: run.id, runTitle: run.title },
        });
      } catch {
        if (!cancelled) setState({ status: "example" });
      }
    }

    void load();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, []);

  return state;
}
