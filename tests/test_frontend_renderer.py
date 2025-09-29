from __future__ import annotations

import json
import shutil
import subprocess
import textwrap

import pytest

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node binary not available")
def test_read_renderer_outputs_table(tmp_path):
    payload = {
        "intent": "read",
        "result": {
            "columns": ["url", "seen_at"],
            "rows": [
                ["http://example.com/first", "2024-01-01T00:00:00"],
                ["http://example.com/second", "2024-01-02T00:00:00"],
            ],
            "row_count": 2,
        },
        "metrics": {"exec_ms": 7, "engine_ms": 3},
        "linkable_columns": ["url"],
        "breadcrumbs": {"deterministic": True, "llm_ms": 0},
    }
    script = textwrap.dedent(
        f"""
        const renderer = require('./frontend/read_renderer.js');
        const payload = {json.dumps(payload)};
        const html = renderer.buildReadResultHtml(payload);
        if (!html.includes('<table')) {{
          throw new Error('table missing');
        }}
        if (html.includes('Plan:')) {{
          throw new Error('unexpected scaffold text');
        }}
        console.log(html);
        """
    )
    completed = subprocess.run(
        [NODE, '-e', script],
        cwd='.',
        text=True,
        capture_output=True,
        check=True,
    )
    output = completed.stdout
    assert '<td><a' in output
    assert 'rows=2' in output
    assert 'exec_ms=7' in output
