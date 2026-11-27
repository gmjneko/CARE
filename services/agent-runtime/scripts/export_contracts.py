"""Export cross-language canonical event JSON Schema from RuntimeEvent.

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

SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID = "https://care.local/contracts/canonical-event.schema.json"


def default_schema_path() -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "contracts" / "canonical-event.schema.json"


def build_canonical_event_schema() -> dict[str, Any]:
    schema = RuntimeEvent.model_json_schema(mode="serialization")
    return {
        "$schema": SCHEMA_DRAFT,
        "$id": SCHEMA_ID,
        **schema,
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
