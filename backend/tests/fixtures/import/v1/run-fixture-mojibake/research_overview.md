# Research Overview: Highest-potential net-new AI-first app ideas, hard-gated on capabilities feasible only very recently, with credible $1-5M+/yr revenue headroom

## Summary

This run explored the consumer and prosumer/professional app idea space across four tournament rounds (23 hypotheses generated, 17 rejected, 6 surviving). Round 1 cast wide across consumer super-categories and was almost entirely wiped out by two failure modes: the "newly-feasible" AI capability was actually *aspirational* (e.g., on-device sarcasm inference at ~39-43% accuracy), or it had *already shipped over a year ago*, or an incumbent had *already shipped the exact product for free* (Amazon Kindle "Ask this Book," Speak/Praktika, Stratablue/Teambridge). The hard gate — not buildable a year ago, no incumbent already shipping it — is brutal, and naive consumer ideas almost never clear it.

From Round 2 onward the tournament converged hard on a single dominant **winning pattern**: a **bounded, single-pass, long-context cross-document CONSISTENCY/CONTRADICTION AUDIT** over a **first-party uploaded corpus**, sold to a **high-willingness-to-pay professional buyer with an expensive or urgent failure mode**. Critically, the recency lock binds not on context-window *size* (1M-token context shipped Feb 2024 via Gemini 1.5, so size alone is no moat) but on **reasoning-quality-at-length** — multi-hop, multi-needle recall across 200K-1M tokens. Anthropic's Feb-2026 Opus 4.6 hits ~76-78% on 8-needle MRCR-v2 at 1M tokens versus ~18.5% prior-gen, and that step-change is exactly what makes whole-corpus contradiction auditing newly trustworthy. This is *why the professional-document-audit ideas won and the consumer ideas lost*: the audit pattern has a real, datable capability gate, a buyer who already pays to avoid a costly miss, and a bounded single-pass architecture that sidesteps compounding agent error.

Every surviving hypothesis had to clear the same five **kill patterns**, which functioned as the screening bar:

1. **Soft recency** — "needs 1M context" or "reads long video" both shipped Feb 2024; not a moat. (Killed RateReconcile, WalkthroughProof, FeeFighter.)
2. **Incumbent already shipping the exact mechanic** — surfaced only on exhaustive vertical-startup search. (Killed bordereaux, eCTD, muni-OS, claims, and bill-audit ideas.)
3. **Catastrophic-single-miss recall** — multi-needle recall drops to ~60% past 400-500K tokens, making high-stakes one-miss-is-fatal domains (e.g., biotech CRL) unsafe; only *tolerant-recall, human-in-the-loop* domains survive.
4. **Compounding multi-step agent error** — ~99%/step compounds to ~37% at 100 steps; favors bounded single-pass over autonomous multi-step execution. (Degraded HouseholdOps, CloseLoop.)
5. **Low-WTP segments / regulatory walls / third-party scraping** — indie authors can't fund $1-5M; UPL/FDA/fee-for-policy-review walls and anti-bot/ToS portal scraping are disqualifiers.

The six survivors are, in effect, **one core product (whole-corpus single-pass contradiction audit) aimed at two verticals (narrative canon and federal grant compliance), each at two GTM motions/segments.**

## Top Hypotheses

### Canon-audit family (same core product, two segments)

**h013 — CanonGuard (Elo 1248, #1)**
*Claim:* A single-pass cross-TITLE narrative QA tool that ingests a game franchise's entire shipped narrative corpus (all prior scripts, branching dialogue, quest logs, lore bible; 300K-1M+ tokens) plus a new sequel/DLC script, and returns every canon contradiction (timeline, character death, faction, magic-rule) with dual-passage citations — sold as a pre-certification ship-gate. Buyer: game-studio QA/narrative; $8-25K/yr per franchise, 150-300 contracts -> $1.5-4M/yr.
*Why it ranked highly:* Highest novelty (no shipping product does cross-title full-corpus ship-blocking canon audit; incumbents are within-project draft-time/glossary only), cleanest recency lock (binds squarely on multi-hop-at-length), bounded single-pass, and tolerant recall (a human editor adjudicates each flag).
