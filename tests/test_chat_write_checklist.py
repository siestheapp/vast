from __future__ import annotations

import json
import shutil
import subprocess
import textwrap

import pytest

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node binary not available")
def test_write_checklist_gated_for_select():
    message = textwrap.dedent(
        """
        Summary block.

        Plan:
        - Outline the proposed schema or data changes.

        Staging:
        - Describe how to stage the changes safely before production.

        Validation:
        - Explain validation and QA steps to verify correctness.

        Rollback:
        - Provide a rollback or remediation strategy if issues arise.

        Thanks.
        """
    ).strip()

    payload_select = {"execution": {"stmt_kind": "SELECT", "write": False}}
    payload_insert = {"execution": {"stmt_kind": "INSERT"}}

    script = textwrap.dedent(
        f"""
        const gate = require('./frontend/chat_write_checklist.js');
        const results = {{
          select: gate.applyWriteChecklistGate({json.dumps(message)}, {json.dumps(payload_select)}),
          insert: gate.applyWriteChecklistGate({json.dumps(message)}, {json.dumps(payload_insert)}),
          selectWriteLike: gate.isWriteLike({json.dumps(payload_select)}),
          insertWriteLike: gate.isWriteLike({json.dumps(payload_insert)}),
        }};
        process.stdout.write(JSON.stringify(results));
        """
    )

    completed = subprocess.run(
        [NODE, '-e', script],
        cwd='.',
        text=True,
        capture_output=True,
        check=True,
    )

    results = json.loads(completed.stdout)

    assert 'Plan:' not in results['select']
    assert 'Staging:' not in results['select']
    assert 'Validation:' not in results['select']
    assert 'Rollback:' not in results['select']
    assert 'Summary block.' in results['select']
    assert 'Thanks.' in results['select']

    assert results['selectWriteLike'] is False
    assert results['insertWriteLike'] is True

    assert 'Plan:' in results['insert']
    assert 'Rollback:' in results['insert']
