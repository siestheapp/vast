from __future__ import annotations
import json, os, time
from pathlib import Path
from typing import Any, Dict

AUDIT_DIR = Path(".vast") / "audit"
AUDIT_DIR.mkdir(parents=True, exist_ok=True)
AUDIT_FILE = AUDIT_DIR / "events.jsonl"

def audit_event(event: Dict[str, Any]) -> None:
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with AUDIT_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
