import { useId } from "react";

const positions: Record<
  string,
  { x: number; y: number; w: number; h: number; title: string; lines: string[] }
> = {
  framing: {
    x: 235,
    y: 60,
    w: 245,
    h: 110,
    title: "Frame the problem",
    lines: ["Competing explanations", "Subproblems + source strategy"],
  },
  choose: {
    x: 555,
    y: 60,
    w: 270,
    h: 110,
    title: "Choose the next work",
    lines: ["Explore · develop · verify · reframe", "Guided by evidence and open gaps"],
  },
  explore: {
    x: 80,
    y: 280,
    w: 460,
    h: 155,
    title: "Explore independently or develop",
    lines: [],
  },
  verify: {
    x: 690,
    y: 280,
    w: 250,
    h: 125,
    title: "Critique + evidence",
    lines: [
      "Test decisive claims",
      "Find support and counterevidence",
      "Check bounded arithmetic",
    ],
  },
  portfolio: {
    x: 700,
    y: 535,
    w: 250,
    h: 110,
    title: "Preserve alternatives",
    lines: ["Different approaches and strengths", "Uncertain ideas can develop"],
  },
  synthesis: {
    x: 390,
    y: 535,
    w: 250,
    h: 110,
    title: "Build a complete answer",
    lines: ["Combine compatible discoveries", "Expose dependencies and gaps"],
  },
  challenge: {
    x: 80,
    y: 535,
    w: 250,
    h: 110,
    title: "Challenge the answer",
    lines: ["Does it solve the original problem?", "What could invalidate it?"],
  },
};

export function ResearchFlowMap({
  stages,
  selected,
  onSelect,
  detailId,
}: {
  stages: readonly { id: string; title: string }[];
  selected: string;
  onSelect: (id: string) => void;
  detailId: string;
}) {
  const id = useId();
  return (
    <figure className="research-map">
      <div
        className="research-map__viewport"
        tabIndex={0}
        aria-label="Workflow diagram; scroll horizontally on narrow screens"
      >
        <svg
          viewBox="0 0 1240 815"
          role="group"
          aria-labelledby={`${id}-title ${id}-description`}
        >
          <title id={`${id}-title`}>
            Oracle workflow: independent approaches, evidence and feedback loops
          </title>
          <desc id={`${id}-description`}>
            Your question leads to framing and a research decision. Independent approaches
            feed critique, evidence checks, a diverse portfolio, synthesis and challenge.
            Challenge findings return to the next research decision; preserved ideas and
            evidence return to development. Reframing revisits assumptions. Finishing
            produces the answer with its evidence limits. Select any numbered stage for
            details.
          </desc>
          <defs>
            {["ideas", "feedback", "evidence", "answer"].map((tone) => (
              <marker
                key={tone}
                id={`${id}-${tone}`}
                viewBox="0 0 10 10"
                refX="9"
                refY="5"
                markerWidth="7"
                markerHeight="7"
                orient="auto"
              >
                <path
                  d="M 0 0 L 10 5 L 0 10 z"
                  className={`research-map__arrow research-map__arrow--${tone}`}
                />
              </marker>
            ))}
            <pattern
              id={`${id}-grid`}
              width="24"
              height="24"
              patternUnits="userSpaceOnUse"
            >
              <circle cx="2" cy="2" r="1" className="research-map__dot" />
            </pattern>
          </defs>
          <rect width="1240" height="815" fill={`url(#${id}-grid)`} />
          <g aria-hidden="true">
            {[
              "M195 115 H235",
              "M480 115 H555",
              "M620 170 V220 H310 V280",
              "M540 345 H690",
              "M700 590 H640",
              "M390 590 H330",
            ].map((d) => (
              <path
                key={d}
                className="research-map__wire"
                markerEnd={`url(#${id}-ideas)`}
                d={d}
              />
            ))}
            {["M795 170 V220 H815 V280", "M815 405 V535"].map((d) => (
              <path
                key={d}
                className="research-map__wire research-map__wire--evidence"
                markerEnd={`url(#${id}-evidence)`}
                d={d}
              />
            ))}
            <path
              className="research-map__wire research-map__wire--feedback"
              markerEnd={`url(#${id}-feedback)`}
              d="M605 60 V25 H355 V60"
            />
            <path
              className="research-map__wire research-map__wire--feedback"
              markerEnd={`url(#${id}-feedback)`}
              d="M205 535 V480 H680 V170"
            />
            <path
              className="research-map__wire research-map__wire--feedback"
              markerEnd={`url(#${id}-feedback)`}
              d="M825 645 V705 H350 V488 Q362 480 350 472 V435"
            />
            <path
              className="research-map__wire research-map__wire--answer"
              markerEnd={`url(#${id}-answer)`}
              d="M205 645 V770 H1200 V115 H1190"
            />
            <text
              x="465"
              y="19"
              className="research-map__label research-map__label--feedback"
            >
              Revisit the framing
            </text>
            <text x="400" y="212" className="research-map__label">
              Explore / develop
            </text>
            <text
              x="839"
              y="225"
              className="research-map__label research-map__label--evidence"
            >
              Verify a decisive gap
            </text>
            <text
              x="367"
              y="470"
              className="research-map__label research-map__label--feedback"
            >
              Challenge findings → next checkpoint
            </text>
            <text
              x="415"
              y="697"
              className="research-map__label research-map__label--feedback"
            >
              Preserved ideas + evidence → further development
            </text>
            <text
              x="395"
              y="762"
              className="research-map__label research-map__label--answer"
            >
              When you finish or a limit is reached → answer + open questions
            </text>
            <g className="research-map__endpoint" transform="translate(30,75)">
              <rect width="165" height="80" rx="12" />
              <text x="18" y="32" className="research-map__heading">
                Your hard problem
              </text>
              <text x="18" y="56" className="research-map__copy">
                Goal + constraints
              </text>
            </g>
            <g
              className="research-map__endpoint research-map__endpoint--answer"
              transform="translate(975,60)"
            >
              <rect width="215" height="125" rx="12" />
              <text x="18" y="30" className="research-map__heading">
                The strongest answer
              </text>
              <text x="18" y="54" className="research-map__copy">
                <tspan x="18">Developed solution + alternatives</tspan>
                <tspan x="18" dy="22">
                  Evidence limits + next tests
                </tspan>
                <tspan x="18" dy="22">
                  Unfinished work stays explicit
                </tspan>
              </text>
            </g>
            <g className="research-map__sources" transform="translate(985,290)">
              <text className="research-map__kicker">EVIDENCE FITS THE QUESTION</text>
              <text y="30" className="research-map__heading">
                Internal knowledge
              </text>
              <text y="52" className="research-map__copy">
                Reasoning + search direction
              </text>
              <text y="92" className="research-map__heading">
                Published research
              </text>
              <text y="114" className="research-map__copy">
                Journals, reviews, conferences
              </text>
              <text y="154" className="research-map__heading">
                Other primary sources
              </text>
              <text y="176" className="research-map__copy">
                Official docs, data, standards
              </text>
              <text y="218" className="research-map__copy">
                <tspan x="0">Framing sets the search mix.</tspan>
                <tspan x="0" dy="22">
                  Claims guide each source choice.
                </tspan>
              </text>
            </g>
          </g>
          {stages.map((stage, index) => {
            const p = positions[stage.id];
            return (
              <g
                key={stage.id}
                transform={`translate(${p.x},${p.y})`}
                role="button"
                tabIndex={0}
                aria-label={stage.title}
                aria-pressed={selected === stage.id}
                aria-controls={detailId}
                className={`research-map__node${selected === stage.id ? " research-map__node--selected" : ""}`}
                onClick={() => onSelect(stage.id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onSelect(stage.id);
                  }
                }}
              >
                <rect width={p.w} height={p.h} rx="12" />
                <text x="18" y="26" className="research-map__kicker">
                  {String(index + 1).padStart(2, "0")} /{" "}
                  {stage.id === "choose" || stage.id === "portfolio"
                    ? "ORCHESTRATOR"
                    : "RESEARCH"}
                </text>
                <text x="18" y="51" className="research-map__heading">
                  {p.title}
                </text>
                {p.lines.map((line, i) => (
                  <text key={line} x="18" y={75 + i * 20} className="research-map__copy">
                    {line}
                  </text>
                ))}
                {stage.id === "explore" ? (
                  <g aria-hidden="true">
                    <path
                      className="research-map__wire"
                      d="M230 56 V61 H85 V67 M230 61 V67 M230 61 H372 V67 M85 107 V115 H372 V107 M230 107 V119"
                    />
                    {["Approach A", "Approach B", "Approach C / D"].map((label, i) => (
                      <g key={label} transform={`translate(${18 + i * 143},67)`}>
                        <rect
                          className="research-map__branch"
                          width="135"
                          height="40"
                          rx="7"
                        />
                        <text
                          x="67"
                          y="25"
                          textAnchor="middle"
                          className="research-map__copy"
                        >
                          {label}
                        </text>
                      </g>
                    ))}
                    <text x="18" y="132" className="research-map__copy">
                      Separate methods and histories. Deliberate sharing in development.
                    </text>
                  </g>
                ) : null}
              </g>
            );
          })}
        </svg>
      </div>
      <figcaption className="research-map__legend">
        <span>
          <i className="research-map__key" /> Ideas and integration
        </span>
        <span>
          <i className="research-map__key research-map__key--evidence" /> Evidence checks
        </span>
        <span>
          <i className="research-map__key research-map__key--feedback" /> Feedback and
          retained work
        </span>
        <span>
          <i className="research-map__key research-map__key--answer" /> Final report
        </span>
      </figcaption>
    </figure>
  );
}
