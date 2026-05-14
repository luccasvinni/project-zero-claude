let allAnnouncements = [];
let parishId = '';
let dateStr = '';
let parishCss = '';
let editingAnnId = null;

let _activeImageGenAnnId = null;
let _activeImageGenTitle = '';
let _activeContentGenAnnId = null;
let _activeContentGenTitle = '';

function _showBusyModal(text) {
  document.getElementById('img-busy-text').textContent = text;
  document.getElementById('img-busy-modal').classList.add('open');
}
function _showImageBusy() {
  _showBusyModal(`Uma nova imagem já está sendo gerada para o "${_activeImageGenTitle}", aguarde a conclusão antes de solicitar uma nova geração.`);
}
function _showContentBusy() {
  _showBusyModal(`Um novo texto já está sendo gerado para o "${_activeContentGenTitle}", aguarde a conclusão antes de solicitar uma nova geração.`);
}

function _clearActiveImageGen() {
  _activeImageGenAnnId = null;
  _activeImageGenTitle = '';
}
function _clearActiveContentGen() {
  _activeContentGenAnnId = null;
  _activeContentGenTitle = '';
}

const _sourceView = {};
const _cropState = { annId: null, page: null, drawing: false, sx: 0, sy: 0, ex: 0, ey: 0, sel: null, zoom: 1.0, baseW: null, panMode: false, fromComparison: false };
const ZOOM_STEPS = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0];
const _compareState = {
  annId: null,
  hasImage: false, imageChoice: 'new',
  hasHtml: false,  htmlChoice: 'new',
  pendingNewHtml: null,
  hasHtmlBackup: false, originalHtml: null,
  imageInstruction: '', htmlInstruction: '',
};

// Map of parish_id -> [{parish_id, date}, ...]  (most recent first)
let _parishRuns = {};

// --- Progress bar ---
const BAR_STEPS = [
  { key: 'scraper',    label: 'Buscando boletim',        icon: '1' },
  { key: 'reader',     label: 'Lendo boletim',           icon: '2' },
  { key: 'preparing',  label: 'Organizando informações', icon: '3' },
  { key: 'generation', label: 'Gerando conteúdo',        icon: '4' },
  { key: 'reviewer',   label: 'Revisando',               icon: '5' },
  { key: 'done',       label: 'Atualizando Dashboard',   icon: '6' },
];

function renderProgressBar(currentStep, isError) {
  const activeIdx = BAR_STEPS.findIndex(s => s.key === currentStep);
  const pct = (!currentStep || activeIdx === -1 || isError || currentStep === 'done')
    ? null
    : Math.round((activeIdx / (BAR_STEPS.length - 1)) * 100);

  const container = document.getElementById('progress-steps-bar');
  container.innerHTML = BAR_STEPS.map((s, i) => {
    let nodeClass = '';
    let connClass = '';
    if (isError && i === activeIdx) nodeClass = 'error';
    else if (currentStep === 'done' || i < activeIdx) { nodeClass = 'done'; connClass = 'done'; }
    else if (i === activeIdx) { nodeClass = 'active'; connClass = 'active'; }
    const icon = nodeClass === 'done' ? '✓' : s.icon;
    const pctLabel = (connClass === 'active' && pct !== null)
      ? `<span class="connector-pct">${pct}%</span>` : '';
    const connector = i < BAR_STEPS.length - 1
      ? `<div class="step-connector ${connClass}">${pctLabel}</div>` : '';
    return `<div class="progress-step">
      <div class="step-node ${nodeClass}">
        <div class="step-circle">${icon}</div>
        <div class="step-label">${s.label}</div>
      </div>${connector}</div>`;
  }).join('');
}

function setProgressDetail(text) {
  document.getElementById('progress-detail-text').textContent = text || '';
}

// --- Parish dropdown ---
function toggleParishDropdown() {
  document.getElementById('parish-dropdown').classList.toggle('open');
}

function selectParish(pid) {
  document.getElementById('parish-dropdown').classList.remove('open');
  document.getElementById('parish-btn-label').textContent = pid;
  document.querySelectorAll('.parish-option').forEach(el => {
    el.classList.toggle('selected', el.dataset.parish === pid);
  });
  const runs = _parishRuns[pid];
  if (!runs || !runs.length) return;
  loadRunByParishDate(pid, runs[0].date);
}

// --- Init ---
async function init() {
  renderProgressBar('', false);
  setProgressDetail('Aguardando início do workflow.');

  const savedJob = localStorage.getItem('workflow_job_id');
  if (savedJob) resumeWorkflowPolling(savedJob);

  const [runs, allParishes] = await Promise.all([
    fetch('/api/runs').then(r => r.json()),
    fetch('/api/parishes').then(r => r.json()),
  ]);

  _parishRuns = {};
  runs.forEach(r => {
    if (!_parishRuns[r.parish_id]) _parishRuns[r.parish_id] = [];
    _parishRuns[r.parish_id].push(r);
  });

  // Include configured parishes even if they have no runs yet
  allParishes.forEach(p => { if (!_parishRuns[p]) _parishRuns[p] = []; });

  const parishes = Object.keys(_parishRuns).sort();
  const dropdown = document.getElementById('parish-dropdown');
  dropdown.innerHTML = parishes.map(p =>
    `<button class="parish-option" data-parish="${escHtml(p)}" onclick="selectParish(this.dataset.parish)">${escHtml(p)}</button>`
  ).join('');

  if (parishes.length) selectParish(parishes[0]);
}

// Close dropdown when clicking outside
document.addEventListener('click', e => {
  const wrap = document.getElementById('parish-select-wrap');
  if (wrap && !wrap.contains(e.target)) {
    document.getElementById('parish-dropdown').classList.remove('open');
  }
});

async function loadRunByParishDate(pid, date) {
  parishId = pid;
  dateStr = date;

  const [runData, cssData] = await Promise.all([
    fetch(`/api/run/${pid}/${date}`).then(r => r.json()),
    fetch(`/api/css/${pid}`).then(r => r.json()),
  ]);

  allAnnouncements = runData.announcements.slice().sort((a, b) => {
    const da = a.event_date || '9999-99-99';
    const db = b.event_date || '9999-99-99';
    return da < db ? -1 : da > db ? 1 : 0;
  });
  parishCss = cssData.css;
  renderGrid();
}

async function resumeWorkflowPolling(job_id) {
  try {
    const job = await fetch(`/api/workflow/status/${job_id}`).then(r => {
      if (!r.ok) throw new Error('not found');
      return r.json();
    });
    renderProgressBar(job.step, job.status === 'error');
    setProgressDetail(job.detail || '');
    if (job.status === 'running') {
      _workflowPollInterval = setInterval(() => pollWorkflowBar(job_id), 2500);
    } else {
      localStorage.removeItem('workflow_job_id');
    }
  } catch {
    localStorage.removeItem('workflow_job_id');
  }
}

async function pollWorkflowBar(job_id) {
  const job = await fetch(`/api/workflow/status/${job_id}`).then(r => r.json());
  renderProgressBar(job.step, job.status === 'error');
  setProgressDetail(job.detail || '');
  if (job.status === 'done') {
    stopWorkflowPolling();
    localStorage.removeItem('workflow_job_id');
    if (job.parish_id && job.date) {
      if (!_parishRuns[job.parish_id]) _parishRuns[job.parish_id] = [];
      if (!_parishRuns[job.parish_id].find(r => r.date === job.date)) {
        _parishRuns[job.parish_id].unshift({ parish_id: job.parish_id, date: job.date });
      }
      // Rebuild dropdown and select updated parish
      const dropdown = document.getElementById('parish-dropdown');
      dropdown.innerHTML = Object.keys(_parishRuns).sort().map(p =>
        `<button class="parish-option" data-parish="${escHtml(p)}" onclick="selectParish(this.dataset.parish)">${escHtml(p)}</button>`
      ).join('');
      selectParish(job.parish_id);
    }
    showToast('Workflow concluído com sucesso!');
  } else if (job.status === 'error') {
    stopWorkflowPolling();
    localStorage.removeItem('workflow_job_id');
    showToast('Erro no workflow: ' + (job.detail || 'Erro desconhecido'), true);
  }
}

// --- Render ---
let _gridCacheBust = Date.now();

function renderGrid() {
  _gridCacheBust = Date.now();
  Object.keys(_sourceView).forEach(k => delete _sourceView[k]);
  const grid = document.getElementById('grid');
  if (!allAnnouncements.length) {
    grid.innerHTML = '<div class="empty-state">Nenhum anúncio encontrado.</div>';
    return;
  }
  grid.innerHTML = allAnnouncements.map(ann => cardHtml(ann)).join('');
  allAnnouncements.forEach(ann => {
    renderPreviewFrame(ann);
    initTabs(ann.id);
  });
}

function statusBadge(status) {
  if (status === 'needs_review') return '<span class="badge badge-review">Revisão</span>';
  return '';
}

function cardHtml(ann) {
  const id = ann.id;
  const reviewIssues = getReviewIssues(ann);
  const bulletinFmt = dateStr ? dateStr.replace(/-/g, '') : '';
  const _months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const eventFmt = ann.event_date
    ? (() => { const [y,m,d] = ann.event_date.split('-'); return `${_months[+m-1]} ${+d}, ${y}`; })()
    : '—';

  return `
  <div class="card" id="card-${id}">
    <div class="card-image-wrap">
      ${ann.has_image
        ? `<img src="/api/image/${parishId}/${dateStr}/${id}?t=${_gridCacheBust}" id="img-${id}" alt="${escHtml(ann.title)}">`
        : `<div class="img-no-image">Imagem não gerada</div>`}
      <div id="img-gen-overlay-${id}" style="position:absolute;inset:0;background:rgba(255,255,255,.88);display:none;flex-direction:column;align-items:center;justify-content:center;gap:.75rem;z-index:5;backdrop-filter:blur(2px)">
        <img src="/static/icon-refresh.png" style="width:26px;height:26px;object-fit:contain;filter:brightness(0) invert(18%) sepia(55%) saturate(800%) hue-rotate(185deg) brightness(60%) contrast(95%);animation:spin .7s linear infinite">
        <span id="img-gen-overlay-text-${id}" style="font-size:.9rem;font-weight:600;color:#1a3a5c">Gerando...</span>
      </div>
    </div>

    ${ann.has_image ? `
    <div style="display:flex;gap:.3rem;flex-wrap:nowrap;padding:.75rem .85rem;border-bottom:1px solid #f1f5f9;overflow:hidden">
      <a class="btn btn-secondary" id="dl-btn-${id}" href="/api/image/${parishId}/${dateStr}/${id}?t=${_gridCacheBust}" download="announcement_${String(id).padStart(2,'0')}.png" style="text-decoration:none;font-size:.64rem;padding:5px 9px;white-space:nowrap;flex-shrink:0">
        <img src="/static/icon-download.png" alt="" style="width:12px;height:12px;object-fit:contain;filter:brightness(0) opacity(.5)"> Baixar imagem
      </a>
      ${ann.has_source ? `
      <button class="btn btn-secondary" id="source-btn-${id}" onclick="toggleSourceImage('${id}')" style="font-size:.64rem;padding:5px 9px;white-space:nowrap;flex-shrink:0">
        <img src="/static/icon-photo.png" alt="" style="width:12px;height:12px;object-fit:contain;filter:brightness(0) opacity(.5)" id="source-btn-icon-${id}">
        <span id="source-btn-label-${id}">Ver imagem fonte</span>
      </button>
      <button class="btn btn-secondary" id="mark-btn-${id}" onclick="openManualCrop('${id}')" style="font-size:.64rem;padding:5px 9px;white-space:nowrap;flex-shrink:0">
        <img src="/static/icon-edit.png" alt="" style="width:12px;height:12px;object-fit:contain;filter:brightness(0) opacity(.5)"> Marcar manualmente
      </button>
      ` : ''}
    </div>
    ` : ''}

    <div class="card-body">
      <div class="card-meta">
        ${bulletinFmt ? `<span class="card-run-date">BOLETIM <strong>${bulletinFmt}</strong> | DATA DO EVENTO <strong style="text-transform:uppercase">${eventFmt}</strong></span>` : ''}
      </div>

      <div style="font-size:1.4rem;font-weight:700;color:#1a1a2e;line-height:1.3">${escHtml(ann.title || '')}</div>

      <div class="review-notes" id="review-wrap-${id}" ${reviewIssues || ann.status === 'needs_review' ? '' : 'style="background:transparent;border:none;padding:.1rem 0"'}>
        ${reviewIssues || ann.status === 'needs_review'
          ? `<div style="display:flex;justify-content:space-between;align-items:center${reviewIssues ? ';margin-bottom:.6rem' : ''}">
              ${statusBadge(ann.status)}
              <button class="btn-instr" style="margin-left:0;border-color:#f59e0b;color:#b45309" onclick="reviewAnn('${id}')"><img src="/static/icon-refresh.png" alt="" style="filter:brightness(0) saturate(100%) invert(32%) sepia(90%) saturate(800%) hue-rotate(10deg) brightness(88%) contrast(100%)"><span class="btn-instr-text">REVISAR NOVAMENTE</span></button>
            </div>`
          : `<div style="display:flex;justify-content:space-between;align-items:center;border:1.5px solid #10b981;border-radius:20px;padding:4px 10px 4px 14px;color:#059669${reviewIssues ? ';margin-bottom:.6rem' : ''}">
              <span style="font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em">✓ APROVADO NA REVISÃO</span>
              <button class="btn-instr" style="margin-left:0;border-color:#10b981;color:#059669" onclick="reviewAnn('${id}')"><img src="/static/icon-refresh.png" alt="" style="filter:brightness(0) saturate(100%) invert(63%) sepia(40%) saturate(800%) hue-rotate(120deg) brightness(95%) contrast(95%)"><span class="btn-instr-text">REVISAR NOVAMENTE</span></button>
            </div>`}
        ${reviewIssues}
      </div>

      <div class="rating-section">
        <div class="rating-row">
          <span class="rating-label">Imagem</span>
          <div class="stars" id="img-stars-${id}">${starsHtml(id, 'image', ann.rating?.image || 0)}</div>
          <span class="rating-saved" id="img-saved-${id}">✓</span>
          <div style="display:flex;align-items:center;gap:.3rem;margin-left:auto">
            <button class="btn-instr" style="margin-left:0" onclick="toggleInstruction('${id}','image')"><img src="/static/icon-write.png" alt=""><span class="btn-instr-text">ADICIONAR INSTRUÇÃO</span></button>
            <span class="regen-status" id="regen-image-status-${id}"></span>
            <button class="btn-instr" id="regen-image-btn-${id}" style="margin-left:0" onclick="quickRegen('${id}','image')"><img src="/static/icon-refresh.png" alt=""><span class="btn-instr-text">GERAR NOVAMENTE</span></button>
          </div>
        </div>
        <div class="feedback-form" id="ff-image-${id}">
          <textarea id="ft-image-${id}" placeholder="Descreva o que deve melhorar na geração da imagem..."></textarea>
          <div style="display:flex;justify-content:flex-end;gap:.35rem;margin-top:.35rem">
            <button class="btn btn-sm btn-secondary" onclick="toggleInstruction('${id}','image')">Cancelar</button>
            <button class="btn btn-sm btn-primary" onclick="sendFeedback('${id}','image')">Gerar</button>
          </div>
        </div>

        <div class="rating-row" style="margin-top:.35rem">
          <span class="rating-label">Textos</span>
          <div class="stars" id="html-stars-${id}">${starsHtml(id, 'html', ann.rating?.html || 0)}</div>
          <span class="rating-saved" id="html-saved-${id}">✓</span>
          <div style="display:flex;align-items:center;gap:.3rem;margin-left:auto">
            <button class="btn-instr" style="margin-left:0" onclick="toggleInstruction('${id}','content')"><img src="/static/icon-write.png" alt=""><span class="btn-instr-text">ADICIONAR INSTRUÇÃO</span></button>
            <span class="regen-status" id="regen-content-status-${id}"></span>
            <button class="btn-instr" id="regen-content-btn-${id}" style="margin-left:0" onclick="quickRegen('${id}','content')"><img src="/static/icon-refresh.png" alt=""><span class="btn-instr-text">GERAR NOVAMENTE</span></button>
          </div>
        </div>
        <div class="feedback-form" id="ff-content-${id}">
          <textarea id="ft-content-${id}" placeholder="Descreva o que deve melhorar na geração dos textos..."></textarea>
          <div style="display:flex;justify-content:flex-end;gap:.35rem;margin-top:.35rem">
            <button class="btn btn-sm btn-secondary" onclick="toggleInstruction('${id}','content')">Cancelar</button>
            <button class="btn btn-sm btn-primary" onclick="sendFeedback('${id}','content')">Gerar</button>
          </div>
        </div>
      </div>

      <div class="preview-section" style="position:relative">
        <div id="html-gen-overlay-${id}" style="position:absolute;inset:0;background:rgba(255,255,255,.88);display:none;flex-direction:column;align-items:center;justify-content:center;gap:.75rem;z-index:5;backdrop-filter:blur(2px);border-radius:4px">
          <img src="/static/icon-refresh.png" style="width:26px;height:26px;object-fit:contain;filter:brightness(0) invert(18%) sepia(55%) saturate(800%) hue-rotate(185deg) brightness(60%) contrast(95%);animation:spin .7s linear infinite">
          <span id="html-gen-overlay-text-${id}" style="font-size:.9rem;font-weight:600;color:#1a3a5c">Gerando...</span>
        </div>
        <div class="preview-tabs">
          <div class="lang-tab-group">
            <button class="tab-btn active" id="tab-en-${id}" onclick="switchTab('${id}','en')" aria-label="Inglês (EN)"><span aria-hidden="true">🇺🇸</span> EN</button>
            <button class="lang-edit-btn" onmousedown="return false" onclick="openInlineEditor('${id}','en')" aria-label="Editar versão em inglês"><img src="/static/icon-edit.png" alt="" style="width:11px;height:11px;object-fit:contain"></button>
          </div>
          <div class="lang-tab-group">
            <button class="tab-btn" id="tab-es-${id}" onclick="switchTab('${id}','es')" aria-label="Espanhol (ES)"><span aria-hidden="true">🇪🇸</span> ES</button>
            <button class="lang-edit-btn" onmousedown="return false" onclick="openInlineEditor('${id}','es')" aria-label="Editar versão em espanhol"><img src="/static/icon-edit.png" alt="" style="width:11px;height:11px;object-fit:contain"></button>
          </div>
          <div class="lang-tab-group">
            <button class="tab-btn" id="tab-pt-${id}" onclick="switchTab('${id}','pt')" aria-label="Português (PT)"><span aria-hidden="true">🇧🇷</span> PT</button>
            <button class="lang-edit-btn" onmousedown="return false" onclick="openInlineEditor('${id}','pt')" aria-label="Editar versão em português"><img src="/static/icon-edit.png" alt="" style="width:11px;height:11px;object-fit:contain"></button>
          </div>
          <div class="lang-tab-group">
            <button class="tab-btn" id="tab-source-${id}" onclick="switchTab('${id}','source')">GERAL</button>
            <button class="lang-edit-btn" onmousedown="return false" onclick="openInlineEditor('${id}','source')" aria-label="Editar versão geral"><img src="/static/icon-edit.png" alt="" style="width:11px;height:11px;object-fit:contain"></button>
          </div>
        </div>

        <div class="inline-editor-bar" id="inline-bar-${id}" style="display:none">
          <div class="inline-editor-actions">
            <button class="wysiwyg-tab-btn active" id="inline-tab-visual-${id}" onmousedown="return false" onclick="switchInlineViewTab('${id}','visual')">Visual</button>
            <button class="wysiwyg-tab-btn" id="inline-tab-html-${id}" onmousedown="return false" onclick="switchInlineViewTab('${id}','html')">HTML</button>
          </div>
          <div class="inline-toolbar" id="inline-toolbar-${id}">
            <button onmousedown="return false" onclick="wExecInline('${id}','bold')" title="Negrito"><b>B</b></button>
            <button onmousedown="return false" onclick="wExecInline('${id}','italic')" title="Itálico"><i>I</i></button>
            <button onmousedown="return false" onclick="wExecInline('${id}','underline')" title="Sublinhado" style="text-decoration:underline">U</button>
            <div class="wysiwyg-sep"></div>
            <button onmousedown="return false" onclick="wExecInline('${id}','insertOrderedList')" title="Lista ordenada" style="font-size:.73rem">1.</button>
            <button onmousedown="return false" onclick="wExecInline('${id}','insertUnorderedList')" title="Lista sem ordem">•</button>
            <div class="wysiwyg-sep"></div>
            <button onmousedown="return false" onclick="wInsertLinkInline('${id}')" title="Inserir link" style="font-size:.73rem">⎔ Link</button>
            <div class="wysiwyg-sep"></div>
            <button onmousedown="return false" onclick="wExecInline('${id}','justifyLeft')" title="Alinhar à esquerda">
              <svg width="12" height="12" viewBox="0 0 14 14" fill="none"><rect x="0" y="1" width="14" height="2" rx="1" fill="currentColor"/><rect x="0" y="6" width="9" height="2" rx="1" fill="currentColor"/><rect x="0" y="11" width="14" height="2" rx="1" fill="currentColor"/></svg>
            </button>
            <button onmousedown="return false" onclick="wExecInline('${id}','justifyCenter')" title="Centralizar">
              <svg width="12" height="12" viewBox="0 0 14 14" fill="none"><rect x="0" y="1" width="14" height="2" rx="1" fill="currentColor"/><rect x="2.5" y="6" width="9" height="2" rx="1" fill="currentColor"/><rect x="0" y="11" width="14" height="2" rx="1" fill="currentColor"/></svg>
            </button>
            <button onmousedown="return false" onclick="wExecInline('${id}','justifyRight')" title="Alinhar à direita">
              <svg width="12" height="12" viewBox="0 0 14 14" fill="none"><rect x="0" y="1" width="14" height="2" rx="1" fill="currentColor"/><rect x="5" y="6" width="9" height="2" rx="1" fill="currentColor"/><rect x="0" y="11" width="14" height="2" rx="1" fill="currentColor"/></svg>
            </button>
            <div class="wysiwyg-sep"></div>
            <button onmousedown="return false" onclick="wExecInline('${id}','undo')" title="Desfazer">↩</button>
            <button onmousedown="return false" onclick="wExecInline('${id}','redo')" title="Refazer">↪</button>
          </div>
        </div>

        <div class="tab-content active" id="content-en-${id}">
          <iframe class="preview-frame" id="frame-en-${id}" scrolling="no"></iframe>
          <div class="inline-wysiwyg" id="inline-visual-en-${id}" contenteditable="true" style="display:none"></div>
          <textarea class="inline-html-area" id="inline-html-en-${id}" spellcheck="false" style="display:none"></textarea>
        </div>
        <div class="tab-content" id="content-es-${id}">
          <iframe class="preview-frame" id="frame-es-${id}" scrolling="no"></iframe>
          <div class="inline-wysiwyg" id="inline-visual-es-${id}" contenteditable="true" style="display:none"></div>
          <textarea class="inline-html-area" id="inline-html-es-${id}" spellcheck="false" style="display:none"></textarea>
        </div>
        <div class="tab-content" id="content-pt-${id}">
          <iframe class="preview-frame" id="frame-pt-${id}" scrolling="no"></iframe>
          <div class="inline-wysiwyg" id="inline-visual-pt-${id}" contenteditable="true" style="display:none"></div>
          <textarea class="inline-html-area" id="inline-html-pt-${id}" spellcheck="false" style="display:none"></textarea>
        </div>
        <div class="tab-content" id="content-source-${id}">
          <iframe class="preview-frame" id="frame-source-${id}" scrolling="no"></iframe>
          <div class="inline-wysiwyg" id="inline-visual-source-${id}" contenteditable="true" style="display:none"></div>
          <textarea class="inline-html-area" id="inline-html-source-${id}" spellcheck="false" style="display:none"></textarea>
          <textarea id="editor-${id}" spellcheck="false" style="display:none">${escHtml(ann.html_content)}</textarea>
        </div>

        <div class="inline-editor-footer" id="inline-footer-${id}" style="display:none">
          <button class="btn btn-secondary btn-sm" onclick="cancelInlineEdit('${id}')">Cancelar</button>
          <button class="btn btn-success btn-sm" onclick="saveInlineBlock('${id}')">✓ Salvar</button>
        </div>
      </div>

      <div class="card-actions">
        <button class="btn btn-secondary btn-sm" onclick="copyHtml('${id}')"><img src="/static/icon-copy.png" alt="" style="width:13px;height:13px;object-fit:contain"> Copiar HTML</button>
        <button class="btn btn-danger btn-sm" onclick="openCorrections('${id}')"><img src="/static/icon-suggest.png" alt="" style="width:13px;height:13px;object-fit:contain"> Corrigir erros</button>
      </div>
    </div>
  </div>`;
}

function getReviewIssues(ann) {
  const rev = ann.review?.review;
  if (!rev) return '';

  const imgItems  = rev.image?.issues || [];
  const htmlItems = rev.html?.issues  || [];
  const spelling  = rev.html?.spelling_errors || [];
  const htmlAll   = [
    ...htmlItems.map(i => `<li>${escHtml(i)}</li>`),
    ...(spelling.length ? [`<li>Erros ortográficos: ${spelling.map(escHtml).join(', ')}</li>`] : []),
  ];

  if (!imgItems.length && !htmlAll.length) return '';

  const label = txt => `<div style="font-size:.6rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#b45309;margin-bottom:.3rem">${txt}</div>`;
  let out = '';

  if (imgItems.length) {
    out += `<div style="${htmlAll.length ? 'margin-bottom:.65rem' : ''}">
      ${label('Imagem')}
      <ul>${imgItems.map(i => `<li>${escHtml(i)}</li>`).join('')}</ul>
    </div>`;
  }
  if (htmlAll.length) {
    out += `<div${imgItems.length ? ' style="border-top:1px solid #fde68a;padding-top:.65rem"' : ''}>
      ${label('Texto')}
      <ul>${htmlAll.join('')}</ul>
    </div>`;
  }
  return out;
}

const LANG_TABS = ['en', 'es', 'pt', 'source'];
const LANG_IDX  = {en: 0, es: 1, pt: 2};

const _LANG_MARKERS = {
  en: /\b(january|february|march|april|may|june|july|august|september|october|november|december|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b/i,
  es: /\b(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre|lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\b/i,
  pt: /\b(janeiro|fevereiro|mar[çc]o|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro|segunda|ter[çc]a|quarta|quinta|sexta|s[aá]bado|domingo)\b/i,
};

function _detectLang(text) {
  const plain = text.replace(/<[^>]+>/g, ' ');
  const scores = {};
  for (const [lang, rx] of Object.entries(_LANG_MARKERS)) {
    scores[lang] = (plain.match(new RegExp(rx.source, 'gi')) || []).length;
  }
  const best = Object.entries(scores).sort((a, b) => b[1] - a[1]);
  return best[0][1] > 0 ? best[0][0] : null;
}

function splitLangs(html) {
  const SEP = /<p[^>]*>\s*-\s*-\s*-\s*<\/p>/gi;
  return html.split(SEP).map(s => s.trim()).filter(Boolean);
}

function stripRevisarBox(html) {
  return html.replace(/<p[^>]*background\s*:\s*#fff3cd[^>]*>[\s\S]*?<\/p>/gi, '').trim();
}

function frameDoc(content) {
  return `<!DOCTYPE html><html><head><meta charset="UTF-8"><style>${parishCss} body{margin:0;padding:14px 16px;}</style></head><body>${stripRevisarBox(content)}</body></html>`;
}

function setFrame(frame, content) {
  frame.srcdoc = frameDoc(content);
  frame.onload = () => { frame.style.height = (frame.contentDocument.body.scrollHeight + 10) + 'px'; };
}

function getLangContent(html, lang) {
  if (lang === 'source') return html;
  const parts = splitLangs(html);
  if (parts.length < 2) return parts[0] ?? '';
  const detected = parts.map(_detectLang);
  const idx = detected.indexOf(lang);
  if (idx !== -1) return parts[idx];
  // fallback: positional order
  return parts[LANG_IDX[lang]] ?? parts[0] ?? '';
}

function renderLangFrame(id, lang) {
  const ann = allAnnouncements.find(a => String(a.id) === String(id));
  if (!ann) return;
  const frame = document.getElementById(`frame-${lang}-${id}`);
  if (!frame) return;
  setFrame(frame, getLangContent(ann.html_content, lang));
}

function renderPreviewFrame(ann) {
  renderLangFrame(ann.id, 'en');
}

function refreshPreview(id) {
  const ann = allAnnouncements.find(a => String(a.id) === String(id));
  const editor = document.getElementById(`editor-${id}`);
  if (!ann || !editor) return;
  ann.html_content = editor.value;
  const activeLang = LANG_TABS.find(t =>
    document.getElementById(`tab-${t}-${id}`)?.classList.contains('active'));
  if (activeLang) renderLangFrame(id, activeLang);
}

function initTabs(id) {}
function switchTab(id, tab) {
  if (_inlineEdit[id] && tab !== _inlineEdit[id].lang) cancelInlineEdit(id, true);
  LANG_TABS.forEach(t => {
    document.getElementById(`tab-${t}-${id}`)?.classList.toggle('active', t === tab);
    document.getElementById(`content-${t}-${id}`)?.classList.toggle('active', t === tab);
  });
  renderLangFrame(id, tab);
}

// --- Copy HTML ---
function copyHtml(id) {
  const html = document.getElementById(`editor-${id}`).value;
  navigator.clipboard.writeText(html).then(() => {
    const btn = document.querySelector(`button[onclick="copyHtml('${id}')"]`);
    if (!btn) return;
    const orig = btn.innerHTML;
    const origStyle = btn.getAttribute('style') || '';
    btn.innerHTML = '<span style="font-size:.9rem;line-height:1">✓</span> HTML COPIADO';
    btn.style.cssText = (origStyle ? origStyle + ';' : '') + 'background:#d1fae5;color:#065f46;border-color:#6ee7b7;';
    setTimeout(() => { btn.innerHTML = orig; btn.setAttribute('style', origStyle); }, 2500);
  });
}

// --- Stars / Rating ---
function starsHtml(annId, type, current) {
  return [1,2,3,4,5].map(n =>
    `<span role="button" tabindex="0" class="star ${n <= current ? 'filled' : ''}"
      aria-label="${n} estrela${n > 1 ? 's' : ''}"
      aria-pressed="${n <= current}"
      onclick="setRating('${annId}','${type}',${n})"
      onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();setRating('${annId}','${type}',${n})}"
      onmouseenter="hoverStars('${annId}','${type}',${n})"
      onmouseleave="unhoverStars('${annId}','${type}')"
    >&#9733;</span>`
  ).join('');
}

function hoverStars(annId, type, n) {
  const prefix = type === 'image' ? 'img' : 'html';
  document.querySelectorAll(`#${prefix}-stars-${annId} .star`).forEach((s, i) => {
    s.classList.toggle('hover', i < n);
  });
}

function unhoverStars(annId, type) {
  const prefix = type === 'image' ? 'img' : 'html';
  document.querySelectorAll(`#${prefix}-stars-${annId} .star`).forEach(s => s.classList.remove('hover'));
}

async function setRating(annId, type, value) {
  const ann = allAnnouncements.find(a => String(a.id) === String(annId));
  if (!ann) return;
  if (!ann.rating) ann.rating = { image: 0, html: 0 };
  ann.rating[type] = ann.rating[type] === value ? 0 : value;
  const newValue = ann.rating[type];
  const prefix = type === 'image' ? 'img' : 'html';
  const container = document.getElementById(`${prefix}-stars-${annId}`);
  if (container) container.innerHTML = starsHtml(annId, type, newValue);
  await fetch(`/api/rate/${parishId}/${dateStr}/${annId}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ image_rating: ann.rating.image, html_rating: ann.rating.html }),
  });
  const saved = document.getElementById(`${prefix}-saved-${annId}`);
  if (saved) { saved.classList.add('show'); setTimeout(() => saved.classList.remove('show'), 1800); }
}

// --- Toast ---
function showToast(msg, error = false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.background = error ? '#991b1b' : '#1a3a5c';
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3000);
}

function escHtml(s) {
  return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// --- Spelling corrections ---
let correctingAnnId = null;
let pendingCorrections = [];

const LANG_LABELS = ['EN', 'ES', 'PT'];

function extractRedSpanWords(htmlStr) {
  const matches = [...(htmlStr || '').matchAll(/<span[^>]+color\s*:\s*red[^>]*>([^<]+)<\/span>/gi)];
  return [...new Set(matches.map(m => m[1].trim()).filter(Boolean))];
}

function hasSpellingErrors(ann) {
  const fromReview = ann.review?.review?.html?.spelling_errors || [];
  if (fromReview.length > 0) return true;
  return extractRedSpanWords(ann.html_content).length > 0;
}

// Split HTML preserving separators: returns [block0, sep0, block1, sep1, block2, ...]
function splitByLangSep(html) {
  return html.split(/(<p[^>]*>\s*-\s*-\s*-\s*<\/p>)/gi);
}

async function openCorrections(id) {
  correctingAnnId = id;
  const ann = allAnnouncements.find(a => String(a.id) === String(id));
  const rev = ann?.review?.review?.html || {};
  const spelling    = rev.spelling_errors || [];
  const html_issues = rev.issues          || [];
  const html = document.getElementById(`editor-${id}`)?.value || ann?.html_content || '';

  const list = document.getElementById('corrections-list');
  document.getElementById('corrections-modal').classList.add('open');

  if (!spelling.length && !html_issues.length) {
    list.innerHTML = '<div style="color:#64748b;font-size:.82rem">Nenhum erro identificado pelo revisor.</div>';
    pendingCorrections = [];
    return;
  }

  list.innerHTML = '<div style="text-align:center;color:#64748b;font-size:.82rem">Avaliando sugestões...</div>';

  const res = await fetch(`/api/suggest-corrections/${parishId}/${dateStr}/${id}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({html, spelling_errors: spelling, html_issues}),
  });
  const data = await res.json();
  const suggestions = data.suggestions || [];

  // Split HTML into per-language blocks
  const parts = splitByLangSep(html);
  const blocks = [parts[0] || '', parts[2] || '', parts[4] || ''];

  // Expand each suggestion into one row per language block where the word appears
  pendingCorrections = [];
  suggestions.forEach(s => {
    const found = LANG_LABELS
      .map((label, li) => ({ label, li }))
      .filter(({ li }) => blocks[li].includes(s.original));
    if (found.length > 0) {
      found.forEach(({ label, li }) => pendingCorrections.push({ ...s, lang: li, langLabel: label }));
    } else {
      pendingCorrections.push({ ...s, lang: -1, langLabel: 'Todos' });
    }
  });

  if (!pendingCorrections.length) {
    list.innerHTML = '<div style="color:#64748b;font-size:.82rem">Nenhuma sugestão gerada.</div>';
    return;
  }

  list.innerHTML = pendingCorrections.map((s, i) => `
    <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:.7rem">
      <div style="font-size:.72rem;color:#94a3b8;margin-bottom:.3rem">
        <strong style="color:#475569;margin-right:.35rem">${escHtml(s.langLabel)}</strong>${escHtml(s.context || '')}
      </div>
      <div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap">
        <span style="font-size:.82rem;color:#991b1b;font-weight:600;text-decoration:line-through">${escHtml(s.original)}</span>
        <span style="color:#64748b">→</span>
        <input type="text" value="${escHtml(s.suggestion)}" id="corr-${i}"
          style="border:1px solid #e2e8f0;border-radius:5px;padding:3px 8px;font-size:.82rem;flex:1;min-width:100px">
        <label style="font-size:.75rem;color:#64748b;display:flex;align-items:center;gap:3px">
          <input type="checkbox" id="corr-check-${i}" checked> Aplicar
        </label>
      </div>
    </div>
  `).join('');
}

async function applyCorrections() {
  if (!correctingAnnId) return;
  const editor = document.getElementById(`editor-${correctingAnnId}`);
  if (!editor) return;

  const parts = splitByLangSep(editor.value);
  const blocks = [parts[0] || '', parts[2] || '', parts[4] || ''];
  const seps   = [parts[1] || '', parts[3] || ''];

  pendingCorrections.forEach((s, i) => {
    const checked   = document.getElementById(`corr-check-${i}`)?.checked;
    const corrected = document.getElementById(`corr-${i}`)?.value || s.suggestion;
    if (!checked || !s.original) return;

    const esc     = s.original.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const spanPat = new RegExp(`<span style="color:red; font-weight: bold;">${esc}<\/span>`, 'gi');
    const phrasePat = new RegExp(esc, 'g');
    const applyTo = s.lang >= 0 ? [s.lang] : [0, 1, 2];

    applyTo.forEach(bi => {
      // First unwrap any red span wrapping the phrase, then replace plain occurrences
      blocks[bi] = blocks[bi].replace(spanPat, corrected).replace(phrasePat, corrected);
    });
  });

  let html = blocks[0];
  if (seps[0] || blocks[1]) html += (seps[0] || '<p align="center" style="color:gray;">- - -</p>') + blocks[1];
  if (seps[1] || blocks[2]) html += (seps[1] || '<p align="center" style="color:gray;">- - -</p>') + blocks[2];

  editor.value = html;
  const ann = allAnnouncements.find(a => String(a.id) === String(correctingAnnId));
  if (ann) ann.html_content = html;
  refreshPreview(correctingAnnId);
  closeModal('corrections-modal');

  // Auto-salva em disco para que o revisor leia a versão corrigida
  const saveRes = await fetch(`/api/edit/html/${parishId}/${dateStr}/${correctingAnnId}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({html}),
  });
  showToast(saveRes.ok ? 'Correções aplicadas e salvas.' : 'Correções aplicadas — erro ao salvar.', !saveRes.ok);
}

// --- Inline WYSIWYG Language Editor ---
const LANG_BLOCK_IDX = { en: 0, es: 2, pt: 4 };
const _inlineEdit = {}; // { annId: { lang, viewTab } }

function openInlineEditor(annId, lang) {
  if (_inlineEdit[annId]) cancelInlineEdit(annId, true);

  const ann = allAnnouncements.find(a => String(a.id) === String(annId));
  if (!ann) return;

  switchTab(annId, lang);

  const blockHtml = getLangContent(ann.html_content, lang);
  document.getElementById(`inline-visual-${lang}-${annId}`).innerHTML = blockHtml;
  document.getElementById(`inline-html-${lang}-${annId}`).value = blockHtml;

  document.getElementById(`frame-${lang}-${annId}`).style.display = 'none';
  document.getElementById(`inline-visual-${lang}-${annId}`).style.display = 'block';
  document.getElementById(`inline-html-${lang}-${annId}`).style.display = 'none';

  document.getElementById(`inline-bar-${annId}`).style.display = 'flex';
  document.getElementById(`inline-footer-${annId}`).style.display = 'flex';
  document.getElementById(`inline-tab-visual-${annId}`)?.classList.add('active');
  document.getElementById(`inline-tab-html-${annId}`)?.classList.remove('active');

  _inlineEdit[annId] = { lang, viewTab: 'visual' };
}

function switchInlineViewTab(annId, viewTab) {
  const state = _inlineEdit[annId];
  if (!state) return;
  const { lang } = state;
  const visual = document.getElementById(`inline-visual-${lang}-${annId}`);
  const htmlEl = document.getElementById(`inline-html-${lang}-${annId}`);

  if (viewTab === 'visual') {
    visual.innerHTML = htmlEl.value;
    visual.style.display = 'block';
    htmlEl.style.display = 'none';
  } else {
    htmlEl.value = visual.innerHTML;
    visual.style.display = 'none';
    htmlEl.style.display = 'block';
  }
  state.viewTab = viewTab;
  document.getElementById(`inline-tab-visual-${annId}`)?.classList.toggle('active', viewTab === 'visual');
  document.getElementById(`inline-tab-html-${annId}`)?.classList.toggle('active', viewTab === 'html');
  const toolbar = document.getElementById(`inline-toolbar-${annId}`);
  if (toolbar) toolbar.style.display = viewTab === 'visual' ? 'flex' : 'none';
}

function wExecInline(annId, cmd, value = null) {
  const state = _inlineEdit[annId];
  if (!state) return;
  const el = document.getElementById(`inline-visual-${state.lang}-${annId}`);
  if (el) { el.focus(); document.execCommand(cmd, false, value); }
}

function wInsertLinkInline(annId) {
  const url = prompt('URL do link (ex: https://...):');
  if (url?.trim()) wExecInline(annId, 'createLink', url.trim());
}

function cancelInlineEdit(annId, silent = false) {
  const state = _inlineEdit[annId];
  if (!state) return;
  const { lang } = state;
  const frame = document.getElementById(`frame-${lang}-${annId}`);
  frame.style.display = 'block';
  frame.style.height = '';
  document.getElementById(`inline-visual-${lang}-${annId}`).style.display = 'none';
  document.getElementById(`inline-html-${lang}-${annId}`).style.display = 'none';
  document.getElementById(`inline-bar-${annId}`).style.display = 'none';
  document.getElementById(`inline-footer-${annId}`).style.display = 'none';
  delete _inlineEdit[annId];
  renderLangFrame(annId, lang);
}

async function saveInlineBlock(annId) {
  const state = _inlineEdit[annId];
  if (!state) return;
  const { lang, viewTab } = state;

  const blockHtml = viewTab === 'visual'
    ? document.getElementById(`inline-visual-${lang}-${annId}`).innerHTML
    : document.getElementById(`inline-html-${lang}-${annId}`).value;

  const ann = allAnnouncements.find(a => String(a.id) === String(annId));
  if (!ann) return;

  let newHtml;
  if (lang === 'source') {
    newHtml = blockHtml;
  } else {
    const parts = splitByLangSep(ann.html_content);
    const idx = LANG_BLOCK_IDX[lang];
    if (parts[idx] !== undefined) parts[idx] = blockHtml;
    newHtml = parts.join('');
  }

  ann.html_content = newHtml;
  const editor = document.getElementById(`editor-${annId}`);
  if (editor) editor.value = newHtml;

  cancelInlineEdit(annId, true);
  renderLangFrame(annId, lang);

  const saveRes = await fetch(`/api/edit/html/${parishId}/${dateStr}/${annId}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({html: newHtml}),
  });
  if (saveRes.ok) {
    clearReviewSection(annId, 'html');
  }
  showToast(saveRes.ok ? 'Bloco salvo.' : 'Erro ao salvar.', !saveRes.ok);
}

// --- Modal ---
function closeModal(id) {
  document.getElementById(id).classList.remove('open');
  if (id === 'corrections-modal') correctingAnnId = null;
}

function showConfirm(message, onConfirm) {
  document.getElementById('confirm-title').textContent = message;
  const btn = document.getElementById('confirm-ok-btn');
  btn.onclick = () => { closeModal('confirm-modal'); onConfirm(); };
  document.getElementById('confirm-modal').classList.add('open');
}

document.querySelectorAll('.modal-backdrop').forEach(el => {
  el.addEventListener('click', e => {
    if (e.target !== e.currentTarget) return;
    if (el.id === 'compare-modal') closeCompareModal();
    else if (el.id === 'update-texts-modal') _doRegenFromCrop(false);
    else closeModal(el.id);
  });
});

// --- Workflow ---
let _workflowPollInterval = null;
let _selectedMode = 'complete';

function stopWorkflowPolling() {
  if (_workflowPollInterval) { clearInterval(_workflowPollInterval); _workflowPollInterval = null; }
}

function selectMode(mode) {
  _selectedMode = mode;
  document.querySelectorAll('.workflow-mode-btn').forEach(btn => {
    const isActive = btn.dataset.mode === mode;
    btn.classList.toggle('btn-primary', isActive);
    btn.classList.toggle('active-mode', isActive);
    btn.classList.toggle('btn-secondary', !isActive);
  });
  const wrap = document.getElementById('reader-instruction-wrap');
  if (wrap) wrap.style.display = mode === 'complete' ? '' : 'none';
  const urlWrap = document.getElementById('parish-url-wrap');
  if (urlWrap) urlWrap.style.display = (_parishDirectPdf && mode === 'complete') ? '' : 'none';
}

let _parishDirectPdf = false;

async function updateParishConfig() {
  const parish = document.getElementById('workflow-parish').value;
  if (!parish) return;
  const cfg = await fetch(`/api/parish-config/${encodeURIComponent(parish)}`).then(r => r.json());
  _parishDirectPdf = !!cfg.direct_pdf;
  const wrap = document.getElementById('parish-url-wrap');
  if (wrap) wrap.style.display = (_parishDirectPdf && _selectedMode === 'complete') ? '' : 'none';
}

async function openWorkflowModal() {
  const parishes = await fetch('/api/parishes').then(r => r.json());
  const sel = document.getElementById('workflow-parish');
  sel.innerHTML = parishes.map(p =>
    `<option value="${escHtml(p)}"${p === parishId ? ' selected' : ''}>${escHtml(p)}</option>`
  ).join('');
  selectMode('complete');
  document.getElementById('workflow-setup').style.display = '';
  document.getElementById('workflow-progress').style.display = 'none';
  document.getElementById('workflow-modal').classList.add('open');
  await updateParishConfig();
}

async function startWorkflow() {
  const parish = document.getElementById('workflow-parish').value;
  if (!parish) return;

  const bulletinUrl = document.getElementById('parish-bulletin-url')?.value?.trim() || '';
  if (_parishDirectPdf && _selectedMode === 'complete' && !bulletinUrl) {
    showToast('Informe o link do boletim PDF para esta paróquia.', true);
    return;
  }

  const readerInstruction = document.getElementById('reader-instruction')?.value?.trim() || '';
  const res = await fetch('/api/workflow/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ parish_id: parish, mode: _selectedMode, reader_instruction: readerInstruction, bulletin_url: bulletinUrl }),
  });
  const { job_id } = await res.json();
  localStorage.setItem('workflow_job_id', job_id);

  closeModal('workflow-modal');
  showToast('Workflow iniciado! Acompanhe o progresso na barra acima.');

  stopWorkflowPolling();
  renderProgressBar(_selectedMode === 'complete' ? 'scraper' : 'preparing', false);
  setProgressDetail('Iniciando...');
  _workflowPollInterval = setInterval(() => pollWorkflowBar(job_id), 2500);
}

// --- Feedback & Regen ---
const _instrScope   = {};
const _pontualInstr = {};

function toggleInstruction(id, type) {
  const form = document.getElementById(`ff-${type}-${id}`);
  if (!form) return;
  form.style.display = form.style.display === 'block' ? 'none' : 'block';
}

async function sendFeedback(id, type) {
  const instruction = document.getElementById(`ft-${type}-${id}`)?.value?.trim();
  if (!instruction) { showToast('Escreva uma instrução antes de gerar.', true); return; }

  _pontualInstr[`${id}-${type}`] = instruction;

  const form = document.getElementById(`ff-${type}-${id}`);
  if (form) form.style.display = 'none';

  quickRegen(id, type);
}

async function reviewAnn(id) {
  const wrap = document.getElementById(`review-wrap-${id}`);
  if (wrap) {
    const btn = wrap.querySelector('.btn-instr');
    if (btn) { btn.disabled = true; btn.classList.add('icon-spin'); }
  }

  const res = await fetch(`/api/review/${parishId}/${dateStr}/${id}`, { method: 'POST' });
  if (!res.ok) {
    let msg = 'Erro na revisão.';
    try { const e = await res.json(); if (e.detail) msg = e.detail; } catch {}
    if (wrap) {
      const btn = wrap.querySelector('.btn-instr');
      if (btn) { btn.disabled = false; btn.classList.remove('icon-spin'); }
    }
    showToast(msg, true);
    return;
  }
  const data = await res.json();

  const ann = allAnnouncements.find(a => String(a.id) === String(id));
  if (ann) {
    ann.review = { review: data.review };
    ann.status = data.status;
  }

  if (wrap && ann) {
    const issues = getReviewIssues(ann);
    const hasIssues = !!issues;
    wrap.style.cssText = (hasIssues || ann.status === 'needs_review') ? '' : 'background:transparent;border:none;padding:.1rem 0';
    const showBtn = hasIssues || ann.status === 'needs_review';
    const headerHtml = showBtn
      ? `<div style="display:flex;justify-content:space-between;align-items:center${hasIssues ? ';margin-bottom:.6rem' : ''}">
           ${statusBadge(ann.status)}
           <button class="btn-instr" style="margin-left:0;border-color:#f59e0b;color:#b45309" onclick="reviewAnn('${id}')"><img src="/static/icon-refresh.png" alt="" style="filter:brightness(0) saturate(100%) invert(32%) sepia(90%) saturate(800%) hue-rotate(10deg) brightness(88%) contrast(100%)"><span class="btn-instr-text">REVISAR NOVAMENTE</span></button>
         </div>`
      : `<div style="display:flex;justify-content:space-between;align-items:center;border:1.5px solid #10b981;border-radius:20px;padding:4px 10px 4px 14px;color:#059669">
           <span style="font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em">✓ APROVADO NA REVISÃO</span>
           <button class="btn-instr" style="margin-left:0;border-color:#10b981;color:#059669" onclick="reviewAnn('${id}')"><img src="/static/icon-refresh.png" alt="" style="filter:brightness(0) saturate(100%) invert(63%) sepia(40%) saturate(800%) hue-rotate(120deg) brightness(95%) contrast(95%)"><span class="btn-instr-text">REVISAR NOVAMENTE</span></button>
         </div>`;
    wrap.innerHTML = headerHtml + issues;

    const doneMsg = data.status === 'approved' ? 'Aprovado!' : 'Revisão concluída.';
    const btn = wrap.querySelector('.btn-instr');
    if (btn) {
      const statusEl = document.createElement('span');
      statusEl.style.cssText = 'font-size:.72rem;color:#059669;margin-left:auto;margin-right:.5rem';
      statusEl.textContent = doneMsg;
      btn.parentNode.insertBefore(statusEl, btn);
      setTimeout(() => statusEl.remove(), 4000);
    }
  }
}

function quickRegen(id, type) {
  if (type === 'image') regenImage(id);
  else regenContent(id);
}

async function regenImage(id) {
  if (_activeImageGenAnnId !== null) { _showImageBusy(); return; }
  const ann = allAnnouncements.find(a => String(a.id) === String(id));
  _activeImageGenAnnId = id;
  _activeImageGenTitle = ann?.title || String(id);

  const statusEl = document.getElementById(`regen-image-status-${id}`);
  const btn = document.getElementById(`regen-image-btn-${id}`);
  if (statusEl) statusEl.textContent = 'Gerando...';
  if (btn) { btn.disabled = true; btn.classList.add('icon-spin'); }
  const imgOverlay = document.getElementById(`img-gen-overlay-${id}`);
  const imgOverlayText = document.getElementById(`img-gen-overlay-text-${id}`);
  if (imgOverlayText) imgOverlayText.textContent = ann?.has_image ? 'Gerando nova imagem...' : 'Gerando...';
  if (imgOverlay) imgOverlay.style.display = 'flex';
  const instruction = _pontualInstr[`${id}-image`] || '';
  _compareState.imageInstruction = instruction;
  const res = await fetch(`/api/regen/image/${parishId}/${dateStr}/${id}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ instruction }),
  });
  const { job_id } = await res.json();
  delete _pontualInstr[`${id}-image`];
  pollRegen(job_id, id, 'image');
}

async function regenContent(id) {
  if (_activeContentGenAnnId !== null) { _showContentBusy(); return; }
  const ann = allAnnouncements.find(a => String(a.id) === String(id));
  _activeContentGenAnnId = id;
  _activeContentGenTitle = ann?.title || String(id);
  const statusEl = document.getElementById(`regen-content-status-${id}`);
  const btn = document.getElementById(`regen-content-btn-${id}`);
  if (statusEl) statusEl.textContent = 'Gerando...';
  if (btn) { btn.disabled = true; btn.classList.add('icon-spin'); }
  const htmlOverlay = document.getElementById(`html-gen-overlay-${id}`);
  const htmlOverlayText = document.getElementById(`html-gen-overlay-text-${id}`);
  if (htmlOverlayText) htmlOverlayText.textContent = ann?.has_html ? 'Gerando novo texto...' : 'Gerando...';
  if (htmlOverlay) htmlOverlay.style.display = 'flex';
  const instruction = _pontualInstr[`${id}-content`] || '';
  _compareState.htmlInstruction = instruction;
  const res = await fetch(`/api/regen/content/${parishId}/${dateStr}/${id}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ instruction }),
  });
  const { job_id } = await res.json();
  delete _pontualInstr[`${id}-content`];
  pollRegen(job_id, id, 'content');
}

function clearReviewSection(id, type) {
  const ann = allAnnouncements.find(a => String(a.id) === String(id));
  if (!ann?.review?.review) return;
  if (type === 'html') ann.review.review.html = { issues: [], spelling_errors: [] };
  else ann.review.review.image = { issues: [] };

  const wrap = document.getElementById(`review-wrap-${id}`);
  if (!wrap) return;
  const issues = getReviewIssues(ann);
  const headerEl = wrap.querySelector(':scope > div:first-child');
  if (headerEl) {
    let next = headerEl.nextSibling;
    while (next) { const r = next; next = next.nextSibling; wrap.removeChild(r); }
    if (issues) {
      const tmp = document.createElement('div');
      tmp.innerHTML = issues;
      while (tmp.firstChild) wrap.appendChild(tmp.firstChild);
    } else {
      wrap.style.cssText = 'background:transparent;border:none;padding:.1rem 0';
    }
  }
}

function resetRegenArea(id, type) {
  const ann = allAnnouncements.find(a => String(a.id) === String(id));
  if (!ann) return;

  // Zero rating
  if (!ann.rating) ann.rating = { image: 0, html: 0 };
  const ratingType = type === 'image' ? 'image' : 'html';
  ann.rating[ratingType] = 0;
  const prefix = type === 'image' ? 'img' : 'html';
  const starsEl = document.getElementById(`${prefix}-stars-${id}`);
  if (starsEl) starsEl.innerHTML = starsHtml(id, ratingType, 0);
  fetch(`/api/rate/${parishId}/${dateStr}/${id}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ image_rating: ann.rating.image, html_rating: ann.rating.html }),
  });

  clearReviewSection(id, type);
}

async function pollRegen(job_id, id, type) {
  const statusEl = document.getElementById(`regen-${type}-status-${id}`);
  const job = await fetch(`/api/regen/status/${job_id}`)
    .then(r => { if (!r.ok) return null; return r.json(); })
    .catch(() => null);
  if (!job) {
    const regenBtn = document.getElementById(`regen-${type}-btn-${id}`);
    if (regenBtn) { regenBtn.disabled = false; regenBtn.classList.remove('icon-spin'); }
    if (statusEl) { statusEl.textContent = ''; }
    if (type === 'image') _clearActiveImageGen();
    if (type === 'content') _clearActiveContentGen();
    const overlay0 = document.getElementById(type === 'image' ? `img-gen-overlay-${id}` : `html-gen-overlay-${id}`);
    if (overlay0) overlay0.style.display = 'none';
    resetRegenArea(id, type);
    return;
  }
  const regenBtn = document.getElementById(`regen-${type}-btn-${id}`);
  if (job.status === 'done') {
    if (type === 'image') _clearActiveImageGen();
    if (type === 'content') _clearActiveContentGen();
    const overlayDone = document.getElementById(type === 'image' ? `img-gen-overlay-${id}` : `html-gen-overlay-${id}`);
    if (overlayDone) overlayDone.style.display = 'none';
    if (statusEl) { statusEl.textContent = '✓ Concluído'; setTimeout(() => { statusEl.textContent = ''; }, 3000); }
    if (regenBtn) { regenBtn.disabled = false; regenBtn.classList.remove('icon-spin'); }
    resetRegenArea(id, type);
    if (type === 'image') {
      const newUrl = `/api/image/${parishId}/${dateStr}/${id}?t=${Date.now()}`;
      if (job.has_backup) {
        showImageComparison(id, newUrl);
      } else {
        const img = document.getElementById(`img-${id}`);
        if (img) img.src = newUrl;
        showToast('Imagem gerada com sucesso!');
      }
    } else {
      if (job.has_html_backup) {
        const oldRes = await fetch(`/api/html-backup/${parishId}/${dateStr}/${id}`);
        const oldData = await oldRes.json();
        populateHtmlComparison(id, oldData.html || '', job.html || '', true);
      } else if (job.html) {
        const ann = allAnnouncements.find(a => String(a.id) === String(id));
        if (ann && ann.html_content) {
          populateHtmlComparison(id, ann.html_content, job.html, false);
        } else {
          if (ann) {
            ann.html_content = job.html;
            const editor = document.getElementById(`editor-${id}`);
            if (editor) editor.value = job.html;
            refreshPreview(id);
          }
          showToast('Texto gerado com sucesso!');
        }
      }
    }
  } else if (job.status === 'error') {
    if (type === 'image') _clearActiveImageGen();
    if (type === 'content') _clearActiveContentGen();
    const overlayErr = document.getElementById(type === 'image' ? `img-gen-overlay-${id}` : `html-gen-overlay-${id}`);
    if (overlayErr) overlayErr.style.display = 'none';
    if (statusEl) { statusEl.textContent = '✗ Erro'; setTimeout(() => { statusEl.textContent = ''; }, 4000); }
    if (regenBtn) { regenBtn.disabled = false; regenBtn.classList.remove('icon-spin'); }
    showToast(`Erro: ${job.detail}`, true);
  } else {
    if (statusEl) statusEl.textContent = `${job.detail || 'Processando...'}`;
    setTimeout(() => pollRegen(job_id, id, type), 3000);
  }
}

// --- Source image toggle ---
function toggleSourceImage(id) {
  const imgEl = document.getElementById(`img-${id}`);
  const sourceBtn = document.getElementById(`source-btn-${id}`);
  const sourceBtnLabel = document.getElementById(`source-btn-label-${id}`);
  const sourceBtnIcon = document.getElementById(`source-btn-icon-${id}`);
  const dlBtn = document.getElementById(`dl-btn-${id}`);

  if (_sourceView[id]) {
    imgEl.src = `/api/image/${parishId}/${dateStr}/${id}`;
    imgEl.style.objectFit = '';
    imgEl.style.background = '';
    _sourceView[id] = false;
    if (sourceBtn) { sourceBtn.classList.add('btn-secondary'); sourceBtn.classList.remove('btn-primary'); }
    if (sourceBtnIcon) sourceBtnIcon.style.filter = 'brightness(0) opacity(.5)';
    if (sourceBtnLabel) sourceBtnLabel.textContent = 'Ver imagem fonte';
    if (dlBtn) dlBtn.href = `/api/image/${parishId}/${dateStr}/${id}`;
  } else {
    imgEl.src = `/api/source-crop/${parishId}/${dateStr}/${id}?t=${Date.now()}`;
    imgEl.style.objectFit = 'contain';
    imgEl.style.background = '#f1f5f9';
    _sourceView[id] = true;
    if (sourceBtn) { sourceBtn.classList.remove('btn-secondary'); sourceBtn.classList.add('btn-primary'); }
    if (sourceBtnIcon) sourceBtnIcon.style.filter = 'brightness(0) invert(1)';
    if (sourceBtnLabel) sourceBtnLabel.textContent = 'Ver imagem gerada';
    if (dlBtn) dlBtn.href = `/api/source-crop/${parishId}/${dateStr}/${id}`;
  }
}

// --- Manual crop ---
function _updateZoomUI() {
  const pct = Math.round(_cropState.zoom * 100);
  const label = document.getElementById('crop-zoom-label');
  if (label) label.textContent = pct + '%';
  const idx = ZOOM_STEPS.indexOf(_cropState.zoom);
  const btnIn  = document.getElementById('crop-zoom-in');
  const btnOut = document.getElementById('crop-zoom-out');
  if (btnIn)  btnIn.disabled  = idx >= ZOOM_STEPS.length - 1;
  if (btnOut) btnOut.disabled = idx <= 0;
}

function changeCropZoom(delta) {
  const idx = ZOOM_STEPS.indexOf(_cropState.zoom);
  const next = idx + delta;
  if (next < 0 || next >= ZOOM_STEPS.length) return;
  _cropState.zoom = ZOOM_STEPS[next];
  applyCropZoom();
  _updateZoomUI();
}

function toggleCropPan() {
  _cropState.panMode = !_cropState.panMode;
  const btn = document.getElementById('crop-pan-btn');
  const canvas = document.getElementById('crop-canvas');
  if (btn) {
    btn.style.background   = _cropState.panMode ? '#e0f2fe' : '';
    btn.style.borderColor  = _cropState.panMode ? '#0ea5e9' : '';
    btn.style.color        = _cropState.panMode ? '#0369a1' : '';
  }
  if (canvas) canvas.style.cursor = _cropState.panMode ? 'grab' : 'crosshair';
}

function applyCropZoom() {
  const img = document.getElementById('crop-page-img');
  const canvas = document.getElementById('crop-canvas');
  const scrollEl = document.querySelector('.crop-scroll');
  if (!_cropState.baseW || !img || !scrollEl) return;

  // Anchor point: center of selection, or scroll viewport center if no selection
  let anchorPctX = 0.5, anchorPctY = 0.5;
  if (_cropState.sel) {
    anchorPctX = (_cropState.sel.left + _cropState.sel.right) / 2;
    anchorPctY = (_cropState.sel.top  + _cropState.sel.bottom) / 2;
  }
  const oldW = img.offsetWidth;
  const oldH = img.offsetHeight;
  const viewX = anchorPctX * oldW - scrollEl.scrollLeft;
  const viewY = anchorPctY * oldH - scrollEl.scrollTop;

  if (_cropState.zoom === 1.0) {
    img.style.width = '';
    img.style.maxWidth = '';
    img.style.maxHeight = '';
  } else {
    img.style.width = (_cropState.baseW * _cropState.zoom) + 'px';
    img.style.maxWidth = 'none';
    img.style.maxHeight = 'none';
  }
  requestAnimationFrame(() => {
    canvas.width = img.offsetWidth;
    canvas.height = img.offsetHeight;
    // Adjust scroll so anchor stays at the same viewport position
    scrollEl.scrollLeft = anchorPctX * img.offsetWidth  - viewX;
    scrollEl.scrollTop  = anchorPctY * img.offsetHeight - viewY;
    if (_cropState.sel) {
      drawCropRect(_cropState.sel.left, _cropState.sel.top, _cropState.sel.right, _cropState.sel.bottom);
    } else {
      canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
    }
  });
}

async function openManualCrop(id) {
  if (_activeImageGenAnnId !== null) { _showImageBusy(); return; }
  const locRes = await fetch(`/api/location/${parishId}/${dateStr}/${id}`);
  if (!locRes.ok) { showToast('Localização fonte não disponível.', true); return; }
  const loc = await locRes.json();

  _cropState.annId = id;
  _cropState.page = loc.page;
  _cropState.sel = { top: loc.top, left: loc.left, bottom: loc.bottom, right: loc.right };
  _cropState.drawing = false;
  _cropState.zoom = 1.0;
  _cropState.baseW = null;
  _cropState.panMode = false;

  const img = document.getElementById('crop-page-img');
  img.style.width = '';
  img.style.maxWidth = '';
  img.style.maxHeight = '';
  const panBtn = document.getElementById('crop-pan-btn');
  if (panBtn) { panBtn.style.background = ''; panBtn.style.borderColor = ''; }
  img.src = `/api/bulletin-page/${parishId}/${dateStr}/${id}`;
  document.getElementById('crop-modal').classList.add('open');
  _updateZoomUI();

  if (img.complete && img.naturalWidth) {
    setupCropCanvas();
  } else {
    img.onload = setupCropCanvas;
  }
}

function drawCropRect(left, top, right, bottom) {
  const canvas = document.getElementById('crop-canvas');
  const ctx = canvas.getContext('2d');
  const cw = canvas.width, ch = canvas.height;

  ctx.clearRect(0, 0, cw, ch);
  ctx.fillStyle = 'rgba(0,0,0,0.5)';
  ctx.fillRect(0, 0, cw, ch);

  const x = left * cw, y = top * ch, rw = (right - left) * cw, rh = (bottom - top) * ch;
  ctx.clearRect(x, y, rw, rh);

  ctx.strokeStyle = '#fbd024';
  ctx.lineWidth = 2;
  ctx.strokeRect(x, y, rw, rh);

  // 8 handles: corners (proportional) + edge midpoints (single-axis)
  const HS = 5;
  const mx = (left + right) / 2 * cw, my = (top + bottom) / 2 * ch;
  [
    [x,       y      ], [mx,       y      ], [right * cw, y      ],
    [x,       my     ],                       [right * cw, my     ],
    [x,       bottom * ch], [mx, bottom * ch], [right * cw, bottom * ch],
  ].forEach(([hx, hy]) => {
    ctx.fillStyle = '#fff';
    ctx.fillRect(hx - HS, hy - HS, HS * 2, HS * 2);
    ctx.strokeStyle = '#1a3a5c';
    ctx.lineWidth = 1;
    ctx.strokeRect(hx - HS, hy - HS, HS * 2, HS * 2);
  });
}

function setupCropCanvas() {
  const img = document.getElementById('crop-page-img');
  const canvas = document.getElementById('crop-canvas');
  if (!_cropState.baseW) _cropState.baseW = img.offsetWidth;
  canvas.width = img.offsetWidth;
  canvas.height = img.offsetHeight;

  if (_cropState.sel) drawCropRect(_cropState.sel.left, _cropState.sel.top, _cropState.sel.right, _cropState.sel.bottom);

  let mode = null; // null | 'draw' | 'move' | 'resize' | 'pan'
  let activeHandle = null;
  let moveOfsX = 0, moveOfsY = 0;
  let panStartX = 0, panStartY = 0, panScrollLeft = 0, panScrollTop = 0;
  const HIT = 10;

  function pct(e) {
    const r = canvas.getBoundingClientRect();
    return {
      px: e.clientX - r.left,
      py: e.clientY - r.top,
      x: Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)),
      y: Math.max(0, Math.min(1, (e.clientY - r.top) / r.height)),
    };
  }

  function getHandles() {
    const s = _cropState.sel;
    if (!s) return [];
    const cw = canvas.width, ch = canvas.height;
    const mx = (s.left + s.right) / 2, my = (s.top + s.bottom) / 2;
    return [
      { id: 'tl', x: s.left,  y: s.top,    cursor: 'nwse-resize' },
      { id: 'tc', x: mx,      y: s.top,    cursor: 'ns-resize'   },
      { id: 'tr', x: s.right, y: s.top,    cursor: 'nesw-resize' },
      { id: 'ml', x: s.left,  y: my,       cursor: 'ew-resize'   },
      { id: 'mr', x: s.right, y: my,       cursor: 'ew-resize'   },
      { id: 'bl', x: s.left,  y: s.bottom, cursor: 'nesw-resize' },
      { id: 'bc', x: mx,      y: s.bottom, cursor: 'ns-resize'   },
      { id: 'br', x: s.right, y: s.bottom, cursor: 'nwse-resize' },
    ].map(h => ({ ...h, px: h.x * cw, py: h.y * ch }));
  }

  function hitHandle(px, py) {
    return getHandles().find(h => Math.abs(px - h.px) <= HIT && Math.abs(py - h.py) <= HIT) || null;
  }

  function isInsideSel(px, py) {
    const s = _cropState.sel;
    if (!s || hitHandle(px, py)) return false;
    const cw = canvas.width, ch = canvas.height;
    return px > s.left * cw && px < s.right * cw && py > s.top * ch && py < s.bottom * ch;
  }

  let _docPanMove = null, _docPanUp = null;

  function _startDocPan() {
    _docPanMove = (ev) => {
      const scrollEl = document.querySelector('.crop-scroll');
      scrollEl.scrollLeft = panScrollLeft - (ev.clientX - panStartX);
      scrollEl.scrollTop  = panScrollTop  - (ev.clientY - panStartY);
    };
    _docPanUp = () => {
      document.removeEventListener('mousemove', _docPanMove);
      document.removeEventListener('mouseup',   _docPanUp);
      _docPanMove = null; _docPanUp = null;
      mode = null;
      canvas.style.cursor = (_cropState.panMode || _cropState.sel) ? 'grab' : 'crosshair';
    };
    document.addEventListener('mousemove', _docPanMove);
    document.addEventListener('mouseup',   _docPanUp);
  }

  canvas.onmousemove = (e) => {
    if (mode === 'pan') return; // handled by document listener
    const { px, py, x, y } = pct(e);
    if (mode === 'draw') {
      drawCropRect(Math.min(_cropState.sx, x), Math.min(_cropState.sy, y),
                   Math.max(_cropState.sx, x), Math.max(_cropState.sy, y));
    } else if (mode === 'move') {
      const s = _cropState.sel;
      const w = s.right - s.left, h = s.bottom - s.top;
      const nl = Math.max(0, Math.min(1 - w, x - moveOfsX));
      const nt = Math.max(0, Math.min(1 - h, y - moveOfsY));
      _cropState.sel = { left: nl, top: nt, right: nl + w, bottom: nt + h };
      drawCropRect(nl, nt, nl + w, nt + h);
    } else if (mode === 'resize') {
      let { left, top, right, bottom } = _cropState.sel;
      const { id } = activeHandle;
      switch (id) {
        case 'tl': left  = Math.max(0, Math.min(right  - 0.02, x)); top    = Math.max(0, Math.min(bottom - 0.02, y)); break;
        case 'tr': right = Math.min(1, Math.max(left   + 0.02, x)); top    = Math.max(0, Math.min(bottom - 0.02, y)); break;
        case 'bl': left  = Math.max(0, Math.min(right  - 0.02, x)); bottom = Math.min(1, Math.max(top    + 0.02, y)); break;
        case 'br': right = Math.min(1, Math.max(left   + 0.02, x)); bottom = Math.min(1, Math.max(top    + 0.02, y)); break;
        case 'tc': top    = Math.max(0, Math.min(bottom - 0.02, y)); break;
        case 'bc': bottom = Math.min(1, Math.max(top    + 0.02, y)); break;
        case 'ml': left   = Math.max(0, Math.min(right  - 0.02, x)); break;
        case 'mr': right  = Math.min(1, Math.max(left   + 0.02, x)); break;
      }
      _cropState.sel = { left, top, right, bottom };
      drawCropRect(left, top, right, bottom);
    } else {
      const h = hitHandle(px, py);
      if (h) { canvas.style.cursor = h.cursor; return; }
      if (isInsideSel(px, py)) { canvas.style.cursor = 'grab'; return; }
      canvas.style.cursor = (_cropState.panMode || _cropState.sel) ? 'grab' : 'crosshair';
    }
  };

  canvas.onmousedown = (e) => {
    const { px, py, x, y } = pct(e);
    const h = hitHandle(px, py);
    if (h) {
      mode = 'resize'; activeHandle = h;
      canvas.style.cursor = h.cursor;
    } else if (isInsideSel(px, py)) {
      mode = 'move';
      moveOfsX = x - _cropState.sel.left;
      moveOfsY = y - _cropState.sel.top;
      canvas.style.cursor = 'grabbing';
    } else if (_cropState.panMode || _cropState.sel) {
      mode = 'pan';
      panStartX = e.clientX; panStartY = e.clientY;
      const scrollEl = document.querySelector('.crop-scroll');
      panScrollLeft = scrollEl.scrollLeft; panScrollTop = scrollEl.scrollTop;
      canvas.style.cursor = 'grabbing';
      _startDocPan();
    } else {
      mode = 'draw';
      _cropState.sx = x; _cropState.sy = y; _cropState.ex = x; _cropState.ey = y;
    }
    e.preventDefault();
  };

  canvas.onmouseup = (e) => {
    if (mode === 'pan') return; // handled by document listener
    const { px, py, x, y } = pct(e);
    if (mode === 'draw') {
      _cropState.sel = {
        left: Math.min(_cropState.sx, x), top: Math.min(_cropState.sy, y),
        right: Math.max(_cropState.sx, x), bottom: Math.max(_cropState.sy, y),
      };
    }
    mode = null; activeHandle = null;
    const h = hitHandle(px, py);
    canvas.style.cursor = h ? h.cursor : (isInsideSel(px, py) ? 'grab' : 'crosshair');
  };

  canvas.onmouseleave = (e) => { if (mode && mode !== 'pan') canvas.onmouseup(e); };
}

async function _persistCropSel() {
  const { annId, page, sel } = _cropState;
  if (!sel || (sel.right - sel.left) < 0.01 || (sel.bottom - sel.top) < 0.01) return false;
  const res = await fetch(`/api/manual-crop/${parishId}/${dateStr}/${annId}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ page, top: sel.top, left: sel.left, bottom: sel.bottom, right: sel.right }),
  });
  return res.ok;
}

async function saveManualCrop() {
  const { annId, sel } = _cropState;
  if (!annId || !sel) { showToast('Selecione uma área primeiro.', true); return; }
  if ((sel.right - sel.left) < 0.01 || (sel.bottom - sel.top) < 0.01) {
    showToast('Área muito pequena. Arraste para selecionar uma região maior.', true);
    return;
  }
  const ok = await _persistCropSel();
  closeModal('crop-modal');
  if (ok) {
    showToast('Marcação salva. Clique em "Gerar Novamente" para aplicar.');
    if (_sourceView[annId]) {
      const imgEl = document.getElementById(`img-${annId}`);
      if (imgEl) imgEl.src = `/api/source-crop/${parishId}/${dateStr}/${annId}?t=${Date.now()}`;
    }
  } else {
    showToast('Erro ao salvar marcação.', true);
  }
}

async function clearManualCrop() {
  const { annId } = _cropState;
  if (!annId) return;
  const res = await fetch(`/api/manual-crop/${parishId}/${dateStr}/${annId}`, { method: 'DELETE' });
  if (res.ok) {
    _cropState.sel = null;
    const canvas = document.getElementById('crop-canvas');
    if (canvas) canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
    showToast('Marcação removida. O agente irá re-localizar automaticamente.');
  } else {
    showToast('Erro ao remover marcação.', true);
  }
}

async function regenFromCrop() {
  const { annId, sel } = _cropState;
  if (!annId) return;

  // Save selection before showing popup
  if (sel && (sel.right - sel.left) >= 0.01 && (sel.bottom - sel.top) >= 0.01) {
    await _persistCropSel();
  }

  // Show confirmation popup asking about texts
  document.getElementById('update-texts-modal').classList.add('open');
}

async function _doRegenFromCrop(updateTexts) {
  document.getElementById('update-texts-modal').classList.remove('open');

  const { annId } = _cropState;
  if (!annId) return;

  if (_activeImageGenAnnId !== null) { _showImageBusy(); return; }
  const ann = allAnnouncements.find(a => String(a.id) === String(annId));
  _activeImageGenAnnId = annId;
  _activeImageGenTitle = ann?.title || String(annId);

  const overlay = document.getElementById('crop-regen-overlay');
  const overlayText = document.getElementById('crop-regen-overlay-text');
  const setSaving = (saving, text) => {
    if (overlay) overlay.classList.toggle('active', saving);
    if (overlayText && text) overlayText.textContent = text;
  };

  setSaving(true, 'Gerando nova imagem...');

  const res = await fetch(`/api/regen/image/${parishId}/${dateStr}/${annId}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ instruction: '' }),
  });
  const { job_id } = await res.json();

  const poll = async () => {
    const job = await fetch(`/api/regen/status/${job_id}`)
      .then(r => { if (!r.ok) return null; return r.json(); })
      .catch(() => null);
    if (!job) { _clearActiveImageGen(); setSaving(false); return; }
    if (job.status === 'done') {
      _clearActiveImageGen();
      setSaving(false);
      _cropState.fromComparison = false;
      closeModal('crop-modal');
      resetRegenArea(annId, 'image');
      if (_sourceView[annId]) toggleSourceImage(annId);
      const newUrl = `/api/image/${parishId}/${dateStr}/${annId}?t=${Date.now()}`;
      if (job.has_backup) {
        showImageComparison(annId, newUrl);
      } else {
        const imgEl = document.getElementById(`img-${annId}`);
        if (imgEl) imgEl.src = newUrl;
        showToast('Imagem gerada com sucesso!');
      }
      if (updateTexts) {
        showHtmlComparisonPending(annId);
        fetch(`/api/regen/content/${parishId}/${dateStr}/${annId}`, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ instruction: '', use_crop: true }),
        }).then(r => r.json()).then(({ job_id }) => pollRegen(job_id, annId, 'content'));
      }
    } else if (job.status === 'error') {
      _clearActiveImageGen();
      setSaving(false);
      showToast(`Erro: ${job.detail}`, true);
    } else {
      setSaving(true, job.detail || 'Gerando nova imagem...');
      setTimeout(poll, 3000);
    }
  };
  poll();
}

// --- Comparison modal helpers ---

function _resetCompareState() {
  _compareState.annId = null;
  _compareState.hasImage = false; _compareState.imageChoice = 'new';
  _compareState.hasHtml = false;  _compareState.htmlChoice = 'new';
  _compareState.pendingNewHtml = null;
  _compareState.hasHtmlBackup = false; _compareState.originalHtml = null;
  _compareState.imageInstruction = ''; _compareState.htmlInstruction = '';
  const instrEl = document.getElementById('compare-instr');
  if (instrEl) instrEl.value = '';
  const confirmBtn = document.querySelector('#compare-modal .btn-primary');
  if (confirmBtn) confirmBtn.disabled = false;
}

function selectImageChoice(choice) {
  _compareState.imageChoice = choice;
  const oldWrap = document.getElementById('cmp-img-old');
  const newWrap = document.getElementById('cmp-img-new');
  const oldImg  = document.getElementById('compare-old-img');
  const newImg  = document.getElementById('compare-new-img');
  if (choice === 'old') {
    if (oldWrap) oldWrap.style.opacity = '1';
    if (newWrap) newWrap.style.opacity = '.45';
    if (oldImg)  oldImg.style.outline  = '3px solid #fbd024';
    if (newImg)  newImg.style.outline  = '2px solid transparent';
  } else {
    if (newWrap) newWrap.style.opacity = '1';
    if (oldWrap) oldWrap.style.opacity = '.45';
    if (newImg)  newImg.style.outline  = '3px solid #fbd024';
    if (oldImg)  oldImg.style.outline  = '2px solid transparent';
  }
}

function selectHtmlChoice(choice) {
  _compareState.htmlChoice = choice;
  const oldWrap  = document.getElementById('cmp-html-old');
  const newWrap  = document.getElementById('cmp-html-new');
  const oldFrame = document.getElementById('compare-old-html');
  const newFrame = document.getElementById('compare-new-html');
  if (choice === 'old') {
    if (oldWrap)  oldWrap.style.opacity  = '1';
    if (newWrap)  newWrap.style.opacity  = '.45';
    if (oldFrame) oldFrame.style.border  = '2px solid #fbd024';
    if (newFrame) newFrame.style.border  = '1px solid #e8ecf0';
  } else {
    if (newWrap)  newWrap.style.opacity  = '1';
    if (oldWrap)  oldWrap.style.opacity  = '.45';
    if (newFrame) newFrame.style.border  = '2px solid #fbd024';
    if (oldFrame) oldFrame.style.border  = '1px solid #e8ecf0';
  }
}

function closeCompareModal() {
  closeModal('compare-modal');
  _hideCompareSource();
  _resetCompareState();
}

function _hideCompareSource() {
  const row = document.getElementById('compare-source-row');
  const btn = document.getElementById('compare-source-btn');
  const icon = document.getElementById('compare-source-btn-icon');
  const label = document.getElementById('compare-source-btn-label');
  if (row) row.style.display = 'none';
  if (btn) { btn.classList.add('btn-secondary'); btn.classList.remove('btn-primary'); }
  if (icon) icon.style.filter = 'brightness(0) opacity(.5)';
  if (label) label.textContent = ' Ver imagem fonte';
}

function toggleCompareSource() {
  const { annId } = _compareState;
  if (!annId) return;
  const row = document.getElementById('compare-source-row');
  const img = document.getElementById('compare-source-img');
  const btn = document.getElementById('compare-source-btn');
  const icon = document.getElementById('compare-source-btn-icon');
  const label = document.getElementById('compare-source-btn-label');
  const visible = row && row.style.display !== 'none';
  if (visible) {
    if (row) row.style.display = 'none';
    if (btn) { btn.classList.add('btn-secondary'); btn.classList.remove('btn-primary'); }
    if (icon) icon.style.filter = 'brightness(0) opacity(.5)';
    if (label) label.textContent = ' Ver imagem fonte';
  } else {
    if (img) img.src = `/api/source-crop/${parishId}/${dateStr}/${annId}?t=${Date.now()}`;
    if (row) row.style.display = 'block';
    if (btn) { btn.classList.remove('btn-secondary'); btn.classList.add('btn-primary'); }
    if (icon) icon.style.filter = 'brightness(0) invert(1)';
    if (label) label.textContent = ' Ver imagem gerada';
  }
}

async function reopenCropFromComparison() {
  const { annId } = _compareState;
  if (!annId) return;
  closeModal('compare-modal');
  _cropState.fromComparison = true;
  await openManualCrop(annId);
}

function cancelCrop() {
  closeModal('crop-modal');
  if (_cropState.fromComparison) {
    _cropState.fromComparison = false;
    document.getElementById('compare-modal').classList.add('open');
  }
}

async function confirmComparison() {
  const { annId, hasImage, imageChoice, hasHtml, htmlChoice, pendingNewHtml } = _compareState;
  if (!annId) return;

  const confirmBtn = document.querySelector('#compare-modal .btn-primary');
  if (confirmBtn) confirmBtn.disabled = true;

  if (hasImage) {
    const imgEl = document.getElementById(`img-${annId}`);
    if (imgEl) { imgEl.style.objectFit = ''; imgEl.style.background = ''; }
    if (_sourceView[annId]) { _sourceView[annId] = false; }
    if (imageChoice === 'new') {
      fetch(`/api/image-backup/${parishId}/${dateStr}/${annId}`, { method: 'DELETE' });
      if (imgEl) imgEl.src = `/api/image/${parishId}/${dateStr}/${annId}?t=${Date.now()}`;
    } else {
      await fetch(`/api/restore-image/${parishId}/${dateStr}/${annId}`, { method: 'POST' });
      if (imgEl) imgEl.src = `/api/image/${parishId}/${dateStr}/${annId}?t=${Date.now()}`;
    }
  }

  if (hasHtml) {
    if (htmlChoice === 'new') {
      if (_compareState.hasHtmlBackup) {
        fetch(`/api/html-backup/${parishId}/${dateStr}/${annId}`, { method: 'DELETE' });
      }
      if (pendingNewHtml) {
        const ann = allAnnouncements.find(a => String(a.id) === String(annId));
        if (ann) {
          ann.html_content = pendingNewHtml;
          const editor = document.getElementById(`editor-${annId}`);
          if (editor) editor.value = pendingNewHtml;
          refreshPreview(annId);
        }
      }
    } else {
      if (_compareState.hasHtmlBackup) {
        const res = await fetch(`/api/restore-html/${parishId}/${dateStr}/${annId}`, { method: 'POST' });
        const data = await res.json();
        if (data.html) {
          const ann = allAnnouncements.find(a => String(a.id) === String(annId));
          if (ann) {
            ann.html_content = data.html;
            const editor = document.getElementById(`editor-${annId}`);
            if (editor) editor.value = data.html;
            refreshPreview(annId);
          }
        }
      } else if (_compareState.originalHtml) {
        const ann = allAnnouncements.find(a => String(a.id) === String(annId));
        if (ann) {
          ann.html_content = _compareState.originalHtml;
          const editor = document.getElementById(`editor-${annId}`);
          if (editor) editor.value = _compareState.originalHtml;
          refreshPreview(annId);
        }
      }
    }
  }

  const imgMsg  = hasImage ? (imageChoice === 'new' ? 'nova imagem' : 'imagem anterior') : null;
  const htmlMsg = hasHtml  ? (htmlChoice  === 'new' ? 'novo texto'  : 'texto anterior')  : null;
  const parts   = [imgMsg, htmlMsg].filter(Boolean);
  showToast(`Seleção aplicada: ${parts.join(' + ')}.`);

  closeCompareModal();
}

function showImageComparison(annId, newUrl) {
  _compareState.annId = annId;
  _compareState.hasImage = true;
  _compareState.imageChoice = 'new';
  document.getElementById('compare-old-img').src = `/api/image-backup/${parishId}/${dateStr}/${annId}?t=${Date.now()}`;
  document.getElementById('compare-new-img').src = newUrl;
  document.getElementById('compare-image-section').style.display = '';
  document.getElementById('compare-html-section').style.display = 'none';
  document.getElementById('compare-html-divider').style.display = 'none';
  // Apply default visual state: new selected
  selectImageChoice('new');
  document.getElementById('compare-modal').classList.add('open');
}

function showHtmlComparisonPending(annId) {
  _compareState.annId = annId;
  _compareState.hasHtml = true;
  _compareState.htmlChoice = 'new';
  document.getElementById('compare-html-pending').style.display = 'flex';
  document.getElementById('compare-html-content').style.display = 'none';
  document.getElementById('compare-html-divider').style.display = '';
  document.getElementById('compare-html-section').style.display = '';
  document.getElementById('compare-modal').classList.add('open');
}

function populateHtmlComparison(annId, oldHtml, newHtml, hasBackup = false) {
  _compareState.annId = annId;
  _compareState.hasHtml = true;
  _compareState.htmlChoice = 'new';
  _compareState.pendingNewHtml = newHtml;
  _compareState.hasHtmlBackup = hasBackup;
  _compareState.originalHtml = oldHtml;

  const oldFrame = document.getElementById('compare-old-html');
  const newFrame = document.getElementById('compare-new-html');
  setFrame(oldFrame, getLangContent(oldHtml, 'en'));
  setFrame(newFrame, getLangContent(newHtml, 'en'));

  document.getElementById('compare-html-pending').style.display = 'none';
  document.getElementById('compare-html-content').style.display = '';
  document.getElementById('compare-html-divider').style.display = '';
  document.getElementById('compare-html-section').style.display = '';

  // Apply default visual state: new selected
  selectHtmlChoice('new');

  if (!_compareState.hasImage) {
    document.getElementById('compare-image-section').style.display = 'none';
  }
  document.getElementById('compare-modal').classList.add('open');
}

async function regenFromComparison() {
  const { annId, hasImage, hasHtml } = _compareState;
  if (!annId) return;

  const instrEl     = document.getElementById('compare-instr');
  const instruction = instrEl?.value?.trim() || '';
  const overlay     = document.getElementById('compare-regen-overlay');
  const overlayText = document.getElementById('compare-regen-overlay-text');
  const regenBtn    = document.getElementById('compare-regen-btn');
  const confirmBtn  = document.querySelector('#compare-modal .btn-primary');
  const cancelBtn   = document.getElementById('compare-cancel-btn');

  if (overlay) overlay.style.display = 'flex';
  if (overlayText) overlayText.textContent = 'Gerando...';
  [regenBtn, confirmBtn, cancelBtn].forEach(b => { if (b) b.disabled = true; });

  try {
    if (hasImage) {
      await fetch(`/api/restore-image/${parishId}/${dateStr}/${annId}`, { method: 'POST' });
      const res = await fetch(`/api/regen/image/${parishId}/${dateStr}/${annId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instruction }),
      });
      const { job_id } = await res.json();
      await _pollRegenForModal(job_id, annId, 'image', overlayText);
    }
    if (hasHtml) {
      if (_compareState.hasHtmlBackup) {
        await fetch(`/api/restore-html/${parishId}/${dateStr}/${annId}`, { method: 'POST' });
      }
      const res = await fetch(`/api/regen/content/${parishId}/${dateStr}/${annId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instruction }),
      });
      const { job_id } = await res.json();
      await _pollRegenForModal(job_id, annId, 'content', overlayText);
    }
    if (instrEl) instrEl.value = '';
  } catch (e) {
    showToast(`Erro ao gerar novamente: ${e.message}`, true);
  }

  if (overlay) overlay.style.display = 'none';
  [regenBtn, confirmBtn, cancelBtn].forEach(b => { if (b) b.disabled = false; });
}

async function _pollRegenForModal(job_id, annId, type, overlayText) {
  while (true) {
    const job = await fetch(`/api/regen/status/${job_id}`).then(r => r.json()).catch(() => null);
    if (!job) throw new Error('Job não encontrado');

    if (job.status === 'done') {
      if (type === 'image') {
        const newUrl = `/api/image/${parishId}/${dateStr}/${annId}?t=${Date.now()}`;
        _compareState.imageChoice = 'new';
        document.getElementById('compare-old-img').src = `/api/image-backup/${parishId}/${dateStr}/${annId}?t=${Date.now()}`;
        document.getElementById('compare-new-img').src = newUrl;
        selectImageChoice('new');
      } else {
        if (job.html) {
          const oldRes  = await fetch(`/api/html-backup/${parishId}/${dateStr}/${annId}`);
          const oldData = await oldRes.json();
          _compareState.pendingNewHtml  = job.html;
          _compareState.hasHtmlBackup   = true;
          _compareState.originalHtml    = oldData.html || '';
          _compareState.htmlChoice      = 'new';
          setFrame(document.getElementById('compare-old-html'), getLangContent(oldData.html || '', 'en'));
          setFrame(document.getElementById('compare-new-html'), getLangContent(job.html, 'en'));
          document.getElementById('compare-html-pending').style.display = 'none';
          document.getElementById('compare-html-content').style.display = '';
          selectHtmlChoice('new');
        }
      }
      return;
    } else if (job.status === 'error') {
      throw new Error(job.detail || 'Erro desconhecido');
    } else {
      if (overlayText) overlayText.textContent = job.detail || 'Gerando...';
      await new Promise(r => setTimeout(r, 3000));
    }
  }
}

init();
