#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys

from app.database import SessionLocal
from app.models import VetBizImportRecord
from app.services.vetbiz_imports import (
    VetBizImportError,
    repair_committed_interaction_summaries,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Repair untouched fallback summaries in a committed VetBiz import."
    )
    parser.add_argument("--import-id", type=int, required=True)
    parser.add_argument("--filename", required=True)
    args = parser.parse_args()
    data = sys.stdin.buffer.read()
    try:
        with SessionLocal() as db:
            record = db.get(VetBizImportRecord, args.import_id)
            if record is None:
                raise VetBizImportError("The requested VetBiz import does not exist.")
            counts = repair_committed_interaction_summaries(
                db, record, args.filename, data
            )
            db.commit()
        print(json.dumps({"import_id": args.import_id, **counts}, sort_keys=True))
        return 0
    except VetBizImportError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
