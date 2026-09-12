/**
 * Generate `src/api/schema.ts` from `src/api/openapi.json`.
 *
 *   node scripts/gen-api-types.mjs           # write the file
 *   node scripts/gen-api-types.mjs --check   # exit 1 if it is out of date
 *
 * Hand-rolled rather than `openapi-typescript` on purpose: the input is not arbitrary
 * OpenAPI, it is what pydantic emits for this one app, which is a small and predictable
 * subset — refs, enums, arrays, records, and `anyOf` with a null branch. Two hundred lines
 * of translation is cheaper to reason about than a dependency in the audit surface, and it
 * fails loudly on anything it was not built for instead of emitting `unknown` and moving
 * on.
 *
 * The output is a flat list of type aliases, one per component schema, in name order.
 * `src/api/types.ts` is what the app imports; it re-exports from here.
 */

import { readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const INPUT = resolve(HERE, "../src/api/openapi.json");
const OUTPUT = resolve(HERE, "../src/api/schema.ts");

const HEADER = `/**
 * GENERATED FILE — DO NOT EDIT.
 *
 * Written by \`npm run gen:api\` from \`openapi.json\`, which the backend emits from the
 * FastAPI app (\`backend/scripts/emit_openapi.py\`). Every response and request shape the
 * API has is here, and nothing else is allowed to describe them: the app imports from
 * \`./types\`, which re-exports this file.
 *
 * To change a type, change the pydantic model and re-run \`npm run gen:api\`.
 * \`npm run gen:api:check\` fails the build when this file and the backend disagree.
 */
`;

/** Names that are valid bare TS/JS property keys; anything else gets quoted. */
const BARE_KEY = /^[A-Za-z_$][A-Za-z0-9_$]*$/;

function fail(message) {
  console.error(`gen-api-types: ${message}`);
  process.exit(1);
}

/** `RunSummary` -> `RUN_SUMMARY`, so an enum's values get a predictable const name. */
function screamingSnake(name) {
  return name
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1_$2")
    .toUpperCase();
}

function quote(value) {
  return JSON.stringify(value);
}

/** A doc comment, or nothing. Indented to sit with whatever it describes. */
function docComment(schema, indent) {
  const description = schema?.description?.trim();
  if (!description) return "";
  const lines = description.split("\n");
  if (lines.length === 1) return `${indent}/** ${lines[0]} */\n`;
  return `${indent}/**\n${lines.map((line) => `${indent} * ${line}`.trimEnd()).join("\n")}\n${indent} */\n`;
}

/**
 * One schema node to a TypeScript type expression.
 *
 * `path` is only ever used to say where an unsupported construct was found — a generator
 * that guesses is worse than one that stops.
 */
function typeOf(schema, path) {
  if (
    schema === true ||
    (schema && typeof schema === "object" && Object.keys(schema).length === 0)
  ) {
    // pydantic's `Any`.
    return "unknown";
  }
  if (!schema || typeof schema !== "object") fail(`${path}: not a schema`);

  if (schema.$ref) {
    const match = /^#\/components\/schemas\/(.+)$/.exec(schema.$ref);
    if (!match) fail(`${path}: cannot resolve $ref ${schema.$ref}`);
    return match[1];
  }

  if (schema.const !== undefined) return quote(schema.const);

  if (schema.anyOf || schema.oneOf) {
    const branches = schema.anyOf ?? schema.oneOf;
    const parts = branches.map((branch, index) =>
      branch?.type === "null" ? "null" : typeOf(branch, `${path}/anyOf[${index}]`),
    );
    return [...new Set(parts)].join(" | ");
  }

  if (schema.allOf) {
    const parts = schema.allOf.map((branch, index) =>
      typeOf(branch, `${path}/allOf[${index}]`),
    );
    return [...new Set(parts)].join(" & ");
  }

  if (schema.enum) {
    return schema.enum.map(quote).join(" | ");
  }

  // A node with a title but no type is pydantic's `Any` — `ValidationError.input`, for
  // one. It constrains nothing, so neither does the type.
  if (schema.type === undefined && !schema.properties && !schema.additionalProperties) {
    return "unknown";
  }

  switch (schema.type) {
    case "string":
      return "string";
    case "integer":
    case "number":
      return "number";
    case "boolean":
      return "boolean";
    case "null":
      return "null";
    case "array":
      if (schema.prefixItems) {
        const parts = schema.prefixItems.map((item, index) =>
          typeOf(item, `${path}/prefixItems[${index}]`),
        );
        return `[${parts.join(", ")}]`;
      }
      if (!schema.items) return "unknown[]";
      return `${wrap(typeOf(schema.items, `${path}/items`))}[]`;
    case "object": {
      if (schema.properties) return objectLiteral(schema, path, "  ");
      const values =
        schema.additionalProperties && schema.additionalProperties !== true
          ? typeOf(schema.additionalProperties, `${path}/additionalProperties`)
          : "unknown";
      return `Record<string, ${values}>`;
    }
    default:
      fail(`${path}: unsupported schema ${JSON.stringify(schema).slice(0, 160)}`);
  }
}

/** `A | B` needs brackets before `[]`; `A` does not. */
function wrap(type) {
  return /[|&]/.test(type) ? `(${type})` : type;
}

function objectLiteral(schema, path, indent) {
  const required = new Set(schema.required ?? []);
  const entries = Object.entries(schema.properties ?? {});
  if (entries.length === 0) return "Record<string, never>";

  const body = entries
    .map(([name, property]) => {
      const key = BARE_KEY.test(name) ? name : quote(name);
      const optional = required.has(name) ? "" : "?";
      const doc = docComment(property, indent);
      return `${doc}${indent}${key}${optional}: ${typeOf(property, `${path}/${name}`)};`;
    })
    .join("\n");
  return `{\n${body}\n${indent.slice(2)}}`;
}

function generate(document) {
  const schemas = document?.components?.schemas;
  if (!schemas) fail("openapi.json has no components.schemas — did the emitter run?");

  const blocks = Object.keys(schemas)
    .sort()
    .map((name) => {
      const schema = schemas[name];
      const doc = docComment(schema, "");

      // A standalone enum becomes a runtime array as well as a type: the app needs the
      // values themselves for filter chips and exhaustiveness, and a second hand-written
      // copy of the same list is exactly what this generator exists to prevent.
      if (schema.enum && !schema.properties) {
        const constName = `${screamingSnake(name)}_VALUES`;
        const values = schema.enum.map(quote).join(", ");
        return (
          `${doc}export const ${constName} = [${values}] as const;\n` +
          `export type ${name} = (typeof ${constName})[number];`
        );
      }

      const body =
        schema.type === "object" || schema.properties
          ? objectLiteral(schema, name, "  ")
          : typeOf(schema, name);
      return `${doc}export type ${name} = ${body};`;
    });

  return `${HEADER}\n${blocks.join("\n\n")}\n`;
}

function main() {
  const check = process.argv.includes("--check");
  let document;
  try {
    document = JSON.parse(readFileSync(INPUT, "utf8"));
  } catch (error) {
    fail(`cannot read ${INPUT}: ${error.message}`);
  }

  const rendered = generate(document);

  if (check) {
    let current = null;
    try {
      current = readFileSync(OUTPUT, "utf8");
    } catch {
      fail(`${OUTPUT} does not exist. Run: npm run gen:api`);
    }
    if (current !== rendered) {
      fail(
        "src/api/schema.ts is out of date with openapi.json.\n" +
          "The API changed without its types being regenerated. Run: npm run gen:api",
      );
    }
    console.log("schema.ts is up to date");
    return;
  }

  writeFileSync(OUTPUT, rendered, "utf8");
  console.log(`wrote ${OUTPUT}`);
}

main();
