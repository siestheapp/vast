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

  const maybeRenderExplain = (rows) => {
    if (!Array.isArray(rows) || !rows.length) {
      return null;
    }
    const first = rows[0];
    if (!first || typeof first !== 'object' || !Object.prototype.hasOwnProperty.call(first, 'plan')) {
      return null;
    }
    try {
      const rawPlan = first.plan;
      const parsed = typeof rawPlan === 'string' ? JSON.parse(rawPlan) : rawPlan;
      const rootCandidate = Array.isArray(parsed)
        ? (parsed[0] && (parsed[0].Plan || parsed[0]))
        : (parsed && (parsed.Plan || parsed));
      if (!rootCandidate || typeof rootCandidate !== 'object') {
        return null;
      }

      const flattened = [];
      const visit = (node) => {
        if (!node || typeof node !== 'object') {
          return;
        }
        flattened.push({
          node_type: node['Node Type'] || '',
          sort_key: Array.isArray(node['Sort Key']) ? node['Sort Key'].join(', ') : (node['Sort Key'] || ''),
          plan_rows: node['Plan Rows'] ?? '',
          startup_cost: node['Startup Cost'] ?? '',
          total_cost: node['Total Cost'] ?? '',
        });
        const children = Array.isArray(node.Plans) ? node.Plans : [];
        children.forEach(visit);
      };

      visit(rootCandidate);
      if (!flattened.length) {
        return null;
      }

      const columns = ['node_type', 'sort_key', 'plan_rows', 'startup_cost', 'total_cost'];
      const normalized = flattened.map((row) => columns.map((col) => coerceCell(row[col])));
      return {
        html: buildTableHtml(columns, normalized),
        rowCount: flattened.length,
      };
    } catch (err) {
      return null;
    }
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
    if (kind && !['SELECT', 'EXPLAIN'].includes(kind)) {
      return '';
    }

    const rows = Array.isArray(execution.rows) ? execution.rows : [];
    const columns = deriveColumns(execution, rows);
    let rowCount = typeof execution.row_count === 'number' ? execution.row_count : rows.length;
    const { execMs, engineMs } = extractTimings(execution, payload && payload.meta);

    let tableHtml = '';
    let showEmpty = rowCount === 0 && rows.length === 0;

    let explainView = null;
    if (kind === 'EXPLAIN') {
      explainView = maybeRenderExplain(rows);
    }

    if (explainView) {
      tableHtml = explainView.html;
      showEmpty = false;
      if (typeof explainView.rowCount === 'number') {
        rowCount = explainView.rowCount;
      }
    } else {
      const normalizedRows = normalizeRows(rows, columns);
      tableHtml = normalizedRows.length ? buildTableHtml(columns, normalizedRows) : '';
      showEmpty = rowCount === 0 && rows.length === 0;
    }

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
