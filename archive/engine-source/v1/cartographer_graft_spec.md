# Implementation & Experiment Spec — The Cartographer Graft

**Status:** design spec (not yet implemented). **Source:** winner of run `run-20260531-002116`
("new co-scientist structure") — h009 Cartographer Graft + h016 hardened multi-signal trigger.
**One-line:** add a single *reactive, zero-when-idle* organ to the existing loop that, **only when the
idea pool is detected to be collapsing**, injects one distant-domain analogy seed into the next
Generation call. When it does not fire, the system is byte-identical to today's pipeline.

This spec deliberately follows the run's two hard-won laws:
1. **Minimal-first.** Ship the smallest version (single-signal trigger) first; add the multi-signal
   quorum only after it is calibrated and shown to beat the single signal. Do **not** ship a bundle.
2. **Winning template.** Reactive (fires on a detected condition) · byte-identical degradation when
   idle · Supervisor stays single state owner (restart-safe) · no unproven core primitive.

---

## 1. What changes (3 files) and what does NOT

| File | Change | Size |
|---|---|---|
| `coscientist.py` | Add a **deterministic** `collapse-check` command + helpers + a `collapse` state block. The trigger is pure math over existing state — no LLM, no new dependency, stdlib-only. | ~60 lines |
| `.claude/agents/cartographer.md` | New stateless subagent: given the current cluster summary + goal, returns ONE distant-domain relational skeleton + a seed framing. | new file |
| `.claude/skills/coscientist/SKILL.md` | Insert **Step 3.5** (collapse-check → conditional Cartographer) after Proximity; Step 1 (Generate) reads & clears a `pending_injection` field. | ~1 paragraph |

**Unchanged (the backbone):** `init/add/review/match/pairs/top/cluster/feedback/tick/round/status`,
the Elo math, the budget/tick engine, IDs, persistence, parallelism. The graft is **off by default**
(`config.graft.enabled = false`); with it off, every existing command returns byte-identical output.

---

## 2. State-schema delta (`state.json`)

Two additions, both owned by the Supervisor (single-writer, restart-safe):

```jsonc
"config": {
  ...,
  "graft": {                      // all knobs in one place; absent => disabled
    "enabled": false,
    "quorum_k": 1,                // 1 = single-signal (h009 / minimal-first); 2 = hardened (h016)
    "window": 3,                  // rounds of history for plateau/birth-rate signals
    "thresholds": {               // calibrated on logged runs (see §6); these are conservative defaults
      "hhi": 0.50,                // S2: cluster-concentration (Herfindahl) >= hhi  => collapse vote
      "birth_rate": 0.0,          // S3: new clusters born in window <= birth_rate  => collapse vote
      "plateau_slack": 0          // S1: (max-min) distinct-cluster-count over window <= slack => vote
    },
    "cooldown": 2                 // min rounds between fires (prevents thrashing)
  }
},
"collapse": {                     // rolling telemetry + last decision; pure bookkeeping
  "history": [],                  // [{iter, n_active, n_clusters, hhi, born}] one per cluster-step
  "last_fired_iter": null,
  "fired_count": 0
},
"pending_injection": ""           // skeleton text for the NEXT Generate; "" => no injection (idle)
```

`pending_injection` is the *only* thing that changes Generation behavior, and it is `""` whenever the
organ has not fired — that is the byte-identical-when-idle guarantee in one field.

---

## 3. The trigger (deterministic, stdlib-only)

The organ detects **idea-space collapse** using three signals computable from data the engine already
has after `cmd_cluster` runs (cluster labels live on each hypothesis; history is appended each round).
No embeddings required — collapse is read off the **proximity agent's cluster structure**, which the
pipeline already produces.

Let `A` = active hypotheses, `C` = multiset of their `cluster` labels, `N = |A|`.

- **S1 — cluster-count plateau (diversity not growing).**
  `n_clusters = |distinct(C)|`. Over the last `window` recorded steps, vote collapse if
  `max(n_clusters) - min(n_clusters) <= plateau_slack`.
- **S2 — cluster concentration (mass in few clusters).**
  Herfindahl `HHI = Σ_c (size_c / N)²`. Vote collapse if `HHI >= thresholds.hhi`.
  (HHI = 1 when all active ideas share one cluster; ≈ 1/n_clusters when evenly spread.)
- **S3 — new-cluster birth rate ≈ 0 (newcomers stop opening new regions).**
  `born` = number of cluster labels appearing this step that were absent `window` steps ago.
  Vote collapse if `born_over_window <= thresholds.birth_rate`.

**Decision:** fire iff `(#votes >= quorum_k)` **and** `(iter - last_fired_iter > cooldown)`.
`quorum_k=1` → minimal h009 (any one signal; in practice S2 is the strongest single signal — see §6).
`quorum_k=2` → hardened h016 (≥2-of-3; robust to each signal's blind spot, resists false fires).

These three signals fail in **different** regimes (count level / mass distribution / birth dynamics),
which is exactly why 2-of-3 is more robust than tuning any one — and why we must *prove* that on
logged data (§6) rather than assert it.

> **Optional 4th signal (extension, needs an embedding tool):** mean pairwise cosine distance of
> active hypotheses; drop ⇒ collapse. Add only if calibration shows S1–S3 miss multi-modal collapse
> (population diverse by count but clustered in 2–3 tight basins). Keep stdlib core shippable without it.

### New command (proposed `coscientist.py`)

```python
def _hhi(active):
    from collections import Counter
    n = len(active)
    if n == 0: return 0.0
    sizes = Counter(h["cluster"] for h in active if h.get("cluster") is not None)
    return round(sum((c / n) ** 2 for c in sizes.values()), 3) if sizes else 0.0

def cmd_collapse_check(a):
    """Deterministic collapse detector. Call once per round, right after `cluster`.
    Appends telemetry; returns whether the Cartographer organ should fire. No LLM, no tick."""
    s = load(a.run_id); g = s["config"].get("graft", {})
    act = _active(s)
    clusters = {h["cluster"] for h in act if h.get("cluster") is not None}
    rec = {"iter": s["iteration"], "n_active": len(act),
           "n_clusters": len(clusters), "hhi": _hhi(act), "clusters": sorted(clusters)}
    hist = s.setdefault("collapse", {"history": [], "last_fired_iter": None, "fired_count": 0})
    hist["history"].append(rec)
    if not g.get("enabled"):
        save(s); out({"fire": False, "enabled": False, **rec}); return

    th, W = g["thresholds"], g["window"]
    window = hist["history"][-(W + 1):]
    counts = [r["n_clusters"] for r in window]
    # S1 plateau, S2 concentration, S3 birth-rate (new labels vs W steps ago)
    s1 = (max(counts) - min(counts)) <= th["plateau_slack"] if len(window) > W else False
    s2 = rec["hhi"] >= th["hhi"]
    old = set(window[0]["clusters"]) if len(window) > W else set()
    born = len(set(rec["clusters"]) - old)
    s3 = born <= th["birth_rate"] if len(window) > W else False
    votes = sum([s1, s2, s3])
    lf = hist["last_fired_iter"]
    cooled = lf is None or (s["iteration"] - lf) > g["cooldown"]
    fire = votes >= g["quorum_k"] and cooled
    if fire:
        hist["last_fired_iter"] = s["iteration"]; hist["fired_count"] += 1
    save(s)
    out({"fire": fire, "votes": votes, "signals": {"s1": s1, "s2": s2, "s3": s3},
         "born": born, **rec})
```

Wire-up in `main()`: `sp = sub.add_parser("collapse-check"); add_run(sp); sp.set_defaults(func=cmd_collapse_check)`.
A companion `cmd_inject` (`--text` / `--clear`) sets/clears `pending_injection` (mirrors `cmd_feedback`).

---

## 4. The Cartographer subagent (`.claude/agents/cartographer.md`)

Stateless, like every other agent. **Tools:** `WebSearch, WebFetch` (to ground a distant domain).

**Input (Supervisor puts everything in the prompt):** GOAL; the current top-cluster summaries
(`id | title | claim` for the champion of each active cluster) so the agent can see *where the search
currently sits*; the count of active clusters.

**Job:** pick ONE source domain that is **far from the current cluster centroids** (not merely far from
the goal — this is h016's fix to h005's distance-overshoot: target the collapse, not the abstract
maximum), extract its relational skeleton, and emit a seed the next Generation call can instantiate.

**Output contract (strict):**
```
SOURCE_DOMAIN: <the distant domain chosen, + 1 line on why it is far from the current clusters>
SKELETON: <the transferable relational structure, 2–4 bullet relations>
SEED_FRAMING: <one sentence telling Generation how to map the skeleton onto the goal>
```
The Supervisor stores `SKELETON + SEED_FRAMING` as `pending_injection`.

---

## 5. Orchestration change (`SKILL.md`, one inserted step)

Today's loop is Generate → Reflect → **Proximity/cluster** → Rank → Evolve → Meta-review. Insert:

> **Step 3.5 — Collapse check (conditional Cartographer).** Immediately after applying the cluster map,
> run `python coscientist.py collapse-check --run-id <id>`. If `"fire": true`, invoke the `cartographer`
> subagent once with GOAL + the per-cluster champion list; store its output via
> `coscientist.py inject --run-id <id> --text "<SKELETON+SEED_FRAMING>"`; then `tick --n 1`.
> If `"fire": false`, do nothing (no call, no tick).

And amend **Step 1 — Generate**: "If `pending_injection` is non-empty, append it to the Generation
prompt as an extra `DIVERGENCE SEED:` field, then clear it (`inject --clear`). A bad analogy simply
loses in the tournament and costs one call — the same safety property bad mutations already have."

Budget accounting: `collapse-check` is deterministic → **0 ticks**. The Cartographer is **1 tick, only
when it fires**. On a healthy run it never fires ⇒ **zero added cost**, and `pending_injection` stays
`""` ⇒ identical generation prompts ⇒ identical run.

---

## 6. Calibration (do this before trusting `quorum_k=2`)

The thresholds and the quorum claim must be **fit on logged runs and validated on a held-out split** —
not asserted (this was the reviewers' main caveat on h016).

1. **Corpus:** replay the JSON logs of completed runs (we already have ≥3: the startup-ideas run and
   the two architecture runs). `collapse-check` appends telemetry per round, so re-running it over saved
   states reconstructs the `history` series offline at zero LLM cost.
2. **Labels:** for each round, a human (or a meta-review pass) marks `collapse = {yes,no}` — "did the
   pool visibly converge onto one region here?".
3. **Fit:** split goals train/test. On train, choose `thresholds` and compare single-signal (each of
   S1/S2/S3 alone) vs 2-of-3 by ROC. **Gate to adopt `quorum_k=2`:** on the held-out test split it must
   **strictly dominate the best single signal on recall at equal precision.** If it does not, ship
   `quorum_k=1` with the best single signal and stop (minimal-first).

---

## 7. Experiment — does the graft actually help? (matched-compute A/B)

**Arms** (identical base config, identical total LLM-call budget `B`):
- **A — Incumbent:** `graft.enabled=false`.
- **B — Single-signal graft (h009):** `enabled=true, quorum_k=1`, best single signal from §6.
- **C — Hardened graft (h016):** `enabled=true, quorum_k=2`.
- **Ablations:** **C₁** quorum_k=1-as-noisy-OR and **C₃** quorum_k=3 (AND) — isolate that *2-of-3
  specifically*, not just "more/less firing," carries any gain. **B′** best-tuned-single-signal control —
  so C's edge isn't just "we added signals."

**Goal sets (pre-registered):**
- **Collapse-prone set** (~20 goals): narrow/over-specified goals, or goals empirically shown to
  converge in pilot runs. This is where the organ *should* fire and help.
- **Healthy set** (~20 goals): open/crisp goals that don't collapse. This is the **no-harm** control —
  the organ should ~never fire here.

**Metrics:**
- *Offline (from §6):* trigger precision/recall + ROC of single vs 2-of-3 on hand-labeled collapse ticks.
- *Online — quality:* **blind external rating** of the final top-k for novelty AND correctness
  (use an external/held-out judge, NOT the run's own Elo — avoids the self-evaluation critique).
- *Online — diversity:* final distinct active-cluster count; mean pairwise distance of top-k (blind).
- *Online — "free when idle" proof:* number of Cartographer fires per run. **Prediction:** ≈0 on the
  healthy set, >0 on the collapse-prone set.
- *Attribution:* fraction of fires whose injected hypothesis (or its descendants) survives into the
  final top-k — confirms the *mechanism*, not just a correlation.

**Success criteria (pre-registered):**
1. **No harm:** on the healthy set, C is statistically indistinguishable from A on blind quality, and
   fire-count ≈ 0 (the byte-identical-when-idle promise, empirically).
2. **Helps on collapse:** on the collapse-prone set, C > A on blind top-k novelty *without* a
   correctness drop, at matched `B`, and the gain is attributable (criterion above).
3. **Hardening earns its place:** C ≥ B on the collapse-prone set AND C's offline recall > B′'s at equal
   precision. If criterion 3 fails, **ship B, not C** (minimal-first).

**Stats:** paired by goal (each goal run under all arms with fixed seeds where possible); report effect
sizes + CIs; pre-register n for ~80% power on the target novelty effect; hold out the calibration goals
from the online test set to avoid leakage.

---

## 8. Risks & mitigations (carried from the tournament reviews)

| Risk | Mitigation |
|---|---|
| Collapse proxy **under-fires** (misses multi-modal collapse) | The whole reason for `quorum_k=2`; plus the optional embedding-distance 4th signal if §6 shows a gap. |
| **False fire** disrupts a healthy pool | `cooldown`; conservative thresholds; the injected idea must still *win* in the tournament or it dies — bounded downside of one call. |
| Threshold **overfit** | Mandatory train/held-out split in §6; report test-split ROC, not train. |
| "It's just a better h009, not a new structure" | True and intended — the run's verdict is that minimal grafts win. h016 is the *production form* of the champion, not a new paradigm. |
| Inherits the premise that **analogy injection rescues diversity** | Criterion-2 attribution test (does the injected idea survive into top-k?) directly checks it; if injection doesn't help once collapse is correctly detected, the organ is rejected regardless of trigger quality. |

---

## 9. Rollout (minimal-first, the run's own discipline)

1. Land `collapse-check` (deterministic, `enabled=false`) + telemetry. Zero behavior change; start
   accumulating `collapse.history` on real runs.
2. Run §6 calibration offline on logged runs. Pick the best single signal.
3. Ship **B** (`quorum_k=1`) behind the flag; run the §7 A/B vs A.
4. Only if §6/§7 criterion 3 passes, promote to **C** (`quorum_k=2`).
5. Do **not** add the Ratchet (h011) or cross-run memory (h013/h018) at the same time — each is its own
   flag, its own A/B, added one at a time (the Organ-Suite roadmap, never the bundle).

---

*Caveat: this design won an Elo tournament judged by LLM panels — a self-evaluated proxy, not ground
truth. The §7 A/B with a blind external judge is what converts "ranked #1" into "actually works."*
