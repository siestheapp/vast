const $ = (selector) => document.querySelector(selector);

const state = {
  get baseUrl() {
    return $('#apiBase').value.replace(/\/$/, '');
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
const appendBubble = (role, text) => {
  const wrap = document.createElement('div');
  wrap.className = `bubble ${role}`;
  wrap.textContent = text;
  $('#chatMessages').appendChild(wrap);
  $('#chatMessages').scrollTop = $('#chatMessages').scrollHeight;
};

const sendChat = async () => {
  const input = $('#chatText');
  const msg = input.value.trim();
  if (!msg) return;
  appendBubble('user', msg);
  input.value = '';
  try {
    const data = await request('/conversations/process', {
      method: 'POST',
      body: JSON.stringify({ message: msg, auto_execute: true }),
    });
    appendBubble('assistant', data.response || '');
  } catch (err) {
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
  const statusNode = $('#healthStatus');
  statusNode.textContent = 'Checking…';
  try {
    const data = await request('/health', { method: 'GET' });
    statusNode.textContent = `OK (env=${data.environment.vast_env || 'unset'})`;
  } catch (err) {
    statusNode.textContent = 'Error';
    console.error(err);
  }
});

$('#askSubmit').addEventListener('click', async () => {
  const output = $('#askResult');
  output.textContent = 'Thinking…';
  try {
    const message = $('#askInput').value;
    const data = await request('/conversations/process', {
      method: 'POST',
      body: JSON.stringify({ message, auto_execute: true }),
    });
    output.textContent = data.response || '';
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
