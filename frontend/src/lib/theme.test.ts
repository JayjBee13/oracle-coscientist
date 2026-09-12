import { describe, expect, it } from "vitest";

import { applyTheme, getTheme, nextTheme, setTheme, THEME_KEY } from "./theme";

/** What the CSS actually keys off. `null` is "following the system". */
function stamped(): string | null {
  return document.documentElement.getAttribute("data-theme");
}

describe("theme", () => {
  it("follows the system until something is stored", () => {
    expect(getTheme()).toBe("system");
    expect(stamped()).toBeNull();
  });

  it("cycles system → light → dark → system", () => {
    expect(nextTheme("system")).toBe("light");
    expect(nextTheme("light")).toBe("dark");
    expect(nextTheme("dark")).toBe("system");
  });

  it("stores the choice and stamps the document", () => {
    setTheme("light");
    expect(window.localStorage.getItem(THEME_KEY)).toBe("light");
    expect(stamped()).toBe("light");

    setTheme("dark");
    expect(window.localStorage.getItem(THEME_KEY)).toBe("dark");
    expect(stamped()).toBe("dark");
  });

  it("removes the attribute for system, rather than stamping a palette", () => {
    setTheme("dark");
    setTheme("system");

    // Stored, so the choice survives a reload — but *absent* from the document,
    // which is the only state prefers-color-scheme can be read through.
    expect(window.localStorage.getItem(THEME_KEY)).toBe("system");
    expect(stamped()).toBeNull();
  });

  it("treats a malformed stored value as system", () => {
    for (const junk of ["", "Dark", "midnight", "{}", "null"]) {
      window.localStorage.setItem(THEME_KEY, junk);
      expect(getTheme()).toBe("system");
    }
  });

  it("survives storage throwing on read", () => {
    const getItem = window.localStorage.getItem;
    window.localStorage.getItem = () => {
      throw new Error("private window");
    };
    try {
      expect(getTheme()).toBe("system");
    } finally {
      window.localStorage.getItem = getItem;
    }
  });

  it("applies a theme without going through storage", () => {
    applyTheme("dark");
    expect(stamped()).toBe("dark");
    applyTheme("system");
    expect(stamped()).toBeNull();
  });
});
