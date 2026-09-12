import { Monitor, Moon, Sun } from "lucide-react";

import { nextTheme, useTheme } from "../lib/theme";
import type { Theme } from "../lib/theme";

/**
 * One button for three states, because a menu for this is a menu nobody opens.
 *
 * The cycle is System → Light → Dark → System, and `system` being *in* the cycle
 * is the point: it is reachable again after you have forced a palette, so the
 * control is never a one-way door out of following the OS.
 *
 * A cycling button is only honest if it says what the next press does — an icon
 * alone leaves the reader to guess whether it shows the current state or the one
 * they are about to get. So the accessible name and the tooltip carry both, and
 * they are the same sentence.
 */
export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const next = nextTheme(theme);
  const label = `Theme: ${theme} — click for ${next}`;
  const Icon = ICONS[theme];

  return (
    <button
      type="button"
      className="btn btn--ghost btn--icon btn--sm"
      aria-label={label}
      title={label}
      onClick={() => setTheme(next)}
    >
      <Icon size={15} aria-hidden="true" />
    </button>
  );
}

const ICONS: Record<Theme, typeof Monitor> = {
  system: Monitor,
  light: Sun,
  dark: Moon,
};
