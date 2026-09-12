import { describe, expect, it } from "vitest";

import { markdownToText, parseInline, parseMarkdown } from "./markdown";

describe("blocks", () => {
  it("reads headings, paragraphs and lists", () => {
    const blocks = parseMarkdown(
      "# Claim\n\nAmmonia forms at ambient pressure.\n\n- First\n- Second\n",
    );
    expect(blocks).toEqual([
      { kind: "heading", level: 1, children: [{ kind: "text", text: "Claim" }] },
      {
        kind: "paragraph",
        children: [{ kind: "text", text: "Ammonia forms at ambient pressure." }],
      },
      {
        kind: "list",
        ordered: false,
        items: [[{ kind: "text", text: "First" }], [{ kind: "text", text: "Second" }]],
      },
    ]);
  });

  it("keeps a fenced block verbatim, markup and all", () => {
    const blocks = parseMarkdown('```json\n{"a": **not bold**}\n```\n');
    expect(blocks).toEqual([{ kind: "code", lang: "json", text: '{"a": **not bold**}' }]);
  });

  it("joins a wrapped paragraph into one", () => {
    const blocks = parseMarkdown("One line\nand its continuation.\n");
    expect(blocks).toEqual([
      {
        kind: "paragraph",
        children: [{ kind: "text", text: "One line and its continuation." }],
      },
    ]);
  });

  it("reads a pipe table", () => {
    const blocks = parseMarkdown(
      "| Role | Model |\n| --- | --- |\n| Ranking | sonnet |\n",
    );
    expect(blocks[0].kind).toBe("table");
    expect(
      markdownToText("| Role | Model |\n| --- | --- |\n| Ranking | sonnet |"),
    ).toContain("Ranking");
  });

  it("reads a block quote as blocks of its own", () => {
    const blocks = parseMarkdown("> Quoted claim\n");
    expect(blocks).toEqual([
      {
        kind: "quote",
        blocks: [
          { kind: "paragraph", children: [{ kind: "text", text: "Quoted claim" }] },
        ],
      },
    ]);
  });

  it("never drops a line it does not understand", () => {
    expect(markdownToText("<script>alert(1)</script>")).toBe("<script>alert(1)</script>");
  });
});

describe("inline", () => {
  it("reads emphasis and code", () => {
    expect(parseInline("**bold** and `code`")).toEqual([
      { kind: "strong", children: [{ kind: "text", text: "bold" }] },
      { kind: "text", text: " and " },
      { kind: "code", text: "code" },
    ]);
  });

  it("leaves snake_case alone", () => {
    expect(parseInline("budget_calls_used")).toEqual([
      { kind: "text", text: "budget_calls_used" },
    ]);
  });

  it("links only to schemes it will follow", () => {
    expect(parseInline("[docs](https://example.com)")).toEqual([
      {
        kind: "link",
        href: "https://example.com",
        children: [{ kind: "text", text: "docs" }],
      },
    ]);
  });

  it("refuses a javascript: url and shows it as text instead", () => {
    expect(parseInline("[click](javascript:alert(1))")).toEqual([
      { kind: "text", text: "click (javascript:alert(1)" },
      { kind: "text", text: ")" },
    ]);
  });

  it("honours a backslash escape", () => {
    expect(parseInline("\\*not emphasis\\*")).toEqual([
      { kind: "text", text: "*not emphasis*" },
    ]);
  });
});
