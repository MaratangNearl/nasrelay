'use strict';
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const terminal = status => ['succeeded','failed','cancelled'].includes(status);
let state = {connected:false,jobs:[],telegram:{},profile:null,profiles:[]};
let loggedIn = false;
let refreshInFlight = null;
let pendingTransfer = null;
let folderSide = null;
let folderProfileId = null;
let folderParent = '/';
let editingProfileId = null;
let account = null;
let recoveryCodes = [];
let authEpoch = 0;
let authChangesInFlight = 0;
const panes = {};
function formatSize(value) {
  let n = Number(value) || 0;
  const units = ['B','KiB','MiB','GiB','TiB'];
  let unit = 0;
  while(n >= 1024 && unit < units.length - 1) { n /= 1024; unit++; }
  return `${n.toLocaleString('ko-KR', {maximumFractionDigits:unit ? 1 : 0})} ${units[unit]}`;
}
function fileTimestamp(value) {
  if (!value) return null;
  // FTP MLSD dates are UTC without separators; NAS/WebDAV use ISO or HTTP dates.
  const ftp = /^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\.\d+)?$/.exec(value);
  const timestamp = Date.parse(ftp ? `${ftp[1]}-${ftp[2]}-${ftp[3]}T${ftp[4]}:${ftp[5]}:${ftp[6]}${ftp[7] || ''}Z` : value);
  return Number.isFinite(timestamp) ? timestamp : null;
}
function formatFileDate(value) {
  const timestamp = fileTimestamp(value);
  return timestamp === null ? '—' : new Date(timestamp).toLocaleString('ko-KR', {
    year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false
  });
}
function toast(message, error = false) {
  const element = document.createElement('div');
  element.className = `toast${error ? ' error' : ''}`;
  element.textContent = message;
  $('#toasts').append(element);
  setTimeout(() => element.remove(), error ? 9000 : 5000);
}
async function api(path, options = {}) {
  const changesSession = ['/login','/logout','/auth/password','/auth/totp/enable','/auth/totp'].includes(path);
  if (changesSession) { authChangesInFlight++; authEpoch++; }
  const requestEpoch = authEpoch;
  try {
    let response;
    try {
      response = await fetch(`/api${path}`, {credentials:'same-origin', ...options,
        headers:{'Content-Type':'application/json','X-Relay-Request':'1',...options.headers}});
    } catch (_) { throw new Error('NAS에 연결할 수 없습니다. 네트워크를 확인하세요.'); }
    let body;
    try { body = await response.json(); } catch (_) { throw new Error('서버 응답을 확인할 수 없습니다.'); }
    if (!response.ok) {
      const stale = requestEpoch !== authEpoch || (!changesSession && authChangesInFlight > 0);
      if (response.status === 401 && path !== '/login' && !stale) showLogin();
      if (response.status === 403 && !stale && String(body.detail).includes('비밀번호를 변경')) { loggedIn = false; bootstrap(); }
      const detail = Array.isArray(body.detail) ? body.detail.map(x => `${x.loc.slice(1).join('.')}: ${x.msg}`).join('\n') : body.detail;
      const error = new Error(detail || '요청을 처리할 수 없습니다.');
      error.staleAuth = response.status === 401 && stale;
      throw error;
    }
    return body;
  } finally {
    if (changesSession) { authChangesInFlight--; authEpoch++; }
  }
}
function showLogin() {
  loggedIn = false;
  $('#shell').hidden = true;
  $$('dialog[open]').forEach(dialog => dialog.close());
  $('#loginForm').reset();
  $('#passwordForm').reset();
  recoveryCodes = []; $('#recoveryCodes').replaceChildren();
  $('#loginCodeField').hidden = true;
  $('#loginForm input[name=code]').required = false;
  $('#loginDialog').showModal();
}
function showDialog(id) {
  const dialog = $(id);
  const error = $('.form-error', dialog);
  if (error) error.textContent = '';
  if (!dialog.open) dialog.showModal();
}
async function formAction(form, action) {
  const buttons = $$('button', form);
  const error = $('.form-error', form);
  if (error) error.textContent = '';
  buttons.forEach(button => button.disabled = true);
  form.setAttribute('aria-busy','true');
  try { await action(); }
  catch (err) { if (error) error.textContent = err.message; else toast(err.message,true); }
  finally { buttons.forEach(button => button.disabled = false); form.removeAttribute('aria-busy'); }
}
function updateConnection(failed = state.profile?.connection_error || '') {
  const connected = state.connected;
  const badge = $('#connectionBadge');
  badge.className = `status-badge${connected ? ' connected' : failed ? ' failed' : ''}`;
  badge.innerHTML = `<span></span>${connected ? '연결됨' : failed ? '연결 실패' : '연결 안 됨'}`;
  $('#connectionName').textContent = state.profile?.name || '원격 서버';
  $('#connectionDescription').textContent = failed || (state.profile ? `${state.profile.host}:${state.profile.port} · ${state.profile.username}` : '파일 전송을 시작하려면 서버를 연결하세요.');
  $('#connectOpen span').textContent = '프로필 관리';
  const profiles = state.profiles || [];
  $('#profileSelect').innerHTML = profiles.length ? profiles.map(profile => `<option value="${escapeHTML(profile.id)}">${profile.connected ? '● ' : ''}${escapeHTML(profile.name)} · ${profile.protocol.toUpperCase()}</option>`).join('') : '<option value="">저장된 프로필 없음</option>';
  $('#profileSelect').value = state.selected_profile_id || '';
  $('#profileSelect').disabled = !profiles.length;
  $('#connectedCount').textContent = `${profiles.filter(profile => profile.connected).length}개 연결됨`;
  $('#connectSelected').disabled = !state.profile || connected;
  $('[data-side=ftp] .protocol-tag').textContent = (state.profile?.protocol || 'ftp').toUpperCase();
  renderProfiles();
  $('#disconnect').hidden = !connected;
  $('#ftpSubtitle').textContent = connected ? state.profile.host : '연결된 서버 없음';
  panes.ftp?.selectionChanged();
  panes.nas?.selectionChanged();
}
function protocolOptions(changed = false) {
  const form = $('#connectForm');
  const protocol = form.elements.protocol.value;
  if (changed) {
    form.elements.tls.checked = protocol === 'webdav';
    form.elements.port.value = protocol === 'sftp' ? 22 : protocol === 'webdav' ? 443 : 21;
  }
  $('#sftpOptions').hidden = protocol !== 'sftp';
  $('#encodingOption').hidden = protocol !== 'ftp';
  $('#passiveOption').hidden = protocol !== 'ftp';
  $('#tlsOption').hidden = protocol === 'sftp';
  $('#tlsLabel').textContent = protocol === 'webdav' ? 'HTTPS 사용' : '명시적 TLS (FTPS)';
}
function renderProfiles() {
  $('#profilesList').innerHTML = (state.profiles || []).map(profile => `<div class="profile-row" data-profile="${escapeHTML(profile.id)}"><div><strong>${escapeHTML(profile.name)}</strong><small>${profile.protocol.toUpperCase()} · ${escapeHTML(profile.host)} · ${profile.connected ? '연결됨' : profile.connection_error ? '연결 실패' : '연결 안 됨'}</small></div><button type="button" class="text-button" data-profile-action="edit">수정</button><button type="button" class="text-button" data-profile-action="select">선택</button><button type="button" class="text-button" data-profile-action="${profile.connected ? 'disconnect' : 'connect'}">${profile.connected ? '해제' : '연결'}</button><button type="button" class="text-button delete-profile" data-profile-action="delete">삭제</button></div>`).join('');
}
function editProfile(profile = null) {
  const form = $('#connectForm');
  form.reset();
  editingProfileId = profile?.id || null;
  if (profile) {
    for (const [key,value] of Object.entries(profile)) {
      const input = form.elements.namedItem(key);
      if (!input) continue;
      if (input.type === 'checkbox') input.checked = value;
      else input.value = value;
    }
  }
  form.elements.password.value = '';
  form.elements.password.placeholder = profile?.has_password ? '저장된 비밀번호 사용 (변경 시 입력)' : '서버 비밀번호';
  $('#profileEditorTitle').textContent = profile ? `${profile.name} · 프로필 수정` : '새 프로필';
  $('.form-error',form).textContent = '';
  protocolOptions();
}
function openConnect() {
  editProfile(state.profile);
  renderProfiles();
  showDialog('#connectDialog');
}
async function saveProfile(connect = false) {
  const form = $('#connectForm');
  const body = Object.fromEntries(new FormData(form));
  body.port = Number(body.port); body.tls = form.elements.tls.checked; body.passive = form.elements.passive.checked;
  const saved = await api(editingProfileId ? `/profiles/${editingProfileId}` : '/profiles', {method:editingProfileId ? 'PUT' : 'POST',body:JSON.stringify(body)});
  editingProfileId = saved.id;
  form.elements.password.value = '';
  try {
    if (connect) await api('/connection',{method:'POST',body:JSON.stringify({...body,id:saved.id})});
    $('#connectDialog').close();
    toast(connect ? '서버에 연결되었습니다.' : '프로필을 저장했습니다.');
  } catch (err) {
    await refreshState(false,true);
    updateConnection(err.message);
    throw err;
  }
  await refreshState(false,true);
}
async function selectProfile(id) {
  await api(`/profiles/${id}/select`,{method:'POST'});
  await refreshState(false,true);
}
function openTelegram() {
  const form = $('#telegramForm');
  form.elements.token.value = '';
  form.elements.token.placeholder = state.telegram.has_token ? '저장된 토큰 사용 (변경 시 입력)' : '123456789:AA…';
  form.elements.chat_id.value = state.telegram.chat_id || '';
  form.elements.enabled.checked = state.telegram.enabled || !state.telegram.has_token;
  showDialog('#telegramDialog');
}
function telegramValues() {
  const form = $('#telegramForm');
  return {token:form.elements.token.value.trim(),chat_id:form.elements.chat_id.value.trim(),enabled:form.elements.enabled.checked};
}
function mode() { return $('input[name="mode"]:checked').value; }
function prepareTransfer(side, paths = null) {
  const selected = paths || [...panes[side].selected];
  if (!selected.length || !state.connected) return;
  const destinationSide = side === 'ftp' ? 'nas' : 'ftp';
  pendingTransfer = {direction:side === 'ftp' ? 'download' : 'upload',mode:mode(),sources:selected,destination:panes[destinationSide].path,profile_id:state.profile.id};
  const moving = pendingTransfer.mode === 'move';
  $('#transferTitle').textContent = moving ? '파일 이동 확인' : '파일 복사 확인';
  $('#transferSummary').innerHTML = `<strong>${side === 'ftp' ? `${escapeHTML(state.profile.name)} → NAS` : `NAS → ${escapeHTML(state.profile.name)}`} · ${moving ? '이동' : '복사'}</strong><br>선택한 항목: ${selected.length.toLocaleString()}개 (폴더 내부 포함)<br>대상 폴더: <strong>${escapeHTML(pendingTransfer.destination)}</strong><ul>${selected.map(path => `<li>${escapeHTML(path)}</li>`).join('')}</ul>`;
  $('#transferWarning').textContent = moving ? '이동 작업은 전송 및 크기 검증이 끝난 파일의 원본을 삭제합니다. 같은 이름의 대상 파일이 있으면 원본을 보존하고 실패로 표시합니다.' : '원본은 유지됩니다. 같은 이름의 대상 파일은 덮어쓰지 않습니다. 작업은 브라우저를 닫아도 계속됩니다.';
  showDialog('#transferDialog');
}

class Pane {
  constructor(side) {
    this.side = side;
    this.root = $(`[data-side="${side}"]`);
    this.list = $('.file-list', this.root);
    this.path = '/';
    this.entries = [];
    this.selected = new Set();
    this.anchor = -1;
    this.generation = 0;
    this.suppressClick = false;
    this.sortKey = 'name';
    this.sortDirection = 1;
    $('.path-form',this.root).addEventListener('submit',event => { event.preventDefault(); this.load($('input',event.currentTarget).value); });
    $('.up',this.root).addEventListener('click',() => this.load(this.path.split('/').slice(0,-1).join('/') || '/'));
    $('.refresh',this.root).addEventListener('click',() => this.load(this.path));
    $('.filter',this.root).addEventListener('input',() => { this.anchor = -1; this.render(); });
    $('.sort-key',this.root).addEventListener('change',event => {
      this.sortKey = event.target.value;
      this.anchor = -1;
      this.render();
    });
    $('.sort-direction',this.root).addEventListener('click',event => {
      this.sortDirection *= -1;
      event.currentTarget.textContent = this.sortDirection === 1 ? '오름차순 ↑' : '내림차순 ↓';
      this.anchor = -1;
      this.render();
    });
    $$('.date-toggle',this.root).forEach(input => input.addEventListener('change',() => this.render()));
    $('.select-all',this.root).addEventListener('change',event => {
      this.visible().forEach(entry => event.target.checked ? this.selected.add(entry.path) : this.selected.delete(entry.path));
      this.paintSelection();
    });
    $('.new-folder',this.root).addEventListener('click',() => {
      if (this.side === 'ftp' && !state.connected) return openConnect();
      folderSide = this.side;
      folderProfileId = state.profile?.id;
      folderParent = this.path;
      $('#folderForm').reset();
      $('#folderLocation').textContent = `${this.side === 'ftp' ? state.profile.name : 'NAS'} · ${this.path}`;
      showDialog('#folderDialog');
    });
    this.list.addEventListener('click',event => this.click(event));
    this.list.addEventListener('keydown',event => {
      if ((event.ctrlKey || event.metaKey) && event.key === 'a' && event.target === this.list) {
        event.preventDefault(); this.visible().forEach(entry => this.selected.add(entry.path)); this.paintSelection();
      }
      if (event.key === 'Escape') { this.selected.clear(); this.paintSelection(); }
    });
    this.list.addEventListener('pointerdown',event => this.marquee(event));
    this.list.addEventListener('dragstart',event => {
      const handle = event.target.closest('.drag-handle');
      if (!handle) return event.preventDefault();
      const path = handle.closest('.file-row').dataset.path;
      if (!this.selected.has(path)) { this.selected = new Set([path]); this.paintSelection(); }
      event.dataTransfer.setData('application/x-nasrelay',JSON.stringify({side:this.side,paths:[...this.selected],profile_id:state.profile?.id}));
      event.dataTransfer.effectAllowed = 'copyMove';
    });
    this.root.addEventListener('dragover',event => {
      if (!event.dataTransfer.types.includes('application/x-nasrelay')) return;
      event.preventDefault(); this.root.classList.add('drop-target');
    });
    this.root.addEventListener('dragleave',event => { if (!this.root.contains(event.relatedTarget)) this.root.classList.remove('drop-target'); });
    this.root.addEventListener('drop',event => {
      event.preventDefault(); this.root.classList.remove('drop-target');
      try {
        const data = JSON.parse(event.dataTransfer.getData('application/x-nasrelay'));
        if (data.side !== this.side && Array.isArray(data.paths) && data.profile_id === state.profile?.id) prepareTransfer(data.side,data.paths);
      } catch (_) { /* Ignore external file drops: transfers run between NAS and FTP. */ }
    });
  }
  visible() {
    const query = $('.filter',this.root).value.toLocaleLowerCase();
    return this.entries.filter(entry => entry.name.toLocaleLowerCase().includes(query)).sort((a,b) => {
      // Keep folders first and missing dates last in either direction.
      if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
      const byName = a.name.localeCompare(b.name,'ko',{numeric:true,sensitivity:'base'});
      if (this.sortKey === 'name') return this.sortDirection * byName;
      const left = this.sortKey === 'size' ? a.size : fileTimestamp(a[this.sortKey]);
      const right = this.sortKey === 'size' ? b.size : fileTimestamp(b[this.sortKey]);
      if (left === null && right !== null) return 1;
      if (right === null && left !== null) return -1;
      return this.sortDirection * ((left - right) || byName);
    });
  }
  async load(path = this.path) {
    const generation = ++this.generation;
    const profileId = state.profile?.id;
    const requestEpoch = authEpoch;
    if (this.side === 'ftp' && !state.connected) { this.clear(); return; }
    this.list.setAttribute('aria-busy','true');
    this.list.innerHTML = '<div class="loading">폴더를 불러오는 중…</div>';
    try {
      const data = await api(`/files/${this.side}?path=${encodeURIComponent(path)}${this.side === 'ftp' ? `&profile_id=${encodeURIComponent(profileId)}` : ''}`);
      if (generation !== this.generation || requestEpoch !== authEpoch || (this.side === 'ftp' && profileId !== state.profile?.id)) return;
      this.path = data.path;
      this.entries = data.entries;
      this.selected.clear(); this.anchor = -1;
      $('.path-form input',this.root).value = this.path;
      this.render();
    } catch (err) {
      if (generation !== this.generation || requestEpoch !== authEpoch || (this.side === 'ftp' && profileId !== state.profile?.id)) return;
      this.entries = []; this.selected.clear(); this.selectionChanged();
      this.list.innerHTML = `<div class="empty-pane"><span class="empty-icon">${icon('info')}</span><strong>폴더를 열 수 없습니다</strong><p>${escapeHTML(err.message)}</p><button class="text-button retry-load">다시 시도 ${icon('refresh')}</button></div>`;
      $('.retry-load',this.list).addEventListener('click',() => this.load(path));
      toast(err.message,true);
    } finally { if (generation === this.generation) this.list.removeAttribute('aria-busy'); }
  }
  clear() {
    this.generation++;
    this.entries = []; this.selected.clear(); this.path = '/';
    $('.path-form input',this.root).value = '/';
    $('.entry-count',this.root).textContent = '0개 항목';
    this.list.innerHTML = `<div class="empty-pane"><span class="empty-icon">${icon('server')}</span><strong>서버를 연결해 주세요</strong><p>원격 서버를 연결하면<br>파일과 폴더가 여기에 표시됩니다.</p><button class="text-button open-connect">서버 연결 ${icon('arrow')}</button></div>`;
    $('.open-connect',this.list).addEventListener('click',openConnect);
    this.selectionChanged();
  }
  render() {
    const entries = this.visible();
    const dateFields = $$('.date-toggle:checked',this.root).map(input => input.value);
    const dates = entry => dateFields.length ? `<div class="file-dates">${dateFields.map(field => `<span class="file-date" data-field="${field}" title="${field === 'modified' ? '수정 날짜' : '만든 날짜'}${fileTimestamp(entry[field]) === null ? ': 서버 또는 파일 시스템에서 제공하지 않음' : ''}">${field === 'modified' ? '수정' : '만듦'} <time>${escapeHTML(formatFileDate(entry[field]))}</time></span>`).join('')}</div>` : '';
    $('.entry-count',this.root).textContent = `${this.entries.length.toLocaleString()}개 항목`;
    this.list.innerHTML = entries.length ? entries.map((entry,index) => `<div class="file-row" data-path="${escapeHTML(entry.path)}" data-index="${index}"><input type="checkbox" aria-label="${escapeHTML(entry.name)} 선택"><svg class="file-icon ${entry.is_dir ? 'folder-icon' : ''}" aria-hidden="true"><use href="#i-${entry.is_dir ? 'folder' : 'file'}"/></svg>${entry.is_dir ? `<button class="file-name folder-link" title="${escapeHTML(entry.name)} 폴더 열기">${escapeHTML(entry.name)}</button>` : `<span class="file-name" title="${escapeHTML(entry.name)}">${escapeHTML(entry.name)}</span>`}<span class="file-size">${entry.is_dir ? '폴더' : formatSize(entry.size)}</span><button class="drag-handle" draggable="true" aria-label="${escapeHTML(entry.name)} 전송 드래그" title="반대쪽 패널로 드래그">${icon('drag')}</button>${dates(entry)}</div>`).join('') : `<div class="empty-pane"><span class="empty-icon">${icon('folder')}</span><strong>${this.entries.length ? '검색 결과가 없습니다' : '폴더가 비어 있습니다'}</strong><p>${this.entries.length ? '다른 이름으로 검색해 보세요.' : '반대쪽 파일을 선택해 이 폴더로 전송하세요.'}</p></div>`;
    this.paintSelection();
  }
  click(event) {
    if (this.suppressClick) { event.preventDefault(); return; }
    if (event.target.closest('.open-connect')) return;
    const row = event.target.closest('.file-row');
    if (!row) return;
    const path = row.dataset.path;
    const index = Number(row.dataset.index);
    if (event.target.closest('.folder-link') && !event.ctrlKey && !event.metaKey && !event.shiftKey) return this.load(path);
    if (event.target.closest('.drag-handle')) return;
    if (event.target.matches('input[type="checkbox"]') || event.ctrlKey || event.metaKey) {
      this.selected.has(path) ? this.selected.delete(path) : this.selected.add(path);
    } else if (event.shiftKey && this.anchor >= 0) {
      this.visible().slice(Math.min(index,this.anchor),Math.max(index,this.anchor)+1).forEach(entry => this.selected.add(entry.path));
    } else this.selected = new Set([path]);
    this.anchor = index;
    this.paintSelection();
  }
  paintSelection() {
    $$('.file-row',this.list).forEach(row => {
      const selected = this.selected.has(row.dataset.path);
      row.classList.toggle('selected',selected);
      $('input',row).checked = selected;
    });
    this.selectionChanged();
  }
  selectionChanged() {
    const selected = this.entries.filter(entry => this.selected.has(entry.path));
    const count = selected.length;
    const label = $('.selection-count',this.root);
    label.textContent = count ? `${count.toLocaleString()}개 항목 선택` : '선택한 항목 없음';
    label.classList.toggle('has-selection',!!count);
    $('.selection-size',this.root).textContent = count ? formatSize(selected.reduce((sum,entry) => sum + entry.size,0)) + (selected.some(entry => entry.is_dir) ? ' + 폴더' : '') : '—';
    const all = $('.select-all',this.root);
    const visible = this.visible();
    const checked = visible.filter(entry => this.selected.has(entry.path)).length;
    all.checked = !!visible.length && checked === visible.length;
    all.indeterminate = checked > 0 && checked < visible.length;
    $(`#${this.side === 'ftp' ? 'download' : 'upload'}`).disabled = !state.connected || !count;
  }
  marquee(event) {
    if (event.button !== 0 || event.pointerType !== 'mouse' || event.target.closest('button,input') || !this.entries.length) return;
    const list = this.list;
    const rect = list.getBoundingClientRect();
    const startX = event.clientX - rect.left + list.scrollLeft;
    const startY = event.clientY - rect.top + list.scrollTop;
    const initial = event.ctrlKey || event.metaKey || event.shiftKey ? new Set(this.selected) : new Set();
    let box = null;
    let dragging = false;
    const move = pointer => {
      if (!dragging && Math.hypot(pointer.clientX-event.clientX,pointer.clientY-event.clientY) < 5) return;
      if (!dragging) {
        dragging = true; box = document.createElement('div'); box.className = 'selection-rectangle'; list.append(box);
      }
      pointer.preventDefault();
      if (pointer.clientY > rect.bottom - 24) list.scrollTop += 12;
      if (pointer.clientY < rect.top + 24) list.scrollTop -= 12;
      const endX = pointer.clientX - rect.left + list.scrollLeft;
      const endY = pointer.clientY - rect.top + list.scrollTop;
      const left = Math.min(startX,endX), top = Math.min(startY,endY), right = Math.max(startX,endX), bottom = Math.max(startY,endY);
      Object.assign(box.style,{left:`${left}px`,top:`${top}px`,width:`${right-left}px`,height:`${bottom-top}px`});
      this.selected = new Set(initial);
      $$('.file-row',list).forEach(row => {
        if (row.offsetTop + row.offsetHeight > top && row.offsetTop < bottom && row.offsetLeft + row.offsetWidth > left && row.offsetLeft < right) this.selected.add(row.dataset.path);
      });
      this.paintSelection();
    };
    const finish = () => {
      document.removeEventListener('pointermove',move);
      document.removeEventListener('pointerup',finish);
      document.removeEventListener('pointercancel',finish);
      box?.remove();
      if (dragging) { this.suppressClick = true; setTimeout(() => this.suppressClick = false,0); }
    };
    document.addEventListener('pointermove',move,{passive:false});
    document.addEventListener('pointerup',finish,{once:true});
    document.addEventListener('pointercancel',finish,{once:true});
  }
}
function renderJobs() {
  const active = state.jobs.filter(job => !terminal(job.status));
  $('#metricActive').innerHTML = `${active.length}<span>개</span>`;
  $('#metricFiles').innerHTML = `${state.jobs.reduce((sum,job) => sum + (job.completed_files || 0),0).toLocaleString()}<span>개</span>`;
  const [bytes,unit] = formatSize(state.jobs.reduce((sum,job) => sum + (job.completed_bytes || 0),0)).split(' ');
  $('#metricBytes').innerHTML = `${bytes}<span>${unit}</span>`;
  $('#queueCount').textContent = active.length;
  const showHistory = $('#showHistory').checked;
  const jobs = showHistory ? state.jobs : active;
  if (!jobs.length) {
    $('#jobList').innerHTML = `<div class="empty-jobs">${icon(showHistory ? 'clock' : 'check')}<div><strong>${showHistory ? '아직 전송 기록이 없습니다' : '대기 중인 작업이 없습니다'}</strong><p>파일을 선택하고 전송 버튼을 눌러 첫 작업을 시작해 보세요.</p></div></div>`;
    return;
  }
  const labels = {queued:'대기 중',planning:'파일 확인 중',running:'전송 중',paused:'일시정지',succeeded:'전송 완료',failed:'실패',cancelled:'취소됨'};
  $('#jobList').innerHTML = jobs.map(job => `<article class="job-card" data-job="${job.id}"><div class="job-heading">${icon(job.status === 'succeeded' ? 'check' : 'transfer')}<div class="job-title"><strong>${escapeHTML(job.direction === 'download' ? `${job.profile_name || '원격 서버'} → NAS` : `NAS → ${job.profile_name || '원격 서버'}`)} · ${job.mode === 'move' ? '이동' : '복사'} · ${escapeHTML(job.sources[0]?.split('/').pop())}${job.sources.length > 1 ? ` 외 ${job.sources.length-1}개` : ''}</strong><small>${escapeHTML((job.protocol || 'ftp').toUpperCase())} · ${escapeHTML(job.destination)} · ${new Date(job.created_at).toLocaleString('ko-KR')} · #${job.id.slice(0,8)}</small></div><span class="job-status ${job.status}">${job.pause_requested && job.status !== 'paused' ? '일시정지 요청 중' : labels[job.status]}</span></div><div class="job-progress"><progress max="100" value="${job.progress}" aria-label="작업 진행률"></progress><span>${job.progress}%</span></div><div class="job-meta"><span title="${escapeHTML(job.current_file)}">${job.completed_files}/${job.total_files}개 · ${formatSize(job.transferred_bytes)} / ${formatSize(job.total_bytes)}${job.current_file ? ` · ${escapeHTML(job.current_file)}` : ''}</span><div class="job-actions">${!terminal(job.status) ? `<button data-action="${job.status === 'paused' || job.pause_requested ? 'resume' : 'pause'}" ${job.cancel_requested ? 'disabled' : ''}>${job.status === 'paused' || job.pause_requested ? '재개' : '일시정지'}</button><button data-action="cancel" ${job.cancel_requested ? 'disabled' : ''}>${job.cancel_requested ? '취소 요청됨' : '작업 취소'}</button>` : ['failed','cancelled'].includes(job.status) ? '<button data-action="retry">재시도</button>' : ''}</div></div>${job.error ? `<p class="job-error">${escapeHTML(job.error)}</p>` : ''}${job.warnings?.length ? `<p class="job-error">${escapeHTML(job.warnings.join('\n'))}</p>` : ''}</article>`).join('');
}
async function refreshState(initial = false, force = false) {
  if (refreshInFlight) {
    await refreshInFlight;
    if (force || initial) return refreshState(initial,true);
    return;
  }
  const work = performRefresh(initial);
  refreshInFlight = work;
  try { await work; }
  finally { if (refreshInFlight === work) refreshInFlight = null; }
}
async function performRefresh(initial = false) {
  const requestEpoch = authEpoch;
  try {
    const previous = state;
    const latest = await api('/state');
    if (requestEpoch !== authEpoch || authChangesInFlight > 0) return;
    state = latest;
    loggedIn = true;
    $('#loginDialog').close();
    $('#shell').hidden = false;
    updateConnection();
    $('#telegramDot').classList.toggle('on',state.telegram.enabled);
    renderJobs();
    if (initial) {
      await Promise.allSettled([panes.nas.load(),panes.ftp.load()]);
    } else {
      if (previous.profile?.id !== state.profile?.id || previous.connected !== state.connected) {
        panes.ftp.clear();
        if (state.connected) await panes.ftp.load('/');
      }
      const completed = state.jobs.filter(job => terminal(job.status) && previous.jobs.some(old => old.id === job.id && !terminal(old.status)));
      if (completed.length) {
        completed.forEach(job => toast(`${job.status === 'succeeded' ? '전송 완료' : job.status === 'failed' ? '전송 실패' : '전송 취소'} · ${job.completed_files}개 파일 · ${formatSize(job.completed_bytes)}`,job.status === 'failed'));
        await Promise.allSettled([panes.nas.load(),panes.ftp.load()]);
      }
    }
  } catch (err) {
    if (err.staleAuth) return;
    if (initial && !$('#loginDialog').open) toast(err.message,true);
    else if (loggedIn) $('#connectionDescription').textContent = err.message;
  }
}
$$('.close-dialog').forEach(button => button.addEventListener('click',() => button.closest('dialog').close()));
$('#loginDialog').addEventListener('cancel',event => event.preventDefault());
$('#loginForm').addEventListener('submit',event => {
  event.preventDefault();
  formAction(event.currentTarget,async () => {
    const form = event.target;
    const result = await api('/login',{method:'POST',body:JSON.stringify({username:form.elements.username.value,password:form.elements.password.value,code:form.elements.code.value.trim()})});
    if (result.requires_totp) {
      $('#loginCodeField').hidden = false;
      form.elements.code.required = true;
      form.elements.code.focus();
      return;
    }
    form.reset();
    await bootstrap();
  });
});
$('#connectForm').addEventListener('submit',event => {
  event.preventDefault();
  formAction(event.currentTarget,() => saveProfile(true));
});
$('#saveProfile').addEventListener('click',() => {
  if ($('#connectForm').reportValidity()) formAction($('#connectForm'),() => saveProfile());
});
$('#connectForm select[name=protocol]').addEventListener('change',() => protocolOptions(true));
$('#connectForm input[name=tls]').addEventListener('change',event => {
  const form = $('#connectForm');
  if (form.elements.protocol.value === 'webdav' && [80,443].includes(Number(form.elements.port.value))) form.elements.port.value = event.target.checked ? 443 : 80;
});
$('#addProfile').addEventListener('click',() => editProfile());
$('#newProfile').addEventListener('click',() => { editProfile(); renderProfiles(); showDialog('#connectDialog'); });
$('#profileSelect').addEventListener('change',async event => {
  try { await selectProfile(event.target.value); } catch(err) { toast(err.message,true); }
});
$('#connectSelected').addEventListener('click',async event => {
  event.currentTarget.disabled = true;
  try { await api(`/profiles/${state.profile.id}/connect`,{method:'POST'}); await refreshState(false,true); }
  catch(err) { await refreshState(false,true); updateConnection(err.message); toast(err.message,true); }
});
$('#profilesList').addEventListener('click',async event => {
  const button = event.target.closest('[data-profile-action]');
  if (!button) return;
  const id = button.closest('[data-profile]').dataset.profile;
  const action = button.dataset.profileAction;
  const profile = state.profiles.find(profile => profile.id === id);
  if (action === 'edit') return editProfile(profile);
  if (action === 'delete' && !window.confirm(`“${profile.name}” 프로필을 삭제할까요? 등록된 전송 작업은 계속 실행됩니다.`)) return;
  formAction($('#connectForm'),async () => {
    if (action === 'select') await selectProfile(id);
    else if (action === 'connect') await api(`/profiles/${id}/connect`,{method:'POST'});
    else if (action === 'disconnect') await api(`/connection?profile_id=${id}`,{method:'DELETE'});
    else if (action === 'delete') {
      await api(`/profiles/${id}`,{method:'DELETE'});
      if (editingProfileId === id) editProfile();
    }
    await refreshState(false,true);
  });
});
$('#disconnect').addEventListener('click',async () => {
  try { const result = await api(`/connection?profile_id=${state.profile.id}`,{method:'DELETE'}); await refreshState(false,true); toast(result.message); }
  catch(err) { toast(err.message,true); }
});
$('#transferForm').addEventListener('submit',event => {
  event.preventDefault();
  formAction(event.currentTarget,async () => {
    await api('/jobs',{method:'POST',body:JSON.stringify(pendingTransfer)});
    $('#transferDialog').close();
    const side = pendingTransfer.direction === 'download' ? 'ftp' : 'nas';
    panes[side].selected.clear(); panes[side].paintSelection();
    toast('전송 작업을 등록했습니다. 브라우저를 닫아도 계속 실행됩니다.');
    pendingTransfer = null;
    await refreshState(false,true);
  });
});
$('#telegramForm').addEventListener('submit',event => {
  event.preventDefault();
  formAction(event.currentTarget,async () => {
    await api('/telegram',{method:'PUT',body:JSON.stringify(telegramValues())});
    $('#telegramDialog').close(); toast('텔레그램 설정을 저장했습니다.'); await refreshState(false,true);
  });
});
$('#testTelegram').addEventListener('click',() => formAction($('#telegramForm'),async () => {
  await api('/telegram/test',{method:'POST',body:JSON.stringify(telegramValues())}); toast('테스트 알림을 보냈습니다. 텔레그램을 확인하세요.');
}));
$('#folderForm').addEventListener('submit',event => {
  event.preventDefault();
  formAction(event.currentTarget,async () => {
    await api('/folders',{method:'POST',body:JSON.stringify({side:folderSide,profile_id:folderProfileId,parent:folderParent,name:event.target.elements.name.value})});
    $('#folderDialog').close(); await panes[folderSide].load(); toast('폴더를 만들었습니다.');
  });
});
$('#jobList').addEventListener('click',async event => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  button.disabled = true;
  try {
    await api(`/jobs/${button.closest('[data-job]').dataset.job}/${button.dataset.action}`,{method:'POST'});
    await refreshState(false,true);
  } catch(err) { toast(err.message,true); button.disabled = false; }
});
$('#logout').addEventListener('click',async () => {
  try { await api('/logout',{method:'POST'}); showLogin(); }
  catch(err) { toast(err.message,true); }
});
$('#download').addEventListener('click',() => prepareTransfer('ftp'));
$('#upload').addEventListener('click',() => prepareTransfer('nas'));
$('#connectOpen').addEventListener('click',openConnect);
$$('.open-connect').forEach(button => button.addEventListener('click',openConnect));
$('#navSettings').addEventListener('click',openConnect);
$('#navTelegram').addEventListener('click',openTelegram);
$('#telegramOpen').addEventListener('click',openTelegram);
$('#navFiles').addEventListener('click',() => {
  $('#transferSection').scrollIntoView({behavior:'smooth'});
  $('#navHistory').classList.remove('active'); $('#navFiles').classList.add('active');
});
$('#navHistory').addEventListener('click',() => {
  $('#showHistory').checked = true; renderJobs(); $('#jobsSection').scrollIntoView({behavior:'smooth'});
  $('#navFiles').classList.remove('active'); $('#navHistory').classList.add('active');
});
$('#showHistory').addEventListener('change',renderJobs);
$$('input[name="mode"]').forEach(input => input.addEventListener('change',() => {
  $('#modeHelp').textContent = mode() === 'move' ? '이동: 전송과 크기 검증이 완료된 파일의 원본을 삭제합니다. 대상에 같은 이름의 파일이 있으면 원본을 보존합니다.' : '복사: 원본 파일을 유지합니다. 대상에 같은 이름의 파일이 있으면 건너뛰지 않고 실패로 표시합니다.';
}));
panes.ftp = new Pane('ftp');
panes.nas = new Pane('nas');
bootstrap();
setInterval(() => { if (loggedIn && !document.hidden) refreshState(); },2500);
document.addEventListener('visibilitychange',() => { if (!document.hidden && loggedIn) refreshState(); });

async function bootstrap() {
  try {
    account = await api('/auth/me');
    if (account.must_change_password) {
      loggedIn = false;
      $('#shell').hidden = true;
      $('#loginDialog').close();
      openPasswordDialog();
      return;
    }
    await refreshState(true);
  } catch(err) {
    if (!$('#loginDialog').open) showLogin();
  }
}
function openPasswordDialog() {
  $('#securityDialog').close();
  const form = $('#passwordForm');
  form.reset();
  const mandatory = !!account?.must_change_password;
  $('#passwordTitle').textContent = mandatory ? '새 비밀번호를 설정하세요' : '비밀번호 변경';
  $('#passwordDescription').textContent = mandatory ? '계속 사용하려면 최초 비밀번호를 반드시 변경해야 합니다.' : '변경하면 다른 로그인 세션은 종료됩니다.';
  $('.optional-close',form).hidden = mandatory;
  $('#passwordCodeField').hidden = !account?.totp_enabled;
  form.elements.code.required = !!account?.totp_enabled;
  showDialog('#passwordDialog');
}
async function openSecurity() {
  try {
    account = await api('/auth/me');
    if (account.must_change_password) return openPasswordDialog();
    $('#totpSetupForm').reset();
    $('#totpDisableForm').reset();
    $('#totpSetupForm').hidden = false;
    $('#totpEnrollment').hidden = true;
    $('#totpQr').removeAttribute('src');
    $('#totpKey').value = '';
    $('#totpEnableForm').reset();
    $('#totpSetupSection').hidden = account.totp_enabled;
    $('#totpDisableForm').hidden = !account.totp_enabled;
    $('#totpStatus').textContent = account.totp_enabled ? `활성화됨 · 남은 복구 코드 ${account.recovery_codes_remaining}개` : '사용 안 함 · 인증 앱을 등록해 로그인 보안을 강화하세요.';
    $('#totpBadge').textContent = account.totp_enabled ? '활성화됨' : '사용 안 함';
    $('#totpBadge').classList.toggle('connected',account.totp_enabled);
    showDialog('#securityDialog');
  } catch(err) { toast(err.message,true); }
}
$('#navSecurity').addEventListener('click',openSecurity);
$('#openPasswordChange').addEventListener('click',openPasswordDialog);
$('#passwordDialog').addEventListener('cancel',event => { if (account?.must_change_password) event.preventDefault(); });
$('#passwordLogout').addEventListener('click',async () => {
  try { await api('/logout',{method:'POST'}); showLogin(); } catch(err) { toast(err.message,true); }
});
$('#passwordForm').addEventListener('submit',event => {
  event.preventDefault();
  formAction(event.currentTarget,async () => {
    const form = event.target;
    if (form.elements.new_password.value !== form.elements.confirmation.value) throw new Error('새 비밀번호와 확인 값이 일치하지 않습니다.');
    await api('/auth/password',{method:'POST',body:JSON.stringify({current_password:form.elements.current_password.value,new_password:form.elements.new_password.value,code:form.elements.code.value.trim()})});
    form.reset();
    $('#passwordDialog').close();
    toast('비밀번호를 변경했습니다. 다른 로그인 세션은 종료되었습니다.');
    await bootstrap();
  });
});
$('#totpSetupForm').addEventListener('submit',event => {
  event.preventDefault();
  formAction(event.currentTarget,async () => {
    const result = await api('/auth/totp/setup',{method:'POST',body:JSON.stringify({current_password:event.target.elements.current_password.value})});
    event.target.reset(); event.target.hidden = true;
    $('#totpKey').value = result.secret;
    $('#totpQr').src = result.qr_code;
    $('#totpEnrollment').hidden = false;
    $('#totpEnableForm input').focus();
  });
});
$('#totpEnableForm').addEventListener('submit',event => {
  event.preventDefault();
  formAction(event.currentTarget,async () => {
    const result = await api('/auth/totp/enable',{method:'POST',body:JSON.stringify({code:event.target.elements.code.value.trim()})});
    recoveryCodes = result.recovery_codes;
    $('#securityDialog').close();
    $('#totpQr').removeAttribute('src'); $('#totpKey').value = ''; event.target.reset();
    $('#recoveryCodes').innerHTML = recoveryCodes.map(code => `<li><code>${escapeHTML(code)}</code></li>`).join('');
    $('#recoverySaved').checked = false; $('#closeRecovery').disabled = true;
    showDialog('#recoveryDialog');
    account = await api('/auth/me');
  });
});
$('#totpDisableForm').addEventListener('submit',event => {
  event.preventDefault();
  formAction(event.currentTarget,async () => {
    await api('/auth/totp',{method:'DELETE',body:JSON.stringify({current_password:event.target.elements.current_password.value,code:event.target.elements.code.value.trim()})});
    event.target.reset(); toast('TOTP와 기존 복구 코드를 삭제했습니다.'); await openSecurity();
  });
});
$('#recoveryDialog').addEventListener('cancel',event => event.preventDefault());
$('#recoverySaved').addEventListener('change',event => { $('#closeRecovery').disabled = !event.target.checked; });
$('#closeRecovery').addEventListener('click',() => {
  if (!$('#recoverySaved').checked) return;
  $('#recoveryDialog').close(); $('#recoveryCodes').replaceChildren(); recoveryCodes = []; openSecurity();
});
$('#downloadRecovery').addEventListener('click',() => {
  const blob = new Blob([`nasrelay · admin · TOTP 복구 코드\n각 코드는 한 번만 사용 가능합니다. 안전한 곳에 보관하세요.\n\n${recoveryCodes.join('\n')}\n`],{type:'text/plain;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = 'nasrelay-recovery-codes.txt'; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url),1000);
});
$('#securityDialog').addEventListener('close',() => {
  $('#totpQr').removeAttribute('src'); $('#totpKey').value = '';
  $('#totpSetupForm').reset(); $('#totpDisableForm').reset(); $('#totpEnableForm').reset();
});
