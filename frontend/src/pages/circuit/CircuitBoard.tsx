import { useEffect } from "react";

import {
  CIRCUIT_LABEL,
  EDGES,
  GRAVEYARD_NOTE,
  STATIONS,
  type StationId,
} from "./stations";

/**
 * The round, drawn as a circuit board.
 *
 * Every station is a button. Pressing one dims the whole board and lights that
 * station, the wires that touch it and the stations on the other end of those
 * wires — which is the one question this drawing exists to answer: *what talks
 * to what*. A diagram you can only look at answers it for the two boxes nearest
 * your eye and leaves the other eight to a legend.
 *
 * The board draws itself and reports the selection upward; the copy that appears
 * beside it is the page's business, not this component's. That split is what
 * lets the inspector rail carry live model data — a fact this file knows nothing
 * about and should not.
 *
 * Accessibility is not a layer over the drawing, it is how it is built: the SVG
 * carries one sentence describing the whole flow, each station is a real toggle
 * button in the tab order with a name and a pressed state, and the focus ring is
 * drawn on the station box rather than suppressed. Escape clears the selection
 * from anywhere on the page, because a reader who has dimmed the board while
 * exploring should not have to find the same box again to undim it.
 */
export function CircuitBoard({
  selected,
  onSelect,
}: {
  selected: StationId | null;
  onSelect: (station: StationId | null) => void;
}) {
  useEffect(() => {
    if (!selected) return;
    function onKeyDown(event: KeyboardEvent): void {
      if (event.key === "Escape") onSelect(null);
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [selected, onSelect]);

  const lit = new Set(
    EDGES.filter((edge) => selected !== null && edge.ends.includes(selected)).map(
      (edge) => edge.id,
    ),
  );
  const peers = new Set<StationId>();
  for (const edge of EDGES) {
    if (!lit.has(edge.id)) continue;
    for (const end of edge.ends) if (end !== selected) peers.add(end);
  }

  return (
    <div className="board">
      {/* The viewBox is cropped to what is actually drawn (y 92…495) with a
          margin. The stations are laid out in a 1240×620 space and the bands
          above and below the drawing are empty; rendered, they are 150px of
          dark nothing under the diagram. */}
      <svg
        viewBox="0 56 1240 450"
        className={`circuit${selected ? " circuit--focused" : ""}`}
        role="img"
        aria-label={CIRCUIT_LABEL}
      >
        <defs>
          {(["idea", "eval", "guide", "death", "exit"] as const).map((kind) => (
            <marker
              key={kind}
              id={`circuit-arrow-${kind}`}
              viewBox="0 0 8 8"
              refX="7"
              refY="4"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
              className={`circuit__head circuit__head--${kind}`}
            >
              <path d="M0 0L8 4L0 8z" />
            </marker>
          ))}
        </defs>

        {EDGES.map((edge) => {
          const on = lit.has(edge.id) ? " is-lit" : "";
          return (
            <g key={edge.id}>
              <path
                className={`circuit__wire circuit__wire--${edge.kind}${on}`}
                d={edge.d}
                markerEnd={`url(#circuit-arrow-${edge.kind})`}
              />
              {edge.labels.map((label) => (
                <text
                  key={`${label.x}-${label.y}-${label.text}`}
                  className={`circuit__wire-label circuit__wire-label--${label.tone ?? "muted"}${on}`}
                  x={label.x}
                  y={label.y}
                >
                  {label.text}
                </text>
              ))}
            </g>
          );
        })}

        {STATIONS.map((station) => {
          const active = station.id === selected;
          const state = active ? " is-active" : peers.has(station.id) ? " is-peer" : "";
          return (
            <g
              key={station.id}
              className={`circuit__station${station.small ? " circuit__station--small" : ""}${state}`}
              role="button"
              tabIndex={0}
              aria-pressed={active}
              aria-label={station.title}
              onClick={() => onSelect(active ? null : station.id)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  onSelect(active ? null : station.id);
                }
                // Escape is also handled document-wide; keeping it here means a
                // keyboard reader gets it without the selection having reached
                // the effect above yet.
                if (event.key === "Escape") onSelect(null);
              }}
            >
              <rect
                x={station.rect.x}
                y={station.rect.y}
                width={station.rect.width}
                height={station.rect.height}
                rx={6}
              />
              {station.texts.map((text) => (
                <text
                  key={`${text.x}-${text.y}-${text.text}`}
                  className={`circuit__${text.kind}${text.tight ? " circuit__title--tight" : ""}`}
                  x={text.x}
                  y={text.y}
                >
                  {text.text}
                </text>
              ))}
            </g>
          );
        })}

        <text
          className="circuit__wire-label circuit__wire-label--red"
          x={GRAVEYARD_NOTE.x}
          y={GRAVEYARD_NOTE.y}
        >
          {GRAVEYARD_NOTE.text}
        </text>
      </svg>
    </div>
  );
}
