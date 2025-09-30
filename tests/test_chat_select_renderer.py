from __future__ import annotations

import json
import shutil
import subprocess

import pytest

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node binary not available")
def test_select_renderer_outputs_table():
    payload = {
        "execution": {
            "stmt_kind": "SELECT",
            "columns": ["id", "url", "captured_at"],
            "rows": [
                [1, "https://example.com/first", "2024-01-01T00:00:00Z"],
                [2, "https://example.com/second", "2024-01-02T00:00:00Z"],
            ],
            "row_count": 2,
            "meta": {"exec_ms": 12, "engine_ms": 5},
        }
    }

    script = """
    const renderer = require('./frontend/chat_select_renderer.js');
    const payload = %s;
    const html = renderer.buildExecutionHtml(payload);
    process.stdout.write(JSON.stringify({ html }));
    """ % json.dumps(payload)

    completed = subprocess.run(
        [NODE, '-e', script],
        cwd='.',
        text=True,
        capture_output=True,
        check=True,
    )

    html = json.loads(completed.stdout)["html"]

    assert '<table' in html
    assert html.count('<tr>') == 3  # header + 2 rows
    assert 'rows=2' in html
    assert 'exec=12ms' in html
    assert 'engine=5ms' in html
    assert '<a href="https://example.com/first"' in html


@pytest.mark.skipif(NODE is None, reason="node binary not available")
def test_select_renderer_handles_empty():
    payload = {
        "execution": {
            "stmt_kind": "SELECT",
            "columns": ["id"],
            "rows": [],
            "row_count": 0,
            "meta": {"exec_ms": 3},
        }
    }

    script = """
    const renderer = require('./frontend/chat_select_renderer.js');
    const payload = %s;
    const html = renderer.buildExecutionHtml(payload);
    process.stdout.write(JSON.stringify({ html }));
    """ % json.dumps(payload)

    completed = subprocess.run(
        [NODE, '-e', script],
        cwd='.',
        text=True,
        capture_output=True,
        check=True,
    )

    html = json.loads(completed.stdout)["html"]

    assert 'No rows.' in html
    assert '<table' not in html
    assert 'exec=3ms' in html


@pytest.mark.skipif(NODE is None, reason="node binary not available")
def test_select_renderer_caps_rows():
    rows = [[i, f"https://example.com/{i}"] for i in range(30)]
    payload = {
        "execution": {
            "stmt_kind": "SELECT",
            "columns": ["id", "url"],
            "rows": rows,
            "row_count": 30,
        }
    }

    script = """
    const renderer = require('./frontend/chat_select_renderer.js');
    const payload = %s;
    const html = renderer.buildExecutionHtml(payload);
    process.stdout.write(JSON.stringify({ html }));
    """ % json.dumps(payload)

    completed = subprocess.run(
        [NODE, '-e', script],
        cwd='.',
        text=True,
        capture_output=True,
        check=True,
    )

    html = json.loads(completed.stdout)["html"]

    # header + capped 20 body rows
    assert html.count('<tr>') == 21

