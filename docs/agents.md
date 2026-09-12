# The agents

Thirteen roles. Each is **one process, one prompt, one JSON schema**, and either no tools at
all or web search and nothing else. Nothing is a conversation: a role sees exactly the
context the orchestrator hands it and returns exactly the shape `schemas.py` demands.

The table below is the contract. `Class` fixes which model a role runs on (heavy or light —
this never moves, whatever the tier); `Tools` is what the CLI is allowed to reach.

| Agent | Class | Tools | Workflow | What it does |
| --- | --- | --- | --- | --- |
| `workshop` | light | WebSearch | both | Turns a rough question into two framed research goals, before a run exists. |
| `framing` | heavy | none | adaptive | Creates several distinct approaches and a dependency map. |
| `generation` | heavy | WebSearch | both | Proposes hypotheses, in parallel, on deliberately different angles. |
| `reflection` | heavy | WebSearch | both | Formative critique: novelty, correctness, testability, key risk. |
| `proximity` | light | none | both | Clusters near-duplicate ideas. |
| `ranking` | heavy | none | tournament | Judges head-to-head matches and moves Elo. |
| `evolution` | light | WebSearch | both | Derives new ideas from survivors. |
| `meta_review` | heavy | none | both | Writes the guidance the next round is generated against. |
| `cartographer` | light | none | both | Novelty injection: grafts a structure from a distant domain. |
| `verification` | heavy | WebSearch | adaptive | Independently checks decisive claims. |
| `synthesis` | heavy | none | adaptive | Integrates the portfolio into one complete answer. |
| `challenge` | heavy | WebSearch | adaptive | Attacks that answer and reopens weaknesses. |
| `overview` | heavy | none | both | Writes the report. |

---

## workshop

Runs before a run exists. It takes the question as you typed it and returns **two** properly
framed research goals with different strategies. You pick one, merge them, ask for another
pair with a note, or edit the final prompt directly. A mistake here is cheap to catch, which
is why it is a light role.

## framing

Preserves your objective while creating three or four genuinely distinct ways to understand
the problem, and maps the separable subproblems with acceptance tests. It also sets a
topic-specific search emphasis — published scholarly work versus other authoritative sources
— that the grounded roles inherit. A decisive challenge can reopen this framing later.

## generation

Proposes the hypotheses. The batch is split across parallel calls given deliberately
different angles (mechanism-level, population-or-system-level, adversarial reframe) and, in
adaptive runs, one of four rotating creative methods:

- **First principles** — derive the mechanism from constraints; question familiar categories.
- **Assumption inversion** — reverse a pivotal assumption and develop the strongest feasible result.
- **Structural analogy** — transfer a causal structure from a distant field; test where it breaks.
- **Recombination** — integrate complementary mechanisms without hiding incompatible premises.

Exploration calls see their own framing and their own previous work, never another
approach's winning narrative, so they cannot converge by imitation. Round 1 seeds every
later round, so generation escalates its effort there.

## reflection

The only gate on whether an idea reaches the tournament — and a wrong reject is
unrecoverable, because nothing downstream reconsiders it. So it is **formative**: it reviews
novelty, correctness and testability and names the key risk, and it **rejects only for a
fundamental flaw, never for being unfashionable**. In adaptive runs it does not
automatically retire a candidate at all; it tells the next call what to fix.

## proximity

Clusters hypotheses by what they actually claim, so near-duplicates are marked as duplicates
instead of quietly winning twice. Mechanical work — it reads a list of titles and labels
them — so it is pinned to `low` effort in **every** tier, including the speed tier. Thinking
buys clustering nothing, and it sits on the critical path of every round.

## ranking

Runs the tournament. Pairs are argued head-to-head by a judge that writes out its reasoning,
and the winner takes Elo from the loser. Pairings are adjacent-in-standings, seeded, and
never within a cluster. Its verdicts compound into every rating, so it escalates to higher
effort when both sides are top-5 or when the round is the last one.

## evolution

Derives new hypotheses from the top of the leaderboard by grounding, combination,
simplification, or an out-of-the-box reframe. **Parents are never mutated** — lineage is
preserved, and the Ideas tab draws the genealogy. Bounded by what generation and ranking
gave it, so it is a light role.

## meta_review

Reads every review and every debate from the round and writes the guidance that steers the
next one. One call per round, felt in every call of the next. This is the loop's memory: the
system learns *within* a run. Your own steering note, if you leave one, enters here as
top-priority guidance.

## cartographer

Novelty injection. It fires only when the idea space measurably collapses — clusters
concentrating, Elo plateauing, new ideas ceasing to be born — under a quorum-of-signals rule
across a window of rounds, with a cooldown afterwards. When it fires it fetches a structural
skeleton from an *unrelated* domain and seeds the next generation with it.

Off by default (`graft.enabled`). Rare enough that its cost never shows up in a budget.

## verification

Independently checks the claims a conclusion actually turns on. It records supporting **and
contradicting** sources, the assumptions still unresolved, and the next discriminating test.
Only a bounded arithmetic interpreter runs calculations; the role cannot execute programs,
and source relationships remain model-assessed rather than proven.

## synthesis

Integrates a diverse portfolio into one complete answer: how the parts fit, which
dependencies they resolve, which alternatives still compete, and which gaps remain. It may
propose a strong complete answer while stating explicitly what is still unproven.
Contradicted candidates are excluded from the portfolio it works from.

## challenge

An independent call that attacks the finished answer — against the original objective,
against the evidence, and at the interfaces between its components. Blocking findings feed
the next checkpoint as `reframe`, `develop` or `verify`. **A favourable challenge is not
empirical proof**, and the report keeps the challenged answer together with its unresolved
issues rather than replacing it with a clean rewrite.

## overview

Writes the report — the artefact a person actually reads: several thousand words on what was
found, what won, and what the open questions are. Its two calls are reserved up front, so a
run that is stopped or that exhausts its budget still produces one.

---

## Timeouts

Ceilings are per role class, taken from the latencies earlier runs actually recorded, and scaled
by effort and grounding depth:

| Class | Ceiling | Why |
| --- | --- | --- |
| Tool-less roles | 180 s | The maximum ever observed across the corpus is 95 s. |
| Grounded, single-item (reflection) | 420 s | One hypothesis, one critique, search on. |
| Grounded, batch (generation, evolution, workshop) | 1200 s | These search and then compose several complete hypotheses. One run made ten calls of this class at the old 420 s ceiling: two finished, at 388 s and 407 s, and eight were killed by the ceiling itself — so the old number was a measurement of the limit, not of the work. |

A call past 80% of its ceiling is reported as `near_timeout` on `call_finished`, so a run
that is quietly getting slower says so before it starts failing.
