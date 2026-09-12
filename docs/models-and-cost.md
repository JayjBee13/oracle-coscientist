# Models, depth and cost

Oracle drives **CLI harnesses on subscriptions** rather than a metered API, and the engine
is shaped by that decision.

- One **stateless process per role call** — `claude -p …` or `codex exec …`. No session, no
  shell, no filesystem, no MCP servers.
- The answer's shape is enforced by the CLI (`--json-schema`) and re-validated on the way
  back in.
- **There is no API key anywhere in this system.** You sign each CLI in once with your own
  subscription.
- Because nothing is billed per token, **effort buys thinking with latency, not money**.
  A deeper run costs minutes.

That is why effort and model are separate dials here. How hard a step is fixes its **model
class**; the tier chooses how long the run is willing to **think**.

## The five models

| id | Label | Provider / CLI | Class | List price (in / out per Mtok) |
| --- | --- | --- | --- | --- |
| `gpt-5.6-luna` | Luna | OpenAI / `codex` | light | subscription — no per-token list price |
| `claude-opus-5` | Opus 5 | Anthropic / `claude` | light | $5 / $25 |
| `gpt-5.6-sol` | Sol | OpenAI / `codex` | heavy | subscription — no per-token list price |
| `gpt-6-astra` | Astra | OpenAI / `codex` | heavy | $10 / $50 |
| `fable` | Fable 5.1 | Anthropic / `claude` | heavy | $10 / $50 |

The two OpenAI models run on a ChatGPT account with no per-token list price to quote, so
they report `None` for their rates and carry `price_basis="class_parity"`: their planning
bracket is the bracket of the Anthropic model of the same class, and the field says so
rather than inventing a number that would read as measured.

Every model here prices output at exactly 5× input, so the ratio between any two is a single
scalar that holds whatever the token mix turns out to be — which is what makes a per-call
bracket derivable from a run measured on a *different* model rather than guessed. The
brackets are deliberately not a token model: the engine's token mix is dominated by cached
prompt reads whose size depends on the goal, the round and the pool, and a token model would
put a spurious third decimal on a number whose real uncertainty is ±2×.

### Why the allowlist is closed

`--model` accepts identifiers the CLI does not recognise and **silently substitutes
something else**. Probed live against CLI 2.1.220:

| `--model` | What actually ran |
| --- | --- |
| `claude-opus-5` | `claude-opus-5` — correct |
| `fable` | Fable 5 — correct, and the only working identifier for it |
| `claude-fable-5` | **`claude-opus-5`**, with no error and no warning |
| `fable-5` | hard error |

That third row is why `backend/app/engine/models.py` exists. A run configured with
`claude-fable-5` looks right in the config, right in the Settings tab and right in the argv
log, and is judged throughout by a different model than the one you chose. So the allowlist
is closed, `claude-fable-5` is refused **by name** with the trap spelled out in the error,
and every call compares the model the result envelope reports against the model that was
requested. A downgrade *below* the floor is a hard failure; a sideways move degrades loudly
and carries on.

## Classes

`heavy` is the judgement that compounds across a run — generating, reviewing, ranking,
guiding the next round, writing the report. `light` is bounded work the next step re-derives
anyway — clustering, mutating, grafting, shaping a question.

**A role's class never moves.** A tier can change how hard a step thinks; it cannot hand a
heavy step the light model of the provider it is already on. That is the substitution this
module exists to refuse, because it is the one nobody can see.

## Tiers

| Tier | Heavy effort | Light effort | What choosing it does |
| --- | --- | --- | --- |
| **Max** | `xhigh` | `high` | Everything thinks at the top of its ladder. The strongest run, and the slowest. |
| **High** | `high` | `medium` | The default shape: strong where judgement compounds, quick everywhere else. |
| **Med** | `low` | `low` | Every step at low effort, on exactly the models the stronger tiers use. A run that thinks less — never one that thinks with something weaker. |
| **Low** | `high` | `high` | The speed preset. Pins **every** role to Luna at high effort, whatever the run's provider says. Requires the `codex` CLI installed and signed in. |

Three of the four tiers are a pair of efforts and nothing else. `Low` is the exception: it
pins the model as well. That is a decision rather than a substitution, and it is kept
**visible** — the pin is data, each tier publishes it, and the model appears on every row of
the resolved table the wizard shows you before you launch. A Low run says on screen that it
is running Luna.

`proximity` is held at `low` effort in every tier, including Low. Clustering reads a list of
titles and labels them; thinking buys it nothing, and it sits on the critical path of every
round.

## Effort

The vocabulary is `low` → `medium` → `high` → `xhigh` → `max`, weakest first. It is a subset
of both providers': each also accepts levels below `low`, which nothing here has a use for,
and Codex lists `ultra` above `max` for one model — a CLI-side auto-delegation mode that the
API enum does not contain at all, so it is never emitted.

**The ladder belongs to the model, not the provider.** Each model publishes its own in
`GET /api/capabilities`, and a level outside it is clamped *visibly* in the resolved table
rather than sent for the server to reject mid-run. A picker must read the ladder off the
selected model.

## Per-role overrides

Any of the thirteen roles can name its own model, its own effort, or both. An override may
name **either provider's** model regardless of the run's `provider` — mixing providers
inside one run is the point of the feature, not an accident of it. `routing.ProviderRouter`
picks the runner per call from that row's own model, which is exactly why a Low table stored
under `provider="anthropic"` executes entirely on the Codex CLI.

Set them in two places, both reading one contract:

- **System-wide** — Settings → Models. The stored value holds one override bucket per tier,
  so saving redefines the tier that is selected. A `PUT` is validated by resolving *every*
  bucket through the same code a launch uses, so a default that could not become a run is
  rejected here, with the model policy's own message naming the specific trap.
- **Per run** — the wizard's Advanced step, or `model_overrides` on `POST /api/runs`. An
  unknown role name is a 422 rather than a silently ignored key.

The Confirm step prints the resolved table — provider, model, effort and the reason each
role is set the way it is — before anything is spent.

## What a run costs

For reference on the Anthropic side, a real one-round smoke run is roughly $2 of
API-equivalent telemetry and takes seven or eight minutes; a Standard three-round run is
meaningfully more. On a subscription none of that is billed — it is the number the estimator
reports so the shape of a run is legible.

A run is bounded by **two** ceilings:

- `budget_calls` — the governor, always set, enforced in the store **and** re-checked at the
  spawn layer so the cap survives an orchestrator bug.
- `budget_usd` — optional and off by default. Enforcing a dollar figure against subscription
  telemetry produced exactly one outcome in practice: a run stopped one step short of the
  report it had already done the work for. It is still honoured when set, for a metered key.

`wall_clock_minutes` is the guard against a pathological loop now that dollars are not one.
Two overview calls are reserved up front, so a run that hits a ceiling still writes a report.
