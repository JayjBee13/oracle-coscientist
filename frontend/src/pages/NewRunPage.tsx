import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import * as api from "../api/client";
import { errorMessage, isApiError } from "../api/client";
import { MODEL_TIERS } from "../api/types";
import type {
  ContextDocInput,
  ModelSettings,
  RunConfig,
  Workshop,
  WorkshopOption,
} from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { ErrorState, LoadingPage } from "../components/States";
import { deriveTitle as deriveTitleFromQuestion } from "./wizard/state";
import {
  requestNotificationPermission,
  setNotificationOptIn,
} from "../lib/notifications";
import { pushToast } from "../lib/toast";
import { useIdentity } from "../store/identity";
import { primeRuns } from "../store/runs";
import { onModelSettings } from "../store/settings";
import { ConfigureStep } from "./wizard/ConfigureStep";
import { ConfirmStep } from "./wizard/ConfirmStep";
import type { LaneConflict } from "./wizard/ConfirmStep";
import { IntentStep } from "./wizard/IntentStep";
import { OptionsStep, WorkshopWaiting } from "./wizard/OptionsStep";
import { PromptStep } from "./wizard/PromptStep";
import { StepRail } from "./wizard/parts";
import {
  applyRecommended,
  blankDraft,
  clampStep,
  clearDraft,
  furthestStep,
  harnessFor,
  inheritDefault,
  isClone,
  isWizardStep,
  readDraft,
  writeDraft,
} from "./wizard/state";
import type { WizardDraft, WizardStep } from "./wizard/state";
import "../styles/wizard.css";

/** Workshops have no event stream, so the options step polls for them. */
export const WORKSHOP_POLL_MS = 1500;

type Busy = null | "workshop" | "refine" | "choose" | "launch";

/**
 * The launch wizard: question → directions → prompt → settings → confirm.
 *
 * It is deliberately the longest path in the app, because the thing at the end
 * of it runs for half an hour against your plan's rate limits. Everything here
 * answers something the UX review found missing: the step lives in the URL so a
 * reload does not throw the work away, the draft lives in localStorage for the
 * same reason, the chosen direction is *visibly* chosen, the workshop's
 * recommended settings are applied instead of ignored, and the Confirm step
 * states the shape, the models and the ceilings before anything runs.
 */
export function NewRunPage() {
  const identity = useIdentity();
  if (!identity.data) return <LoadingPage label="Opening your run setup" />;
  return (
    <AuthenticatedNewRunPage
      key={identity.data.username}
      username={identity.data.username}
    />
  );
}

function AuthenticatedNewRunPage({ username }: { username: string }) {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();

  const workshopParam = params.get("workshop");
  const stepParam = params.get("step");
  const runnerParam = params.get("runner");
  const fromParam = params.get("from");

  const [draft, setDraft] = useState<WizardDraft>(() =>
    initialDraft({ username, workshopParam, runnerParam, fromParam }),
  );
  const [workshop, setWorkshop] = useState<Workshop | null>(null);
  const [workshopError, setWorkshopError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [busy, setBusy] = useState<Busy>(null);
  const [laneConflict, setLaneConflict] = useState<LaneConflict | null>(null);

  /* --- Persistence ------------------------------------------------------- */
  useEffect(() => {
    writeDraft(username, draft);
  }, [draft, username]);

  /* --- Inheriting the system default -------------------------------------
     The top bar sets which models a new run uses; this is where "a new run"
     reads it. It is an effect rather than a value in `initialDraft` because the
     stored default arrives over the network and may not be here when the wizard
     mounts — going straight to `/new` on a cold load is the ordinary case.

     `inheritDefault` is what decides whether to apply it, and it declines on a
     draft anybody has touched: a person who has already moved a step and come
     back to the tab is not asking for their work to be replaced by whatever the
     default happens to be now. So this is idempotent and safe to re-run.

     Subscribed rather than read-and-set: taking `settings.saved` out of the
     store and calling `setDraft` with it in an effect body is the cascading
     render React warns about, and `onModelSettings` is the shape it asks for
     instead — an external system, and an update arriving from it.
     ---------------------------------------------------------------------- */
  useEffect(
    () =>
      onModelSettings((stored) => {
        setDraft((previous) => inheritDefault(previous, asStoredDefault(stored)));
      }),
    [],
  );

  const patch = useCallback(
    (changes: Partial<WizardDraft>): void => {
      setDraft((previous) => ({ ...previous, ...changes }));
    },
    [setDraft],
  );

  const patchConfig = useCallback(
    (changes: Partial<RunConfig>): void => {
      setDraft((previous) => ({
        ...previous,
        config: { ...previous.config, ...changes },
      }));
    },
    [setDraft],
  );

  /* --- Where we are ------------------------------------------------------ */
  const requested: WizardStep = isWizardStep(stepParam) ? stepParam : "intent";
  const step = clampStep(requested, draft);
  const furthest = furthestStep(draft);

  const goStep = useCallback(
    (next: WizardStep, workshopId?: string | null): void => {
      setParams(
        (previous) => {
          const search = new URLSearchParams(previous);
          search.set("step", next);
          const id = workshopId ?? draft.workshopId;
          if (id) search.set("workshop", id);
          else search.delete("workshop");
          return search;
        },
        { replace: false },
      );
    },
    [setParams, draft.workshopId],
  );

  /* --- Cloning an earlier run (`/new?from=<id>`) -------------------------- */
  const cloneRequested = fromParam && draft.fromRun !== fromParam;
  useEffect(() => {
    if (!cloneRequested || !fromParam) return;
    const abort = new AbortController();
    api
      .getRunDetail(fromParam, abort.signal)
      .then((detail) => {
        if (abort.signal.aborted) return;
        setDraft((previous) => ({
          ...previous,
          fromRun: fromParam,
          question: detail.run.question,
          title: `${detail.run.title} (again)`,
          prompt: "",
          promptSource: "",
          optionId: null,
          workshopId: null,
          recommended: null,
          config: { ...previous.config, ...normaliseConfig(detail.config) },
          contextDocs: [],
          // Cleared, so the backend inherits the source run's own documents. A stale
          // draft's attachments survived this reset and `launch()` sent them, which both
          // attached one scientist's documents to another scientist's question and
          // suppressed the inheritance the clone was for — `launcher._inherit` prefers a
          // caller-supplied list over the source run's whenever the list is non-empty.
        }));
        goStep("configure", null);
      })
      .catch((error: unknown) => {
        if (abort.signal.aborted) return;
        pushToast({
          tone: "danger",
          title: "Could not read that run",
          message: errorMessage(error),
        });
      });
    return () => abort.abort();
  }, [cloneRequested, fromParam, goStep]);

  /* --- Loading a workshop we only know the id of ------------------------- */
  const workshopId = draft.workshopId;
  const loaded = workshop?.id === workshopId;

  useEffect(() => {
    if (!workshopId || loaded) return;
    const abort = new AbortController();
    api
      .getWorkshop(workshopId, abort.signal)
      .then((next) => {
        if (abort.signal.aborted) return;
        setWorkshop(next);
        setWorkshopError(null);
      })
      .catch((error: unknown) => {
        if (abort.signal.aborted) return;
        setWorkshopError(errorMessage(error));
      });
    return () => abort.abort();
  }, [workshopId, loaded]);

  /* --- Polling while the workshop drafts --------------------------------- */
  const refining = loaded && workshop?.state === "refining";

  // `elapsed` is reset where the wait *starts* — an event handler — rather than
  // here, because a setState in an effect body is a cascading render.
  useEffect(() => {
    if (!workshopId || !refining) return;
    const startedAt = Date.now();
    const ticker = setInterval(
      () => setElapsed(Math.floor((Date.now() - startedAt) / 1000)),
      1000,
    );
    const poller = setInterval(() => {
      void api
        .getWorkshop(workshopId)
        .then(setWorkshop)
        .catch(() => {
          /* a blip mid-poll is not worth a message; the next tick retries */
        });
    }, WORKSHOP_POLL_MS);
    return () => {
      clearInterval(ticker);
      clearInterval(poller);
    };
  }, [workshopId, refining]);

  /* --- Actions ----------------------------------------------------------- */

  const startWorkshop = async (): Promise<void> => {
    setBusy("workshop");
    setElapsed(0);
    try {
      const created = await api.createWorkshop({
        question: draft.question.trim(),
        harness: harnessFor(draft.config),
        context_docs: draft.contextDocs,
      });
      setWorkshop(created);
      setWorkshopError(null);
      patch({
        workshopId: created.id,
        optionId: null,
        prompt: "",
        promptSource: "",
        recommended: null,
        chosen: false,
        fromRun: null,
      });
      goStep("options", created.id);
    } catch (error) {
      pushToast({
        tone: "danger",
        title: "Could not start the prompt workshop",
        message: errorMessage(error),
      });
    } finally {
      setBusy(null);
    }
  };

  const writePromptMyself = (): void => {
    patch({
      workshopId: null,
      optionId: null,
      prompt: draft.prompt || draft.question.trim(),
      promptSource: "",
      chosen: false,
      fromRun: null,
    });
    goStep("prompt", null);
  };

  const refine = async (base: string, note: string): Promise<void> => {
    if (!workshopId) return;
    setBusy("refine");
    setElapsed(0);
    try {
      const next = await api.refineWorkshop(workshopId, { base, note });
      setWorkshop(next);
      patch({ optionId: null });
    } catch (error) {
      pushToast({
        tone: "danger",
        title: "Could not refine those options",
        message: errorMessage(error),
      });
    } finally {
      setBusy(null);
    }
  };

  const chooseDirection = (option: WorkshopOption): void => {
    // Coming back to the same direction keeps whatever was typed over it;
    // moving to the other one starts from that option's wording. Selecting a
    // card is not enough to count as "edited" — there has to be a prompt.
    const keepEdits =
      draft.promptSource === option.prompt && draft.prompt.trim().length > 0;
    patch({
      optionId: option.id,
      prompt: keepEdits ? draft.prompt : option.prompt,
      promptSource: option.prompt,
      chosen: false,
      recommended: option.recommended_settings,
      config: applyRecommended(draft.config, option.recommended_settings),
    });
    goStep("prompt");
  };

  /**
   * Records the choice with the workshop on the way to settings. A failure here
   * costs the workshop's bookkeeping, not the run: `POST /runs` carries the
   * prompt itself, so the wizard says what happened and keeps going.
   */
  const commitPrompt = async (): Promise<void> => {
    if (workshopId && draft.optionId && !draft.chosen) {
      setBusy("choose");
      try {
        await api.chooseWorkshopOption(workshopId, {
          option_id: draft.optionId,
          final_prompt: draft.prompt,
        });
        patch({ chosen: true });
      } catch (error) {
        pushToast({
          tone: "caution",
          title: "The workshop did not record your choice",
          message: `${errorMessage(error)} Your prompt is still exactly what the run will use.`,
        });
      } finally {
        setBusy(null);
      }
    }
    goStep("configure");
  };

  const launch = async (): Promise<void> => {
    setBusy("launch");
    setLaneConflict(null);
    try {
      if (draft.notify) await requestNotificationPermission();
      else setNotificationOptIn(false);

      const detail = await api.createRun({
        question: draft.question.trim() || draft.prompt.slice(0, 200),
        prompt: draft.prompt,
        title: draft.title.trim() || undefined,
        harness: harnessFor(draft.config),
        from_run: draft.fromRun ?? undefined,
        context_docs: draft.contextDocs.length > 0 ? draft.contextDocs : undefined,
        // `config.provider` joins the tier and the overrides as the third
        // *resolution input*: with two vendors, `(tier, overrides)` no longer
        // identifies a table, and a launcher resolving without it would freeze a
        // different table than the Confirm step showed. It is never the run's
        // answer — the frozen `model_table` is that, one row per step with an
        // explicit model id, which is the only honest record once overrides can
        // mix vendors inside one run.
        config: draft.config,
      });
      primeRuns([detail.run]);
      clearDraft(username);
      navigate(`/runs/${detail.run.id}`);
    } catch (error) {
      const conflict = laneConflictFrom(error);
      if (conflict) setLaneConflict(conflict);
      else {
        pushToast({
          tone: "danger",
          title: "The run did not start",
          message: errorMessage(error),
        });
      }
      setBusy(null);
    }
  };

  /* --- Render ------------------------------------------------------------ */

  const titlePlaceholder = useMemo(
    () => deriveTitleFromQuestion(draft.question) || "Untitled run",
    [draft.question],
  );

  return (
    <>
      <PageHeader eyebrow="New run" title="Start a research run">
        Describe the question, choose a direction, then set how far it should go. Nothing
        runs until the last step.
      </PageHeader>

      <StepRail current={step} furthest={furthest} onGo={(next) => goStep(next)} />

      {step === "intent" ? (
        <IntentStep
          question={draft.question}
          docs={draft.contextDocs}
          busy={busy === "workshop"}
          onQuestionChange={(question) => patch({ question })}
          onDocsChange={(contextDocs: ContextDocInput[]) => patch({ contextDocs })}
          onStartWorkshop={() => void startWorkshop()}
          onWritePromptMyself={writePromptMyself}
        />
      ) : null}

      {step === "options" ? (
        workshopError ? (
          <ErrorState
            title="Could not load those options"
            message={workshopError}
            onRetry={() => {
              setWorkshopError(null);
              setWorkshop(null);
            }}
          />
        ) : !loaded || refining ? (
          <WorkshopWaiting elapsed={elapsed} />
        ) : workshop ? (
          <OptionsStep
            workshop={workshop}
            selectedId={draft.optionId}
            refining={busy === "refine"}
            onSelect={(optionId) => patch({ optionId })}
            onRefine={(base, note) => void refine(base, note)}
            onContinue={chooseDirection}
            onBack={() => goStep("intent")}
          />
        ) : null
      ) : null}

      {step === "prompt" ? (
        <PromptStep
          prompt={draft.prompt}
          source={draft.promptSource}
          busy={busy === "choose"}
          onChange={(prompt) => patch({ prompt })}
          onRevert={() => patch({ prompt: draft.promptSource })}
          onContinue={() => void commitPrompt()}
          onBack={() => goStep(draft.workshopId ? "options" : "intent")}
        />
      ) : null}

      {step === "configure" ? (
        <>
          {isClone(draft) ? (
            <CloneNotice onStartOver={() => goStep("intent", null)} />
          ) : null}
          <ConfigureStep
            config={draft.config}
            provider={draft.config.provider}
            title={draft.title}
            titlePlaceholder={titlePlaceholder}
            recommended={draft.recommended}
            onConfigChange={patchConfig}
            onProviderChange={(provider) =>
              patchConfig({ provider: asProvider(provider) })
            }
            onTitleChange={(title) => patch({ title })}
            onContinue={() => goStep("confirm")}
            onBack={() => goStep(isClone(draft) ? "intent" : "prompt")}
          />
        </>
      ) : null}

      {step === "confirm" ? (
        <ConfirmStep
          config={draft.config}
          provider={draft.config.provider}
          question={draft.question}
          prompt={
            isClone(draft)
              ? "Inherited from the run this was started from — the original wording is used exactly."
              : draft.prompt
          }
          title={draft.title.trim() || titlePlaceholder}
          contextDocs={draft.contextDocs}
          notify={draft.notify}
          launching={busy === "launch"}
          laneConflict={laneConflict}
          onNotifyChange={(notify) => patch({ notify })}
          onUseDemo={() => patchConfig({ runner: "demo" })}
          onUseClaude={() => patchConfig({ runner: "claude" })}
          onLaunch={() => void launch()}
          onBack={() => goStep("configure")}
        />
      ) : null}
    </>
  );
}

function CloneNotice({ onStartOver }: { onStartOver: () => void }) {
  return (
    <div
      className="row-wrap"
      style={{
        marginBottom: "var(--space-4)",
        padding: "var(--space-3) var(--space-4)",
        borderRadius: "var(--radius-md)",
        background: "var(--info-wash)",
        border: "var(--hairline) solid var(--border)",
      }}
    >
      <span>
        Running an earlier question again. Its prompt and context documents are reused
        exactly; adjust the settings below.
      </span>
      <span className="spacer" />
      <button type="button" className="btn btn--sm" onClick={onStartOver}>
        Start from a new question
      </button>
    </div>
  );
}

/* --- Initialisation -------------------------------------------------------
   The URL addresses a step; the draft holds the words. When they disagree —
   somebody opened a link to a different workshop than the one in the draft —
   the URL wins, because that is what the person just clicked.
   ------------------------------------------------------------------------- */

function initialDraft({
  username,
  workshopParam,
  runnerParam,
  fromParam,
}: {
  username: string;
  workshopParam: string | null;
  runnerParam: string | null;
  fromParam: string | null;
}): WizardDraft {
  const stored = readDraft(username);
  let draft =
    stored && (!workshopParam || stored.workshopId === workshopParam)
      ? stored
      : { ...blankDraft(), workshopId: workshopParam };

  if (fromParam && draft.fromRun !== fromParam) draft = { ...blankDraft() };
  if (runnerParam === "demo") {
    draft = { ...draft, config: { ...draft.config, runner: "demo" } };
  }
  return draft;
}

/**
 * The stored default, narrowed to the request body's vocabulary.
 *
 * The same boundary `asRoleModel` documents in `wizard/state`: the values come
 * from `/api/settings/models`, which the backend writes from the same allowlist
 * the request body's literal unions are generated from — so this is a boundary
 * and not a guess. A named function rather than an inline cast, so the one place
 * the two vocabularies meet is greppable.
 */
function asStoredDefault(
  stored: ModelSettings,
): Pick<RunConfig, "provider" | "model_tier" | "model_overrides"> {
  return {
    provider: stored.provider,
    model_tier: stored.tier,
    model_overrides: stored.overrides,
  };
}

/** The other half of the same boundary, for a provider chosen in the matrix. */
function asProvider(provider: string): RunConfig["provider"] {
  return provider as RunConfig["provider"];
}

/**
 * `RunDetail.config` is typed loosely by the backend; keep only known keys.
 *
 * The tier is *validated*, not merely copied. Runs stored before the model floor
 * landed carry tiers the app no longer offers, and letting one through took the
 * whole wizard down: the cost estimate destructured a table entry that did not
 * exist and the error boundary replaced the page, so "Run again" on an older run
 * simply did not work. The backend keeps those runs launchable by mapping the
 * legacy name server-side, which is exactly why the client must not break first.
 */
function normaliseConfig(config: Partial<RunConfig> | undefined): Partial<RunConfig> {
  if (!config) return {};
  const {
    rounds,
    workflow,
    generation_batch,
    matches_per_round,
    evolve_top_k,
    budget_calls,
    budget_usd,
    wall_clock_minutes,
    grounding_depth,
    graft,
    provider,
    model_tier,
    model_overrides,
    runner,
  } = config;
  const known = (MODEL_TIERS as readonly string[]).includes(model_tier ?? "");
  return JSON.parse(
    JSON.stringify({
      rounds,
      workflow: workflow ?? "tournament",
      generation_batch,
      matches_per_round,
      evolve_top_k,
      budget_calls,
      budget_usd,
      wall_clock_minutes,
      grounding_depth,
      graft,
      // The three resolution inputs travel together, and stand or fall together.
      //
      // An unknown tier is dropped rather than carried: the draft keeps the
      // default it already had, and the role matrix offers a table that exists.
      model_tier: known ? model_tier : undefined,
      // Per-role choices only survive alongside the tier they were differences
      // from; against a different table they would mean something else.
      model_overrides: known ? model_overrides : undefined,
      // And the vendor those two resolve against. Left out, "Run again" on an
      // OpenAI run silently produced an Anthropic one: the draft starts from
      // `blankDraft()`, whose provider is `null`, and the wizard posts its whole
      // config — so an omitted provider is not an omission on the wire, it is an
      // explicit "no opinion" that lands the clone on today's system default and
      // moves it into the other CLI's lane. A tier the engine no longer offers
      // takes the provider down with it for the same reason it takes the
      // overrides: what survives has to be a table that can still be resolved.
      provider: known ? provider : undefined,
      runner,
    }),
  ) as Partial<RunConfig>;
}

/** The 409 from `POST /runs`: one active run per harness. */
function laneConflictFrom(error: unknown): LaneConflict | null {
  if (!isApiError(error)) return null;
  if (error.code !== "lane_busy" && error.status !== 409) return null;
  const details = error.details as { conflicting_run_id?: unknown } | null;
  const runId =
    details && typeof details.conflicting_run_id === "string"
      ? details.conflicting_run_id
      : null;
  return { runId, message: error.message };
}
