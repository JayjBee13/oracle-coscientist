#!/usr/bin/env python3
"""Helper: split a generation/evolution agent's raw output into
===HYPOTHESIS===...===END=== blocks and register each via coscientist.cmd_add.

Usage: python _ingest.py --run-id <id> --file <raw_output.txt> [--parent <ids>]
Prints one JSON line: {"count": N, "added": [{"id","title","file"}, ...]}.
Stdlib only; reuses the engine's own add() so state stays consistent.
"""
import argparse, os, re, tempfile, importlib.util, types, io, contextlib, json

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("cs", os.path.join(HERE, "coscientist.py"))
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)

ap = argparse.ArgumentParser()
ap.add_argument("--run-id", required=True)
ap.add_argument("--file", required=True)
ap.add_argument("--parent", default=None)
a = ap.parse_args()

raw = open(a.file, encoding="utf-8").read()
blocks = re.findall(r"===HYPOTHESIS===(.*?)===END===", raw, re.S)
added = []
for b in blocks:
    body = b.strip()
    if not body:
        continue
    tf = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
    tf.write(body)
    tf.close()
    ns = types.SimpleNamespace(run_id=a.run_id, file=tf.name, parent=a.parent)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cs.cmd_add(ns)
    os.unlink(tf.name)
    try:
        added.append(json.loads(buf.getvalue().strip().splitlines()[-1]))
    except Exception:
        added.append({"raw": buf.getvalue()})
print(json.dumps({"count": len(added), "added": added}))
