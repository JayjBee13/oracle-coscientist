"""Run `c4566ed2`, verbatim: the clearest statement of the workshop's collapse defect.

`QUESTION` is what the owner typed (`runs.question`). `REWRITE` is what the workshop wrote
and what the orchestrator actually launched the run with (`runs.prompt`). Both are copied
out of the production row unedited, box-drawing characters and all.

Two things happened between them, and the six round-one hypotheses are the proof:

* `REWRITE` names six families of answer after "The answer being sought is" — services
  roll-up, take-rate on flow, equity/revenue share, permissioned data, verification
  infrastructure, systematic capital — and the six hypotheses the run produced map onto
  those clauses one-to-one, in order.
* `REWRITE` closes with an out-of-scope list containing "hourly consulting" and "any model
  whose ceiling is the operator's own attention". Neither appears anywhere in `QUESTION`.
  Between them they delete the services business, the consumer product and the content
  play, none of which the owner ruled out.

Nothing in this module is imported by the application. It exists so the tests can assert
against the real artefact rather than a sketch of it.
"""

from __future__ import annotations

__all__ = ["INVENTED_EXCLUSIONS", "QUESTION", "REWRITE"]

QUESTION = (
    "I have $50,000 in deployable capital, 12 months, and strong AI-engineering "
    "capability — I operate Claude Code and multi-agent systems daily and can ship "
    "software fast. I am a solo operator with no team and no outside funding at the "
    "start.\n▎\n▎ Find the strategies with the highest realistic expected value "
    "for turning that into life-changing wealth, explicitly including paths with genuine "
    "asymmetric upside toward $1M+, and identify which ones retain a non-trivial "
    "probability of reaching $10M within 12 months.\n▎\n▎ Assume frontier model "
    "capability and agent reliability improve substantially every ~3 months. Strongly "
    "prefer strategies that compound with that improvement over ones that are a fixed "
    "arbitrage on today's capabilities — and consider strategies that are impossible "
    "today but become possible at the capability level you expect in 3, 6 and 9 months. "
    "Assume I can run many agents in parallel, and that my scarcest resources are "
    "attention and distribution, not capital.\n▎\n▎ For every hypothesis, state: "
    "the mechanism by which money is actually made and who pays; a concrete first-90-day "
    "plan; the specific edge AI agents give over a competitor not using them; why this is "
    "not already competed away; the honest probability of reaching $250k / $1M / $10M in "
    "12 months, with the basis for those numbers stated; the failure modes that kill it; "
    "and the cheapest test in the first two weeks that would falsify it.\n▎\n▎ "
    "Prefer: fast feedback loops, capability-compounding advantages, defensibility against "
    "a frontier lab shipping the same feature natively, and clean legal and tax structure. "
    "Exclude: generic index-fund advice, MLM and affiliate churn, anything requiring "
    "credentials I don't have, anything illegal or fraudulent, and anything whose entire "
    "edge evaporates the moment a model provider ships an obvious feature.\n▎\n▎ "
    "Rank primarily on expected value adjusted for probability of ruin, then on the size "
    "of the achievable tail outcome, then on speed to first revenue."
)

REWRITE = (
    "A solo operator with $50,000, twelve months, no team and no outside funding, but "
    "elite ability to build and run fleets of coding and computer-use agents, wants "
    "structures with a non-trivial probability of $10M in twelve months — not a good "
    "living. Assume frontier capability and agent reliability improve substantially every "
    "~3 months (longer-horizon autonomous task completion, cheaper tokens, reliable "
    "computer-use on legacy systems, maturing agent-to-agent payment rails), and that the "
    "binding constraints are attention and distribution, not capital. Identify the "
    "strategies whose payoff is convex in that improvement, explicitly including ones "
    "impossible today that become possible at the capability level expected at month 3, 6 "
    "and 9.\n\n"
    "The answer being sought is a portfolio of leveraged structures where value accrues "
    "to ownership rather than to billed hours: acquiring or partnering into a fragmented "
    "labor-intensive services business and re-rating its margin with agent-run workflows; "
    "take-rate positions on transaction flow the operator intermediates; equity- or "
    "revenue-share for agent-delivered outcomes instead of fees; assets that appreciate "
    "as capability rises (permissioned proprietary data, evaluation and verification "
    "infrastructure, integration into systems of record, owned audience); and systematic "
    "capital strategies only where a stated, testable edge exists and ruin is hard-capped. "
    "Deliberately transfer reasoning from AI-enabled roll-up economics, marketplace "
    "take-rate design, insurance and liability underwriting for autonomous work, and "
    "distribution economics.\n\n"
    "Each hypothesis must state: the payer and the mechanism of value capture; the "
    "specific capability milestone it bets on, with an observable that would confirm or "
    "deny that milestone by a named month, and what remains if capability stalls; a "
    "first-90-day plan; why a competitor without agent fleets cannot copy it and why a "
    "frontier lab shipping the obvious feature does not kill it; calibrated probabilities "
    "of $250k, $1M and $10M within twelve months with the reference class stated — a 200x "
    "on $50k is rare, so name who has actually done it and how often; the failure modes, "
    "including deal-sourcing adverse selection, counterparty and key-man risk, unbounded "
    "liability from outcome guarantees, and regulatory exposure; and a two-week test "
    "costing under $5,000 that would falsify it. Structure must stay clean: entity and tax "
    "treatment (including QSBS eligibility), securities-law limits on taking outside "
    "money, and contract form for revenue-share.\n\n"
    "Rank on expected value adjusted for probability of ruin, then tail size, then speed "
    "to first revenue. Out of scope: index funds, MLM and affiliate churn, "
    "credential-gated work, anything illegal or fraudulent, hourly consulting, and any "
    "model whose ceiling is the operator's own attention."
)

INVENTED_EXCLUSIONS: tuple[str, ...] = (
    "hourly consulting",
    "any model whose ceiling is the operator's own attention",
)
"""The two out-of-scope clauses in `REWRITE` with no counterpart anywhere in `QUESTION`."""
