import { describe, expect, it } from "vitest";

import { LABEL_BUDGET, MAX_LINE_CHARS, shortLabel, wrapLabel } from "./shortLabel";

/**
 * The label is the only thing most readers will ever read off the canvas, so
 * the two properties that matter are that it is *short* — the layout is sized
 * against `LABEL_BUDGET` and cannot absorb a surprise — and that it is never
 * empty, whatever the title turns out to be.
 */

describe("shortLabel — cutting a title down to a phrase", () => {
  it("keeps a title that already fits, whole", () => {
    expect(shortLabel("Plasmon-driven loop", "h001")).toBe("Plasmon-driven loop");
  });

  it("drops a leading article, and only a leading one", () => {
    expect(shortLabel("The lithium nitride loop", "h001")).toBe("lithium nitride loop");
    expect(shortLabel("An ambient Haber route", "h001")).toBe("ambient Haber route");
    expect(shortLabel("A cheaper route", "h001")).toBe("cheaper route");
    // Not an article in first position: nothing is dropped.
    expect(shortLabel("Route to a nitride", "h001")).toBe("Route to a nitride");
  });

  it("keeps a verb, because a verb is content", () => {
    expect(shortLabel("Grounding the claim", "h001")).toBe("Grounding the claim");
  });

  it("never takes more than four words", () => {
    const label = shortLabel("one two three four five six seven", "h001");
    expect(label.split(" ")).toHaveLength(4);
    expect(label).toBe("one two three four");
  });

  it("stays inside the character budget, on whole words", () => {
    const label = shortLabel(
      "Electrochemical nitride cycling at a lithium interface",
      "h001",
    );
    expect(label).toBe("Electrochemical nitride");
    expect(label.length).toBeLessThanOrEqual(LABEL_BUDGET);
    // Whole words only: never a word sliced down the middle.
    expect("Electrochemical nitride cycling at a lithium interface").toContain(label);
  });

  it("holds the budget across every title a real run produced", () => {
    const titles = [
      "Plasmonic nitrogen fixation on gold nanostructures",
      "Ambient-pressure Haber variant with a sacrificial reductant",
      "Lithium-mediated nitride loop with a solid electrolyte",
      "**Plasmonic fixation grounded on a titanium nitride support**",
      '"Plasmon-driven electrochemical nitride loop"',
    ];
    for (const title of titles) {
      const label = shortLabel(title, "h001");
      expect(label.length).toBeLessThanOrEqual(LABEL_BUDGET);
      expect(label.split(" ").length).toBeLessThanOrEqual(4);
    }
  });

  it("truncates a single word that spends the whole budget, visibly", () => {
    const label = shortLabel("Supercalifragilisticexpialidocious catalysis", "h001");
    expect(label).toHaveLength(LABEL_BUDGET);
    expect(label.endsWith("…")).toBe(true);
    expect(label).toBe("Supercalifragilisticexpia…");
  });

  it("strips the quotes and emphasis a model wraps a title in", () => {
    expect(shortLabel("**Plasmonic nitrogen**", "h001")).toBe("Plasmonic nitrogen");
    expect(shortLabel('"Plasmonic nitrogen"', "h001")).toBe("Plasmonic nitrogen");
    expect(shortLabel("_Plasmonic nitrogen_", "h001")).toBe("Plasmonic nitrogen");
    expect(shortLabel("## Plasmonic nitrogen", "h001")).toBe("Plasmonic nitrogen");
  });

  it("collapses whatever whitespace the title arrived with", () => {
    expect(shortLabel("  Plasmonic \n  nitrogen  ", "h001")).toBe("Plasmonic nitrogen");
  });

  it("falls back to the hid when there is no title to cut", () => {
    expect(shortLabel("", "h007")).toBe("h007");
    expect(shortLabel("   \n ", "h007")).toBe("h007");
    expect(shortLabel("**  **", "h007")).toBe("h007");
  });

  it("falls back to the hid when the title *is* the hid", () => {
    // What `graphModel` writes when `hypothesis_added` carried no title.
    expect(shortLabel("h007", "h007")).toBe("h007");
    expect(shortLabel("**h007**", "h007")).toBe("h007");
  });

  it("is deterministic", () => {
    const title = "Ambient-pressure Haber variant with a sacrificial reductant";
    expect(shortLabel(title, "h002")).toBe(shortLabel(title, "h002"));
  });
});

describe("wrapLabel — two lines when one would run wide", () => {
  it("leaves a short label on one line", () => {
    expect(wrapLabel("h001")).toEqual(["h001"]);
    expect(wrapLabel("Plasmon-driven")).toEqual(["Plasmon-driven"]);
  });

  it("breaks a long label at the word boundary nearest the middle", () => {
    expect(wrapLabel("Electrochemical nitride")).toEqual(["Electrochemical", "nitride"]);
    expect(wrapLabel("one two three four")).toEqual(["one two", "three four"]);
  });

  it("never splits a word: an over-long one is cut, not hyphenated", () => {
    const lines = wrapLabel("Supercalifragilisticexpia…");
    expect(lines).toHaveLength(1);
    expect(lines[0]).toBe("Supercalifragilis…");
    // The cut is where the canvas runs out, and it is shown rather than hidden.
    expect(lines[0]).toHaveLength(MAX_LINE_CHARS);
    expect("Supercalifragilisticexpia…").toContain(lines[0].slice(0, -1));
  });

  it("loses nothing: the lines rejoin into the label", () => {
    const label = "Lithium-mediated nitride";
    expect(wrapLabel(label).join(" ")).toBe(label);
  });

  /**
   * The one property three spacing constants in two files depend on.
   *
   * `LABEL_SIDE` and `BAND_SLOT` in `IdeaGraph` and `FORCE.labelReach` in
   * `lib/graphLayout` are all `MAX_LINE_CHARS * LABEL_CHAR_PX` or half of it, so
   * a line wider than `MAX_LINE_CHARS` is a label drawn off the edge of the
   * canvas (`.ig__svg` does not set `overflow: visible`) or over its neighbour's.
   * This used to be asserted on a single hand-picked three-word string, which is
   * how a whole class of real titles — one long first word, no second word that
   * fits behind it — drew 26 characters against constants sized for 18.
   */
  it("never returns a line wider than MAX_LINE_CHARS, for any title", () => {
    const titles = [
      // No space to break on, at every length that matters.
      "Electrochemicallymediatednitrogenreductionpathway",
      "Supercalifragilisticexpialidociousness!!",
      // A long first word whose second word does not fit the budget behind it.
      "Photoelectrocatalytic ammonia synthesis",
      "Photoelectrocatalytically driven ammonia synthesis",
      // A long word in *second* position, so the cut lands on the tail line.
      "Ion photoelectrocatalysis",
      // The ordinary shapes, which must come through untouched.
      "Electrochemical nitride cyc",
      "one two three four five",
      "🔬 Lithium anode retreat",
      "The lithium nitride loop",
      "Plasmon-driven loop",
      "h001",
    ];
    for (const title of titles) {
      const lines = wrapLabel(shortLabel(title, "h001"));
      expect(lines.length).toBeGreaterThanOrEqual(1);
      expect(lines.length).toBeLessThanOrEqual(2);
      for (const line of lines) {
        expect(line.length).toBeGreaterThan(0);
        expect(line.length).toBeLessThanOrEqual(MAX_LINE_CHARS);
      }
    }
  });

  it("wraps rather than cuts whenever a wrap is enough", () => {
    // Cutting is the last resort, not the first: nothing is lost off a label
    // that two lines can carry.
    for (const label of [
      "Electrochemical nitride",
      "Lithium-mediated nitride",
      "Ambient-pressure Haber",
      "one two three four",
    ]) {
      expect(wrapLabel(label).join(" ")).toBe(label);
    }
  });
});
