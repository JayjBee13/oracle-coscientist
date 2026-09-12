import { useId } from "react";
import type { ReactNode } from "react";

import type { RunConfig } from "../../api/types";
import {
  estimateCalls,
  estimateMinutes,
  estimateUsd,
  formatMinutesRange,
  formatUsdRange,
} from "../../lib/estimates";
import { STEP_LABELS, WIZARD_STEPS, stepIndex } from "./state";
import type { WizardStep } from "./state";

/* --- The step rail --------------------------------------------------------
   Where you are, what you have passed, and what is still ahead. Steps already
   completed are clickable; steps ahead are not — a stepper that lets you jump
   to a page that cannot render is worse than one that does not.
   ------------------------------------------------------------------------- */

export function StepRail({
  current,
  furthest,
  onGo,
}: {
  current: WizardStep;
  furthest: WizardStep;
  onGo: (step: WizardStep) => void;
}) {
  return (
    <nav className="wiz-rail" aria-label="Launch steps">
      {WIZARD_STEPS.map((step, index) => {
        const position = stepIndex(step);
        const state =
          step === current
            ? "current"
            : position < stepIndex(furthest) || position < stepIndex(current)
              ? "done"
              : "ahead";
        const reachable = state === "done";
        return (
          <button
            key={step}
            type="button"
            className="wiz-rail__step"
            data-state={state}
            disabled={!reachable}
            aria-current={step === current ? "step" : undefined}
            onClick={() => onGo(step)}
          >
            <span className="wiz-rail__mark" aria-hidden="true">
              {index + 1}
            </span>
            <span className="wiz-rail__name">{STEP_LABELS[step]}</span>
          </button>
        );
      })}
    </nav>
  );
}

/* --- Estimates ------------------------------------------------------------ */

/**
 * The two resources that actually run out, and nothing else in the same register.
 *
 * Model calls is the governor: it is what the engine counts and what it stops
 * on. Wall clock is the other scarce thing, and it always says it is a bracket —
 * a single number here would be read as a promise.
 *
 * Cost used to be the middle tile, captioned "stops at $5" while the value above
 * it read "$25 – $67". That is a limit the run would visibly blow through, on a
 * subscription where nothing is billed per token; and with the ceiling unset it
 * rendered "stops at $null". The figure survives as one labelled footnote,
 * worded the way the run's own Settings tab words it.
 */
export function EstimateStrip({
  config,
  demo,
  tier,
}: {
  config: RunConfig;
  demo: boolean;
  tier: string;
}) {
  const calls = estimateCalls(config);
  const usd = estimateUsd(config, { tier, demo });
  const minutes = estimateMinutes(config);

  return (
    <>
      <div className="est-strip">
        <div className="est">
          <span className="label">Model calls</span>
          <span className="est__value">~{calls}</span>
          <span className="est__note">stops at {config.budget_calls}</span>
        </div>
        <div className="est">
          <span className="label">Wall clock</span>
          <span className="est__value">{formatMinutesRange(minutes)}</span>
          <span className="est__note">
            {config.wall_clock_minutes == null
              ? "estimated, not a limit"
              : `stops after ${config.wall_clock_minutes} min`}
          </span>
        </div>
      </div>
      <p className="footnote est-strip__footnote">
        {demo
          ? "A demo run invokes no model, so it costs nothing at all."
          : `≈ ${formatUsdRange(usd)} API-equivalent — what these calls would cost on the API. They run on a subscription and are not billed per token.`}
      </p>
    </>
  );
}

/* --- Fields ---------------------------------------------------------------
   `NumberField` lives in `components/` because the Continue dialog raises two
   of the same numbers after a run has ended and must clamp and hint exactly as
   the wizard does. Re-exported here so every wizard step keeps importing its
   fields from one place.
   ------------------------------------------------------------------------- */

export { NumberField } from "../../components/NumberField";

export function SelectField<T extends string>({
  label,
  hint,
  value,
  options,
  onChange,
}: {
  label: string;
  hint?: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
}) {
  const id = useId();
  return (
    <div className="field">
      <label className="field__label" htmlFor={id}>
        {label}
      </label>
      <select
        id={id}
        className="select"
        value={value}
        onChange={(event) => onChange(event.target.value as T)}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      {hint ? <span className="field__hint">{hint}</span> : null}
    </div>
  );
}

export function CheckboxField({
  checked,
  onChange,
  title,
  children,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  title: string;
  children?: ReactNode;
}) {
  return (
    <label className="checkbox">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span>
        <span style={{ display: "block", color: "var(--text-strong)" }}>{title}</span>
        {children ? <span className="faint">{children}</span> : null}
      </span>
    </label>
  );
}

/* --- Section chrome ------------------------------------------------------- */

export function Section({
  title,
  aside,
  children,
}: {
  title: string;
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="wiz-section">
      <div className="wiz-section__head">
        <h2 className="label">{title}</h2>
        <span className="spacer" />
        {aside}
      </div>
      {children}
    </section>
  );
}
