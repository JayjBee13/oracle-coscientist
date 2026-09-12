#!/usr/bin/env python3
"""
co-scientist state engine — deterministic bookkeeping for the multi-agent loop.

The LLM agents generate / critique / rank ideas. This script owns everything
that LLMs are bad at: stable IDs, Elo math, budget counting, and picking which
hypotheses to compare next. All state lives in runs/<run_id>/state.json so the
orchestrator (the main Claude Code session) can crash/restart safely.

Stdlib only. Usage:  python coscientist.py <command> [args]
"""
import argparse, json, math, os, random, re, sys, time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(ROOT, "runs")


# ---------- state io ----------
def run_dir(run_id):  return os.path.join(RUNS, run_id)
def state_path(run_id): return os.path.join(run_dir(run_id), "state.json")
def hyp_dir(run_id):  return os.path.join(run_dir(run_id), "hypotheses")

def load(run_id):
    with open(state_path(run_id)) as f:
        return json.load(f)

def save(s):
    s["updated"] = datetime.now(timezone.utc).isoformat()
    with open(state_path(s["run_id"]), "w") as f:
        json.dump(s, f, indent=2)

def out(obj):  # machine-readable line the orchestrator parses
    print(json.dumps(obj))


# ---------- elo ----------
def expected(a, b):
    return 1.0 / (1.0 + 10 ** ((b - a) / 400.0))

def update_elo(ra, rb, winner, k):
    ea = expected(ra, rb)
    sa = 1.0 if winner == "a" else 0.0
    ra2 = ra + k * (sa - ea)
    rb2 = rb + k * ((1 - sa) - (1 - ea))
    return round(ra2, 1), round(rb2, 1)


# ---------- commands ----------
def cmd_init(a):
    run_id = a.run_id or datetime.now().strftime("run-%Y%m%d-%H%M%S")
    os.makedirs(hyp_dir(run_id), exist_ok=True)
    s = {
        "run_id": run_id,
        "goal": a.goal,
        "config": {
            "initial_elo": 1200,
            "k_factor": 32,
            "max_llm_calls": a.budget,      # hard ceiling across the whole run
            "matches_per_round": a.matches,  # ranking calls per tournament round
            "evolve_top_k": a.top_k,         # how many top ideas to evolve each round
            "graft": {                       # v2.0 Cartographer Graft (reactive divergence injector)
                "enabled": True,            # set false => byte-identical to v1
                "quorum_k": 1,              # 1 = single-signal (shipped/minimal); 2 = hardened, needs calibration (spec section 6)
                "window": 3,                # rounds of cluster history for the plateau/birth-rate signals
                "thresholds": {"hhi": 0.50, "birth_rate": 0, "plateau_slack": 0},
                "cooldown": 2,              # min rounds between fires (anti-thrash)
            },
        },
        "calls_used": 0,
        "iteration": 0,
        "next_id": 1,
        "hypotheses": {},   # id -> record
        "matches": [],      # history
        "feedback": "",     # meta-review feedback, injected into next round
        "collapse": {"history": [], "last_fired_iter": None, "fired_count": 0},  # v2.0 graft telemetry
        "pending_injection": "",  # v2.0: divergence seed for the NEXT generate ("" => idle)
        "created": datetime.now(timezone.utc).isoformat(),
    }
    save(s)
    out({"run_id": run_id, "goal": a.goal, "hyp_dir": hyp_dir(run_id)})


def _title_from(text):
    m = re.search(r"^#\s+(.+)$", text, re.M)
    if m: return m.group(1).strip()
    return text.strip().splitlines()[0][:80] if text.strip() else "untitled"


def cmd_add(a):
    """Register a new hypothesis. Reads body from --file or stdin. Returns its id."""
    s = load(a.run_id)
    body = open(a.file).read() if a.file else sys.stdin.read()
    hid = f"h{s['next_id']:03d}"
    s["next_id"] += 1
    path = os.path.join(hyp_dir(a.run_id), f"{hid}.md")
    with open(path, "w") as f:
        f.write(body)
    s["hypotheses"][hid] = {
        "id": hid,
        "title": _title_from(body),
        "file": os.path.relpath(path, ROOT),
        "elo": s["config"]["initial_elo"],
        "matches": 0, "wins": 0,
        "status": "active",      # active | rejected | archived
        "review": None,          # filled by reflection
        "cluster": None,         # filled by proximity
        "created_iter": s["iteration"],
        "parent": a.parent,      # set when produced by evolution
    }
    save(s)
    out({"id": hid, "title": s["hypotheses"][hid]["title"], "file": path})


def cmd_review(a):
    """Attach a reflection verdict. verdict in {pass, reject}."""
    s = load(a.run_id)
    h = s["hypotheses"][a.id]
    h["review"] = {"verdict": a.verdict, "note": a.note}
    if a.verdict == "reject":
        h["status"] = "rejected"
    save(s)
    out({"id": a.id, "status": h["status"]})


def cmd_match(a):
    """Record a pairwise tournament result. winner in {a, b}."""
    s = load(a.run_id)
    ha, hb = s["hypotheses"][a.a], s["hypotheses"][a.b]
    k = s["config"]["k_factor"]
    ha["elo"], hb["elo"] = update_elo(ha["elo"], hb["elo"], a.winner, k)
    for h in (ha, hb): h["matches"] += 1
    (ha if a.winner == "a" else hb)["wins"] += 1
    s["matches"].append({"a": a.a, "b": a.b, "winner": a.winner,
                         "iter": s["iteration"], "note": a.note})
    save(s)
    out({"a": {"id": a.a, "elo": ha["elo"]}, "b": {"id": a.b, "elo": hb["elo"]}})


def _active(s):
    return [h for h in s["hypotheses"].values() if h["status"] == "active"]


def cmd_pairs(a):
    """Suggest tournament pairs. Bias toward (1) similar Elo and (2) under-played
    hypotheses, so ranking compute is spent where the order is still uncertain."""
    s = load(a.run_id)
    act = sorted(_active(s), key=lambda h: h["elo"], reverse=True)
    n = a.n or s["config"]["matches_per_round"]
    pairs, seen = [], set()
    # neighbour pairs in the standings (closest Elo => most informative match)
    for i in range(len(act) - 1):
        key = tuple(sorted((act[i]["id"], act[i + 1]["id"])))
        if key not in seen:
            seen.add(key); pairs.append(list(key))
    random.shuffle(pairs)
    # prioritise pairs involving the least-played hypotheses
    pairs.sort(key=lambda p: min(s["hypotheses"][p[0]]["matches"],
                                 s["hypotheses"][p[1]]["matches"]))
    out({"pairs": pairs[:n]})


def cmd_top(a):
    s = load(a.run_id)
    act = sorted(_active(s), key=lambda h: h["elo"], reverse=True)
    out({"top": [{"id": h["id"], "title": h["title"], "elo": h["elo"],
                  "file": h["file"]} for h in act[:a.k]]})


def cmd_cluster(a):
    """Apply proximity clusters. --map is JSON: {hid: cluster_label}. Keeps the
    highest-Elo member of each cluster active; archives the rest as duplicates."""
    s = load(a.run_id)
    mapping = json.loads(a.map)
    by_cluster = {}
    for hid, label in mapping.items():
        if hid in s["hypotheses"]:
            s["hypotheses"][hid]["cluster"] = label
            by_cluster.setdefault(label, []).append(hid)
    archived = []
    for label, ids in by_cluster.items():
        ids = [i for i in ids if s["hypotheses"][i]["status"] == "active"]
        if len(ids) <= 1: continue
        ids.sort(key=lambda i: s["hypotheses"][i]["elo"], reverse=True)
        for dup in ids[1:]:
            s["hypotheses"][dup]["status"] = "archived"
            archived.append(dup)
    save(s)
    out({"archived": archived})


def _hhi(active):
    """Herfindahl concentration over active cluster sizes (1.0 => all in one cluster,
    ~1/k => evenly spread over k clusters). The v2 graft's structural-diversity signal."""
    from collections import Counter
    n = len(active)
    if n == 0:
        return 0.0
    sizes = Counter(h["cluster"] for h in active if h.get("cluster") is not None)
    return round(sum((c / n) ** 2 for c in sizes.values()), 3) if sizes else 0.0


def cmd_collapse_check(a):
    """v2.0 Cartographer Graft trigger. Deterministic: no LLM, no tick. Call once per
    round right after `cluster`. Appends telemetry and reports whether the divergence
    organ should fire, using 2-of-3 quorum over signals computed from existing state:
      S1 plateau       - distinct active-cluster count not growing over `window`
      S2 concentration - Herfindahl over cluster sizes >= threshold
      S3 birth-rate    - no new cluster labels born vs `window` rounds ago
    With graft disabled it only records telemetry and reports fire=False."""
    s = load(a.run_id)
    g = s["config"].get("graft", {})
    act = _active(s)
    clusters = sorted({h["cluster"] for h in act if h.get("cluster") is not None})
    rec = {"iter": s["iteration"], "n_active": len(act),
           "n_clusters": len(clusters), "hhi": _hhi(act), "clusters": clusters}
    hist = s.setdefault("collapse", {"history": [], "last_fired_iter": None, "fired_count": 0})
    hist["history"].append(rec)
    if not g.get("enabled"):
        save(s); out({"fire": False, "enabled": False, **rec}); return
    th, W = g["thresholds"], g.get("window", 3)
    window = hist["history"][-(W + 1):]
    have_window = len(window) > W
    counts = [r["n_clusters"] for r in window]
    s1 = have_window and (max(counts) - min(counts)) <= th["plateau_slack"]
    s2 = rec["hhi"] >= th["hhi"]
    old = set(window[0]["clusters"]) if have_window else set()
    born = len(set(clusters) - old)
    s3 = have_window and born <= th["birth_rate"]
    votes = int(s1) + int(s2) + int(s3)
    lf = hist["last_fired_iter"]
    cooled = lf is None or (s["iteration"] - lf) > g.get("cooldown", 0)
    fire = votes >= g.get("quorum_k", 1) and cooled
    if fire:
        hist["last_fired_iter"] = s["iteration"]; hist["fired_count"] += 1
    save(s)
    out({"fire": fire, "votes": votes,
         "signals": {"plateau": s1, "concentration": s2, "birth_rate": s3},
         "born": born, "fired_count": hist["fired_count"], **rec})


def cmd_inject(a):
    """v2.0: set (or --clear) the divergence seed consumed by the NEXT generate call.
    Mirrors cmd_feedback. Empty => idle => generation prompt identical to v1."""
    s = load(a.run_id)
    s["pending_injection"] = "" if a.clear else (a.text or "")
    save(s)
    out({"pending_injection_len": len(s["pending_injection"])})


def cmd_feedback(a):
    s = load(a.run_id)
    s["feedback"] = open(a.file).read() if a.file else (a.text or "")
    save(s)
    out({"feedback_len": len(s["feedback"])})


def cmd_tick(a):
    """Spend budget. Call once per LLM agent invocation. Returns whether the run
    should stop (budget exhausted)."""
    s = load(a.run_id)
    s["calls_used"] += a.n
    stop = s["calls_used"] >= s["config"]["max_llm_calls"]
    save(s)
    out({"calls_used": s["calls_used"],
         "budget": s["config"]["max_llm_calls"], "stop": stop})


def cmd_round(a):
    s = load(a.run_id); s["iteration"] += 1; save(s)
    out({"iteration": s["iteration"]})


def cmd_status(a):
    s = load(a.run_id)
    act = _active(s)
    top = sorted(act, key=lambda h: h["elo"], reverse=True)[:5]
    out({
        "run_id": s["run_id"], "goal": s["goal"], "iteration": s["iteration"],
        "calls_used": s["calls_used"], "budget": s["config"]["max_llm_calls"],
        "counts": {
            "active": len(act),
            "rejected": sum(h["status"] == "rejected" for h in s["hypotheses"].values()),
            "archived": sum(h["status"] == "archived" for h in s["hypotheses"].values()),
            "total": len(s["hypotheses"]),
        },
        "top5": [{"id": h["id"], "elo": h["elo"], "title": h["title"]} for h in top],
        "feedback_present": bool(s["feedback"]),
        "graft": {  # v2.0 Cartographer Graft telemetry
            "enabled": s["config"].get("graft", {}).get("enabled", False),
            "fired_count": s.get("collapse", {}).get("fired_count", 0),
            "pending_injection": bool(s.get("pending_injection")),
        },
    })


def main():
    p = argparse.ArgumentParser(description="co-scientist state engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_run(sp): sp.add_argument("--run-id", dest="run_id", required=True)

    sp = sub.add_parser("init"); sp.add_argument("--goal", required=True)
    sp.add_argument("--run-id", dest="run_id", default=None)
    sp.add_argument("--budget", type=int, default=150)
    sp.add_argument("--matches", type=int, default=6)
    sp.add_argument("--top-k", dest="top_k", type=int, default=3)
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("add"); add_run(sp)
    sp.add_argument("--file", default=None); sp.add_argument("--parent", default=None)
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("review"); add_run(sp)
    sp.add_argument("--id", required=True)
    sp.add_argument("--verdict", choices=["pass", "reject"], required=True)
    sp.add_argument("--note", default=""); sp.set_defaults(func=cmd_review)

    sp = sub.add_parser("match"); add_run(sp)
    sp.add_argument("--a", required=True); sp.add_argument("--b", required=True)
    sp.add_argument("--winner", choices=["a", "b"], required=True)
    sp.add_argument("--note", default=""); sp.set_defaults(func=cmd_match)

    sp = sub.add_parser("pairs"); add_run(sp)
    sp.add_argument("--n", type=int, default=None); sp.set_defaults(func=cmd_pairs)

    sp = sub.add_parser("top"); add_run(sp)
    sp.add_argument("--k", type=int, default=3); sp.set_defaults(func=cmd_top)

    sp = sub.add_parser("cluster"); add_run(sp)
    sp.add_argument("--map", required=True); sp.set_defaults(func=cmd_cluster)

    sp = sub.add_parser("collapse-check"); add_run(sp)   # v2.0 Cartographer Graft trigger (deterministic, no tick)
    sp.set_defaults(func=cmd_collapse_check)

    sp = sub.add_parser("inject"); add_run(sp)            # v2.0 set/clear the divergence seed for next generate
    sp.add_argument("--text", default=None); sp.add_argument("--clear", action="store_true")
    sp.set_defaults(func=cmd_inject)

    sp = sub.add_parser("feedback"); add_run(sp)
    sp.add_argument("--file", default=None); sp.add_argument("--text", default=None)
    sp.set_defaults(func=cmd_feedback)

    sp = sub.add_parser("tick"); add_run(sp)
    sp.add_argument("--n", type=int, default=1); sp.set_defaults(func=cmd_tick)

    sp = sub.add_parser("round"); add_run(sp); sp.set_defaults(func=cmd_round)
    sp = sub.add_parser("status"); add_run(sp); sp.set_defaults(func=cmd_status)

    a = p.parse_args(); a.func(a)


if __name__ == "__main__":
    main()
