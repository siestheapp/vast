(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.VASTReadRenderer = factory();
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const MAX_URL_DISPLAY = 80;
  const URL_HEAD = 40;
  const URL_TAIL = 30;

  const escapeHtml = (value) => String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');

  const escapeAttr = (value) => escapeHtml(value).replace(/`/g, '&#96;');

  const formatUrlDisplay = (value) => {
    if (value.length <= MAX_URL_DISPLAY) {
      return value;
    }
    return `${value.slice(0, URL_HEAD)}...${value.slice(-URL_TAIL)}`;
  };

  const coerceCell = (value) => {
    if (value === null || value === undefined) {
      return '';
    }
    if (typeof value === 'string') {
      return value;
    }
    if (typeof value === 'number') {
      return Number.isFinite(value) ? value.toString() : String(value);
    }
    if (typeof value === 'boolean') {
      return value ? 'true' : 'false';
    }
    if (value instanceof Date) {
      return value.toISOString();
    }
    if (typeof value === 'object') {
      try {
        return JSON.stringify(value);
      } catch (err) {
        return String(value);
      }
    }
    return String(value);
  };

  const buildResultTableHtml = (payload) => {
    if (!payload || !payload.result || !Array.isArray(payload.result.columns)) {
      return '';
    }

    const columns = payload.result.columns;
    const rows = Array.isArray(payload.result.rows) ? payload.result.rows : [];
    const lowerLinkables = new Set((payload.linkable_columns || []).map((c) => String(c).toLowerCase()));

    const headerHtml = columns
      .map((col) => `<th>${escapeHtml(col)}</th>`)
      .join('');

    const bodyHtml = rows
      .map((row) => {
        const normalizedRow = Array.isArray(row)
          ? row
          : columns.map((col, idx) => (row && typeof row === 'object' ? row[col] : row?.[idx]));
        const cells = normalizedRow.map((value, idx) => {
          const columnName = String(columns[idx] || '').toLowerCase();
          if (lowerLinkables.has(columnName) && typeof value === 'string' && value) {
            const safeHref = escapeAttr(value);
            const display = escapeHtml(formatUrlDisplay(value));
            return `<td><a href="${safeHref}" target="_blank" rel="noopener">${display}</a></td>`;
          }
          const safeValue = escapeHtml(coerceCell(value));
          return `<td>${safeValue}</td>`;
        });
        return `<tr>${cells.join('')}</tr>`;
      })
      .join('');

    return `<table class="result-table"><thead><tr>${headerHtml}</tr></thead><tbody>${bodyHtml}</tbody></table>`;
  };

  const buildMetaHtml = (payload) => {
    const parts = [];
    if (payload && payload.result && typeof payload.result.row_count === 'number') {
      parts.push(`rows=${payload.result.row_count}`);
    }
    if (payload && payload.metrics && typeof payload.metrics.exec_ms === 'number') {
      parts.push(`exec_ms=${payload.metrics.exec_ms}`);
    }
    if (payload && payload.metrics && typeof payload.metrics.engine_ms === 'number') {
      parts.push(`engine_ms=${payload.metrics.engine_ms}`);
    }
    if (!parts.length) {
      return '';
    }
    return `<div class="result-meta">${escapeHtml(parts.join(' • '))}</div>`;
  };

  const buildNotesHtml = (payload) => {
    if (!payload || !Array.isArray(payload.notes) || !payload.notes.length) {
      return '';
    }
    const items = payload.notes
      .map((note) => `<li>${escapeHtml(String(note))}</li>`)
      .join('');
    return `<ul class="result-notes">${items}</ul>`;
  };

  const buildReadResultHtml = (payload) => {
    const table = buildResultTableHtml(payload);
    if (!table) {
      return '';
    }
    const meta = buildMetaHtml(payload);
    const notes = buildNotesHtml(payload);
    return `${table}${meta}${notes}`;
  };

  return {
    buildReadResultHtml,
  };
});
