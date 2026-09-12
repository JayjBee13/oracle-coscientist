/**
 * A small markdown parser for the text the engine writes.
 *
 * Why not a dependency: the only markdown in this app is what a language model
 * produced — hypothesis bodies, reviews, debate transcripts, the research
 * overview. That text is untrusted, it is rendered into an app that also shows
 * a scientist's own prompts, and the app runs offline. A parser that produces
 * *data* (which `components/Markdown.tsx` turns into React elements) can never
 * inject HTML, because no HTML string is ever built. Raw `<script>` in the
 * source comes out as the literal characters, which is exactly right.
 *
 * The subset is the one the engine's prompts actually ask for: headings,
 * paragraphs, lists, block quotes, fenced code, rules, pipe tables, and inline
 * emphasis / code / links.
 */

export type Inline =
  | { kind: "text"; text: string }
  | { kind: "strong"; children: Inline[] }
  | { kind: "em"; children: Inline[] }
  | { kind: "code"; text: string }
  | { kind: "link"; href: string; children: Inline[] };

export type Block =
  | { kind: "heading"; level: 1 | 2 | 3 | 4 | 5 | 6; children: Inline[] }
  | { kind: "paragraph"; children: Inline[] }
  | { kind: "list"; ordered: boolean; items: Inline[][] }
  | { kind: "quote"; blocks: Block[] }
  | { kind: "code"; text: string; lang: string | null }
  | { kind: "rule" }
  | { kind: "table"; head: Inline[][]; rows: Inline[][][] };

/** Schemes a link may use. Anything else renders as plain text, not a link. */
const SAFE_HREF = /^(https?:\/\/|mailto:|#|\/)/i;

export function parseMarkdown(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  return parseBlocks(lines);
}

function parseBlocks(lines: string[]): Block[] {
  const blocks: Block[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    if (line.trim() === "") {
      index += 1;
      continue;
    }

    const fence = /^\s*(```|~~~)\s*(\S+)?\s*$/.exec(line);
    if (fence) {
      const marker = fence[1];
      const body: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].trimStart().startsWith(marker)) {
        body.push(lines[index]);
        index += 1;
      }
      index += 1; // closing fence (or end of input)
      blocks.push({ kind: "code", text: body.join("\n"), lang: fence[2] ?? null });
      continue;
    }

    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      blocks.push({ kind: "rule" });
      index += 1;
      continue;
    }

    const heading = /^\s{0,3}(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      blocks.push({
        kind: "heading",
        level: heading[1].length as 1 | 2 | 3 | 4 | 5 | 6,
        children: parseInline(heading[2].replace(/\s+#+\s*$/, "")),
      });
      index += 1;
      continue;
    }

    if (/^\s{0,3}>/.test(line)) {
      const quoted: string[] = [];
      while (index < lines.length && /^\s{0,3}>/.test(lines[index])) {
        quoted.push(lines[index].replace(/^\s{0,3}>\s?/, ""));
        index += 1;
      }
      blocks.push({ kind: "quote", blocks: parseBlocks(quoted) });
      continue;
    }

    const table = readTable(lines, index);
    if (table) {
      blocks.push(table.block);
      index = table.next;
      continue;
    }

    const bullet = /^\s{0,3}([-*+])\s+(.*)$/.exec(line);
    const numbered = /^\s{0,3}(\d{1,9})[.)]\s+(.*)$/.exec(line);
    if (bullet || numbered) {
      const ordered = numbered != null;
      const items: string[] = [];
      while (index < lines.length) {
        const current = lines[index];
        const match = ordered
          ? /^\s{0,3}(\d{1,9})[.)]\s+(.*)$/.exec(current)
          : /^\s{0,3}([-*+])\s+(.*)$/.exec(current);
        if (match) {
          items.push(match[2]);
          index += 1;
          continue;
        }
        // A wrapped continuation line belongs to the item above it.
        if (items.length > 0 && current.trim() !== "" && /^\s+\S/.test(current)) {
          items[items.length - 1] += ` ${current.trim()}`;
          index += 1;
          continue;
        }
        break;
      }
      blocks.push({ kind: "list", ordered, items: items.map(parseInline) });
      continue;
    }

    const paragraph: string[] = [];
    while (
      index < lines.length &&
      lines[index].trim() !== "" &&
      !startsBlock(lines[index])
    ) {
      paragraph.push(lines[index].trim());
      index += 1;
    }
    if (paragraph.length === 0) {
      // `startsBlock` matched on the first line but no rule above claimed it:
      // treat it as text so nothing is ever silently dropped.
      paragraph.push(lines[index].trim());
      index += 1;
    }
    blocks.push({ kind: "paragraph", children: parseInline(paragraph.join(" ")) });
  }

  return blocks;
}

function startsBlock(line: string): boolean {
  return (
    /^\s*(```|~~~)/.test(line) ||
    /^\s{0,3}#{1,6}\s/.test(line) ||
    /^\s{0,3}>/.test(line) ||
    /^\s{0,3}([-*+])\s/.test(line) ||
    /^\s{0,3}\d{1,9}[.)]\s/.test(line) ||
    /^\s*([-*_])(\s*\1){2,}\s*$/.test(line)
  );
}

function readTable(
  lines: string[],
  start: number,
): { block: Block; next: number } | null {
  const header = lines[start];
  const divider = lines[start + 1];
  if (!header?.includes("|") || !divider) return null;
  if (!/^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(divider) || !divider.includes("-")) {
    return null;
  }

  const head = splitRow(header);
  if (head.length === 0) return null;

  const rows: Inline[][][] = [];
  let index = start + 2;
  while (
    index < lines.length &&
    lines[index].includes("|") &&
    lines[index].trim() !== ""
  ) {
    rows.push(splitRow(lines[index]).map(parseInline));
    index += 1;
  }

  return {
    block: { kind: "table", head: head.map(parseInline), rows },
    next: index,
  };
}

function splitRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

/* --- Inline --------------------------------------------------------------- */

export function parseInline(source: string): Inline[] {
  const nodes: Inline[] = [];
  let text = "";
  let index = 0;

  const flush = (): void => {
    if (text) {
      nodes.push({ kind: "text", text });
      text = "";
    }
  };

  while (index < source.length) {
    const char = source[index];

    if (char === "\\" && index + 1 < source.length) {
      text += source[index + 1];
      index += 2;
      continue;
    }

    if (char === "`") {
      const end = source.indexOf("`", index + 1);
      if (end > index) {
        flush();
        nodes.push({ kind: "code", text: source.slice(index + 1, end) });
        index = end + 1;
        continue;
      }
    }

    if (char === "*" && source[index + 1] === "*") {
      const end = source.indexOf("**", index + 2);
      if (end > index + 1) {
        flush();
        nodes.push({
          kind: "strong",
          children: parseInline(source.slice(index + 2, end)),
        });
        index = end + 2;
        continue;
      }
    }

    if (char === "*" || char === "_") {
      const end = source.indexOf(char, index + 1);
      // `snake_case_words` must not become emphasis.
      const boundary = char === "*" || !/\w/.test(source[index - 1] ?? "");
      if (end > index + 1 && boundary) {
        flush();
        nodes.push({ kind: "em", children: parseInline(source.slice(index + 1, end)) });
        index = end + 1;
        continue;
      }
    }

    if (char === "[") {
      const close = source.indexOf("]", index + 1);
      if (close > index && source[close + 1] === "(") {
        const hrefEnd = source.indexOf(")", close + 2);
        if (hrefEnd > close) {
          const label = source.slice(index + 1, close);
          const href = source.slice(close + 2, hrefEnd).trim();
          flush();
          if (SAFE_HREF.test(href)) {
            nodes.push({ kind: "link", href, children: parseInline(label) });
          } else {
            // Not a scheme we will follow — show the text, keep the address
            // visible, link nothing.
            nodes.push({ kind: "text", text: `${label} (${href})` });
          }
          index = hrefEnd + 1;
          continue;
        }
      }
    }

    text += char;
    index += 1;
  }

  flush();
  return nodes;
}

/** Plain text of a markdown string — for previews and `title` attributes. */
export function markdownToText(source: string): string {
  return parseMarkdown(source)
    .map(blockText)
    .filter((part) => part.length > 0)
    .join("\n\n");
}

function blockText(block: Block): string {
  switch (block.kind) {
    case "heading":
    case "paragraph":
      return inlineText(block.children);
    case "list":
      return block.items.map((item) => `• ${inlineText(item)}`).join("\n");
    case "quote":
      return block.blocks.map(blockText).join("\n");
    case "code":
      return block.text;
    case "table":
      return [...block.head, ...block.rows.flat()].map(inlineText).join(" ");
    case "rule":
      return "";
  }
}

function inlineText(nodes: Inline[]): string {
  return nodes
    .map((node) => {
      switch (node.kind) {
        case "text":
        case "code":
          return node.text;
        default:
          return inlineText(node.children);
      }
    })
    .join("");
}
