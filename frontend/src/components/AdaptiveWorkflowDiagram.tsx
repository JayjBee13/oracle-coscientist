import { useId, useState } from "react";
import { ResearchFlowMap } from "./ResearchFlowMap";
import type { ModelsPayload } from "../api/types";
import { applyOverrides, modelLabel, resolveTier, type RoleChoice } from "../lib/models";
import { describeEffort } from "../lib/status";
import "../styles/research.css";

const stages = [
  {
    id: "framing",
    title: "Frame the problem",
    roles: ["framing"],
    summary: "Question assumptions. Establish several ways to understand the problem.",
    detail:
      "Oracle preserves your objective, creates three or four distinct approaches, and maps separable subproblems with acceptance tests. Framing also sets a topic-specific search emphasis between published scholarly research and other authoritative sources. Internal knowledge guides reasoning and search vocabulary. A holistic route stays available. A decisive challenge can reopen this framing.",
  },
  {
    id: "choose",
    title: "Choose the next research work",
    roles: [],
    summary: "The current evidence and unresolved weaknesses determine the next action.",
    detail:
      "At each checkpoint, Oracle chooses fresh exploration, targeted development, evidence review or reframing. Fresh exploration returns after targeted work, protecting breadth. The checkpoint target and your call budget bound the process.",
  },
  {
    id: "explore",
    title: "Explore independently or develop",
    roles: ["generation", "evolution"],
    summary:
      "Independent approaches search creatively. Development deliberately shares useful results.",
    detail:
      "Exploration calls see their own framing and previous work, not other approaches' winning narratives. Methods rotate through first principles, assumption inversion, structural analogy and recombination. Development brings a diverse portfolio together to solve bottlenecks and construct compatible combinations.",
  },
  {
    id: "verify",
    title: "Critique and check evidence",
    roles: ["reflection", "verification"],
    summary:
      "Improve unfinished ideas, investigate decisive claims and check bounded arithmetic.",
    detail:
      "Formative criticism does not automatically retire an idea. Independent verification records supporting and contradicting sources, open questions and the next discriminating test. Oracle can execute limited arithmetic; real experiments and simulations remain pending. Source relationships are model-assessed.",
  },
  {
    id: "portfolio",
    title: "Preserve a diverse portfolio",
    roles: [],
    summary: "Keep useful alternatives and meaningful variants while evidence develops.",
    detail:
      "Selection considers approach coverage, originality, usefulness, feasibility, upside and evidence status. A single Elo score cannot choose the final answer. Contradicted candidates remain in the record but are excluded from the recommendation portfolio.",
  },
  {
    id: "synthesis",
    title: "Build a complete solution",
    roles: ["synthesis"],
    summary: "Integrate compatible strengths and address the whole objective.",
    detail:
      "The synthesis explains how the parts fit, which dependencies they address, which alternatives compete and which gaps remain. It can propose a strong complete answer while explicitly stating what is still unproven.",
  },
  {
    id: "challenge",
    title: "Challenge the whole answer",
    roles: ["challenge"],
    summary:
      "An independent call looks for decisive weaknesses and stronger alternatives.",
    detail:
      "The challenge checks the original goal, evidence and interfaces between components. Blocking findings feed the next checkpoint: reframe, develop or verify. A favorable assessment is not empirical proof. The final report preserves the challenged answer and its unresolved issues.",
  },
];

export function AdaptiveWorkflowDiagram({
  models,
  provider,
  tier,
  overrides,
  demo = false,
  variant = "full",
}: {
  models: ModelsPayload | null;
  provider?: string | null;
  tier?: string | null;
  overrides?: Readonly<Record<string, RoleChoice>> | null;
  demo?: boolean;
  variant?: "full" | "compact";
}) {
  const [selected, setSelected] = useState("framing");
  const detailId = useId();
  const resolution = resolveTier(models, provider ?? null, tier);
  const rows = applyOverrides(resolution.rows, overrides);
  const current = stages.find((stage) => stage.id === selected) ?? stages[0];
  return (
    <section className="research-flow" aria-label="Adaptive research workflow">
      <div className="research-flow__heading">
        <h2>The research loop</h2>
        <p className="muted">
          Follow the arrows. Select a stage to see how it improves the answer.
        </p>
      </div>
      <ResearchFlowMap
        stages={stages}
        selected={selected}
        onSelect={setSelected}
        detailId={detailId}
      />
      <div id={detailId} className="research-flow__detail" aria-live="polite">
        <h3>{current.title}</h3>
        <p>{current.detail}</p>
        {demo ? (
          <p className="muted">
            Demo: simulated responses; no models or external evidence checks.
          </p>
        ) : current.roles.length === 0 ? (
          <p className="muted">Deterministic orchestration — no model call.</p>
        ) : (
          <ul>
            {current.roles.map((role) => {
              const row = rows.find((item) => item.role === role);
              return (
                <li key={role}>
                  {role}:{" "}
                  {row
                    ? `${modelLabel(row.model, models?.catalog ?? [])} · ${describeEffort(row.effort)}`
                    : "Model settings loading"}
                </li>
              );
            })}
          </ul>
        )}
      </div>
      {variant === "full" ? (
        <p className="muted">
          The final report includes the integrated answer, competing ideas, evidence
          limits and next tests. If a run ends early, missing work remains explicit. You
          can steer, pause or finish at a boundary.
        </p>
      ) : null}
    </section>
  );
}
