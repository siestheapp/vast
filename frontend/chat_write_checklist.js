(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.VASTChatWriteChecklist = factory();
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const WRITE_KINDS = new Set([
    'INSERT',
    'UPDATE',
    'DELETE',
    'MERGE',
    'ALTER',
    'CREATE',
    'DROP',
    'TRUNCATE',
    'GRANT',
    'REVOKE',
  ]);

  const normalizeKind = (value) => {
    if (!value || typeof value !== 'string') {
      return '';
    }
    return value.trim().toUpperCase();
  };

  const isWriteLike = (payload) => {
    const execution = payload && payload.execution;
    if (!execution || typeof execution !== 'object') {
      return false;
    }
    if (execution.write === true) {
      return true;
    }
    const kind = normalizeKind(execution.stmt_kind);
    if (!kind) {
      return false;
    }
    return WRITE_KINDS.has(kind);
  };

  const stripWriteChecklist = (markdown) => {
    if (typeof markdown !== 'string' || !markdown) {
      return typeof markdown === 'string' ? markdown : '';
    }
    const headings = ['Plan:', 'Staging:', 'Validation:', 'Rollback:'];
    const lowerHeadings = headings.map((h) => h.toLowerCase());
    const lines = markdown.split(/\r?\n/);

    const matches = lines
      .map((line, idx) => ({ idx, line: line.trim().toLowerCase() }))
      .filter(({ line }) => lowerHeadings.includes(line));

    if (!matches.length) {
      return markdown;
    }

    const indices = matches.map(({ idx }) => idx);
    const startHeading = Math.min(...indices);
    const endHeading = Math.max(...indices);

    let start = startHeading;
    while (start > 0 && !lines[start - 1].trim()) {
      start -= 1;
    }

    let end = endHeading;
    for (let cursor = endHeading + 1; cursor < lines.length; cursor += 1) {
      const trimmed = lines[cursor].trim();
      if (!trimmed) {
        end = cursor;
        continue;
      }
      if (/^[\-\*\u2022]/.test(trimmed)) {
        end = cursor;
        continue;
      }
      break;
    }

    const remaining = lines.slice(0, start).concat(lines.slice(end + 1));

    while (remaining.length && !remaining[0].trim()) {
      remaining.shift();
    }
    while (remaining.length && !remaining[remaining.length - 1].trim()) {
      remaining.pop();
    }

    const cleaned = [];
    let blankRun = 0;
    for (const line of remaining) {
      if (!line.trim()) {
        blankRun += 1;
        if (blankRun > 1) {
          continue;
        }
      } else {
        blankRun = 0;
      }
      cleaned.push(line);
    }

    return cleaned.join('\n');
  };

  const applyWriteChecklistGate = (markdown, payload) => {
    if (isWriteLike(payload)) {
      return typeof markdown === 'string' ? markdown : '';
    }
    return stripWriteChecklist(markdown);
  };

  return {
    isWriteLike,
    stripWriteChecklist,
    applyWriteChecklistGate,
  };
});
