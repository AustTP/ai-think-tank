// Director-owned role templates, authored in The House (the directors'/admin's
// private workspace -- the Board dialog). The SEED defines the launch profiles;
// after first seed the DB owns the library. VIEWING is open (the player opens
// the House and is not an agent); only directors + the admin may create/edit/
// delete. Every write performs the action AS the acting authority
// (availableAuthority(): the admin, else the senior-most director), matching
// how big-task delegation already works. Template edits retroactively re-stamp
// every live agent holding that role (their AGENTS.md regenerates).

function templateAuthorId() {
  // Prefer a real admin (Faye) so the House's highest authority authors when
  // present; else any director (availableAuthority's senior-most director).
  // availableAuthority() only returns the admin via its `def` fast-path, so we
  // look for the admin explicitly first.
  const admin = AGENT_ROSTER.find(d => d.isAdmin);
  if (admin) return admin.id;
  const authority = availableAuthority();
  return authority && authority.id;
}

function isTemplateAuthor() {
  const id = templateAuthorId();
  // "Director or admin" structurally: the acting authority is one by
  // construction (availableAuthority only returns admin or senior director).
  // We also accept any agent the server would treat as a director, but the
  // client has no reliable structural-director flag -- so we lean on the
  // server's own _is_director derivation, which is what the gate actually is.
  return !!id;
}

async function fetchTemplates() {
  const res = await apiFetch('/api/templates');
  const data = await res.json();
  return data.templates || {};
}

async function fetchTemplate(role) {
  const res = await apiFetch('/api/templates/' + encodeURIComponent(role));
  return await res.json();
}

// Author a template: upsert an existing role, or create a brand-new one.
// `role` empty means "create a new role template."
function buildTemplatePayload(role, mission, instructions, notes) {
  return {
    role,
    mission: (mission || '').trim(),
    instructions: instructions.map(s => s.trim()).filter(Boolean),
    notes: notes.map(s => s.trim()).filter(Boolean),
  };
}

async function saveTemplate(role, mission, instructions, notes) {
  const author = templateAuthorId();
  if (!author) return { error: 'No director or admin available to author templates.' };
  const res = await agentFetch('/api/templates?requesterId=' + encodeURIComponent(author),
    author, { method: 'POST', body: JSON.stringify(buildTemplatePayload(role, mission, instructions, notes)) });
  return await res.json();
}

async function deleteTemplate(role) {
  const author = templateAuthorId();
  if (!author) return { error: 'No director or admin available.' };
  const res = await agentFetch('/api/templates/' + encodeURIComponent(role) + '?requesterId=' + encodeURIComponent(author),
    author, { method: 'DELETE' });
  return await res.json();
}

// Render the templates editor into #boardTemplatesBody. Called by renderBoard.
async function renderTemplatesEditor() {
  const container = document.getElementById('boardTemplatesBody');
  if (!container) return;
  if (!state.ui || state.ui !== 'board') return; // only refresh inside The House dialog
  if (!isTemplateAuthor()) {
    container.innerHTML = '<div class="empty">Only directors and the admin may author templates. No director or admin is available right now.</div>';
    return;
  }
  container.innerHTML = '<div class="empty">Loading templates...</div>';
  let templates;
  try {
    templates = await fetchTemplates();
  } catch (e) {
    container.innerHTML = '<div class="empty">Could not load templates.</div>';
    return;
  }
  const roles = Object.keys(templates).sort();
  const rows = roles.map(role => {
    const t = templates[role];
    const by = t.updatedBy ? ` &mdash; ${AGENT_ROSTER.find(d => d.id === t.updatedBy)?.name || t.updatedBy}` : '';
    const time = t.updatedAt ? `, ${new Date(t.updatedAt).toLocaleString()}` : '';
    return `<div class="template-row" data-role="${encodeURIComponent(role)}">
      <button class="template-edit" data-role="${encodeURIComponent(role)}">Edit</button>
      <button class="template-delete" data-role="${encodeURIComponent(role)}">Delete</button>
      <strong>${escapeHtml(role)}</strong> <span style="opacity:0.6">(${t.instructions ? t.instructions.length : 0} rule${(t.instructions?.length || 0) === 1 ? '' : 's'})</span>
      <div style="opacity:0.7; font-size:12px;">${escapeHtml((t.mission || '').slice(0, 120))}</div>
      <div style="opacity:0.5; font-size:11px;">${AGENT_ROSTER.some(d => d.role === role) ? 'in use' : 'no live holders'}${by}${time}</div>
    </div>`;
  }).join('');
  container.innerHTML = rows || '<div class="empty">No templates yet.</div>';
  bindTemplateListButtons(container);
  container.appendChild(buildNewTemplateForm(templates));
}

function buildNewTemplateForm(existing) {
  const box = document.createElement('div');
  box.className = 'template-new';
  const roleInput = document.createElement('input');
  roleInput.type = 'text';
  roleInput.id = 'templateNewRole';
  roleInput.placeholder = 'new role name, e.g. Orchard';
  const missionInput = document.createElement('input');
  missionInput.type = 'text';
  missionInput.id = 'templateNewMission';
  missionInput.placeholder = 'one-line mission';
  const instrInput = document.createElement('textarea');
  instrInput.id = 'templateNewInstructions';
  instrInput.rows = 2;
  instrInput.placeholder = 'instructions, one per line';
  const btn = document.createElement('button');
  btn.id = 'templateCreateBtn';
  btn.textContent = 'Create template';
  btn.addEventListener('click', () => handleTemplateCreate(existing));
  box.appendChild(document.createTextNode('Create a new role template: '));
  box.appendChild(roleInput);
  box.appendChild(document.createElement('br'));
  box.appendChild(missionInput);
  box.appendChild(document.createElement('br'));
  box.appendChild(instrInput);
  box.appendChild(document.createElement('br'));
  box.appendChild(btn);
  return box;
}

async function handleTemplateCreate(existing) {
  const role = document.getElementById('templateNewRole').value.trim();
  const mission = document.getElementById('templateNewMission').value.trim();
  const instructions = (document.getElementById('templateNewInstructions').value || '').split('\n');
  if (!role) { alert('Give the new role a name.'); return; }
  if (existing[role]) { alert(`A template for "${role}" already exists -- open it and Edit instead.`); return; }
  const result = await saveTemplate(role, mission, instructions, []);
  if (result.ok) { renderTemplatesEditor(); renderBoard(); }
  else alert(result.error || 'Could not create template.');
}

function openTemplateEditor(role) {
  const prev = document.getElementById('boardTemplatesBody');
  if (prev) prev.classList.add('hidden');
  const editor = document.getElementById('boardTemplateEditor');
  editor.classList.remove('hidden');
  editor.setAttribute('data-role', role);
  // Keep the existing modal tall enough for the editor.
  document.getElementById('boardModal').style.maxHeight = '90vh';

  const filler = document.getElementById('boardTemplateEditorBody');
  filler.innerHTML = '<div class="empty">Loading...</div>';
  fetchTemplate(decodeURIComponent(role)).then(data => {
    const t = data.profile || {};
    filler.innerHTML = `
      <div style="margin-bottom:6px;"><strong>Edit template: ${escapeHtml(data.role || role)}</strong></div>
      <label>Mission</label><br>
      <textarea id="tplMission" style="width:92%; height:52px;">${escapeHtml(t.mission || '')}</textarea><br>
      <label>Instructions (one per line)</label><br>
      <textarea id="tplInstructions" style="width:92%; height:84px;">${escapeHtml((t.instructions || []).join('\\n'))}</textarea><br>
      <label>Notes (one per line)</label><br>
      <textarea id="tplNotes" style="width:92%; height:52px;">${escapeHtml((t.notes || []).join('\\n'))}</textarea>
      <p id="tplStatus" class="meeting-msg" style="opacity:0.75; font-size:12px;">Saving re-stamps every live ${escapeHtml(data.role || role)} as this new mission/instructions.</p>
      <div class="modal-actions">
        <button id="tplSaveBtn">Save &amp; apply to ${escapeHtml(data.role || role)}</button>
        <button id="tplCancelBtn">Back</button>
      </div>`;
    document.getElementById('tplSaveBtn').addEventListener('click', () => handleTemplateSave(role));
    document.getElementById('tplCancelBtn').addEventListener('click', () => {
      document.getElementById('boardTemplateEditor').classList.add('hidden');
      document.getElementById('boardTemplatesBody').classList.remove('hidden');
    });
  }).catch(() => {
    filler.innerHTML = '<div class="empty">Could not load this template.</div>';
  });
}

async function handleTemplateSave(role) {
  const mission = document.getElementById('tplMission').value.trim();
  const instructions = (document.getElementById('tplInstructions').value || '').split('\n');
  const notes = (document.getElementById('tplNotes').value || '').split('\n');
  const statusEl = document.getElementById('tplStatus');
  const btn = document.getElementById('tplSaveBtn');
  btn.disabled = true;
  statusEl.textContent = 'Saving & re-stamping...';
  const result = await saveTemplate(decodeURIComponent(role), mission, instructions, notes);
  if (result.ok) {
    statusEl.textContent = `Saved & applied to ${result.appliedTo.length} live ${decodeURIComponent(role)}(s).`;
    btn.textContent = 'Saved';
    // Refresh the underlying list + activity log visibility.
    renderBoard();
    setTimeout(() => {
      document.getElementById('boardTemplateEditor').classList.add('hidden');
      document.getElementById('boardTemplatesBody').classList.remove('hidden');
      renderTemplatesEditor();
    }, 600);
  } else {
    btn.disabled = false;
    statusEl.textContent = result.error || 'Could not save.';
  }
}

async function handleTemplateDelete(role) {
  const result = await deleteTemplate(decodeURIComponent(role));
  if (result.ok) { renderTemplatesEditor(); renderBoard(); }
  else alert(result.error || 'Could not delete template.');
}

function escapeHtml(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// Wire edit/delete buttons once the list is rendered.
function bindTemplateListButtons(container) {
  container.querySelectorAll('.template-edit').forEach(b =>
    b.addEventListener('click', () => openTemplateEditor(b.getAttribute('data-role'))));
  container.querySelectorAll('.template-delete').forEach(b => {
    const role = b.getAttribute('data-role');
    b.addEventListener('click', () => {
      if (confirm(`Delete the "${decodeURIComponent(role)}" template? Live holders keep their current profiles, but future hires of that role get the generic fallback.`)) {
        handleTemplateDelete(role);
      }
    });
  });
}