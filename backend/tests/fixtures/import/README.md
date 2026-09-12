# Import fixtures

Five small run directories in the shape the archived engines wrote, laid out as the three
import subtrees (`v1/`, `v2/`, `gui/`). Each was cut down from a real run in
`archive/imported-runs/`, so the parsing they exercise is the parsing the
real archive needs.

| fixture | what it proves |
|---|---|
| `v1/run-fixture-legacy` | the `TITLE:`/`CLAIM:`/`ACTIVITY:` plaintext era re-composes onto the schema fields, keeping the sections that have no schema home |
| `v1/run-fixture-mojibake` | mojibake repair, against real archive bytes |
| `v2/run-fixture-graft` | a healthy v2 run: collapse history, a fired graft, two-parent lineage, completed matches |
| `v2/run-fixture-rejected` | an all-rejected run still has a top three and an overview |
| `gui/real-v1-fixture-gui` | a GUI-created run: one hypothesis, a useless `### Hypothesis` title, no overview |

The mojibake in `run-fixture-mojibake` is **real archive bytes**, never hand-typed: the
`feedback` string is verbatim from `v1/run-20260602-064935`, and the two hypothesis titles
are verbatim from `v2/run-20260603-215608`. Hand-typing `â€"` would prove the test parses
the test rather than that the importer repairs what the engines actually wrote.

Worth knowing when adding cases: the archived `.md` bodies are all clean UTF-8. Every
mojibake sequence in the archive lives inside a `state.json` string — titles, `goal` and
`feedback` — which is why repair is applied to text on its way into a column, and why no
review note in the fixture carries damage (none in the archive does either).
