from __future__ import annotations

import argparse
import json
from pathlib import Path

from work_retrieval_core import SearchEngine

from work_retrieval_api import create_app


def _unavailable_factory() -> SearchEngine:
    raise RuntimeError("OpenAPI generation must not initialize a search engine")


def rendered_openapi() -> bytes:
    app = create_app(_unavailable_factory)
    return (json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = rendered_openapi()
    if args.check:
        if not args.output.is_file() or args.output.read_bytes() != rendered:
            raise SystemExit(f"OpenAPI contract is stale: {args.output}")
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(rendered)


if __name__ == "__main__":
    main()
