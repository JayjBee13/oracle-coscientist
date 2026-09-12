import type { RunDetail } from "../../api/types";
import { AdaptiveWorkflowDiagram } from "../../components/AdaptiveWorkflowDiagram";
import { Markdown } from "../../components/Markdown";
import { SkeletonBlock } from "../../components/States";
import { useCapabilities } from "../../store/capabilities";
import "../../styles/research.css";

export function ResearchTab({ detail }: { detail: RunDetail | null }) {
  const capabilities = useCapabilities();
  if (!detail) return <SkeletonBlock height={240} />;
  const research = detail.research;
  if (!research)
    return (
      <p>
        This run uses the earlier tournament workflow. Its original record is preserved.
      </p>
    );
  const rows = Object.fromEntries(
    detail.model_table.map((row) => [row.role, { model: row.model, effort: row.effort }]),
  );
  return (
    <div className="stack">
      <section className="card">
        <h2>Research workspace</h2>
        <p>
          {research.recommendation_ready
            ? "The proposal passed the recorded source and integration checks. External validation may still be needed."
            : "Research remains provisional. Open assumptions and incomplete checks are preserved below."}
        </p>
        <p className="muted">
          Current stage: {research.phase.replaceAll("_", " ")}. Elo does not select the
          answer in this workflow.
        </p>
        <AdaptiveWorkflowDiagram
          models={capabilities.data?.models ?? null}
          overrides={rows}
          demo={detail.run.harness === "demo"}
          variant="compact"
        />
      </section>
      <section className="card">
        <h2>Where Oracle looks for evidence</h2>
        {research.source_strategy ? (
          <>
            <div className="research-source-mix">
              <div>
                <strong>{research.source_strategy.published_research_percent}%</strong>{" "}
                Published scholarly research
              </div>
              <div>
                <strong>
                  {100 - research.source_strategy.published_research_percent}%
                </strong>{" "}
                Other authoritative web sources
              </div>
            </div>
            <p>{research.source_strategy.rationale}</p>
            <p className="muted">
              Planned search emphasis, not measured source counts. Agents adapt to each
              claim. Internal knowledge guides reasoning and searches; it is not part of
              these percentages.
            </p>
            <details>
              <summary>Suggested search directions</summary>
              <h3>Scholarly literature</h3>
              <ul>
                {research.source_strategy.scholarly_queries.map((query) => (
                  <li key={query}>{query}</li>
                ))}
              </ul>
              <h3>Other primary sources</h3>
              <ul>
                {research.source_strategy.other_source_queries.map((query) => (
                  <li key={query}>{query}</li>
                ))}
              </ul>
            </details>
          </>
        ) : (
          <p>
            A topic-specific search strategy has not been recorded for this run. Agents
            consider published research and other primary sources according to the claim.
          </p>
        )}
      </section>
      <section className="card">
        <h2>Independent approaches</h2>
        {research.approaches.length === 0 ? (
          <p>Problem framing is pending.</p>
        ) : (
          <div className="research-flow__stages">
            {research.approaches.map((approach) => (
              <article className="research-card" key={approach.id}>
                <h3>{approach.method}</h3>
                <p>{approach.framing}</p>
                <p className="muted">Assumptions to test</p>
                <ul>
                  {approach.assumptions.map((text, i) => (
                    <li key={i}>{text}</li>
                  ))}
                </ul>
              </article>
            ))}
          </div>
        )}
      </section>
      <section className="card">
        <h2>Subproblems and integration</h2>
        {research.subproblems.length === 0 ? (
          <p>
            No separable subproblems were recorded. A holistic approach may be
            appropriate.
          </p>
        ) : (
          <div className="research-table-wrap">
            <table className="research-table">
              <thead>
                <tr>
                  <th>Subproblem</th>
                  <th>Depends on</th>
                  <th>Acceptance test</th>
                  <th>Integration</th>
                </tr>
              </thead>
              <tbody>
                {research.subproblems.map((problem) => (
                  <tr key={problem.id}>
                    <td>
                      {problem.id}: {problem.question}
                    </td>
                    <td>{problem.depends_on.join(", ") || "Independent"}</td>
                    <td>{problem.acceptance_test}</td>
                    <td>
                      {research.dependencies.find((d) => d.subproblem_id === problem.id)
                        ?.status ?? "Pending"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section className="card stack">
        <h2>Portfolio and evidence</h2>
        <p className="muted">
          Source relationships are model-assessed. Arithmetic checks only the stated
          expression, not its premises. No physical experiments or general code execution
          occur here.
        </p>
        {research.portfolio.map((candidate) => (
          <details className="research-card" key={candidate.hid}>
            <summary>
              {candidate.hid} · {candidate.title} —{" "}
              {candidate.readiness.replaceAll("_", " ")}
            </summary>
            <p>Approach: {candidate.approach}</p>
            <dl>
              {Object.entries(candidate.dimensions).map(([name, dimension]) => (
                <div key={name}>
                  <dt>
                    <strong>
                      {name}: {dimension.grade}
                    </strong>
                  </dt>
                  <dd>{dimension.rationale}</dd>
                </div>
              ))}
            </dl>
            {candidate.claims.map((claim) => (
              <article key={claim.id}>
                <p>
                  <strong>
                    {claim.status}: {claim.claim}
                  </strong>
                </p>
                <p>{claim.rationale}</p>
                <ul>
                  {claim.sources.map((source, i) => (
                    <li key={i}>
                      <a href={source.url} target="_blank" rel="noreferrer">
                        {source.finding}
                      </a>{" "}
                      ({source.relation})
                    </li>
                  ))}
                </ul>
              </article>
            ))}
            {candidate.calculation_results.map((check, i) => (
              <p key={i}>
                Arithmetic: <code>{check.expression}</code> →{" "}
                {check.result ?? check.error ?? "No result"} (
                {check.passed ? "passed" : "failed"}). Premise: {check.assumption}
              </p>
            ))}
            <p>
              <strong>Next test:</strong> {candidate.next_test}
            </p>
          </details>
        ))}
      </section>
      {research.contradictions.length > 0 ? (
        <section className="card">
          <h2>Contradicted candidates</h2>
          <p>
            These candidates remain in the record but are excluded from the recommendation
            portfolio.
          </p>
          {research.contradictions.map((candidate) => (
            <details className="research-card" key={candidate.hid}>
              <summary>
                {candidate.hid} · {candidate.title}
              </summary>
              {candidate.claims.map((claim) => (
                <div key={claim.id}>
                  <p>
                    <strong>
                      {claim.status}: {claim.claim}
                    </strong>
                  </p>
                  <p>{claim.rationale}</p>
                  <ul>
                    {claim.sources.map((source, index) => (
                      <li key={index}>
                        <a href={source.url} target="_blank" rel="noreferrer">
                          {source.finding}
                        </a>{" "}
                        ({source.relation})
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </details>
          ))}
        </section>
      ) : null}
      <section className="card">
        <h2>Complete answer and independent challenge</h2>
        {research.synthesis_markdown ? (
          <Markdown source={research.synthesis_markdown} />
        ) : (
          <p>Complete solution integration is pending.</p>
        )}
        <h3>Challenge</h3>
        <p>
          {research.challenge_assessment ||
            "A successful independent challenge has not been recorded."}
        </p>
        <ul>
          {research.blocking_issues.map((text, i) => (
            <li key={i}>{text}</li>
          ))}
        </ul>
        <h3>Open integration questions</h3>
        <ul>
          {research.unresolved.map((text, i) => (
            <li key={i}>{text}</li>
          ))}
        </ul>
      </section>
      <section className="card">
        <h2>Why the process changed direction</h2>
        <ol>
          {research.decisions.map((decision) => (
            <li key={decision.checkpoint}>
              Checkpoint {decision.checkpoint}: <strong>{decision.action}</strong>.{" "}
              {decision.reason}
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}
