import { Fragment } from "react";
import type { ReactNode } from "react";

import { parseMarkdown } from "../lib/markdown";
import type { Block, Inline } from "../lib/markdown";

/**
 * Renders the engine's markdown as React elements — never as an HTML string,
 * so model output cannot inject markup. Wraps itself in `.prose`, the reading
 * register from the design system: this is a document, not a data grid.
 */
export function Markdown({ source, className }: { source: string; className?: string }) {
  const blocks = parseMarkdown(source);
  return (
    <div className={className ? `prose ${className}` : "prose"}>
      {blocks.map((block, index) => (
        <Fragment key={index}>{renderBlock(block, index)}</Fragment>
      ))}
    </div>
  );
}

function renderBlock(block: Block, key: number): ReactNode {
  switch (block.kind) {
    case "heading": {
      const Tag = `h${block.level}` as "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
      return <Tag>{renderInlines(block.children)}</Tag>;
    }
    case "paragraph":
      return <p>{renderInlines(block.children)}</p>;
    case "list":
      return block.ordered ? (
        <ol>
          {block.items.map((item, index) => (
            <li key={index}>{renderInlines(item)}</li>
          ))}
        </ol>
      ) : (
        <ul>
          {block.items.map((item, index) => (
            <li key={index}>{renderInlines(item)}</li>
          ))}
        </ul>
      );
    case "quote":
      return (
        <blockquote>
          {block.blocks.map((inner, index) => (
            <Fragment key={index}>{renderBlock(inner, index)}</Fragment>
          ))}
        </blockquote>
      );
    case "code":
      return (
        <pre>
          <code>{block.text}</code>
        </pre>
      );
    case "rule":
      return <hr />;
    case "table":
      return (
        <div className="scroll-x" key={key}>
          <table className="table">
            <thead>
              <tr>
                {block.head.map((cell, index) => (
                  <th key={index}>{renderInlines(cell)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {row.map((cell, cellIndex) => (
                    <td key={cellIndex}>{renderInlines(cell)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
  }
}

function renderInlines(nodes: Inline[]): ReactNode {
  return nodes.map((node, index) => (
    <Fragment key={index}>{renderInline(node)}</Fragment>
  ));
}

function renderInline(node: Inline): ReactNode {
  switch (node.kind) {
    case "text":
      return node.text;
    case "strong":
      return <strong>{renderInlines(node.children)}</strong>;
    case "em":
      return <em>{renderInlines(node.children)}</em>;
    case "code":
      return <code>{node.text}</code>;
    case "link":
      return (
        <a href={node.href} target="_blank" rel="noreferrer noopener">
          {renderInlines(node.children)}
        </a>
      );
  }
}
