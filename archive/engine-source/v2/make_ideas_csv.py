#!/usr/bin/env python3
"""Export all hypotheses from a co-scientist run to a ranked CSV spreadsheet.
Usage: python -X utf8 make_ideas_csv.py --run-id <id> [--out <path>]
Columns: Rank, ID, Idea, Category, Status, Verdict, Elo, Matches, Wins, Round,
         Derived-from, What it is (Claim), Why-now capability, Review notes
Active (tournament-ranked) ideas get a numeric Rank by Elo; rejected/archived
ideas are listed below with no rank (they never entered / left the tournament).
"""
import argparse, csv, json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))

ap = argparse.ArgumentParser()
ap.add_argument("--run-id", required=True)
ap.add_argument("--out", default=None)
a = ap.parse_args()

state_path = os.path.join(HERE, "runs", a.run_id, "state.json")
with open(state_path, encoding="utf-8") as f:
    s = json.load(f)
out_path = a.out or os.path.join(HERE, "runs", a.run_id, "ideas_ranked.csv")


def field(body, label):
    """Extract the text of a **Label:** field up to the next **bold** marker."""
    m = re.search(r"\*\*" + re.escape(label) + r"\*\*\s*(.*?)(?:\n\*\*|\Z)", body, re.S)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()


def split_title(title):
    """'Name — [Category]' -> ('Name', 'Category')."""
    m = re.match(r"^(.*?)\s*[—\-]+\s*\[(.*?)\]\s*$", title)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return title.strip(), ""


rows = []
for hid, h in s["hypotheses"].items():
    path = os.path.join(HERE, h["file"])
    body = ""
    try:
        with open(path, encoding="utf-8") as f:
            body = f.read()
    except Exception:
        pass
    name, cat = split_title(h.get("title", hid))
    review = h.get("review") or {}
    rows.append({
        "id": hid,
        "name": name,
        "category": cat,
        "status": h.get("status", ""),
        "verdict": (review.get("verdict") or ""),
        "elo": h.get("elo", ""),
        "matches": h.get("matches", 0),
        "wins": h.get("wins", 0),
        "round": h.get("created_iter", ""),
        "parent": h.get("parent") or "",
        "claim": field(body, "Claim:"),
        "whynow": field(body, "Why-now capability:") or field(body, "Why-now:"),
        "mechanism": field(body, "Mechanism / rationale:"),
        "novelty": field(body, "Novelty:"),
        "test": field(body, "Test:"),
        "assumptions": field(body, "Assumptions:"),
        "derived": field(body, "Derived-from:"),
        "note": (review.get("note") or ""),
    })

# Order: active (ranked) by Elo desc first, then archived, then rejected; ties by id.
order = {"active": 0, "archived": 1, "rejected": 2}
rows.sort(key=lambda r: (order.get(r["status"], 3), -float(r["elo"] or 0), r["id"]))

hdr = ["Rank", "ID", "Idea", "Category", "Status", "Verdict", "Elo", "Matches",
       "Wins", "Round generated", "Derived from", "What it is (claim)",
       "Why-now capability", "Mechanism / rationale", "Novelty",
       "Test (how to validate)", "Assumptions / risks",
       "Review notes (competitors / wedge / key risk)"]

rank = 0
with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(hdr)
    for r in rows:
        if r["status"] == "active":
            rank += 1
            rank_cell = rank
        else:
            rank_cell = ""  # rejected/archived never finished the tournament
        derived = r["derived"] or r["parent"]
        w.writerow([rank_cell, r["id"], r["name"], r["category"], r["status"],
                    r["verdict"], r["elo"], r["matches"], r["wins"], r["round"],
                    derived, r["claim"], r["whynow"], r["mechanism"], r["novelty"],
                    r["test"], r["assumptions"], r["note"]])

print(f"Wrote {len(rows)} ideas to {out_path}")
print(f"  active(ranked)={sum(1 for r in rows if r['status']=='active')} "
      f"rejected={sum(1 for r in rows if r['status']=='rejected')} "
      f"archived={sum(1 for r in rows if r['status']=='archived')}")
