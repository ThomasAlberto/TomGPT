const API = 'http://localhost:8000';
let currentId = null;
let convMap   = {};
let folderMap = {};
let folderKbStatus = {};
let pendingFiles = [];

marked.setOptions({ breaks: true });

// ── Helpers ────────────────────────────────────────────────────────────────
const h    = () => ({ 'Content-Type': 'application/json' });
const esc  = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
const scrollDown = (force = false) => {
  const a = document.getElementById('chat-area');
  if (force || a.scrollHeight - a.scrollTop - a.clientHeight < 100) {
    a.scrollTop = a.scrollHeight;
  }
};

function estimateTokens(text) {
  if (!text) return 0;
  return Math.ceil(text.length / 4);
}

function formatTokens(n) {
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return String(n);
}

function updateTokenBar(messages) {
  const bar = document.getElementById('token-bar');
  const info = getModelInfo(getModel());
  if (!info || !messages) { bar.style.display = 'none'; return; }

  // Show bar immediately with estimate, then refine with exact count
  const contextWindow = info.context || 200000;
  let totalChars = 0;
  for (const m of messages) {
    totalChars += (m.content || '').length;
    if (m.thinking) totalChars += (typeof m.thinking === 'string' ? m.thinking : m.thinking.text || '').length;
    if (m.pro_initial) totalChars += m.pro_initial.length;
    if (m.pro_critique) totalChars += m.pro_critique.length;
  }
  const estTokens = Math.ceil(totalChars / 4);
  _renderTokenBar(estTokens, contextWindow, info, '~');
  bar.style.display = '';

  // Fetch exact count from backend
  if (currentId) {
    fetch(`${API}/token-count`, {
      method: 'POST', headers: h(),
      body: JSON.stringify({ conversation_id: currentId, model: getModel() })
    })
    .then(r => r.ok ? r.json() : null)
    .then(data => {
      if (data) _renderTokenBar(data.token_count, contextWindow, info, '');
    })
    .catch(() => {});
  }
}

function _renderTokenBar(tokens, contextWindow, info, prefix) {
  const pct = Math.min((tokens / contextWindow) * 100, 100);

  document.getElementById('token-memory').textContent =
    `Memory: ${prefix}${formatTokens(tokens)} / ${formatTokens(contextWindow)}`;

  const mode = document.getElementById('mode-select').value;
  const inputMultiplier = mode === 'pro' ? 2 : 1;
  const inputPricePerToken = info.input_price / 1000000;
  const historyCost = tokens * inputPricePerToken * inputMultiplier;
  const modeLabel = mode === 'pro' ? ' (2x Pro)' : '';
  document.getElementById('token-cost').textContent =
    `Next msg cost: ${prefix}$${historyCost < 0.01 ? historyCost.toFixed(4) : historyCost.toFixed(2)}${modeLabel}`;

  const fill = document.getElementById('token-meter-fill');
  fill.style.width = pct + '%';
  fill.style.background = pct > 75 ? '#ff6b6b' : pct > 50 ? '#f0a030' : 'var(--accent)';
}

let _currentMessages = null;

function showToast(msg) {
  let toast = document.getElementById('toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'toast';
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.classList.add('visible');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => toast.classList.remove('visible'), 4000);
}

function getDefaultSendAction() {
  return localStorage.getItem('defaultSendAction') || 'send';
}

function setDefaultSendAction(action) {
  localStorage.setItem('defaultSendAction', action);
  updateSendButtonStyles();
}

function updateSendButtonStyles() {
  const def = getDefaultSendAction();
  const sendBtn = document.getElementById('send-btn');
  const batchBtn = document.getElementById('batch-btn');
  if (def === 'batch') {
    sendBtn.classList.add('send-secondary');
    sendBtn.classList.remove('send-primary');
    batchBtn.classList.add('send-primary');
    batchBtn.classList.remove('send-secondary');
  } else {
    sendBtn.classList.add('send-primary');
    sendBtn.classList.remove('send-secondary');
    batchBtn.classList.add('send-secondary');
    batchBtn.classList.remove('send-primary');
  }
}

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    if (getDefaultSendAction() === 'batch') sendBatch(); else send();
  }
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}

function getModel() { return document.getElementById('model-select').value; }

// ── Theme ──────────────────────────────────────────────────────────────────
const THEMES = [
  { id: 'default', label: 'Default' },
  { id: 'lydia',   label: 'Lydia' },
];

function getStoredTheme() {
  let theme = localStorage.getItem('theme') || 'default';
  if (theme === 'night') { theme = 'default'; localStorage.setItem('theme', 'default'); }
  return theme;
}

function getStoredMode() {
  return localStorage.getItem('mode') || 'day';
}

function initThemeSelect() {
  const select = document.getElementById('theme-select');
  if (!select) return;
  select.innerHTML = THEMES.map(t =>
    `<option value="${t.id}">${t.label}</option>`
  ).join('');
  select.value = getStoredTheme();
}

function applyTheme(theme) {
  const mode = getStoredMode();
  document.documentElement.setAttribute('data-theme', theme);
  document.documentElement.setAttribute('data-mode', mode);
  localStorage.setItem('theme', theme);
  const select = document.getElementById('theme-select');
  if (select && select.value !== theme) select.value = theme;
  const toggle = document.getElementById('mode-toggle');
  if (toggle) toggle.setAttribute('data-mode', mode);
}

function applyMode(mode) {
  const theme = getStoredTheme();
  document.documentElement.setAttribute('data-mode', mode);
  localStorage.setItem('mode', mode);
  const toggle = document.getElementById('mode-toggle');
  if (toggle) toggle.setAttribute('data-mode', mode);
}

function toggleMode() {
  const mode = getStoredMode() === 'day' ? 'night' : 'day';
  applyMode(mode);
}

initThemeSelect();
applyTheme(getStoredTheme());
applyMode(getStoredMode());

let modelList = [];

async function loadModels() {
  try {
  const res = await fetch(`${API}/models`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  modelList = await res.json();

  const select = document.getElementById('model-select');
  const providers = {};
  modelList.forEach(m => {
    if (!providers[m.provider]) providers[m.provider] = [];
    providers[m.provider].push(m);
  });

  select.innerHTML = '';
  for (const [provider, models] of Object.entries(providers)) {
    const group = document.createElement('optgroup');
    group.label = models[0].maker;
    models.forEach(m => {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.name;
      if (m.id === 'claude-sonnet-4-6') opt.selected = true;
      group.appendChild(opt);
    });
    select.appendChild(group);
  }
  updateModeSelect();
  } catch (err) {
    console.error('Failed to load models:', err);
    showToast('Failed to load models. Is the server running?');
  }
}

function getModelInfo(modelId) {
  return modelList.find(m => m.id === modelId);
}

function initPricingTooltip() {
  const select = document.getElementById('model-select');
  const tooltip = document.createElement('div');
  tooltip.id = 'model-tooltip';
  document.body.appendChild(tooltip);

  function showTooltip() {
    const info = getModelInfo(select.value);
    if (!info) { tooltip.style.display = 'none'; return; }
    const ctx = info.context >= 1000000 ? `${(info.context / 1000000).toFixed(1)}M` : `${Math.round(info.context / 1000)}K`;
    tooltip.innerHTML = `<strong>${esc(info.name)}</strong><br>Context: ${ctx} tokens<br>Input: $${info.input_price.toFixed(2)} / 1M tokens<br>Output: $${info.output_price.toFixed(2)} / 1M tokens`;
    const rect = select.getBoundingClientRect();
    tooltip.style.left = rect.left + 'px';
    tooltip.style.top = (rect.bottom + 6) + 'px';
    tooltip.style.display = 'block';
  }

  select.addEventListener('mouseenter', showTooltip);
  select.addEventListener('focus', showTooltip);
  select.addEventListener('change', showTooltip);
  select.addEventListener('mouseleave', () => { tooltip.style.display = 'none'; });
  select.addEventListener('blur', () => { tooltip.style.display = 'none'; });
}

function isFolderCollapsed(fid) {
  const state = JSON.parse(localStorage.getItem('folderState') || '{}');
  return state[fid] === true;
}

function toggleFolderCollapse(fid) {
  const state = JSON.parse(localStorage.getItem('folderState') || '{}');
  state[fid] = !state[fid];
  localStorage.setItem('folderState', JSON.stringify(state));
  renderSidebar();
}

// ── File handling ──────────────────────────────────────────────────────────
function onFilesSelected(input) {
  for (const f of input.files) pendingFiles.push(f);
  input.value = '';
  renderFileChips();
}

function removeFile(idx) {
  pendingFiles.splice(idx, 1);
  renderFileChips();
}

function renderFileChips() {
  const el = document.getElementById('file-chips');
  if (!pendingFiles.length) { el.innerHTML = ''; return; }
  el.innerHTML = pendingFiles.map((f, i) => `
    <span class="file-chip">
      <span class="file-chip-icon">${f.name.endsWith('.pdf') ? '📄' : '📝'}</span>
      <span class="file-chip-name">${esc(f.name)}</span>
      <button class="file-chip-remove" onclick="removeFile(${i})">✕</button>
    </span>
  `).join('');
}

async function uploadFiles() {
  const results = await Promise.allSettled(
    pendingFiles.map(async (f) => {
      const form = new FormData();
      form.append('file', f);
      const res = await fetch(`${API}/upload`, { method: 'POST', body: form });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    })
  );

  const refs = [];
  const failed = [];
  results.forEach((result, i) => {
    if (result.status === 'fulfilled') {
      refs.push({ file_id: result.value.file_id, filename: result.value.filename });
    } else {
      console.error(`Failed to upload ${pendingFiles[i].name}:`, result.reason);
      failed.push(pendingFiles[i].name);
    }
  });

  if (failed.length) showToast(`Failed to upload: ${failed.join(', ')}`);

  pendingFiles = [];
  renderFileChips();
  return refs;
}

// ── Model selection ────────────────────────────────────────────────────────
async function onModelChange() {
  if (!currentId) return;
  try {
    const model = getModel();
    const res = await fetch(`${API}/conversations/${currentId}/model`, {
      method: 'PATCH', headers: h(),
      body: JSON.stringify({ model })
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    updateModeSelect();
    if (_currentMessages) updateTokenBar(_currentMessages);
  } catch (err) {
    console.error('Failed to update model:', err);
    showToast('Failed to update model.');
  }
}

function updateModeSelect() {
  const info = getModelInfo(getModel());
  const select = document.getElementById('mode-select');
  const previous = select.value;
  let html = '<option value="standard">Standard</option>';
  if (info?.thinking) {
    html += '<option value="thinking">Thinking</option>';
  }
  html += '<option value="pro">Pro</option>';
  select.innerHTML = html;
  if (select.querySelector(`option[value="${previous}"]`)) {
    select.value = previous;
  } else {
    select.value = 'standard';
    // Sync to backend if mode was invalidated by model change
    if (previous !== 'standard' && currentId) {
      fetch(`${API}/conversations/${currentId}/mode`, {
        method: 'PATCH', headers: h(),
        body: JSON.stringify({ mode: 'standard', thinking_budget: 8000 })
      }).catch(err => console.error('Failed to sync mode:', err));
    }
  }
}

async function onModeChange() {
  if (!currentId) return;
  const select = document.getElementById('mode-select');
  const mode = select.value;
  if (mode === 'thinking') {
    if (!confirm('Thinking can multiply API costs by 5\u201310\u00d7, as thinking tokens are billed at the full output rate.\n\nContinue?')) {
      select.value = 'standard'; return;
    }
  } else if (mode === 'pro') {
    if (!confirm('Pro mode makes 3 API calls per message (\u22483\u00d7 cost).\n\nContinue?')) {
      select.value = 'standard'; return;
    }
  }
  try {
    const res = await fetch(`${API}/conversations/${currentId}/mode`, {
      method: 'PATCH', headers: h(),
      body: JSON.stringify({ mode, thinking_budget: 8000 })
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
  } catch (err) {
    console.error('Failed to update mode:', err);
    showToast('Failed to update mode.');
  }
  if (_currentMessages) updateTokenBar(_currentMessages);
}

// ── Folders ────────────────────────────────────────────────────────────────
async function loadFolders() {
  try {
    const res = await fetch(`${API}/folders`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const list = await res.json();
    folderMap = {};
    list.forEach(f => folderMap[f.id] = f);
    await loadFolderKbStatus();
  } catch (err) {
    console.error('Failed to load folders:', err);
  }
}

async function loadFolderKbStatus() {
  folderKbStatus = {};
  const ids = Object.keys(folderMap);
  await Promise.all(ids.map(async (fid) => {
    try {
      const res = await fetch(`${API}/folders/${fid}/kb`);
      if (res.ok) {
        const docs = await res.json();
        if (docs.length > 0) folderKbStatus[fid] = docs.length;
      }
    } catch { /* ignore */ }
  }));
}

async function newFolder(parentId) {
  try {
    const res = await fetch(`${API}/folders`, {
      method: 'POST', headers: h(),
      body: JSON.stringify({ name: 'New Folder', parent_id: parentId || null })
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    await loadFolders();
    renderSidebar();
    startFolderRename(data.id);
  } catch (err) {
    console.error('Failed to create folder:', err);
    showToast('Failed to create folder.');
  }
}

function startFolderRename(fid) {
  const el = document.querySelector(`.folder-name[data-fid="${fid}"]`);
  if (!el) return;
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'folder-rename-input';
  input.value = folderMap[fid]?.name || '';
  input.onblur = () => finishFolderRename(fid, input.value);
  input.onkeydown = (e) => {
    if (e.key === 'Enter') input.blur();
    if (e.key === 'Escape') { input.value = folderMap[fid]?.name || ''; input.blur(); }
  };
  el.replaceWith(input);
  input.focus();
  input.select();
}

async function finishFolderRename(fid, name) {
  name = name.trim();
  if (name && name !== folderMap[fid]?.name) {
    try {
      const res = await fetch(`${API}/folders/${fid}`, {
        method: 'PATCH', headers: h(),
        body: JSON.stringify({ name })
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await loadFolders();
    } catch (err) {
      console.error('Failed to rename folder:', err);
      showToast('Failed to rename folder.');
    }
  }
  renderSidebar();
}

async function delFolder(fid) {
  const name = folderMap[fid]?.name || 'this folder';
  if (!confirm(`Delete folder "${name}"? Conversations inside will be moved to the top level.`)) return;
  try {
    const res = await fetch(`${API}/folders/${fid}`, { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await loadFolders();
    await loadList();
  } catch (err) {
    console.error('Failed to delete folder:', err);
    showToast('Failed to delete folder.');
  }
}

async function moveConvToFolder(convId, folderId) {
  try {
    const res = await fetch(`${API}/conversations/${convId}/folder`, {
      method: 'PATCH', headers: h(),
      body: JSON.stringify({ folder_id: folderId })
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (convMap[convId]) convMap[convId].folder_id = folderId;
    renderSidebar();
  } catch (err) {
    console.error('Failed to move conversation:', err);
    showToast('Failed to move conversation.');
  }
}

function buildFolderMenu(convId) {
  const folders = Object.values(folderMap);
  const currentFolderId = convMap[convId]?.folder_id;
  let html = '<div class="folder-menu">';
  html += `<div class="folder-menu-item ${!currentFolderId ? 'active' : ''}" onclick="event.stopPropagation(); moveConvToFolder('${convId}', null); closeFolderMenus()">No folder</div>`;
  folders.forEach(f => {
    html += `<div class="folder-menu-item ${f.id === currentFolderId ? 'active' : ''}" onclick="event.stopPropagation(); moveConvToFolder('${convId}', '${f.id}'); closeFolderMenus()">${esc(f.name)}</div>`;
  });
  html += '</div>';
  return html;
}

function toggleFolderMenu(convId, event) {
  event.stopPropagation();
  const existing = document.querySelector('.folder-menu');
  if (existing) { existing.remove(); return; }
  const btn = event.currentTarget;
  const menu = document.createElement('div');
  menu.innerHTML = buildFolderMenu(convId);
  const menuEl = menu.firstElementChild;
  btn.parentElement.appendChild(menuEl);
}

function closeFolderMenus() {
  document.querySelectorAll('.folder-menu').forEach(m => m.remove());
}

document.addEventListener('click', closeFolderMenus);

// ── Knowledge Base ────────────────────────────────────────────────────────
let kbFolderId = null;

function openKbModal(fid) {
  kbFolderId = fid;
  const name = folderMap[fid]?.name || 'Folder';
  document.getElementById('kb-title').textContent = `${name} — Knowledge Base`;
  document.getElementById('kb-overlay').classList.add('visible');
  loadKbDocs();
  loadKbChain();
}

function closeKbModal() {
  document.getElementById('kb-overlay').classList.remove('visible');
  kbFolderId = null;
}

async function loadKbDocs() {
  if (!kbFolderId) return;
  try {
    const res = await fetch(`${API}/folders/${kbFolderId}/kb`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const docs = await res.json();
    const el = document.getElementById('kb-docs');
    if (!docs.length) {
      el.innerHTML = '<div class="kb-empty">No documents yet. Upload files to build the knowledge base.</div>';
      return;
    }
    el.innerHTML = docs.map(d => `
      <div class="kb-doc-item">
        <span class="kb-doc-name">${d.filename.endsWith('.pdf') ? '📄' : '📝'} ${esc(d.filename)}</span>
        <span class="kb-doc-chunks">${d.chunks} chunks</span>
        <button class="kb-doc-del" onclick="deleteKbDoc('${esc(d.filename)}')">&#x2715;</button>
      </div>
    `).join('');
  } catch (err) {
    console.error('Failed to load KB docs:', err);
    showToast('Failed to load knowledge base.');
  }
}

async function loadKbChain() {
  if (!kbFolderId) return;
  try {
    const res = await fetch(`${API}/folders/${kbFolderId}/kb/chain`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const chain = await res.json();
    const inherited = chain.filter((f, i) => i > 0 && f.doc_count > 0);
    const section = document.getElementById('kb-chain-section');
    const el = document.getElementById('kb-chain');
    if (!inherited.length) {
      section.style.display = 'none';
      return;
    }
    section.style.display = '';
    el.innerHTML = inherited.map(f =>
      `<div class="kb-chain-item"><span class="kb-chain-folder">${esc(f.name)}</span><span class="kb-chain-count">${f.doc_count} chunks</span></div>`
    ).join('');
  } catch (err) {
    console.error('Failed to load KB chain:', err);
  }
}

function setKbProgress(visible, text, pct, errors) {
  const wrap = document.getElementById('kb-progress');
  const textEl = document.getElementById('kb-progress-text');
  const bar = document.getElementById('kb-progress-bar-inner');
  const errEl = document.getElementById('kb-progress-errors');
  wrap.className = visible ? 'kb-progress-visible' : 'kb-progress-hidden';
  if (textEl) textEl.textContent = text || '';
  if (bar) bar.style.width = (pct || 0) + '%';
  if (errEl) errEl.innerHTML = errors || '';
}

async function uploadKbFiles(input) {
  if (!kbFolderId) return;
  const files = Array.from(input.files);
  input.value = '';
  if (!files.length) return;

  const uploadBtn = document.getElementById('kb-upload-btn');
  uploadBtn.disabled = true;
  const failed = [];
  let completed = 0;

  setKbProgress(true, `Indexing 0 / ${files.length}...`, 0, '');

  for (const file of files) {
    setKbProgress(true, `Indexing ${file.name} (${completed + 1} / ${files.length})...`, (completed / files.length) * 100, '');
    try {
      const form = new FormData();
      form.append('file', file);
      const res = await fetch(`${API}/folders/${kbFolderId}/kb/upload`, { method: 'POST', body: form });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      completed++;
      setKbProgress(true, `Indexed ${file.name} (${data.chunks} chunks)`, (completed / files.length) * 100, '');
    } catch (err) {
      console.error(`KB upload failed for ${file.name}:`, err);
      failed.push({ name: file.name, error: err.message });
      completed++;
    }
  }

  let errHtml = '';
  if (failed.length) {
    errHtml = failed.map(f =>
      `<div class="kb-progress-error">${esc(f.name)}: ${esc(f.error)}</div>`
    ).join('');
  }

  const successCount = files.length - failed.length;
  const summary = failed.length
    ? `Done — ${successCount} indexed, ${failed.length} failed`
    : `Done — ${successCount} document${successCount > 1 ? 's' : ''} indexed`;
  setKbProgress(true, summary, 100, errHtml);

  setTimeout(() => { if (!failed.length) setKbProgress(false, '', 0, ''); }, 3000);

  uploadBtn.disabled = false;
  loadKbDocs();
  loadKbChain();
  renderSidebar();
}

async function deleteKbDoc(filename) {
  if (!kbFolderId) return;
  if (!confirm(`Remove "${filename}" from the knowledge base?`)) return;
  try {
    const res = await fetch(`${API}/folders/${kbFolderId}/kb/${encodeURIComponent(filename)}`, { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    loadKbDocs();
    loadKbChain();
    renderSidebar();
  } catch (err) {
    console.error('Failed to delete KB doc:', err);
    showToast('Failed to delete document.');
  }
}

// ── Conversations list ─────────────────────────────────────────────────────
async function loadList() {
  try {
    const res  = await fetch(`${API}/conversations`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const list = await res.json();
    convMap = {};
    list.forEach(c => convMap[c.id] = c);
    renderSidebar();
  } catch (err) {
    console.error('Failed to load conversations:', err);
    showToast('Failed to load conversations. Is the server running?');
  }
}

let _convClickTimer = null;

function renderConvItem(c) {
  return `
    <div class="conv-item ${c.id === currentId ? 'active' : ''}"
         onclick="onConvClick('${c.id}')"
         ondblclick="onConvDblClick('${c.id}')">
      <span class="conv-title" data-cid="${c.id}">${esc(c.title || 'New Conversation')}</span>
      <div class="conv-actions">
        <button class="move-btn" onclick="toggleFolderMenu('${c.id}', event)" title="Move to folder">&#x21C5;</button>
        <button class="del-btn" onclick="event.stopPropagation(); delConv('${c.id}')">&#x2715;</button>
      </div>
    </div>`;
}

function onConvClick(cid) {
  clearTimeout(_convClickTimer);
  _convClickTimer = setTimeout(() => selectConv(cid), 250);
}

function onConvDblClick(cid) {
  clearTimeout(_convClickTimer);
  startConvRename(cid);
}

function startConvRename(cid) {
  const el = document.querySelector(`.conv-title[data-cid="${cid}"]`);
  if (!el) return;
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'conv-rename-input';
  input.value = convMap[cid]?.title || '';
  input.onblur = () => finishConvRename(cid, input.value);
  input.onkeydown = (e) => {
    if (e.key === 'Enter') input.blur();
    if (e.key === 'Escape') { input.value = convMap[cid]?.title || ''; input.blur(); }
  };
  input.onclick = (e) => e.stopPropagation();
  el.replaceWith(input);
  input.focus();
  input.select();
}

async function finishConvRename(cid, title) {
  title = title.trim();
  if (title && title !== convMap[cid]?.title) {
    try {
      const res = await fetch(`${API}/conversations/${cid}/title`, {
        method: 'PATCH', headers: h(),
        body: JSON.stringify({ title })
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      if (convMap[cid]) convMap[cid].title = title;
    } catch (err) {
      console.error('Failed to rename conversation:', err);
      showToast('Failed to rename conversation.');
    }
  }
  renderSidebar();
}

function renderFolderTree(parentId, depth) {
  const childFolders = Object.values(folderMap)
    .filter(f => (f.parent_id || null) === (parentId || null))
    .sort((a,b) => a.name.localeCompare(b.name));

  const convs = Object.values(convMap)
    .filter(c => (c.folder_id || null) === (parentId || null))
    .sort((a,b) => (b.created_at || '').localeCompare(a.created_at || ''));

  let html = '';

  for (const f of childFolders) {
    const collapsed = isFolderCollapsed(f.id);
    const arrow = collapsed ? '&#9654;' : '&#9660;';
    const pad = depth * 16;

    const hasKb = folderKbStatus[f.id] ? `<span class="kb-indicator" title="${folderKbStatus[f.id]} docs"></span>` : '';

    html += `<div class="folder-row" style="padding-left:${pad}px">
      <span class="folder-toggle" onclick="toggleFolderCollapse('${f.id}')">${arrow}</span>
      <span class="folder-name" data-fid="${f.id}" ondblclick="event.stopPropagation(); startFolderRename('${f.id}')">&#128193; ${esc(f.name)}${hasKb}</span>
      <div class="folder-actions">
        <button class="folder-kb-btn" onclick="event.stopPropagation(); openKbModal('${f.id}')" title="Knowledge base">&#128218;</button>
        <button class="folder-add-btn" onclick="newFolder('${f.id}')" title="Add sub-folder">+</button>
        <button class="del-btn" onclick="delFolder('${f.id}')">&#x2715;</button>
      </div>
    </div>`;

    if (!collapsed) {
      html += `<div class="folder-children">`;
      html += renderFolderTree(f.id, depth + 1);
      html += `</div>`;
    }
  }

  const convPad = depth * 16;
  for (const c of convs) {
    html += `<div style="padding-left:${convPad}px">${renderConvItem(c)}</div>`;
  }

  return html;
}

function renderSidebar() {
  const el = document.getElementById('conv-list');

  const topFolders = Object.values(folderMap).filter(f => !f.parent_id);
  const hasAnyFolders = topFolders.length > 0;

  let html = '';
  html += renderFolderTree(null, 0);

  if (hasAnyFolders) {
    const unfiled = Object.values(convMap).filter(c => !c.folder_id);
    if (unfiled.length > 0) {
      html += '<div class="sidebar-separator"></div>';
    }
  }

  el.innerHTML = html;
}

function showNewConvPopup() {
  const select = document.getElementById('new-conv-folder');
  const folders = Object.values(folderMap).sort((a,b) => a.name.localeCompare(b.name));
  let html = '<option value="">No folder</option>';
  function addOptions(parentId, depth) {
    folders.filter(f => (f.parent_id || null) === (parentId || null))
      .forEach(f => {
        const indent = '\u00A0\u00A0'.repeat(depth);
        html += `<option value="${f.id}">${indent}${esc(f.name)}</option>`;
        addOptions(f.id, depth + 1);
      });
  }
  addOptions(null, 0);
  select.innerHTML = html;
  document.getElementById('new-conv-overlay').classList.add('visible');
  select.focus();
}

function closeNewConvPopup() {
  document.getElementById('new-conv-overlay').classList.remove('visible');
}

async function newConv() {
  const folderId = document.getElementById('new-conv-folder')?.value || null;
  closeNewConvPopup();
  try {
    const res  = await fetch(`${API}/conversations`, {
      method: 'POST', headers: h(),
      body: JSON.stringify({ title: 'New Conversation', folder_id: folderId || null })
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    await loadList();
    selectConv(data.id);
  } catch (err) {
    console.error('Failed to create conversation:', err);
    showToast('Failed to create conversation.');
  }
}

async function selectConv(id) {
  try {
    currentId = id;
    const res  = await fetch(`${API}/conversations/${id}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const conv = await res.json();
    document.getElementById('sp-input').value = conv.system_prompt || '';
    if (conv.model) document.getElementById('model-select').value = conv.model;
    updateModeSelect();
    document.getElementById('mode-select').value = conv.mode || 'standard';

    const area = document.getElementById('chat-area');
    area.innerHTML = '';
    // Stop any existing batch polls
    Object.keys(activeBatchJobs).forEach(stopBatchPoll);

    if (!conv.messages?.length) {
      area.innerHTML = '<div class="placeholder">Start the conversation!</div>';
    } else {
      conv.messages.forEach(m => {
        if (m.batch_job_id && !m.content) return; // skip pending batch placeholders
        const names = (m.files || []).map(f => f.filename);
        addBubble(m.role, m.content, names, m.thinking, m.pro_initial, m.pro_critique);
      });
      // Resume polling for pending batch jobs
      resumeBatchPolls(conv.messages, id);
    }
    _currentMessages = conv.messages || [];
    updateTokenBar(_currentMessages);
    scrollDown(true);
    renderSidebar();
  } catch (err) {
    console.error('Failed to load conversation:', err);
    showToast('Failed to load conversation.');
  }
}

async function delConv(id) {
  const title = convMap[id]?.title || 'this conversation';
  if (!confirm(`Delete "${title}"?`)) return;
  try {
    const res = await fetch(`${API}/conversations/${id}`, { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (currentId === id) {
      currentId = null;
      _currentMessages = null;
      document.getElementById('token-bar').style.display = 'none';
      document.getElementById('chat-area').innerHTML =
        '<div class="placeholder" id="placeholder">Select or create a conversation to begin</div>';
      document.getElementById('sp-input').value = '';
    }
    await loadList();
  } catch (err) {
    console.error('Failed to delete conversation:', err);
    showToast('Failed to delete conversation.');
  }
}

// ── Export ─────────────────────────────────────────────────────────────────
async function exportConv() {
  if (!currentId) { showToast('Select a conversation first.'); return; }
  try {
    const res = await fetch(`${API}/conversations/${currentId}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const conv = await res.json();

    const title = conv.title || 'Conversation';
    let md = `# ${title}\n\n`;

    conv.messages.forEach((m, i) => {
      const label = m.role === 'user' ? 'User' : 'Assistant';
      md += `**${label}:**\n${m.content}\n\n`;
      if (i < conv.messages.length - 1 && m.role === 'assistant') {
        md += '---\n\n';
      }
    });

    const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'conversation';
    const blob = new Blob([md.trimEnd() + '\n'], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${slug}.md`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    console.error('Export failed:', err);
    showToast('Failed to export conversation.');
  }
}

let audioVoices = [];

async function loadAudioVoices() {
  try {
    const res = await fetch(`${API}/audio/voices`);
    if (!res.ok) return;
    audioVoices = await res.json();
  } catch { /* ignore */ }
}

let audioMode = 'conversation';

function exportAudio() {
  if (!currentId) { showToast('Select a conversation first.'); return; }
  const populate = (selectId, defaultVoice) => {
    const sel = document.getElementById(selectId);
    sel.innerHTML = audioVoices.map(v =>
      `<option value="${v.id}" ${v.id === defaultVoice ? 'selected' : ''}>${v.name}</option>`
    ).join('');
  };
  populate('audio-user-voice', 'nova');
  populate('audio-assistant-voice', 'onyx');
  populate('audio-speaker-one', 'nova');
  populate('audio-speaker-two', 'onyx');
  setAudioMode('conversation');
  document.getElementById('audio-overlay').classList.add('visible');
}

function setAudioMode(mode) {
  audioMode = mode;
  document.querySelectorAll('.audio-tab').forEach(t => t.classList.toggle('active', t.dataset.mode === mode));
  document.getElementById('audio-conv-fields').style.display = mode === 'conversation' ? '' : 'none';
  document.getElementById('audio-podcast-fields').style.display = mode === 'podcast' ? '' : 'none';
}

function closeAudioModal() {
  document.getElementById('audio-overlay').classList.remove('visible');
  const player = document.getElementById('audio-player');
  player.pause();
  player.style.display = 'none';
}

async function previewVoice(selectId) {
  const voice = document.getElementById(selectId).value;
  const btns = document.querySelectorAll('.audio-preview-btn');
  btns.forEach(b => b.disabled = true);

  try {
    const player = document.getElementById('audio-player');
    player.src = `${API}/audio/preview/${voice}`;
    player.style.display = 'block';
    await player.play();
  } catch (err) {
    console.error('Preview failed:', err);
    showToast('Failed to play preview.');
  } finally {
    const btns2 = document.querySelectorAll('.audio-preview-btn');
    btns2.forEach(b => b.disabled = false);
  }
}

async function generateAudio() {
  const btn = document.getElementById('audio-generate-btn');
  btn.disabled = true;
  btn.textContent = 'Generating…';

  let url, payload;
  if (audioMode === 'podcast') {
    url = `${API}/conversations/${currentId}/audio/podcast`;
    payload = {
      speaker_one_voice: document.getElementById('audio-speaker-one').value,
      speaker_two_voice: document.getElementById('audio-speaker-two').value,
      format: 'mp3', speed: 1.0,
    };
  } else {
    url = `${API}/conversations/${currentId}/audio`;
    payload = {
      user_voice: document.getElementById('audio-user-voice').value,
      assistant_voice: document.getElementById('audio-assistant-voice').value,
      format: 'mp3', speed: 1.0,
    };
  }

  try {
    const res = await fetch(url, {
      method: 'POST', headers: h(),
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || `HTTP ${res.status}`);
    }

    const blob = await res.blob();
    const disposition = res.headers.get('content-disposition') || '';
    const match = disposition.match(/filename="?([^"]+)"?/);
    const filename = match ? match[1] : 'conversation.mp3';

    const dlUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = dlUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(dlUrl);
    showToast('Audio exported!');
    closeAudioModal();
  } catch (err) {
    console.error('Audio export failed:', err);
    showToast(`Audio failed: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Generate';
  }
}

// ── System prompt ──────────────────────────────────────────────────────────
let promptTemplates = [];
let editingTemplateId = null;

function toggleSP() {
  const body  = document.getElementById('sp-body');
  const arrow = document.getElementById('sp-arrow');
  const open  = body.style.display === 'block';
  body.style.display = open ? 'none' : 'block';
  arrow.classList.toggle('open', !open);
}

async function saveSP() {
  if (!currentId) { alert('Select a conversation first.'); return; }
  try {
    const sp = document.getElementById('sp-input').value;
    const res = await fetch(`${API}/conversations/${currentId}/system-prompt`, {
      method: 'PATCH', headers: h(), body: JSON.stringify({ system_prompt: sp })
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const btn = document.getElementById('sp-save');
    btn.textContent = 'Saved ✓';
    setTimeout(() => btn.textContent = 'Save', 1600);
  } catch (err) {
    console.error('Failed to save system prompt:', err);
    showToast('Failed to save system prompt.');
  }
}

async function loadPromptTemplates() {
  try {
    const res = await fetch(`${API}/prompt-templates`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    promptTemplates = await res.json();
    populateSpTemplateSelect();
  } catch (err) {
    console.error('Failed to load prompt templates:', err);
  }
}

function populateSpTemplateSelect() {
  const select = document.getElementById('sp-template-select');
  if (!select) return;
  const current = select.value;
  let html = '<option value="">Custom (this conversation only)</option>';
  promptTemplates.forEach(t => {
    html += `<option value="${t.id}">${esc(t.name)}</option>`;
  });
  select.innerHTML = html;
  if (current && promptTemplates.some(t => t.id === current)) select.value = current;
}

function onSpTemplateChange() {
  const tid = document.getElementById('sp-template-select').value;
  if (!tid) return;
  const tpl = promptTemplates.find(t => t.id === tid);
  if (tpl) document.getElementById('sp-input').value = tpl.content;
}

function openSpTemplateModal() {
  renderSpTemplateList();
  hideSpTemplateEditor();
  document.getElementById('spt-overlay').classList.add('visible');
}

function closeSpTemplateModal() {
  document.getElementById('spt-overlay').classList.remove('visible');
  editingTemplateId = null;
}

function renderSpTemplateList() {
  const el = document.getElementById('spt-list');
  if (!promptTemplates.length) {
    el.innerHTML = '<div class="spt-empty">No templates yet.</div>';
    return;
  }
  el.innerHTML = promptTemplates.map(t => `
    <div class="spt-item">
      <span class="spt-item-name">${esc(t.name)}</span>
      <div class="spt-item-actions">
        <button class="spt-item-edit" onclick="startEditSpTemplate('${t.id}')">✎</button>
        <button class="spt-item-del" onclick="deleteSpTemplate('${t.id}')">&#x2715;</button>
      </div>
    </div>
  `).join('');
}

function showSpTemplateEditor(name, content) {
  document.getElementById('spt-editor').style.display = 'block';
  document.getElementById('spt-new-btn').style.display = 'none';
  document.getElementById('spt-name').value = name || '';
  document.getElementById('spt-content').value = content || '';
  document.getElementById('spt-name').focus();
}

function hideSpTemplateEditor() {
  document.getElementById('spt-editor').style.display = 'none';
  document.getElementById('spt-new-btn').style.display = 'block';
  editingTemplateId = null;
}

function startNewSpTemplate() {
  editingTemplateId = null;
  showSpTemplateEditor('', '');
}

function startEditSpTemplate(tid) {
  const tpl = promptTemplates.find(t => t.id === tid);
  if (!tpl) return;
  editingTemplateId = tid;
  showSpTemplateEditor(tpl.name, tpl.content);
}

function cancelSpTemplateEdit() {
  hideSpTemplateEditor();
}

async function saveSpTemplate() {
  const name = document.getElementById('spt-name').value.trim();
  const content = document.getElementById('spt-content').value.trim();
  if (!name) { showToast('Template name is required.'); return; }
  if (!content) { showToast('Template content is required.'); return; }

  try {
    let res;
    if (editingTemplateId) {
      res = await fetch(`${API}/prompt-templates/${editingTemplateId}`, {
        method: 'PUT', headers: h(), body: JSON.stringify({ name, content })
      });
    } else {
      res = await fetch(`${API}/prompt-templates`, {
        method: 'POST', headers: h(), body: JSON.stringify({ name, content })
      });
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await loadPromptTemplates();
    hideSpTemplateEditor();
    renderSpTemplateList();
    showToast(editingTemplateId ? 'Template updated.' : 'Template created.');
    editingTemplateId = null;
  } catch (err) {
    console.error('Failed to save template:', err);
    showToast('Failed to save template.');
  }
}

async function deleteSpTemplate(tid) {
  const tpl = promptTemplates.find(t => t.id === tid);
  if (!confirm(`Delete template "${tpl?.name || ''}"?`)) return;
  try {
    const res = await fetch(`${API}/prompt-templates/${tid}`, { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await loadPromptTemplates();
    renderSpTemplateList();
    showToast('Template deleted.');
  } catch (err) {
    console.error('Failed to delete template:', err);
    showToast('Failed to delete template.');
  }
}

// ── Chat ───────────────────────────────────────────────────────────────────
let currentAbortController = null;

async function send() {
  if (!currentId) { alert('Select or create a conversation first.'); return; }
  setDefaultSendAction('send');
  const input = document.getElementById('msg-input');
  const msg   = input.value.trim();
  if (!msg && !pendingFiles.length) return;

  input.value = '';
  autoResize(input);
  document.getElementById('send-btn').style.display = 'none';
  document.getElementById('batch-btn').style.display = 'none';
  document.getElementById('stop-btn').style.display = 'inline-block';
  document.getElementById('placeholder')?.remove();

  let fileRefs = [];
  if (pendingFiles.length) {
    fileRefs = await uploadFiles();
  }

  addBubble('user', msg, fileRefs.map(f => f.filename));
  const bubble = addStreamingBubble();

  currentAbortController = new AbortController();

  try {
    const res = await fetch(`${API}/chat/stream`, {
      method: 'POST', headers: h(),
      body: JSON.stringify({
        conversation_id: currentId, message: msg, model: getModel(), files: fileRefs,
        mode: document.getElementById('mode-select').value,
        thinking_budget: 8000,
      }),
      signal: currentAbortController.signal,
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || `HTTP ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let fullContent = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split('\n\n');
      buffer = parts.pop() || '';

      for (const part of parts) {
        if (!part.startsWith('data: ')) continue;
        try {
          const data = JSON.parse(part.slice(6));
          if (data.type === 'thinking_start') {
            bubble._thinkingEl.style.display = '';
            bubble._thinkingEl.open = true;
          } else if (data.type === 'thinking_chunk') {
            bubble._thinkingEl.querySelector('.thinking-content').textContent +=
              data.content;
            scrollDown();
          } else if (data.type === 'thinking_end') {
            bubble._thinkingEl.open = false;
            bubble._thinkingEl.querySelector('summary').textContent = 'Thinking complete';
          } else if (data.type === 'pro_status') {
            bubble.innerHTML = `<span class="pro-status">${esc(data.message)}</span><span class="cursor">▊</span>`;
            scrollDown();
          } else if (data.type === 'pro_stage') {
            bubble._proContainer.style.display = '';
            const details = document.createElement('details');
            details.className = 'pro-stage-block';
            const summary = document.createElement('summary');
            summary.textContent = data.stage === 'initial' ? 'Initial response' : 'Critique';
            const content = document.createElement('div');
            content.className = 'pro-stage-content';
            content.innerHTML = marked.parse(data.content);
            details.appendChild(summary);
            details.appendChild(content);
            bubble._proContainer.appendChild(details);
            scrollDown();
          } else if (data.type === 'chunk') {
            fullContent += data.content;
            updateStreamingBubble(bubble, fullContent);
          } else if (data.type === 'done') {
            finalizeStreamingBubble(bubble, fullContent);
            if (convMap[currentId]) {
              convMap[currentId].title = data.title;
              renderSidebar();
            }
            if (_currentMessages) {
              _currentMessages.push({role: 'user', content: msg});
              _currentMessages.push({role: 'assistant', content: fullContent});
              updateTokenBar(_currentMessages);
            }
          } else if (data.type === 'error') {
            throw new Error(data.message);
          }
        } catch (e) {
          if (!(e instanceof SyntaxError)) throw e;
          console.warn('Failed to parse SSE data:', part);
        }
      }
    }

    if (bubble.classList.contains('streaming')) {
      finalizeStreamingBubble(bubble, fullContent);
    }

  } catch (err) {
    if (err.name === 'AbortError') {
      finalizeStreamingBubble(bubble, bubble._content || '');
    } else {
      console.error('Chat error:', err);
      finalizeStreamingBubble(bubble, `⚠️ ${err.message || 'Could not reach the server.'}`);
    }
  } finally {
    currentAbortController = null;
    document.getElementById('stop-btn').style.display = 'none';
    document.getElementById('send-btn').style.display = 'inline-block';
    document.getElementById('batch-btn').style.display = 'inline-block';
    document.getElementById('send-btn').disabled = false;
    input.focus();
  }
}

function stopGeneration() {
  if (currentAbortController) currentAbortController.abort();
}

function addBubble(role, content, fileNames, thinking, proInitial, proCritique) {
  const area   = document.getElementById('chat-area');
  const wrap   = document.createElement('div');
  wrap.className = `msg ${role}`;

  if (role === 'user' && fileNames && fileNames.length) {
    const tags = document.createElement('div');
    tags.className = 'file-tags';
    tags.innerHTML = fileNames.map(n =>
      `<span class="file-tag">${n.endsWith('.pdf') ? '📄' : '📝'} ${esc(n)}</span>`
    ).join('');
    wrap.appendChild(tags);
  }

  if (role === 'assistant' && thinking) {
    const details = document.createElement('details');
    details.className = 'thinking-block';
    const summary = document.createElement('summary');
    summary.textContent = 'Thinking';
    const pre = document.createElement('pre');
    pre.className = 'thinking-content';
    pre.textContent = thinking;
    details.appendChild(summary);
    details.appendChild(pre);
    wrap.appendChild(details);
  }

  if (role === 'assistant' && proInitial) {
    const container = document.createElement('div');
    container.className = 'pro-stages';
    [['Initial response', proInitial], ['Critique', proCritique]].forEach(([label, text]) => {
      const d = document.createElement('details');
      d.className = 'pro-stage-block';
      const s = document.createElement('summary');
      s.textContent = label;
      const c = document.createElement('div');
      c.className = 'pro-stage-content';
      c.innerHTML = marked.parse(text);
      d.appendChild(s);
      d.appendChild(c);
      container.appendChild(d);
    });
    wrap.appendChild(container);
  }

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.innerHTML = role === 'assistant' ? marked.parse(content) : esc(content);
  wrap.appendChild(bubble);
  area.appendChild(wrap);
  scrollDown(true);
  return wrap;
}

function addStreamingBubble() {
  const area = document.getElementById('chat-area');
  const wrap = document.createElement('div');
  wrap.className = 'msg assistant';

  const thinkingDetails = document.createElement('details');
  thinkingDetails.className = 'thinking-block';
  thinkingDetails.style.display = 'none';
  thinkingDetails.open = true;
  thinkingDetails.innerHTML = '<summary>Thinking\u2026</summary><pre class="thinking-content"></pre>';
  wrap.appendChild(thinkingDetails);

  const proContainer = document.createElement('div');
  proContainer.className = 'pro-stages';
  proContainer.style.display = 'none';
  wrap.appendChild(proContainer);

  const bubble = document.createElement('div');
  bubble.className = 'bubble streaming';
  bubble._content = '';
  bubble._thinkingEl = thinkingDetails;
  bubble._proContainer = proContainer;
  bubble.innerHTML = '<span class="cursor">▊</span>';
  wrap.appendChild(bubble);
  area.appendChild(wrap);
  scrollDown(true);
  return bubble;
}

function updateStreamingBubble(bubble, content) {
  bubble._content = content;
  bubble.innerHTML = marked.parse(content) + '<span class="cursor">▊</span>';
  scrollDown();
}

function finalizeStreamingBubble(bubble, content) {
  bubble.classList.remove('streaming');
  bubble._content = content;
  bubble.innerHTML = marked.parse(content);
  scrollDown();
}

// ── Batch API ──────────────────────────────────────────────────────────────
let activeBatchJobs = {}; // { jobId: { conversationId, interval } }

async function sendBatch() {
  if (!currentId) { alert('Select or create a conversation first.'); return; }
  setDefaultSendAction('batch');
  const input = document.getElementById('msg-input');
  const msg = input.value.trim();
  if (!msg && !pendingFiles.length) return;

  input.value = '';
  autoResize(input);
  document.getElementById('placeholder')?.remove();

  let fileRefs = [];
  if (pendingFiles.length) fileRefs = await uploadFiles();

  addBubble('user', msg, fileRefs.map(f => f.filename));

  try {
    const res = await fetch(`${API}/batch/submit`, {
      method: 'POST', headers: h(),
      body: JSON.stringify({
        conversation_id: currentId, message: msg, model: getModel(), files: fileRefs,
        mode: document.getElementById('mode-select').value,
        thinking_budget: 8000,
      }),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();

    if (convMap[currentId] && data.title) {
      convMap[currentId].title = data.title;
      renderSidebar();
    }

    addBatchPendingBubble(data.job_id, data.total_steps);
    startBatchPoll(data.job_id, currentId);
  } catch (err) {
    console.error('Batch submit error:', err);
    showToast(`Batch failed: ${err.message}`);
  }
}

function addBatchPendingBubble(jobId, totalSteps) {
  const area = document.getElementById('chat-area');
  const wrap = document.createElement('div');
  wrap.className = 'msg assistant';
  wrap.id = `batch-${jobId}`;

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  const stepLabel = totalSteps > 1 ? ' (Step 1/' + totalSteps + ')' : '';
  bubble.innerHTML = `<div class="batch-pending">
    <span class="batch-dot"></span>
    <span class="batch-text">Batch processing${stepLabel}…</span>
    <button class="batch-cancel" onclick="cancelBatch('${jobId}')">Cancel</button>
  </div>`;
  wrap.appendChild(bubble);
  area.appendChild(wrap);
  scrollDown(true);
}

function startBatchPoll(jobId, conversationId) {
  if (activeBatchJobs[jobId]) return;
  const interval = setInterval(() => pollBatchJob(jobId, conversationId), 30000);
  activeBatchJobs[jobId] = { conversationId, interval };
  // Also poll immediately after a short delay (batch might already be done)
  setTimeout(() => pollBatchJob(jobId, conversationId), 5000);
}

async function pollBatchJob(jobId, conversationId) {
  try {
    const res = await fetch(`${API}/batch/jobs/${jobId}`);
    if (!res.ok) return;
    const data = await res.json();

    // Update pending bubble with step progress
    const pendingEl = document.getElementById(`batch-${jobId}`);
    if (pendingEl && data.status === 'processing' && data.total_steps > 1) {
      const textEl = pendingEl.querySelector('.batch-text');
      if (textEl) textEl.textContent = `Batch processing (Step ${data.current_step + 1}/${data.total_steps})…`;
    }

    if (data.status === 'completed') {
      stopBatchPoll(jobId);
      // Reload the conversation to show the result
      if (currentId === conversationId) {
        await selectConv(conversationId);
      }
      showToast('Batch response ready!');
    } else if (data.status === 'failed') {
      stopBatchPoll(jobId);
      if (pendingEl) {
        const bubble = pendingEl.querySelector('.bubble');
        if (bubble) bubble.innerHTML = `<span style="color:#ff6b6b">Batch failed: ${esc(data.error || 'Unknown error')}</span>`;
      }
      showToast('Batch job failed.');
    }
  } catch (err) {
    console.error('Batch poll error:', err);
  }
}

function stopBatchPoll(jobId) {
  const job = activeBatchJobs[jobId];
  if (job) {
    clearInterval(job.interval);
    delete activeBatchJobs[jobId];
  }
}

async function cancelBatch(jobId) {
  if (!confirm('Cancel this batch job?')) return;
  try {
    const res = await fetch(`${API}/batch/jobs/${jobId}`, { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    stopBatchPoll(jobId);
    const pendingEl = document.getElementById(`batch-${jobId}`);
    if (pendingEl) pendingEl.remove();
    // Reload conversation to clean up
    if (currentId) await selectConv(currentId);
    showToast('Batch cancelled.');
  } catch (err) {
    console.error('Failed to cancel batch:', err);
    showToast('Failed to cancel batch.');
  }
}

// On conversation load, resume polling for any pending batch messages
function resumeBatchPolls(messages, conversationId) {
  for (const m of messages) {
    if (m.batch_job_id && !m.content) {
      addBatchPendingBubble(m.batch_job_id, 1);
      startBatchPoll(m.batch_job_id, conversationId);
    }
  }
}

// ── Init ───────────────────────────────────────────────────────────────────
async function init() {
  await Promise.all([loadModels(), loadList(), loadFolders(), loadPromptTemplates(), loadAudioVoices()]);
  renderSidebar();
  initPricingTooltip();
  updateSendButtonStyles();
}
init();
