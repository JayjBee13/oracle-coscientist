/**
 * Builders for the DTOs the run workspace reads. Kept beside `fixtures.ts`
 * rather than inside it so two workers can add cases without fighting over one
 * file.
 */

import type {
  HypothesisDetail,
  HypothesisRow,
  MatchRow,
  RunEvent,
  ReviewRow,
} from "../api/types";

export function makeHypothesisRow(overrides: Partial<HypothesisRow> = {}): HypothesisRow {
  return {
    id: "hyp-uuid-1",
    hid: "h001",
    title: "Plasmonic nitrogen fixation",
    status: "active",
    elo: 1246,
    matches: 4,
    wins: 3,
    cluster: "Photocatalysis",
    duplicate_of: null,
    parent_ids: [],
    operator: null,
    created_round: 1,
    source: "agent",
    novelty_level: "high",
    ...overrides,
  };
}

export function makeReview(overrides: Partial<ReviewRow> = {}): ReviewRow {
  return {
    verdict: "pass",
    novelty_level: "high",
    novelty_note: "No published route uses plasmonic hot electrons for this step.",
    correctness: "The thermodynamics hold at ambient pressure.",
    testability: "A bench photoreactor could show ammonia within weeks.",
    key_risk: "Hot-electron lifetimes may be too short to matter.",
    note: "Worth pursuing despite the lifetime question.",
    model: "claude-sonnet-5",
    ...overrides,
  };
}

export function makeMatchRow(overrides: Partial<MatchRow> = {}): MatchRow {
  return {
    id: "match-1",
    round: 2,
    a: { hid: "h001", title: "Plasmonic nitrogen fixation" },
    b: { hid: "h005", title: "Electrochemical nitride cycling" },
    status: "completed",
    winner: 1,
    elo_a_before: 1200,
    elo_a_after: 1216,
    elo_b_before: 1200,
    elo_b_after: 1184,
    judge_model: "claude-sonnet-5",
    debate_md: "Both are plausible; the first is testable sooner.",
    ts: "2026-08-01T10:20:00Z",
    ...overrides,
  };
}

export function makeHypothesisDetail(
  overrides: Partial<HypothesisDetail> = {},
): HypothesisDetail {
  return {
    ...makeHypothesisRow(),
    body_md:
      "## Claim\n\nAmmonia forms at ambient pressure on plasmonic gold.\n\n- Hot electrons cross the barrier\n- The support stabilises the intermediate\n",
    reviews: [makeReview()],
    match_history: [makeMatchRow()],
    lineage: { parents: [], children: [] },
    ...overrides,
  };
}

export function makeEvent(overrides: Partial<RunEvent> & { type: string }): RunEvent {
  return {
    seq: 1,
    run_id: "run-1",
    round: 2,
    payload: {},
    ts: "2026-08-01T10:30:00Z",
    ...overrides,
  };
}

/** An ISO timestamp `secondsAgo` before now — for the trust line's clock. */
export function agoIso(secondsAgo: number): string {
  return new Date(Date.now() - secondsAgo * 1000).toISOString();
}
