"""Export cross-language canonical event JSON Schema (Draft-07) from RuntimeEvent.

Usage:
    uv run python scripts/export_contracts.py          # write contracts/canonical-event.schema.json
    uv run python scripts/export_contracts.py --check  # fail if checked-in schema is out of date
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from agent_runtime.protocol import RuntimeEvent

SCHEMA_DRAFT = "http://json-schema.org/draft-07/schema#"
SCHEMA_ID = "https://care.local/contracts/canonical-event.schema.json"


def default_schema_path() -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "contracts" / "canonical-event.schema.json"


def _normalize_draft07(node: Any) -> Any:
    """Normalize Pydantic's Draft 2020-12 output into Draft-07 for TS/Java/Python codegen."""
    if isinstance(node, list):
        return [_normalize_draft07(item) for item in node]
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "$defs":
            out["definitions"] = _normalize_draft07(value)
        elif key == "const":
            out["enum"] = [_normalize_draft07(value)]
        else:
            out[key] = _normalize_draft07(value)

    # Collapse primitive nullable `anyOf: [{type: T}, {type: "null"}]` into `type: [T, "null"]`.
    any_of = out.get("anyOf")
    if isinstance(any_of, list) and len(any_of) == 2:
        non_null = [branch for branch in any_of if branch != {"type": "null"}]
        if len(non_null) == 1 and isinstance(non_null[0], dict):
            primary = non_null[0]
            primary_type = primary.get("type")
            if isinstance(primary_type, str) and "$ref" not in primary:
                del out["anyOf"]
                for pkey, pval in primary.items():
                    if pkey == "type":
                        out["type"] = [primary_type, "null"]
                    elif pkey == "enum" and isinstance(pval, list):
                        out["enum"] = [*pval, None] if None not in pval else list(pval)
                    else:
                        out[pkey] = pval

    return out


def build_canonical_event_schema() -> dict[str, Any]:
    raw_schema = RuntimeEvent.model_json_schema(
        mode="serialization",
        ref_template="#/definitions/{model}",
    )
    normalized = _normalize_draft07(raw_schema)
    return {
        "$schema": SCHEMA_DRAFT,
        "$id": SCHEMA_ID,
        **normalized,
    }


def render_canonical_event_schema() -> str:
    rendered = json.dumps(
        build_canonical_event_schema(),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return f"{rendered}\n"


def export_canonical_event_schema(output_path: Path | None = None, *, check: bool = False) -> bool:
    target = output_path if output_path is not None else default_schema_path()
    rendered = render_canonical_event_schema()
    if check:
        if not target.is_file():
            return False
        return target.read_text(encoding="utf-8") == rendered

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8", newline="\n")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export or verify contracts/canonical-event.schema.json."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Override output path (defaults to <repo>/contracts/canonical-event.schema.json).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the checked-in schema matches RuntimeEvent without modifying files.",
    )
    args = parser.parse_args(argv)
    target = args.output if args.output is not None else default_schema_path()
    ok = export_canonical_event_schema(target, check=args.check)
    if args.check:
        if not ok:
            print(f"Schema drift detected: {target} is missing or out of date.")
            return 1
        print(f"Schema up to date: {target}")
        return 0
    print(f"Exported {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
