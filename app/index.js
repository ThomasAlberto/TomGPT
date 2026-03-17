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
const scrollDown = () => {
  const a = document.getElementById('chat-area');
  a.scrollTop = a.scrollHeight;
};

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

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
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
    tooltip.innerHTML = `<strong>${esc(info.name)}</strong><br>Input: $${info.input_price.toFixed(2)} / 1M tokens<br>Output: $${info.output_price.toFixed(2)} / 1M tokens`;
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
  } catch (err) {
    console.error('Failed to update model:', err);
    showToast('Failed to update model.');
  }
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

function renderConvItem(c) {
  return `
    <div class="conv-item ${c.id === currentId ? 'active' : ''}" onclick="selectConv('${c.id}')">
      <span class="conv-title">${esc(c.title || 'New Conversation')}</span>
      <div class="conv-actions">
        <button class="move-btn" onclick="toggleFolderMenu('${c.id}', event)" title="Move to folder">&#x21C5;</button>
        <button class="del-btn" onclick="event.stopPropagation(); delConv('${c.id}')">&#x2715;</button>
      </div>
    </div>`;
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

    const area = document.getElementById('chat-area');
    area.innerHTML = '';
    if (!conv.messages?.length) {
      area.innerHTML = '<div class="placeholder">Start the conversation!</div>';
    } else {
      conv.messages.forEach(m => {
        const names = (m.files || []).map(f => f.filename);
        addBubble(m.role, m.content, names);
      });
    }
    scrollDown();
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
  const input = document.getElementById('msg-input');
  const msg   = input.value.trim();
  if (!msg && !pendingFiles.length) return;

  input.value = '';
  autoResize(input);
  document.getElementById('send-btn').style.display = 'none';
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
      body: JSON.stringify({ conversation_id: currentId, message: msg, model: getModel(), files: fileRefs }),
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
          if (data.type === 'chunk') {
            fullContent += data.content;
            updateStreamingBubble(bubble, fullContent);
          } else if (data.type === 'done') {
            finalizeStreamingBubble(bubble, fullContent);
            if (convMap[currentId]) {
              convMap[currentId].title = data.title;
              renderSidebar();
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
    document.getElementById('send-btn').disabled = false;
    input.focus();
  }
}

function stopGeneration() {
  if (currentAbortController) currentAbortController.abort();
}

function addBubble(role, content, fileNames) {
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

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.innerHTML = role === 'assistant' ? marked.parse(content) : esc(content);
  wrap.appendChild(bubble);
  area.appendChild(wrap);
  scrollDown();
  return wrap;
}

function addStreamingBubble() {
  const area = document.getElementById('chat-area');
  const wrap = document.createElement('div');
  wrap.className = 'msg assistant';
  const bubble = document.createElement('div');
  bubble.className = 'bubble streaming';
  bubble._content = '';
  bubble.innerHTML = '<span class="cursor">▊</span>';
  wrap.appendChild(bubble);
  area.appendChild(wrap);
  scrollDown();
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

// ── Init ───────────────────────────────────────────────────────────────────
async function init() {
  await Promise.all([loadModels(), loadList(), loadFolders(), loadPromptTemplates()]);
  renderSidebar();
  initPricingTooltip();
}
init();
