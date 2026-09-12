# Co-Scientist Principles

A reference specification of the AI co-scientist architecture and workflow, written to
support building a standalone, domain-agnostic application. Summarizes the published
system (Google DeepMind / Google Research) and translates its principles into concrete
engineering components.

---

## 1. Provenance and sources

The system was published in two stages. They are the **same project**, not two systems.

| Stage | Artifact | Link |
|---|---|---|
| Preprint (Feb 2025, Gemini 2.0, "Trusted Tester") | *Towards an AI co-scientist* (arXiv:2502.18864) | https://arxiv.org/abs/2502.18864 |
| Blog for the preprint | Google Research blog | https://research.google/blog/accelerating-scientific-breakthroughs-with-an-ai-co-scientist/ |
| Peer-reviewed (May 2026, public tool) | *Accelerating scientific discovery with Co-Scientist*, **Nature** (s41586-026-10644-y) | https://www.nature.com/articles/s41586-026-10644-y |
| Blog for the Nature paper | Google DeepMind blog | https://deepmind.google/blog/co-scientist-a-multi-agent-ai-partner-to-accelerate-research/ |

Companion validation papers:
- cf-PICI gene-transfer mechanism (He et al., bioRxiv): https://www.biorxiv.org/content/10.1101/2025.02.11.637232v1
- *AI mirrors experimental science…* (Penadés et al.): https://storage.googleapis.com/coscientist_paper/penades2025ai.pdf

Underpinning concepts:
- Elo rating system (Elo & Sloan, 1978) — relative ranking from pairwise outcomes.
- Test-time compute scaling (Snell et al., arXiv:2408.03314).
- GPQA benchmark, used to validate the Elo metric (arXiv:2311.12022).
- Self-play / tournament lineage: AlphaGo (Silver et al., *Nature* 2016), AlphaStar.

---

## 2. Design philosophy

Five principles define the system. Preserve these when rebuilding; everything else is
implementation detail.

1. **Generate → debate → evolve.** Mirror the scientific method as an explicit loop:
   propose hypotheses, subject them to adversarial review, refine the survivors. Quality
   comes from iteration, not from a single large generation.

2. **Test-time compute scaling.** Quality improves by spending more inference compute
   (more generation, review, and tournament rounds) — not by training or fine-tuning. The
   system is model-agnostic and improves "for free" as the base model improves. No
   gradient updates, no RL in the core loop.

3. **Verification-dominant.** The majority of compute goes to *checking* hypotheses
   against literature and logic, not to producing them. Ungrounded but confident output is
   the primary failure mode the architecture is built to suppress.

4. **Novelty grounded in prior evidence.** The goal is genuinely new, testable hypotheses
   that go beyond literature summarization — but every claim is anchored to evidence and
   cited. Not brute-force idea volume; principled, reasoned proposals.

5. **Scientist-in-the-loop, not automation.** The system augments a human expert. The
   scientist sets the goal in natural language, can inject their own hypotheses, give
   feedback, and steer. Final judgment is always human. It does not run experiments or
   make decisions autonomously.

---

## 3. System architecture overview

Four top-level components.

**3.1 Natural-language interface.** The scientist states a research goal in plain
language, optionally with constraints, preferences, and attributes (e.g. "must be novel",
"feasible with X technique"). They can refine the goal mid-run, review/rate outputs,
contribute their own hypotheses, and chat to guide exploration.

**3.2 Asynchronous task framework.** Specialized agents run as worker processes in a
continuous, configurable task queue. A **Supervisor agent** parses the goal into a
*research plan configuration*, assigns agents to workers, allocates resources, and
periodically computes summary statistics on system state and progress. This is the layer
that makes test-time compute *scalable* — work is parallelized and dynamically weighted.

**3.3 Specialized agents.** The scientific-reasoning process is decomposed into sub-tasks,
each handled by an agent with a purpose-built prompt. (Section 4.)

**3.4 Context memory.** Persistent store of agent and system state across the long-running
computation. Enables iterative reasoning over long horizons, feedback propagation between
rounds, and safe restart after failure.

---

## 4. The specialized agents

Seven roles. The Supervisor orchestrates; six workers do the reasoning.

| Agent | Role | Core functions |
|---|---|---|
| **Supervisor** | Orchestrator / planner | Parse goal → research-plan config; queue + weight agents; track statistics; decide resource allocation and terminal state |
| **Generation** | Propose hypotheses | Literature exploration via web search; simulated scientific debate (self-play); iterative assumption identification; research expansion into unexplored space |
| **Reflection** | Peer review | Initial review (fast filter); full review (with literature search); deep verification (decompose into sub-assumptions, test each); observation review (does it explain known data?); simulation review; tournament/recurrent review |
| **Ranking** | Tournament | Elo-based pairwise comparison via simulated multi-turn scientific debate; prioritizes matches between similar and top-ranked hypotheses |
| **Proximity** | Similarity graph | Compute proximity graph over hypotheses; cluster, de-duplicate, organize the idea space for diverse exploration and efficient tournament pairing |
| **Evolution** | Refinement | Enhance via grounding; improve coherence/feasibility; combine top hypotheses; simplify; out-of-box divergence. **Creates new hypotheses — never edits originals in place** |
| **Meta-review** | Synthesis | Summarize recurring critique patterns across all reviews/debates → feedback to other agents; formulate the final research overview; identify candidate expert contacts |

Design notes:
- Each agent is one LLM with a customized instruction prompt. The intelligence is in the
  prompts + orchestration, not in any specialized model.
- Evolution produces *additional* candidates that must re-compete in the tournament. A bad
  mutation simply loses — top hypotheses are protected from destructive edits.
- Reflection is multi-modal review, not a single pass. "Deep verification" — breaking a
  hypothesis into independently-checkable sub-assumptions — is the key correctness
  mechanism.

---

## 5. Control and data flow (one round)

```
research goal (NL)
      │
      ▼
[Supervisor] parse → research-plan config ──────────────► context memory
      │
      ▼
[Generation] literature + debate ──► candidate hypotheses
      │
      ▼
[Reflection] review each ──► pass / reject (+ critiques)
      │
      ▼
[Proximity] cluster ──► de-duplicate, organize
      │
      ▼
[Ranking] pairwise debates ──► Elo updates (tournament state)
      │
      ▼
[Evolution] refine top-ranked ──► new candidates ──► (re-enter tournament)
      │
      ▼
[Meta-review] synthesize critiques ──► feedback ──► next round
      │                                  └────────► Generation + Reflection prompts
      ▼
research overview (to scientist)
```

The loop repeats; the Supervisor decides each round how to weight agents (e.g. generate
new ideas vs. evolve existing ones) based on statistics in context memory. Feedback from
Meta-review is appended to downstream agents' prompts on the next iteration.

---

## 6. The tournament and Elo

Ranking is the engine of self-improvement.

- Every hypothesis enters with a baseline rating (the paper uses **Elo 1200**).
- Matches are **pairwise comparisons** judged by a simulated scientific debate. Top-ranked
  hypotheses get **multi-turn** debates (more rigorous); lower-ranked get single-turn.
- Match prioritization: compare **similar** hypotheses (via the Proximity graph) and favor
  **newer / top-ranked** ones — i.e. spend compute where the ordering is most uncertain or
  most consequential.
- Winners gain rating, losers lose it (standard Elo update). Ratings propagate the relative
  quality signal across the whole pool without any ground-truth labels.

**Why Elo:** it converts noisy pairwise judgments into a stable global ranking, lets new
hypotheses be inserted continuously, and gives the Supervisor a scalar to allocate compute
against. Validated (see §9) by showing higher Elo correlates with higher correctness on
GPQA. **Caveat:** Elo here is *self-evaluated*, not ground truth — it can favor traits that
do not align with real-world quality.

---

## 7. Self-improvement without training

The system gets better *within a run* with no weight updates:

- The **Meta-review** agent reads all reviews and tournament debates, extracts recurring
  failure patterns and winning traits, and writes a feedback summary.
- That feedback is **appended to the prompts** of Generation, Reflection, and Evolution in
  the next round (enabled by long-context models).
- Result: a feedback loop that resembles learning but is purely in-context — "learning
  without back-propagation." Rising Elo over rounds is the observable signature.

This is the cheap, portable alternative to fine-tuning: the same base model produces better
output simply by being given structured feedback about its own prior output.

---

## 8. Tool use and grounding

- **Primary:** web search + retrieval, for up-to-date, cited grounding. Used heavily by
  Generation, Reflection, Evolution.
- **Domain databases:** for constrained search spaces (e.g. all FDA drugs, all receptors of
  a type) the system queries open databases. The biomedical version used ChEMBL, UniProt,
  and DepMap.
- **Specialized AI models as tools:** e.g. AlphaFold integrated to validate protein-design
  hypotheses structurally. Demonstrates the pattern — a general reasoner calling a narrow
  expert model and incorporating its feedback.
- **Private corpora:** the system can index and search a scientist-supplied publication set.

Grounding quality is bounded by tool reach. Open-web-only ⇒ broad but shallow; domain DBs
and specialist models ⇒ deep but narrow. A general app should treat tools as pluggable.

---

## 9. Evaluation methodology

How the authors validated the system — replicate these to know if a rebuild works.

| Method | What it measures | Headline result |
|---|---|---|
| Elo–GPQA concordance | Does the self-rated Elo track real correctness? | Higher Elo ⇒ higher accuracy; top-1 = 78.4% on GPQA diamond |
| Test-time compute scaling | Does quality rise with compute? | Elo rises monotonically over rounds across 203 goals; no saturation |
| Baseline comparison | vs. frontier models + human "best guess" | Outperformed o1, o3-mini-high, DeepSeek R1, Gemini 2.0, and unaided experts on Elo (15 expert-curated goals) |
| Expert preference | Subjective novelty / impact / preference | Preferred over baselines on 11 goals (novelty 3.64, impact 3.09 / 5) |
| Safety / adversarial | Rejection of unsafe goals | Passed all of 1,200 adversarial goals across 40 topics |
| End-to-end wet-lab | Do predictions hold in reality? | AML drug repurposing (KIRA6, IC50 13 nM in KG-1); liver-fibrosis epigenetic targets; cf-PICI mechanism re-discovered in 2 days vs ~10 yrs |

Two metric tiers matter for a rebuild: **internal** (self-Elo, cheap, every run) and
**external** (expert review, benchmark accuracy, real validation — the only ground truth).

---

## 10. Known limitations

Carry these forward; they are structural, not bugs.

- **Self-evaluated metric.** Elo is an internal proxy and may not match expert preference
  or truth.
- **Open-access bias.** Misses paywalled literature and, critically, **negative results**
  (rarely published) that experts use to prune.
- **Inherited LLM flaws.** Hallucination and factual error propagate from the base model
  and from web sources.
- **Weak multimodal reasoning.** Data in figures/charts and large structured datasets is
  under-utilized.
- **Incremental bias.** Tends toward incremental ideas; truly transformative leaps are
  harder. Risk of homogenization / narrowed inquiry from correlated LLM failure modes.
- **Scope.** Identifies targets/mechanisms; does not address delivery, pharmacokinetics,
  trial design, etc. (domain-specific gaps generalize to any field).

---

## 11. Reference architecture for a standalone app

Translation of the above into engineering components. The central rule:

> **Separate deterministic bookkeeping from probabilistic reasoning.** LLMs do judgment;
> ordinary code owns state, math, scheduling, and budget.

**11.1 Component split**

| Layer | Responsibility | Built with |
|---|---|---|
| Orchestrator (Supervisor) | Run the loop; schedule agents; enforce budget; decide weighting + termination | Deterministic code (async runtime / state machine) |
| Agents | Generation, Reflection, Ranking, Evolution, Proximity, Meta-review | LLM calls + prompt templates + tool bindings |
| State engine | IDs, Elo, match history, budget, clustering, persistence | Deterministic code (pure functions over a store) |
| Tool layer | Web search, fetch, domain DBs, specialist models | Pluggable adapters (incl. MCP) |
| Provider layer | Model calls | Abstraction over Anthropic / OpenAI / Google / local (e.g. LiteLLM) |
| Persistence | Hypotheses, reviews, state, overview | Files or DB; must be restart-safe |
| Interface | Goal input, steering, standings, final overview | CLI / web UI / chat |

**11.2 Canonical state model**

```jsonc
Run {
  id, goal, constraints,
  config: { initial_elo, k_factor, budget, matches_per_round, evolve_top_k },
  calls_used, iteration, feedback,        // feedback = current meta-review output
  hypotheses: {                            // id -> record
    h001: {
      id, title, body, parent,             // parent set if produced by Evolution
      elo, matches, wins,
      status: active|rejected|archived,    // rejected by Reflection; archived as duplicate
      review: { verdict, note },           // from Reflection
      cluster,                             // from Proximity
      created_iter
    }
  },
  matches: [ { a, b, winner, iter, note } ]
}
```

**11.3 Agent I/O contracts.** Each agent is a pure function `input → structured output`.
Define and validate a strict output schema per agent (the Ranking agent *must* emit a
parseable winner; the Proximity agent *must* emit a cluster map). Re-invoke once on schema
violation. This is what lets the deterministic orchestrator drive probabilistic workers
reliably.

**11.4 The orchestration loop** (one iteration):
1. `round++`; read state + feedback.
2. **Generation** → N candidates (pass goal, feedback, existing titles to avoid dupes).
3. **Reflection** on each new candidate (parallel) → pass/reject + critique.
4. **Proximity** over active set → cluster map → archive duplicates (keep highest Elo).
5. **Ranking**: select pairs (bias: close Elo + under-played), run debates (parallel),
   update Elo per match.
6. **Evolution** on top-K → new candidates → re-enter at step 3 next round.
7. **Meta-review** (feedback mode) → write feedback for next round.
8. Check budget; loop or finish. On finish, **Meta-review** (overview mode) → deliverable.

**11.5 Concurrency.** Steps 3 and 5 are embarrassingly parallel (independent reviews /
independent matches). Generation can also be sharded. The async task framework from the
paper = a worker pool the orchestrator fills.

**11.6 Budget / cost model.** A subagent-heavy run uses many× a single thread's tokens
(each agent keeps its own context). Make the **total LLM-call count** the hard ceiling and
decrement on every call. Expose `budget`, `matches_per_round`, `evolve_top_k` as the
primary cost knobs. Bias spend toward verification (Reflection + Ranking) per principle 3.

**11.7 Extension points** (where domains plug in):
- **Tools:** swap web-only for domain DBs / specialist models per field (MCP adapters).
- **Reflection depth:** add domain-specific verification (e.g. unit checks, code execution,
  simulation) beyond literature review.
- **Proximity:** upgrade LLM-clustering → embedding + cosine clustering for precision.
- **Evaluation:** add an external scorer or human-rating UI alongside the internal Elo.
- **Output format:** constrain Meta-review output to a target schema (the paper used NIH
  Specific Aims format; pick whatever the domain expects).

**11.8 Concept → component map**

| Paper concept | Standalone-app component |
|---|---|
| Supervisor agent | Orchestrator loop / scheduler |
| Specialized agents | Prompt templates + provider calls + tool bindings |
| Context memory | Persistent run store (files / DB), restart-safe |
| Elo tournament | Deterministic Elo module + match history |
| Self-improving loop | Meta-review feedback appended to next-round prompts |
| Test-time compute scaling | Round count + budget ceiling |
| Tool use | Pluggable tool/adapter layer (web, DB, models, MCP) |
| Scientist-in-the-loop | Steering interface between rounds |
| Research overview | Final synthesis renderer |

---

## 12. Minimal viable build

Smallest version that demonstrates the principle: **Generation → Reflection → Ranking
(Elo) → Evolution**, one loop, web search only, hard budget cap, file-based state. Prove it
produces decent ranked hypotheses for an arbitrary goal, then add Proximity, Meta-review
feedback, domain tools, and a UI. Do not build provider abstraction and prompt-tuning at
the same time — get the loop right on one model first, then generalize the provider layer.

---

*This document summarizes published work for engineering reference. Verify specifics
against the primary sources in §1 before relying on them. The system is an augmentation
tool: its rankings are self-evaluated proxies, and all outputs require human validation.*
