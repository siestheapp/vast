from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

from src.vast.resolver import detect_latest_per_group


def test_detect_latest_per_group_basic():
    payload = detect_latest_per_group("latest 5 product urls per brand")
    assert payload is not None
    assert payload.get("intent") == "latest_per_group"
    assert payload.get("k") == 5
    assert payload.get("group_noun") == "brand"


@pytest.mark.integration
def test_cli_latest_per_group_debug_output():
    dsn = os.getenv("DATABASE_URL_RO") or os.getenv("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL_RO not configured for integration test")

    # Run CLI in debug mode and capture output
    proc = subprocess.run(
        ["python", "cli.py", "ask", "latest 5 product urls per brand", "--debug"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )

    out = proc.stdout

    # Deterministic path should avoid LLM
    assert re.search(r"llm_ms=0", out) or "llm_ms': 0" in out

    # Should include window function and join chain
    assert "ROW_NUMBER() OVER" in out
    assert "product_url" in out and "style" in out and "brand" in out


