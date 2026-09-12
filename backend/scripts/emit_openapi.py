"""Write this API's OpenAPI document to the frontend, where types are generated from it.

    python scripts/emit_openapi.py            # write frontend/src/api/openapi.json
    python scripts/emit_openapi.py --check    # exit 1 if that file is out of date
    python scripts/emit_openapi.py --stdout   # print it instead

The frontend's `npm run gen:api` calls this first and then generates `types.ts`, so the
chain from a pydantic field to a TypeScript property has no hand-written link in it. Wave
6 gates on `npm run gen:api:check`, which runs both halves in `--check` mode.

No database is touched: building the app registers routes, and the document is read off
those. The lifespan — imports, listener, reconciler — only runs under a real server.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

DEFAULT_OUTPUT = BACKEND_ROOT.parent / "frontend" / "src" / "api" / "openapi.json"


def build_document() -> dict:
    from app.main import create_app

    document = create_app().openapi()
    _mark_response_fields_required(document)
    return document


def _refs(node: Any) -> Iterator[str]:
    """Every `#/components/schemas/X` reachable from a schema node, however nested."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            yield ref.rsplit("/", 1)[1]
        for value in node.values():
            yield from _refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _refs(item)


def _reachable(roots: Iterable[str], schemas: Mapping[str, Any]) -> set[str]:
    seen: set[str] = set()
    queue = list(roots)
    while queue:
        name = queue.pop()
        if name in seen or name not in schemas:
            continue
        seen.add(name)
        queue.extend(_refs(schemas[name]))
    return seen


def _mark_response_fields_required(document: dict) -> None:
    """Say in the schema what FastAPI actually does: responses carry every field.

    Pydantic marks a field with a default as not-required, which is right for a request
    body — the client may leave it out — and wrong for a response, where the default is
    filled in and serialised like any other value. Generated TypeScript takes that literally
    and makes `top`, `reviews`, `deltas` and every other defaulted field `| undefined`,
    which is a promise the API never breaks and a hundred null checks in the app that can
    never fire.

    Only schemas used exclusively in responses are touched. A model on both sides of the
    wire keeps pydantic's answer, because there the optionality is real.
    """
    schemas = document.get("components", {}).get("schemas", {})
    if not schemas:
        return

    response_roots: list[str] = []
    request_roots: list[str] = []
    for methods in document.get("paths", {}).values():
        for operation in methods.values():
            if not isinstance(operation, dict):
                continue
            response_roots.extend(_refs(operation.get("responses", {})))
            request_roots.extend(_refs(operation.get("requestBody", {})))

    response_only = _reachable(response_roots, schemas) - _reachable(request_roots, schemas)
    for name in response_only:
        schema = schemas[name]
        properties = schema.get("properties")
        if isinstance(properties, dict) and properties:
            schema["required"] = sorted(properties)


def render(document: dict) -> str:
    """Stable bytes for a stable input, so the check is about the API and not the writer."""
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; fail if the file on disk is not what would be written.",
    )
    parser.add_argument("--stdout", action="store_true", help="Print the document instead.")
    args = parser.parse_args(argv)

    rendered = render(build_document())

    if args.stdout:
        sys.stdout.write(rendered)
        return 0

    if args.check:
        if not args.output.exists():
            print(f"{args.output} does not exist. Run: npm run gen:api", file=sys.stderr)
            return 1
        # Byte comparison, not read_text(): universal newlines would let a
        # CRLF-flipped snapshot pass the gate that exists to keep it canonical.
        if args.output.read_bytes() != rendered.encode("utf-8"):
            print(
                f"{args.output} is out of date with the FastAPI app.\n"
                "The API changed without its types being regenerated. Run: npm run gen:api",
                file=sys.stderr,
            )
            return 1
        print(f"{args.output.name} is up to date")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    # newline="" stops Windows translating \n to \r\n; the repo stores LF and
    # .gitattributes (`* -text`) would commit the flip as a 4,889-line diff.
    with args.output.open("w", encoding="utf-8", newline="") as fh:
        fh.write(rendered)
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
