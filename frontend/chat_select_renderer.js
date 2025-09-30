(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.VASTChatSelectRenderer = factory();
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const URL_PATTERN = /^https?:\/\//i;
  const MAX_ROWS = 20;

  const escapeHtml = (value) => String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');

  const escapeAttr = (value) => escapeHtml(value).replace(/`/g, '&#96;');

  const normalizeKind = (kind) => {
    if (!kind || typeof kind !== 'string') {
      return '';
    }
    return kind.trim().toUpperCase();
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

  const deriveColumns = (execution, rows) => {
    const columns = execution && Array.isArray(execution.columns) ? execution.columns : null;
    if (columns && columns.length) {
      return columns.map((name) => String(name));
    }
    if (!rows || !rows.length) {
      return [];
    }
    const first = rows[0];
    if (first && typeof first === 'object' && !Array.isArray(first)) {
      return Object.keys(first);
    }
    if (Array.isArray(first)) {
      return first.map((_, idx) => `col_${idx + 1}`);
    }
    return ['value'];
  };

  const normalizeRows = (rows, columns) => {
    if (!Array.isArray(rows) || !rows.length || !columns.length) {
      return [];
    }
    return rows.slice(0, MAX_ROWS).map((row) => {
      if (row && typeof row === 'object' && !Array.isArray(row)) {
        return columns.map((col) => coerceCell(row[col]));
      }
      if (Array.isArray(row)) {
        return row.map((cell) => coerceCell(cell));
      }
      return [coerceCell(row)];
    });
  };

  const buildCellHtml = (value) => {
    if (!value) {
      return '<td></td>';
    }
    if (URL_PATTERN.test(value)) {
      const safeHref = escapeAttr(value);
      const safeText = escapeHtml(value);
      return `<td><a href="${safeHref}" target="_blank" rel="noopener">${safeText}</a></td>`;
    }
    return `<td>${escapeHtml(value)}</td>`;
  };

  const buildTableHtml = (columns, rows) => {
    if (!columns.length || !rows.length) {
      return '';
    }
    const header = columns.map((col) => `<th>${escapeHtml(col)}</th>`).join('');
    const body = rows
      .map((row) => `<tr>${row.map((cell) => buildCellHtml(cell)).join('')}</tr>`)
      .join('');
    return `<div class="result-scroll"><table class="result-table"><thead><tr>${header}</tr></thead><tbody>${body}</tbody></table></div>`;
  };

  const buildMetaText = (rowCount, execMs, engineMs) => {
    const parts = [];
    if (typeof rowCount === 'number' && !Number.isNaN(rowCount)) {
      parts.push(`rows=${rowCount}`);
    }
    if (typeof execMs === 'number' && !Number.isNaN(execMs)) {
      parts.push(`exec=${execMs}ms`);
    }
    if (typeof engineMs === 'number' && !Number.isNaN(engineMs)) {
      parts.push(`engine=${engineMs}ms`);
    }
    return parts.join(' • ');
  };

  const extractTimings = (execution, payloadMeta) => {
    const meta = execution && typeof execution === 'object' ? execution.meta : null;
    const execMs = meta && typeof meta.exec_ms === 'number' ? meta.exec_ms : payloadMeta?.exec_ms;
    const engineMs = meta && typeof meta.engine_ms === 'number' ? meta.engine_ms : payloadMeta?.engine_ms;
    return { execMs, engineMs };
  };

  const buildExecutionHtml = (payload) => {
    const execution = payload && payload.execution;
    if (!execution || typeof execution !== 'object') {
      return '';
    }
    const kind = normalizeKind(execution.stmt_kind);
    const isWrite = execution.write === true;
    if (isWrite) {
      return '';
    }
    if (kind && kind !== 'SELECT') {
      return '';
    }

    const rows = Array.isArray(execution.rows) ? execution.rows : [];
    const columns = deriveColumns(execution, rows);
    const normalizedRows = normalizeRows(rows, columns);
    const rowCount = typeof execution.row_count === 'number' ? execution.row_count : rows.length;
    const { execMs, engineMs } = extractTimings(execution, payload && payload.meta);

    const showEmpty = rowCount === 0 && rows.length === 0;
    const tableHtml = normalizedRows.length ? buildTableHtml(columns, normalizedRows) : '';
    const metaText = buildMetaText(rowCount, execMs, engineMs);

    if (!tableHtml && !showEmpty && !metaText) {
      return '';
    }

    const parts = [];
    if (tableHtml) {
      parts.push(tableHtml);
    } else if (showEmpty) {
      parts.push('<div class="result-empty">No rows.</div>');
    }
    if (metaText) {
      parts.push(`<div class="result-meta">${escapeHtml(metaText)}</div>`);
    }

    return parts.length ? `<div class="result-execution">${parts.join('')}</div>` : '';
  };

  return {
    buildExecutionHtml,
  };
});
