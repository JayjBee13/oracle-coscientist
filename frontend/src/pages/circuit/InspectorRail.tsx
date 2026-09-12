import type { Station, StationId } from "./stations";

/**
 * What the rail says about the model a station runs on.
 *
 * A discriminated union rather than three optional fields, because the three
 * cases are genuinely different sentences and the one that used to be silent —
 * the payload has not arrived — is the one worth being explicit about. A rail
 * that renders an empty model line while the fetch is in flight reads as "this
 * station runs no model", which is a claim nobody made.
 */
export type StationModelLine =
  /** No engine role runs here at all. */
  | { kind: "none" }
  /** The role table has not arrived, or could not be read. */
  | { kind: "pending" }
  | {
      kind: "ready";
      model: string;
      effort: string;
      /** True when your saved settings move this station off its tier's row. */
      overridden: boolean;
      /** The engine's own sentence about why it is set this way. */
      note: string;
    };

/**
 * Why a station has no model, in its own words.
 *
 * Two stations on the board are not steps of the round, and they are not the
 * same kind of exception: one is you, and one runs before a run exists. Saying
 * "no model" for both would be true and would explain neither.
 */
const NO_MODEL: Record<string, string> = {
  question:
    "No model runs here. This is your question — and any documents you attach — going in.",
  workshop:
    "The Workshop runs before a run exists, so it is not on a round’s model table. Its model is set where you use it.",
};

/**
 * The readout beside the board.
 *
 * It is the second half of the diagram rather than a caption on it: the drawing
 * carries the shape, and everything a reader would otherwise want written *on*
 * the shape — what a station receives, what it emits, how far it fans out, what
 * it runs on — is here instead, one station at a time. That is why the board
 * stays legible at ten stations while the previous list-shaped explainer needed
 * a paragraph per step.
 *
 * `aria-live="polite"` because selecting a station on the board changes this
 * panel and nothing else on screen; without it a keyboard reader presses Enter
 * and hears nothing at all.
 */
export function InspectorRail({
  station,
  model,
}: {
  station: Station | null;
  model: StationModelLine;
}) {
  return (
    <aside className="rail" aria-live="polite" aria-label="Station inspector">
      {station === null ? (
        <p className="rail__hint">
          Click a station in the circuit to inspect it — what it receives, what it emits,
          and how it fans out. The model and thinking effort on each are read live from
          your <strong>Models</strong> settings.
        </p>
      ) : (
        <>
          <div className="rail__caption">{station.caption}</div>
          <h3 className="rail__title">{station.title}</h3>
          {station.fan.length > 0 ? (
            <div className="rail__fans">
              {station.fan.map((fan) => (
                <span key={fan} className="rail__fan">
                  {fan}
                </span>
              ))}
            </div>
          ) : null}
          <dl className="rail__facts">
            <div>
              <dt>Receives</dt>
              <dd>{station.receives}</dd>
            </div>
            <div>
              <dt>Emits</dt>
              <dd>{station.emits}</dd>
            </div>
            <ModelFact station={station.id} model={model} />
          </dl>
        </>
      )}
    </aside>
  );
}

function ModelFact({ station, model }: { station: StationId; model: StationModelLine }) {
  if (model.kind === "none") {
    return (
      <div>
        <dt>Model</dt>
        <dd className="rail__norole">{NO_MODEL[station] ?? "No model runs here."}</dd>
      </div>
    );
  }

  return (
    <div>
      <dt>Model · effort</dt>
      {model.kind === "pending" ? (
        <dd className="rail__norole">Reading the role table…</dd>
      ) : (
        <>
          <dd className="rail__model">
            {model.model} · {model.effort}
            {model.overridden ? <span className="rail__override">set by you</span> : null}
          </dd>
          {model.note ? <dd className="rail__why">{model.note}</dd> : null}
        </>
      )}
    </div>
  );
}
