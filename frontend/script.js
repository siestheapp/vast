const $ = (selector) => document.querySelector(selector);

const state = {
  get baseUrl() {
    return $('#apiBase').value.replace(/\/$/, '');
  }
};

const formatTime = (date = new Date()) =>
  date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

const lastConnected = $('#lastConnected');
const lastResponse = $('#lastResponse');
const envLabel = $('#envLabel');
let typingBubble = null;
let lastConnLabel = null; // Preserve last known friendly DB label

const setStatus = (variant, message) => {
  const node = $('#healthStatus');
  node.className = 'status-pill status-idle';
  node.textContent = message;
  if (variant === 'ok') {
    node.classList.add('status-ok');
  } else if (variant === 'error') {
    node.classList.add('status-error');
  }
};

const renderJSON = (node, value) => {
  node.textContent = JSON.stringify(value, null, 2);
};

const parseJSONField = (value) => {
  if (!value.trim()) return {};
  try {
    return JSON.parse(value);
  } catch (err) {
    throw new Error('Invalid JSON: ' + err.message);
  }
};

const handleError = (target, error) => {
  console.error(error);
  renderJSON(target, { error: error.message || String(error) });
};

const request = async (path, options = {}) => {
  const response = await fetch(`${state.baseUrl}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    const message = detail.detail || response.statusText;
    throw new Error(message);
  }
  return response.json();
};

// --- Chat UI ---
const renderMarkdown = (md) => DOMPurify.sanitize(marked.parse(md || ''));

const appendBubble = (role, text) => {
  const wrap = document.createElement('div');
  wrap.className = `bubble ${role === 'user' ? 'user' : 'assistant'}`;
  const html = role === 'assistant' ? renderMarkdown(text) : DOMPurify.sanitize(text || '');
  wrap.innerHTML = `<div class="content">${html}</div>`;
  wrap.dataset.meta = `${role === 'user' ? 'You' : 'VAST'} • ${formatTime()}`;
  $('#chatMessages').appendChild(wrap);
  $('#chatMessages').scrollTop = $('#chatMessages').scrollHeight;
  return wrap;
};

// Split Markdown into chunks ONLY when we're NOT inside code fences.
const splitMarkdownOutsideCodeFences = (md) => {
  if (!md) return [];
  const lines = md.split(/\r?\n/);
  const chunks = [];
  let buf = [];
  let inFence = false;
  let fenceChar = null; // ``` or ~~~

  const shouldSplitBefore = (prevLine, currLine) => {
    const isPrevBlank = !prevLine || /^\s*$/.test(prevLine);
    const startsHeading = /^#{1,6}\s/.test(currLine);
    const startsUl = /^[-*]\s+/.test(currLine);
    const startsOl = /^(?:\d+)\.\s+/.test(currLine);
    return isPrevBlank && (startsHeading || startsUl || startsOl);
  };

  let prevLine = "";
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const fenceMatch = /^(\s*)(`{3,}|~{3,})(.*)$/.exec(line);
    if (fenceMatch) {
      if (!inFence) {
        inFence = true;
        fenceChar = fenceMatch[2][0]; // ` or ~
      } else {
        const thisFenceChar = fenceMatch[2][0];
        if (thisFenceChar === fenceChar) {
          inFence = false;
          fenceChar = null;
        }
      }
      buf.push(line);
      prevLine = line;
      continue;
    }

    if (!inFence && shouldSplitBefore(prevLine, line) && buf.length) {
      chunks.push(buf.join("\n"));
      buf = [];
    }
    buf.push(line);
    prevLine = line;
  }
  if (buf.length) chunks.push(buf.join("\n"));
  return chunks;
};

const renderAssistant = (md) => {
  const chunks = splitMarkdownOutsideCodeFences(md || "");
  if (chunks.length <= 1) return appendBubble('assistant', md);
  let last;
  chunks.forEach((c) => {
    if (!c.trim()) return;
    last = appendBubble('assistant', c.trim());
  });
  return last;
};

const attachBreadcrumbChip = (bubble, breadcrumbs) => {
  if (!bubble || !breadcrumbs || typeof breadcrumbs !== 'object') return;
  const content = bubble.querySelector('.content') || bubble;
  content.querySelectorAll('.breadcrumbs-chip').forEach((node) => node.remove());
  const chip = document.createElement('div');
  chip.className = 'breadcrumbs-chip';
  const parts = [];
  if (typeof breadcrumbs.deterministic === 'boolean') {
    parts.push(breadcrumbs.deterministic ? 'Deterministic' : 'LLM');
  }
  if (breadcrumbs.rule) {
    parts.push(`rule=${breadcrumbs.rule}`);
  }
  if (typeof breadcrumbs.llm_ms === 'number') {
    parts.push(`llm_ms=${breadcrumbs.llm_ms}`);
  }
  if (!parts.length) return;
  chip.textContent = parts.join(' • ');
  content.appendChild(chip);
};

const renderReadResult = (bubble, payload) => {
  if (!bubble || !payload || payload.intent !== 'read') return;
  const renderer = window.VASTReadRenderer;
  if (!renderer || typeof renderer.buildReadResultHtml !== 'function') return;
  const html = renderer.buildReadResultHtml(payload);
  if (!html) return;
  const content = bubble.querySelector('.content') || bubble;
  content.insertAdjacentHTML('beforeend', html);
};

const decorateAssistantBubble = (bubble, payload) => {
  if (!bubble) return;
  if (payload && payload.intent) {
    bubble.dataset.intent = payload.intent;
  }
  if (bubble && payload && typeof payload.ui_force_plan === 'boolean') {
    bubble.dataset.uiForcePlan = String(!!payload.ui_force_plan);
  } else if (bubble && bubble.dataset.uiForcePlan) {
    delete bubble.dataset.uiForcePlan;
  }
  if (payload) {
    renderReadResult(bubble, payload);
    attachBreadcrumbChip(bubble, payload.breadcrumbs);
  }
};

const showTypingIndicator = () => {
  if (typingBubble) return typingBubble;
  const wrap = document.createElement('div');
  wrap.className = 'bubble assistant typing';
  wrap.innerHTML = '<span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span>';
  wrap.dataset.meta = 'VAST • thinking';
  $('#chatMessages').appendChild(wrap);
  $('#chatMessages').scrollTop = $('#chatMessages').scrollHeight;
  typingBubble = wrap;
  return wrap;
};

const hideTypingIndicator = () => {
  if (!typingBubble) return;
  typingBubble.remove();
  typingBubble = null;
};

const sendChat = async () => {
  const input = $('#chatText');
  const msg = input.value.trim();
  if (!msg) return;
  appendBubble('user', msg);
  input.value = '';
  showTypingIndicator();
  try {
    const data = await request('/conversations/process', {
      method: 'POST',
      body: JSON.stringify({ message: msg, auto_execute: true, allow_writes: false }),
    });
    hideTypingIndicator();
    const bubble = renderAssistant(data.response || '');
    decorateAssistantBubble(bubble, data);
    if (lastResponse) {
      lastResponse.textContent = formatTime();
    }
  } catch (err) {
    hideTypingIndicator();
    appendBubble('assistant', `Error: ${err.message || String(err)}`);
  }
};

$('#chatSend').addEventListener('click', sendChat);
$('#chatText').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendChat();
  }
});

$('#checkHealth').addEventListener('click', async () => {
  setStatus('idle', 'Checking…');
  try {
    const data = await request('/health', { method: 'GET' });
    const env = data.environment?.vast_env || 'unset';
    setStatus('ok', `Connected · ${env}`);
    if (envLabel) envLabel.textContent = env;
    if (lastConnected) lastConnected.textContent = formatTime();
  } catch (err) {
    setStatus('error', 'Connection error');
    console.error(err);
  }
});

async function fetchHealthFull() {
  const baseInput = document.getElementById('apiBase');
  const base = baseInput ? baseInput.value : 'http://localhost:8000';
  try {
    const response = await fetch(`${base.replace(/\/$/, '')}/health/full`, { cache: 'no-store' });
    const payload = await response.json();
    const apiOk = !!(payload && payload.api_ok);
    const dbOk = !!(payload && payload.db_ok);
    const db = (payload && payload.db) || {};

    const pill = document.getElementById('healthStatus');
    const banner = document.getElementById('dbBanner');
    if (!pill || !banner) console.warn('Missing #healthStatus or #dbBanner');

    // Pill reflects API transport health only
    pill.classList.toggle('status-ok', apiOk);
    pill.classList.toggle('status-bad', !apiOk);
    pill.textContent = apiOk ? 'CONNECTED · DEV' : 'DISCONNECTED';
    pill.title = apiOk ? `${db.user || ''}@${db.host || ''}/${db.database || ''}` : (payload && payload.error ? String(payload.error) : '');

    const friendlyNow = db.project_name || db.project_ref || db.database || null;
    if (friendlyNow) {
      lastConnLabel = friendlyNow;
    }
    const dbSub = dbOk ? 'DB: OK' : 'DB: unstable';
    const label = lastConnLabel || friendlyNow || 'Unknown';
    banner.textContent = `Connected to: ${label} • ${dbSub}`;
    banner.title = apiOk ? `${db.user || 'unknown'}@${db.host || 'unknown'}/${db.database || 'unknown'}` : (payload && payload.error ? String(payload.error) : '');
  } catch (error) {
    const pill = document.getElementById('healthStatus');
    const banner = document.getElementById('dbBanner');
    if (!pill || !banner) console.warn('Missing #healthStatus or #dbBanner');

    pill.classList.toggle('status-ok', false);
    pill.classList.toggle('status-bad', true);
    pill.textContent = 'DISCONNECTED';
    pill.title = error && error.message ? error.message : '';
    const label = lastConnLabel || 'Unknown';
    banner.textContent = `Connected to: ${label} • DB: unstable`;
    banner.title = error && error.message ? error.message : '';
    console.error(error);
  }
}

document.getElementById('checkHealth').addEventListener('click', fetchHealthFull);
window.addEventListener('DOMContentLoaded', fetchHealthFull);
// Debounce health polling: refresh every ~20s
setInterval(fetchHealthFull, 20000);

$('#askSubmit').addEventListener('click', async () => {
  const output = $('#askResult');
  output.textContent = 'Thinking…';
  try {
    const message = $('#askInput').value;
    const data = await request('/conversations/process', {
      method: 'POST',
      body: JSON.stringify({ message, auto_execute: true, allow_writes: false }),
    });
    output.textContent = data.response || '';
    if (lastResponse) {
      lastResponse.textContent = formatTime();
    }
  } catch (err) {
    handleError(output, err);
  }
});

$('#sqlSubmit').addEventListener('click', async () => {
  const output = $('#sqlResult');
  output.textContent = 'Executing…';
  try {
    const payload = {
      sql: $('#sqlInput').value,
      params: parseJSONField($('#sqlParams').value || '{}'),
      allow_writes: $('#sqlAllowWrites').checked,
      force_write: $('#sqlForceWrite').checked,
    };
    const data = await request('/sql/run', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    renderJSON(output, data);
    if (lastResponse) {
      lastResponse.textContent = formatTime();
    }
  } catch (err) {
    handleError(output, err);
  }
});

$('#loadTables').addEventListener('click', async () => {
  const list = $('#tablesList');
  list.innerHTML = '<li>Loading…</li>';
  try {
    const data = await request('/schema/tables', { method: 'GET' });
    list.innerHTML = '';
    data.tables.forEach((t) => {
      const li = document.createElement('li');
      li.textContent = `${t.table_schema}.${t.table_name}`;
      li.addEventListener('click', () => loadColumns(t.table_schema, t.table_name));
      list.appendChild(li);
    });
  } catch (err) {
    list.innerHTML = '<li>Error loading tables</li>';
    console.error(err);
  }
});

const loadColumns = async (schema, table) => {
  const view = $('#columnsView');
  view.textContent = `Loading columns for ${schema}.${table}…`;
  try {
    const data = await request(`/schema/tables/${encodeURIComponent(schema)}/${encodeURIComponent(table)}`, { method: 'GET' });
    renderJSON(view, data.columns);
  } catch (err) {
    handleError(view, err);
  }
};

$('#loadArtifacts').addEventListener('click', async () => {
  const list = $('#artifactsList');
  list.innerHTML = '<li>Loading…</li>';
  try {
    const data = await request('/artifacts', { method: 'GET' });
    list.innerHTML = '';
    data.artifacts.forEach((path) => {
      const li = document.createElement('li');
      li.textContent = path;
      list.appendChild(li);
    });
    if (!data.artifacts.length) {
      list.innerHTML = '<li>No artifacts yet</li>';
    }
  } catch (err) {
    list.innerHTML = '<li>Error loading artifacts</li>';
    console.error(err);
  }
});
