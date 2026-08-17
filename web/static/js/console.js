const $ = s => document.querySelector(s);
// --- section-container integrity (self-heal) --------------------------------
// Every section must be a DIRECT child of <main>: only one is shown at a time
// by toggling .visible, so a section nested inside another one is hidden
// whenever its host is hidden — which renders it blank/"not visible" no matter
// what the user clicks. A single unbalanced tag anywhere in the markup (a bad
// merge, a hand edit, a truncated file) makes the browser nest every following
// section inside the previous one. Rather than fail, re-parent them.
let gPagesRepaired = 0;
(function repairSectionContainers() {
  const main = document.querySelector('main');
  if (!main) return;
  document.querySelectorAll('.page').forEach(p => {
    if (p.parentElement !== main) { main.appendChild(p); gPagesRepaired++; }
  });
  if (gPagesRepaired)
    console.warn('MetaBridge: repaired ' + gPagesRepaired + ' misnested section'
      + ' container(s) — the page markup is malformed (rebuild recommended).');
})();
let apiKey = localStorage.getItem('mb_api_key') || '';

async function api(url, opts = {}) {
  opts.headers = Object.assign({}, opts.headers, apiKey ? {'X-API-Key': apiKey} : {});
  // Optional per-call timeout via AbortController. Opt-in (opts.timeoutMs) so
  // legitimately long endpoints are unaffected — but any caller that sets it
  // is guaranteed the promise SETTLES (a hung/black-holed request can never
  // leave a loading state stuck forever).
  let timer = null;
  if (opts.timeoutMs && typeof AbortController !== 'undefined' && !opts.signal) {
    const ctrl = new AbortController();
    opts.signal = ctrl.signal;
    timer = setTimeout(() => ctrl.abort(), opts.timeoutMs);
  }
  let r;
  try {
    r = await fetch(url, opts);
  } catch (e) {
    if (e && (e.name === 'AbortError' || e.code === 20))
      throw new Error('The request timed out — the server may still be busy. '
        + 'Try again, or use a smaller input.');
    throw new Error('Network error — could not reach the server. '
      + 'Check your connection and try again.');
  } finally {
    if (timer) clearTimeout(timer);
  }
  if (r.status === 401) { sessionExpired(); throw new Error('SESSION_EXPIRED'); }
  let data = {};
  try { data = await r.json(); } catch (e) { data = {}; }
  if (!r.ok) throw new Error((data && data.detail) || ('Request failed (HTTP ' + r.status + ')'));
  return data;
}
// Was an immediate window.location.href on the FIRST 401 seen — any unsaved
// input (a file staged in Modernize, a half-filled connector drawer) vanished
// with no warning mid-click. mbAlert (defined below) is called instead so
// navigation waits for an acknowledgement; gShown dedupes when several
// concurrent calls (e.g. loadDashboard's Promise.all) all 401 at once, so only
// one dialog appears rather than one replacing another in quick succession.
let gSessionExpiredShown = false;
async function sessionExpired() {
  if (gSessionExpiredShown) return;
  gSessionExpiredShown = true;
  await mbAlert('Your session expired — sign in again to continue. Anything you had open or '
    + 'part-filled on this page was not saved.', {title: 'Session expired'});
  window.location.href = '/login';
}

// One display-name source for every render site (sidebar, header menu,
// avatars, team table). Resolution: display_name > full_name > name >
// composed parts > email local-part. The stored name is shown EXACTLY as
// the user saved it — no case or token rewriting, otherwise a saved profile
// edit appears not to persist (the form and header would silently show a
// different string than the one on the server).
function getUserDisplayName(user) {
  if (!user) return 'User';
  const raw = String(user.display_name || user.full_name || user.name
    || [user.first_name, user.middle_name, user.last_name]
       .filter(Boolean).join(' ')).trim();
  if (!raw) return (user.email || '').split('@')[0] || 'User';
  return raw.split(/\s+/).filter(Boolean).join(' ');
}
function getUserInitials(displayName) {
  const t = String(displayName || '').trim().split(/\s+/).filter(Boolean);
  if (!t.length) return '?';
  return (t.length === 1 ? t[0][0] : t[0][0] + t[t.length - 1][0]).toUpperCase();
}
// The one avatar renderer used everywhere (header, dropdown, profile page).
// Priority: profile photo > chosen preset > generated initials.
/* ---- password reveal + input hygiene ----
   A credential typed into a masked box cannot be proof-read, and a stray leading
   or trailing space (very easy to introduce by pasting from a console, an email,
   or a vault) produces an auth failure with no visible cause. The reveal toggle
   makes the value checkable; trimOnBlur removes the whitespace at the source so
   every reader of the field (save / test / introspect / artifacts / dirty-check)
   sees the cleaned value without each having to remember to trim. */
const EYE_SHOW = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"'
  + ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
  + '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_HIDE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"'
  + ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
  + '<path d="M2 12s3.6-7 10-7c2 0 3.8.7 5.2 1.6M22 12s-3.6 7-10 7c-2 0-3.8-.7-5.2-1.6"/>'
  + '<path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/><path d="M3 3l18 18"/></svg>';
function revealBtnHtml() {
  return '<button type="button" class="pw-eye" tabindex="0" aria-pressed="false"'
    + ' aria-label="Show password" title="Show password">' + EYE_SHOW + '</button>';
}
// Delegated so it covers fields rendered at any time, in any drawer or panel.
document.addEventListener('click', ev => {
  const btn = ev.target.closest && ev.target.closest('.pw-eye');
  if (!btn) return;
  ev.preventDefault();
  const wrap = btn.closest('.pw-wrap');
  const inp = wrap && wrap.querySelector('input');
  if (!inp) return;
  const show = inp.type === 'password';
  inp.type = show ? 'text' : 'password';
  btn.innerHTML = show ? EYE_HIDE : EYE_SHOW;
  btn.setAttribute('aria-pressed', show ? 'true' : 'false');
  const lbl = show ? 'Hide password' : 'Show password';
  btn.setAttribute('aria-label', lbl); btn.setAttribute('title', lbl);
});
// Static .pw-eye buttons in the page markup carry no icon (the SVG lives here), so
// fill any that are still empty once the DOM is ready.
function paintRevealIcons(root) {
  (root || document).querySelectorAll('.pw-eye').forEach(b => {
    if (!b.firstElementChild) {
      const shown = (b.closest('.pw-wrap') || {}).querySelector
        && b.closest('.pw-wrap').querySelector('input');
      b.innerHTML = shown && shown.type === 'text' ? EYE_HIDE : EYE_SHOW;
    }
  });
}
document.addEventListener('DOMContentLoaded', () => paintRevealIcons());

/* ---------------- ⓘ info disclosures ----------------------------------
   Every tool panel used to print its "what this does" paragraph and its
   accepted-file list permanently, above the first control. Both are now
   behind an ⓘ next to the heading. Delegated, so it covers boxes rendered
   at any time. One box per button; several may be open at once (they are
   independent tools, and forcing an accordion would close the one you were
   reading when you opened its neighbour to compare). */
document.addEventListener('click', ev => {
  // any control carrying data-info, so a text link ("How to create it") opens
  // the same box the round ⓘ does rather than needing a second mechanism
  const btn = ev.target.closest && ev.target.closest('[data-info]');
  if (!btn) return;
  ev.preventDefault();
  const box = document.getElementById(btn.dataset.info);
  if (!box) return;
  const open = !box.classList.contains('on');
  box.classList.toggle('on', open);
  btn.setAttribute('aria-expanded', String(open));
});
// Escape closes every open box, matching the modals' dismissal.
document.addEventListener('keydown', ev => {
  if (ev.key !== 'Escape') return;
  document.querySelectorAll('.infobox.on').forEach(box => {
    box.classList.remove('on');
    const btn = document.querySelector('.ibtn[data-info="' + box.id + '"]');
    if (btn) btn.setAttribute('aria-expanded', 'false');
  });
});

/* The accepted-metadata extensions were written out verbatim in 7 places on
   Reports alone (plus Estate and Pipeline Studio) — nine copies to keep in
   step with the `accept=` attributes. Declared once here and painted into
   any <span data-exts>; data-exts="a,b" narrows it to a subset. */
const MB_EXT_LIST = ['.sql', '.xml', '.dtsx', '.dsx', '.item', '.mp/.dml/.xfr',
                     '.ddls', '.cds', '.abap', '.hdbcalculationview',
                     '.yml', '.json', '.properties'];
function paintExtLists(root) {
  (root || document).querySelectorAll('[data-exts]').forEach(el => {
    if (el.firstElementChild) return;                       // already painted
    const only = el.getAttribute('data-exts');
    const list = only ? only.split(',') : MB_EXT_LIST;
    el.innerHTML = list.map(e => '<code>' + e + '</code>').join(' · ');
  });
}
document.addEventListener('DOMContentLoaded', () => paintExtLists());

// Trim on blur rather than on input, so typing (and a deliberate interior space)
// is never fought mid-keystroke. Fires `input` so dirty-tracking stays accurate.
document.addEventListener('blur', ev => {
  const el = ev.target;
  if (!el || el.tagName !== 'INPUT') return;
  if (!/^(text|password|email|search|url|number)$/.test(el.type)) return;
  // Presence, not truthiness — a valueless `data-no-trim` reads as "" via dataset.
  if (el.hasAttribute('data-no-trim')) return;
  const t = el.value.replace(/^[\s ]+|[\s ]+$/g, '');
  if (t !== el.value) { el.value = t; el.dispatchEvent(new Event('input', {bubbles: true})); }
}, true);   // capture: blur does not bubble

function renderAvatar(el, user) {
  if (!el) return;
  const av = (user && user.avatar) || {type: 'INITIALS'};
  if (av.type === 'PHOTO' && av.url) {
    // Build the node instead of parsing HTML: quote-escaping alone still lets a
    // stored url break out via javascript:/data: in the src. Only same-origin
    // relative paths (what the upload endpoint hands back) are accepted.
    el.textContent = '';
    if (/^\/(?!\/)/.test(String(av.url))) {
      const img = document.createElement('img');
      img.alt = '';
      img.src = av.url;
      el.appendChild(img);
    } else {
      el.textContent = getUserInitials(getUserDisplayName(user));
    }
  } else if (av.type === 'PRESET' && /^mb-[1-6]$/.test(av.preset || '')) {
    el.innerHTML = '<img alt="" src="/static/avatars/' + av.preset + '.svg">';
  } else {
    el.textContent = getUserInitials(getUserDisplayName(user));
  }
}
// fetch the build stamp early so a section-load failure can name it
(async () => { try { gBuildId = (await api('/api/v1/info')).console_build || ''; }
               catch (e) {} })();
let gWorkspaces = [], gActiveWs = '';
function renderWorkspaceSwitcher(d) {
  const sw = $('#wsSwitch'); if (!sw) return;
  gWorkspaces = d.workspaces || [];
  gActiveWs = d.active_workspace || '';
  if (!gWorkspaces.length) { sw.style.display = 'none'; return; }
  sw.style.display = 'flex';
  const active = gWorkspaces.find(w => w.id === gActiveWs) || gWorkspaces[0];
  $('#wsBtnName').textContent = active ? active.name : 'Workspace';
  let html = '<div style="padding:8px 12px 4px;font-size:11px;letter-spacing:.5px;color:#8a94a6;font-weight:700">WORKSPACES</div>'
    + gWorkspaces.map(w =>
      '<button role="menuitem" data-ws="' + esc(w.id) + '" style="display:flex;justify-content:space-between;gap:10px;width:100%;text-align:left">'
      + '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(w.name)
      + '<span style="color:#8a94a6;font-weight:400"> · ' + esc(w.role) + '</span></span>'
      + (w.id === gActiveWs ? '<span style="color:var(--accent)">✓</span>' : '') + '</button>').join('');
  if (d.can_create_workspace)
    html += '<div style="border-top:1px solid #eceef3;margin-top:4px"></div>'
      + '<button role="menuitem" id="wsCreate" style="width:100%;text-align:left;color:var(--accent);font-weight:600">+ New workspace</button>';
  $('#wsMenu').innerHTML = html;
  // The reload after a switch is deliberate — workspace-scoped state (jobs,
  // connections, connectors, twin, RBAC) is cached in a dozen module-level
  // variables, and a full reload is the only way to guarantee none of it
  // leaks across workspaces. What was missing was a warning FIRST: silently
  // reloading discarded whatever was part-filled on another section, same as
  // finding 7. mbPrompt/mbAlert (defined below, already used elsewhere in the
  // file) replace the native dialogs the comment two screens down already
  // names as their intended replacement.
  async function switchWorkspace(id) {
    if (settingsDirty()) {
      const go = await mbConfirm('Switching workspaces will discard your unsaved settings edits.',
        {title: 'Discard unsaved changes?', okText: 'Switch anyway', danger: true});
      if (!go) return;
    }
    try { await api('/api/workspaces/switch', {method: 'POST', body: JSON.stringify({workspace: id})});
          window.location.reload(); }
    catch (e) { mbAlert((e && e.message) || 'Could not switch workspace'); }
  }
  $('#wsMenu').querySelectorAll('button[data-ws]').forEach(b => b.onclick = () => {
    if (b.dataset.ws === gActiveWs) { $('#wsMenu').style.display = 'none'; return; }
    switchWorkspace(b.dataset.ws);
  });
  const cr = $('#wsCreate');
  if (cr) cr.onclick = async () => {
    const name = ((await mbPrompt('Name your new workspace:', '')) || '').trim();
    if (!name) return;
    try {
      const w = await api('/api/workspaces', {method: 'POST', body: JSON.stringify({name})});
      await switchWorkspace(w.id);
    } catch (e) { mbAlert((e && e.message) || 'Could not create workspace'); }
  };
  $('#wsBtn').onclick = (ev) => {
    ev.stopPropagation();
    const m = $('#wsMenu');
    m.style.display = m.style.display === 'block' ? 'none' : 'block';
    $('#wsBtn').setAttribute('aria-expanded', m.style.display === 'block');
  };
  if (!window._wsMenuBound) {
    window._wsMenuBound = true;
    document.addEventListener('click', ev => {
      if (ev.target.closest('#wsSwitch')) return;
      const m = $('#wsMenu'); if (m) m.style.display = 'none';
    });
  }
}
async function loadUser() {
  try {
    const d = await api('/api/v1/me');
    if (d.user) {
      myUser = d.user;
      gFirstRun = !!d.first_run;
      myEmail = d.user.email;
      myPerms = d.permissions || [];
      myPerms._role = d.user.role;
      const dispName = getUserDisplayName(d.user);
      $('#userName').textContent = dispName;
      $('#userRole').textContent = d.user.role;
      renderAvatar($('#userAvatar'), d.user);
      renderAvatar($('#topAvatar'), d.user);
      renderAvatar($('#acctAv'), d.user);
      renderAvatar($('#profAv'), d.user);
      $('#topAvatar').title = dispName + ' · ' + d.user.email;
      $('#topMenuName').textContent = dispName;
      $('#topMenuName').title = dispName;
      $('#topMenuEmail').textContent = d.user.email;
      $('#topMenuEmail').title = d.user.email;
      $('#profName').textContent = dispName;
      $('#profEmail').textContent = d.user.email;
      $('#avInitDemo').textContent = getUserInitials(dispName);
      const hasPhoto = d.user.avatar && d.user.avatar.type === 'PHOTO';
      $('#avRemove').style.display = hasPhoto ? '' : 'none';
      $('#avUploadLbl').textContent = hasPhoto ? 'Change photo' : 'Upload photo';
      $('#logoutBtn').onclick = async () => { await fetch('/auth/logout', {method:'POST'}); window.location.href = '/'; };
      renderWorkspaceSwitcher(d);
    } else {
      $('#userName').textContent = 'Local workspace';
      $('#userRole').textContent = 'open mode';
      $('#userAvatar').textContent = 'MB'; $('#topAvatar').textContent = 'MB';
      $('#acctAv').textContent = 'MB'; $('#acctAvBtn').disabled = true;
      $('#topMenuName').textContent = 'Local workspace';
      $('#topMenuEmail').textContent = 'authentication disabled';
      $('#profilePanel').style.display = 'none';
    }
    applyRbacUi();
    gateCommercialNav();
  } catch (e) { /* open mode */ }
}
// Commercial Admin is a separate staff plane. Only surface its nav entry when
// the feature is actually enabled AND reachable (mounted + migrated), and only
// to workspace owners — the link opens the commercial UI, which enforces its
// own key auth (the product session never bridges into it).
async function gateCommercialNav() {
  const nav = $('#navCommercial');
  if (!nav) return;
  const isOwner = myPerms.includes('*') || myPerms._role === 'owner';
  if (!isOwner) { nav.style.display = 'none'; return; }
  try {
    const s = await api('/api/system/commercial');
    nav.style.display = (s.enabled && s.ready) ? '' : 'none';
    if (s.enabled && s.ready && s.url) nav.href = s.url;
  } catch (e) { nav.style.display = 'none'; }
}
const acctBtn = $('#topAvatarBtn'), acctMenu = $('#topMenu');
function acctSetOpen(open) {
  acctMenu.style.display = open ? 'block' : 'none';
  acctBtn.setAttribute('aria-expanded', String(open));
  if (open) document.addEventListener('click', () => acctSetOpen(false), {once: true});
}
acctBtn.onclick = ev => {
  ev.stopPropagation();
  acctSetOpen(acctMenu.style.display !== 'block');
};
acctBtn.addEventListener('keydown', ev => {
  if (ev.key === 'ArrowDown' && acctMenu.style.display === 'block') {
    ev.preventDefault(); acctMenu.querySelector('button').focus();
  } else if (ev.key === 'Escape') acctSetOpen(false);
});
acctMenu.addEventListener('keydown', ev => {
  const items = [...acctMenu.querySelectorAll('button')];
  const i = items.indexOf(document.activeElement);
  if (ev.key === 'Escape') { acctSetOpen(false); acctBtn.focus(); }
  else if (ev.key === 'ArrowDown') { ev.preventDefault(); items[(i + 1) % items.length].focus(); }
  else if (ev.key === 'ArrowUp') { ev.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
});

// ---- in-app dialogs -------------------------------------------------------
// Promise-based replacements for native confirm()/alert()/prompt(), which render
// as unthemeable OS chrome. Same call shape, so sites just add `await`.
const DLG_IC = {
  warn: '<path d="M12 9v4M12 17h.01M10.3 3.9 2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
  bad:  '<circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
};
let dlgClose = null;
function dlgDismiss(val) { if (dlgClose) dlgClose(val); }

// opts: {title, message, tone, okText, cancelText, danger, input, value}
function mbDialog(opts) {
  const o = opts || {};
  const bg = $('#dlgBg'), box = $('#dlgBox');
  if (dlgClose) dlgDismiss(o.input ? null : false);   // supersede any open dialog
  const tone = o.tone || 'info';
  const prev = document.activeElement;

  box.innerHTML =
      '<div class="dlg-hd">'
    +   '<div class="dlg-ic ' + tone + '" aria-hidden="true"><svg viewBox="0 0 24 24">' + DLG_IC[tone] + '</svg></div>'
    +   '<div style="min-width:0;flex:1">'
    +     '<h4 id="dlgTitle"></h4>'
    +     '<div class="dlg-msg" id="dlgMsg"></div>'
    +   '</div>'
    + '</div>'
    + (o.input ? '<input type="text" id="dlgInput">' : '')
    + '<div class="dlg-acts">'
    +   (o.cancelText === null ? ''
        : '<button type="button" class="secondary" id="dlgCancel"></button>')
    +   '<button type="button" id="dlgOk"' + (o.danger ? ' class="danger"' : '') + '></button>'
    + '</div>';

  // textContent (not innerHTML) — messages interpolate user-supplied names.
  box.querySelector('#dlgTitle').textContent = o.title || 'Confirm';
  box.querySelector('#dlgMsg').textContent = o.message || '';
  const ok = box.querySelector('#dlgOk');
  ok.textContent = o.okText || 'OK';
  const cancel = box.querySelector('#dlgCancel');
  if (cancel) cancel.textContent = o.cancelText || 'Cancel';
  const input = o.input ? box.querySelector('#dlgInput') : null;
  if (input) input.value = o.value == null ? '' : o.value;

  bg.classList.add('open');
  (input || ok).focus();
  if (input) input.select();

  return new Promise(resolve => {
    dlgClose = (val) => {
      dlgClose = null;
      bg.classList.remove('open');
      box.innerHTML = '';
      document.removeEventListener('keydown', onKey, true);
      if (prev && prev.focus) prev.focus();
      resolve(val);
    };
    ok.onclick = () => dlgDismiss(input ? input.value : true);
    if (cancel) cancel.onclick = () => dlgDismiss(input ? null : false);
    bg.onmousedown = (e) => { if (e.target === bg) dlgDismiss(input ? null : false); };
    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); dlgDismiss(input ? null : false); }
      else if (e.key === 'Enter' && input && document.activeElement === input) {
        e.preventDefault(); dlgDismiss(input.value);
      } else if (e.key === 'Tab') {                     // keep focus inside
        const f = [...box.querySelectorAll('button, input')].filter(x => !x.disabled);
        if (!f.length) return;
        const first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    }
    document.addEventListener('keydown', onKey, true);
  });
}
const mbConfirm = (message, opts) => mbDialog(Object.assign(
  {title: 'Confirm', message, tone: 'warn', okText: 'Confirm'}, opts || {}));
const mbAlert = (message, opts) => mbDialog(Object.assign(
  {title: 'Error', message, tone: 'bad', okText: 'Dismiss', cancelText: null}, opts || {}));
const mbPrompt = (message, value, opts) => mbDialog(Object.assign(
  {title: 'Enter a value', message, tone: 'info', okText: 'Continue', input: true, value}, opts || {}));

// ---- profile photo / avatar management -----------------------------------
function closeModal() { $('#modalBg').style.display = 'none';
  $('#modalBody').className = 'modal';     // drop conn-drawer layout
  // Closing the job overlay drops its id from the route but keeps the tab, so
  // the URL keeps describing what is actually on screen.
  if (gOpenJobId) {
    gOpenJobId = null;
    const m = (location.hash || '').match(/^#dashboard\/job\/.+/);
    if (m) setHash('dashboard/job');
  }
}

// Shared overlay chokepoint for every #modalBg mount (connector drawer, job
// detail, findings, member dialogs, photo cropper...). Only openConnector's
// drawer needs a non-default close (it dirty-checks unsaved field edits via
// closeDrawer) — every other opener is content with closeModal, so this only
// needs one explicit override rather than touching all ~20 open sites.
let gOverlayClose = null;
// Returns the close call's result (closeDrawer's dirty-check is async) so a
// caller can tell whether the close actually happened or was cancelled.
function overlayClose() { return (gOverlayClose || closeModal)(); }
function overlayFocusable() {
  return [...$('#modalBody').querySelectorAll(
    'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
    .filter(el => !el.disabled && el.offsetParent !== null);
}
function overlayIsOpen() {
  const d = $('#modalBg').style.display;
  return d !== 'none' && d !== '';
}
// Set right before a history.back() this module triggers itself (as opposed
// to a genuine Back press), so the popstate handler below can tell the two
// apart and not re-run section routing for a pop it caused internally.
let gOverlaySelfPop = false;
// A MutationObserver on #modalBg's inline style (what every existing open
// site already toggles) means focus-trap/restore/ARIA/history apply to all of
// them with no per-site changes: this is the one place "opened"/"closed" is
// detected, instead of editing every "$('#modalBg').style.display = ..." call.
(function () {
  const bg = $('#modalBg');
  let open = false, returnFocus = null;
  new MutationObserver(() => {
    const nowOpen = overlayIsOpen();
    if (nowOpen && !open) {
      returnFocus = document.activeElement;
      // One history entry per open overlay, so Back closes it instead of
      // leaving it painted over whatever page loads next (UX audit #4).
      history.pushState({mbOverlay: true}, '', location.href);
      requestAnimationFrame(() => (overlayFocusable()[0] || $('#modalBody')).focus());
    } else if (!nowOpen && open) {
      if (returnFocus && returnFocus.isConnected) returnFocus.focus();
      returnFocus = null;
      gOverlayClose = null;   // don't leak a stale close fn into the next opener
      // Closed via a button/Escape/backdrop rather than Back: consume the
      // marker entry ourselves so Back and Close agree on history depth,
      // instead of Back later closing an overlay that is already gone.
      if (history.state && history.state.mbOverlay) {
        gOverlaySelfPop = true;
        history.back();
      }
    }
    open = nowOpen;
  }).observe(bg, {attributes: true, attributeFilter: ['style']});
})();
// Escape + focus trap for #modalBg, matching mbDialog's existing contract
// (:3814-3842 in the pre-split file). Yields to mbDialog when IT is the
// topmost layer (e.g. closeDrawer's own "Discard this connection?" confirm
// stacked on top of the drawer) so Escape closes one layer at a time.
document.addEventListener('keydown', ev => {
  if ($('#dlgBg').classList.contains('open')) return;
  if (!overlayIsOpen()) return;
  if (ev.key === 'Escape') { ev.preventDefault(); overlayClose(); }
  else if (ev.key === 'Tab') {
    const f = overlayFocusable();
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
  }
});
// Registered before showPage's own popstate listener (below), so
// stopImmediatePropagation here suppresses it: a Back press that closes an
// overlay must not ALSO re-run section routing for the page underneath.
window.addEventListener('popstate', ev => {
  if (gOverlaySelfPop) { gOverlaySelfPop = false; ev.stopImmediatePropagation(); return; }
  if (!overlayIsOpen()) return;
  ev.stopImmediatePropagation();
  Promise.resolve(overlayClose()).then(() => {
    // Still open means the close was vetoed (dirty-check "Keep editing"), but
    // the physical Back press already consumed our history entry — restore it
    // so a second Back still closes the overlay instead of leaving /console.
    if (overlayIsOpen()) history.pushState({mbOverlay: true}, '', location.href);
  });
});

function openPhotoDialog() {
  $('#modalBody').innerHTML = '<h3 style="margin:0 0 4px">Update profile photo</h3>'
    + '<div style="color:var(--ink3);font-size:13px;margin-bottom:12px">JPEG, PNG or WEBP &middot; up to 5 MB</div>'
    + '<input type="file" id="phFile" accept="image/jpeg,image/png,image/webp">'
    + '<div id="phErr" style="color:var(--red);font-size:13px;margin-top:8px"></div>'
    + '<div id="phCropUi" style="display:none;margin-top:12px">'
    +   '<canvas id="phCanvas" width="280" height="280" style="width:280px;height:280px;border-radius:12px;background:var(--line);cursor:grab;touch-action:none"></canvas>'
    +   '<div style="display:flex;align-items:center;gap:10px;margin:12px 0;max-width:280px">'
    +     '<span style="font-size:13px;color:var(--ink3)">Zoom</span>'
    +     '<input id="phZoom" type="range" min="1" max="3" step="0.01" value="1" style="flex:1;margin:0" aria-label="Zoom"></div>'
    +   '<div style="display:flex;align-items:center;gap:10px">'
    +     '<span style="font-size:13px;color:var(--ink3)">Avatar preview:</span>'
    +     '<canvas id="phPrev" width="48" height="48" style="width:48px;height:48px;border-radius:50%;background:var(--line)"></canvas></div>'
    + '</div>'
    + '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:18px">'
    +   '<button class="secondary" id="phCancel">Cancel</button>'
    +   '<button id="phSave" disabled>Save photo</button></div>';
  $('#modalBody').className = 'modal'; $('#modalBg').style.display = 'flex';
  const st = {img: null, zoom: 1, ox: 0, oy: 0};
  const cv = $('#phCanvas'), pv = $('#phPrev');
  function draw() {
    if (!st.img) return;
    const s = 280 / Math.min(st.img.width, st.img.height) * st.zoom;
    const dw = st.img.width * s, dh = st.img.height * s;
    st.ox = Math.min(0, Math.max(280 - dw, st.ox));
    st.oy = Math.min(0, Math.max(280 - dh, st.oy));
    for (const [c, size] of [[cv, 280], [pv, 48]]) {
      const g = c.getContext('2d'), k = size / 280;
      g.clearRect(0, 0, size, size);
      g.drawImage(st.img, st.ox * k, st.oy * k, dw * k, dh * k);
    }
  }
  $('#phFile').onchange = () => {
    const f = $('#phFile').files[0];
    $('#phErr').textContent = '';
    if (!f) return;
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(f.type)) {
      $('#phErr').textContent = 'Use a JPEG, PNG or WEBP image.'; return;
    }
    if (f.size > 5 * 1024 * 1024) {
      $('#phErr').textContent = 'Image is larger than 5 MB.'; return;
    }
    const url = URL.createObjectURL(f), img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      st.img = img; st.zoom = 1;
      const base = 280 / Math.min(img.width, img.height);
      st.ox = (280 - img.width * base) / 2;
      st.oy = (280 - img.height * base) / 2;
      $('#phCropUi').style.display = 'block';
      $('#phSave').disabled = false;
      $('#phZoom').value = 1;
      draw();
    };
    img.onerror = () => { URL.revokeObjectURL(url); $('#phErr').textContent = 'The file is not a readable image.'; };
    img.src = url;
  };
  $('#phZoom').oninput = () => {
    if (!st.img) return;
    const z = parseFloat($('#phZoom').value);
    st.ox = 140 - (140 - st.ox) * (z / st.zoom);   // zoom around the center
    st.oy = 140 - (140 - st.oy) * (z / st.zoom);
    st.zoom = z; draw();
  };
  let drag = null;
  cv.onpointerdown = ev => { drag = {x: ev.clientX - st.ox, y: ev.clientY - st.oy}; cv.setPointerCapture(ev.pointerId); cv.style.cursor = 'grabbing'; };
  cv.onpointermove = ev => { if (drag) { st.ox = ev.clientX - drag.x; st.oy = ev.clientY - drag.y; draw(); } };
  cv.onpointerup = cv.onpointercancel = () => { drag = null; cv.style.cursor = 'grab'; };
  $('#phCancel').onclick = closeModal;
  $('#phSave').onclick = () => {
    if (!st.img) return;
    $('#phSave').disabled = true; $('#phSave').textContent = 'Saving…';
    const out = document.createElement('canvas');
    out.width = out.height = 512;
    const k = 512 / 280, s = 280 / Math.min(st.img.width, st.img.height) * st.zoom;
    out.getContext('2d').drawImage(st.img, st.ox * k, st.oy * k,
                                   st.img.width * s * k, st.img.height * s * k);
    out.toBlob(async blob => {
      const fd = new FormData();
      fd.append('file', blob, 'photo.png');
      try {
        const r = await fetch('/api/v1/me/avatar', {method: 'POST', body: fd});
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.detail || 'Upload failed');
        closeModal();
        await loadUser();
      } catch (e) {
        $('#phErr').textContent = e.message;
        $('#phSave').disabled = false; $('#phSave').textContent = 'Save photo';
      }
    }, 'image/png');
  };
}

function openAvatarChooser() {
  $('#modalBody').innerHTML = '<h3 style="margin:0 0 4px">Choose avatar</h3>'
    + '<div style="color:var(--ink3);font-size:13px;margin-bottom:12px">Pick one of the MetaBridge AI avatars.</div>'
    + '<div id="presetGrid" style="display:flex;gap:12px;flex-wrap:wrap">'
    + ['mb-1','mb-2','mb-3','mb-4','mb-5','mb-6'].map(p =>
        '<button data-preset="' + p + '" aria-label="Avatar ' + p.slice(3) + '"'
        + ' style="background:none;border:2px solid #dfe3ea;padding:0;margin:0;border-radius:50%;width:56px;height:56px;overflow:hidden;cursor:pointer">'
        + '<img src="/static/avatars/' + p + '.svg" alt="" style="width:100%;height:100%;display:block"></button>').join('')
    + '</div>'
    + '<div id="chErr" style="color:var(--red);font-size:13px;margin-top:8px"></div>'
    + '<div style="display:flex;justify-content:flex-end;margin-top:16px"><button class="secondary" id="chCancel">Cancel</button></div>';
  $('#modalBody').className = 'modal'; $('#modalBg').style.display = 'flex';
  $('#chCancel').onclick = closeModal;
  $('#presetGrid').querySelectorAll('[data-preset]').forEach(b => b.onclick = async () => {
    try {
      await api('/api/v1/me/avatar', {method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({type: 'PRESET', preset: b.dataset.preset})});
      closeModal();
      await loadUser();
    } catch (e) { $('#chErr').textContent = e.message; }
  });
}

function confirmRemovePhoto() {
  $('#modalBody').innerHTML = '<h3 style="margin:0 0 6px">Remove profile photo?</h3>'
    + '<div style="color:var(--ink3);font-size:13.5px">Your initials avatar will be used instead.</div>'
    + '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:18px">'
    + '<button class="secondary" id="rmCancel">Cancel</button>'
    + '<button id="rmGo" style="background:var(--red)">Remove</button></div>';
  $('#modalBody').className = 'modal'; $('#modalBg').style.display = 'flex';
  $('#rmCancel').onclick = closeModal;
  $('#rmGo').onclick = async () => {
    try { await api('/api/v1/me/avatar', {method: 'DELETE'}); } catch (e) { /* shown below */ }
    closeModal();
    await loadUser();
  };
}

$('#acctAvBtn').onclick = ev => { ev.stopPropagation(); acctSetOpen(false); openPhotoDialog(); };
$('#avUpload').onclick = openPhotoDialog;
$('#avChoose').onclick = openAvatarChooser;
$('#avRemove').onclick = confirmRemovePhoto;
$('#avUseInitials').onclick = async () => {
  await api('/api/v1/me/avatar', {method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({type: 'INITIALS'})});
  await loadUser();
};
$('#topMenu').querySelectorAll('[data-go]').forEach(b =>
  b.onclick = () => {
    document.querySelector('nav a[data-page="' + b.dataset.go + '"]').click();
    if (b.dataset.sec) showSettings(b.dataset.sec, true);
  });
$('#topSignOut').onclick = async () => {
  await fetch('/auth/logout', {method: 'POST'});
  window.location.href = '/';
};
$('#profileMenuBtn').onclick = ev => {
  ev.stopPropagation();
  const m = $('#profileMenu');
  m.style.display = m.style.display === 'block' ? 'none' : 'block';
  document.addEventListener('click', () => m.style.display = 'none', {once: true});
};
$('#profileMenu').querySelectorAll('[data-go]').forEach(b =>
  b.onclick = () => {
    document.querySelector('nav a[data-page="' + b.dataset.go + '"]').click();
    if (b.dataset.sec) showSettings(b.dataset.sec, true);
  });
$('#navCollapse').onclick = () => {
  document.body.classList.toggle('nav-min');
  $('#navCollapse').innerHTML = document.body.classList.contains('nav-min')
    ? '&#187;' : '&#171; <span class="lbl">Collapse</span>';
};

/* ---- mobile navigation drawer ---------------------------------------------
   Below 760px nav is off-canvas (see console.css). Opening it is a body class
   so CSS owns the animation; Escape and the scrim both close it, and picking a
   destination closes it too — otherwise the drawer would cover the page the
   user just navigated to. */
function setNavOpen(open) {
  document.body.classList.toggle('nav-open', open);
  const t = $('#navToggle');
  if (t) {
    t.setAttribute('aria-expanded', open ? 'true' : 'false');
    t.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
  }
  if (open) { const a = document.querySelector('nav a[data-page]'); if (a) a.focus(); }
  else if (t && matchMedia('(max-width:760px)').matches) t.focus();
}
if ($('#navToggle')) $('#navToggle').onclick = () =>
  setNavOpen(!document.body.classList.contains('nav-open'));
if ($('#navScrim')) $('#navScrim').onclick = () => setNavOpen(false);
document.querySelector('nav').addEventListener('click', ev => {
  if (ev.target.closest('a[data-page]')) setNavOpen(false);
});
addEventListener('keydown', ev => {
  if (ev.key === 'Escape' && document.body.classList.contains('nav-open')) setNavOpen(false);
});

/* ---- theme ----------------------------------------------------------------
   Three states: 'system' (no attribute — follow prefers-color-scheme),
   'light' and 'dark' (explicit, wins over the OS in both directions). */
function applyTheme(mode) {
  if (mode === 'light' || mode === 'dark') document.documentElement.setAttribute('data-theme', mode);
  else document.documentElement.removeAttribute('data-theme');
  try { localStorage.setItem('mb.theme', mode); } catch (e) {}
}
try { applyTheme(localStorage.getItem('mb.theme') || 'system'); } catch (e) {}
/* Static cross-page links in console.html (e.g. the Modernize hint pointing at
   Integrations) — one delegated handler so the markup only needs data-go. */
document.addEventListener('click', ev => {
  const g = ev.target.closest('a[data-go].gotoIntegrations');
  if (!g) return;
  ev.preventDefault();
  document.querySelector('nav a[data-page="' + g.dataset.go + '"]').click();
});
/* ---- notification bell ----------------------------------------------------
   The feed already existed (/api/system/notifications) but its only entry point
   was the bottom of System, inside the collapsed "Platform internals"
   accordion. Rows there repeated verbatim with no timestamps, no links and no
   way to mark them read, so a real backlog was indistinguishable from noise.
   Here: unread count on the bell, relative timestamps, one row per item with a
   destination where we know one, and an explicit "Mark all as read". */
const NOTIF_TOPIC_PAGE = {approvals: 'governance', governance: 'governance',
  jobs: 'dashboard', connections: 'marketplace', auth: 'settings',
  marketplace: 'marketplace', validation: 'validation'};
let gNotifOpen = false;
/* The feed carries `ts` (unix seconds) and leaves `at` empty, so relative
   times have to come from ts. Rows previously showed no time at all, which is
   why 53 near-identical entries could not be triaged. */
function notifWhen(n) {
  if (n.at) return fmtAgo(n.at);
  if (typeof n.ts === 'number') return fmtAgo(new Date(n.ts * 1000).toISOString());
  return '';
}
function notifSetOpen(open) {
  gNotifOpen = open;
  const m = $('#notifMenu'), b = $('#notifBtn');
  if (!m || !b) return;
  m.style.display = open ? 'block' : 'none';
  b.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open) loadNotifFeed();
}
async function refreshNotifBadge() {
  const dot = $('#notifDot');
  if (!dot) return;
  try {
    const d = await api('/api/system/notifications?limit=1');
    const unseen = (d.counts || {}).unseen || 0;
    dot.hidden = !unseen;
    dot.textContent = unseen > 99 ? '99+' : String(unseen);
    $('#notifBtn').setAttribute('aria-label',
      unseen ? 'Notifications — ' + unseen + ' unread' : 'Notifications');
  } catch (e) { dot.hidden = true; }
}
async function loadNotifFeed() {
  const m = $('#notifMenu');
  m.innerHTML = '<div style="padding:12px;color:var(--muted);font-size:12.5px">Loading…</div>';
  try {
    const d = await api('/api/system/notifications?limit=30');
    const rows = d.notifications || [];
    const unseen = (d.counts || {}).unseen || 0;
    m.innerHTML =
      '<div style="display:flex;align-items:center;gap:8px;padding:9px 12px;border-bottom:1px solid var(--line)">'
      + '<b style="font-size:12.5px">Notifications</b>'
      + '<span style="color:var(--muted);font-size:11.5px">' + (unseen ? unseen + ' unread' : 'all read') + '</span>'
      + '<span style="flex:1"></span>'
      + (unseen ? '<button type="button" id="notifSeen" class="secondary" style="margin:0;padding:3px 10px;font-size:11.5px">Mark all as read</button>' : '')
      + '</div>'
      + (rows.length
          ? rows.map(n => {
              const c = n.severity === 'critical' ? 'var(--red)'
                : n.severity === 'warning' ? 'var(--amber)'
                : n.severity === 'success' ? 'var(--green)' : 'var(--ink3)';
              const page = NOTIF_TOPIC_PAGE[n.topic] || '';
              return '<div class="notif-row" ' + (page ? 'data-page="' + esc(page) + '" ' : '')
                + 'style="padding:8px 12px;border-bottom:1px solid var(--line);'
                + (page ? 'cursor:pointer' : '') + '">'
                + '<div style="display:flex;gap:7px;align-items:baseline">'
                + '<span style="color:' + c + ';font-size:14px;line-height:1">•</span>'
                + '<span style="font-weight:600;font-size:12.5px;flex:1;min-width:0">' + esc(n.title) + '</span>'
                + (n.seen ? '' : '<span title="Unread" style="width:6px;height:6px;border-radius:50%;background:var(--accent);flex:none"></span>')
                + '</div>'
                + (n.body ? '<div style="font-size:11.5px;color:var(--muted);margin:2px 0 0 18px">' + esc(n.body) + '</div>' : '')
                + '<div style="font-size:11px;color:var(--muted2);margin:2px 0 0 18px">'
                + esc(notifWhen(n)) + '</div>'
                + '</div>';
            }).join('')
          : '<div style="padding:14px 12px;color:var(--muted);font-size:12.5px">Nothing to catch up on.</div>');
    const sb = $('#notifSeen');
    if (sb) sb.onclick = async ev => {
      ev.stopPropagation();
      try { await api('/api/system/notifications/seen', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: '{}'}); } catch (e) {}
      await refreshNotifBadge(); loadNotifFeed();
    };
    m.querySelectorAll('.notif-row[data-page]').forEach(r =>
      r.onclick = () => { notifSetOpen(false);
        document.querySelector('nav a[data-page="' + r.dataset.page + '"]').click(); });
  } catch (e) {
    m.innerHTML = '<div style="padding:12px;color:var(--red);font-size:12.5px">'
      + esc(e.message) + '</div>';
  }
}
if ($('#notifBtn')) {
  $('#notifBtn').onclick = ev => { ev.stopPropagation(); notifSetOpen(!gNotifOpen); };
  document.addEventListener('click', ev => {
    if (gNotifOpen && !ev.target.closest('#notifWrap')) notifSetOpen(false);
  });
  addEventListener('keydown', ev => { if (ev.key === 'Escape' && gNotifOpen) notifSetOpen(false); });
  refreshNotifBadge();
}

if ($('#themeSel')) {
  try { $('#themeSel').value = localStorage.getItem('mb.theme') || 'system'; } catch (e) {}
  $('#themeSel').onchange = ev => applyTheme(ev.target.value);
}
function withKey(url) { return apiKey ? url + (url.includes('?') ? '&' : '?') + 'api_key=' + encodeURIComponent(apiKey) : url; }

/* navigation */
let gBuildId = '';
function sectionError(label, why, el) {
  const box = $('#sectionErr');
  if (!box) return;
  let diag = '';
  if (el) {
    const host = el.parentElement;
    diag = 'container: parent=' + esc(host ? (host.id || host.tagName) : 'none')
      + ', height=' + el.offsetHeight
      + ', children=' + el.children.length
      + (el.offsetParent === null ? ', hidden by an ancestor' : '');
  }
  /* This banner used to hand a business user DOM diagnostics
     ("parent=mainContent, height=0, children=13, hidden by an ancestor") and a
     Docker command. The user-facing half now says what happened and offers the
     one action they can actually take; everything an engineer needs moves into
     a collapsed "Technical details" block and into the browser console. */
  const tech = [
    'console build ' + (gBuildId || 'unknown'),
    gPagesRepaired ? 'repaired ' + gPagesRepaired + ' misnested section(s)' : '',
    diag,
    "server rebuild, if this persists: docker compose up -d --build",
  ].filter(Boolean).join('\n');
  $('#sectionErrTitle').textContent = "This section couldn’t be displayed.";
  $('#sectionErrBody').innerHTML = esc(why)
    + '<br>Reloading the page usually fixes it. If it keeps happening, send the '
    + 'technical details below to whoever operates this server.'
    + '<div style="margin-top:9px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
    + '<button type="button" id="sectionErrReload" class="secondary" '
    + 'style="margin:0;padding:4px 12px;font-size:12.5px">Reload page</button></div>'
    + '<details style="margin-top:8px"><summary style="cursor:pointer;font-size:12px;'
    + 'font-weight:600">Technical details</summary>'
    + '<pre style="margin:6px 0 0;font-size:11.5px;white-space:pre-wrap;'
    + 'font-family:var(--mono)">' + esc(tech) + '</pre></details>';
  const rl = $('#sectionErrReload');
  if (rl) rl.onclick = () => location.reload();
  // Engineers reading the console get the same payload without expanding it.
  try { console.error('[MetaBridge] ' + label + ' failed to render\n' + tech); } catch (e) {}
  box.style.display = 'block';
}
// Shared by every section loader's catch block: an unhandled rejection here
// used to leave the section blank and indistinguishable from "no data yet"
// (empty jobs, empty estate...). This makes the failure visible and gives a
// Retry that re-runs the same loader instead of forcing a full reload.
function loadErr(boxId, e, retry) {
  // sessionExpired() already showed its own dialog and is about to navigate to
  // /login — a section error box for the same event would just be a second,
  // more confusing message about the same thing arriving alongside a modal.
  if (e && e.message === 'SESSION_EXPIRED') return;
  const el = $('#' + boxId);
  if (!el) return;
  el.innerHTML = esc(e && e.message ? e.message : String(e))
    + ' <a class="retry" style="color:inherit;font-weight:600;text-decoration:underline;cursor:pointer;margin-left:8px">Retry</a>';
  el.style.display = 'block';
  el.querySelector('.retry').onclick = () => { el.style.display = 'none'; retry(); };
}
/* The console is one document, so the URL is the ONLY place the current
   section can live: without it a refresh, a Back press or a pasted link all
   land on the default page and silently discard where the user was. The hash
   (#estate, #settings/members) carries it — a hash never reaches the server,
   so /console keeps returning the same shell and no route registration is
   needed. showPage() is the single entry point; nav clicks, popstate and
   first load all go through it, so the three can never disagree. */
function showPage(key, push) {
  const a = document.querySelector('nav a[data-page="' + key + '"]');
  if (!a) return false;
  // Switching Settings sub-sections already asks via showSettings' own guard;
  // leaving Settings entirely (sidebar, header menu, a global-search hit) did
  // not, so the same edits that triggered "Discard unsaved changes?" one click
  // earlier vanished silently on the very next click.
  if (key !== 'settings' && $('#page-settings') && $('#page-settings').classList.contains('visible')
      && settingsDirty()) {
    confirmDiscard(() => showPage(key, push));
    return false;
  }
  document.querySelectorAll('nav a').forEach(x => { x.classList.remove('active'); x.removeAttribute('aria-current'); });
  a.classList.add('active');
  a.setAttribute('aria-current', 'page');
  document.querySelectorAll('.page').forEach(p => p.classList.remove('visible'));
  const label = a.querySelector('.lbl') ? a.querySelector('.lbl').textContent : a.title;
  const target = $('#page-' + key);
  $('#sectionErr').style.display = 'none';
  if (!target) {                     // section missing from this build
    $('#crumb').innerHTML = 'Workspace / <b>' + label + '</b>';
    sectionError(label, 'This section is not present in the page the browser loaded.');
    return false;
  }
  target.classList.add('visible');
  // ...and verify it is actually on screen; a hidden/zero-height container is
  // the blank-page symptom, so surface it instead of showing nothing. A
  // misnested container is repaired here too, so a malformed page still works.
  requestAnimationFrame(() => {
    const main = document.querySelector('main');
    if (target.offsetParent === null && main && target.parentElement !== main) {
      main.appendChild(target);          // late self-heal, then re-measure
      gPagesRepaired++;
    }
    if (target.offsetParent === null || target.offsetHeight === 0)
      sectionError(label, 'The section is present but not visible.', target);
  });
  $('#crumb').innerHTML = 'Workspace / <b>' + label + '</b>';
  if (key !== 'reports' && typeof stopAgentRunPolling === 'function') stopAgentRunPolling();
  document.querySelector('main').classList.toggle('wide',
    ['estate', 'validation', 'reports', 'observability', 'system'].includes(key));
  // Every sub-tab that used to live only in localStorage (Reports' tool,
  // Overview's table view, Observability's monitor filter) now travels in the
  // hash like Settings' sub-section already did — so a link to any of them is
  // shareable and shows the same screen to whoever opens it, instead of each
  // visitor's own browser silently overriding it.
  if (push !== false) setHash(
    key === 'settings' ? 'settings/' + curSetSec
    : key === 'reports' ? 'reports/' + reportTool
    : key === 'dashboard' ? 'dashboard/' + dashView
    : key === 'observability' ? 'observability/' + gObsMonFilter
    : key);
  if (key === 'dashboard') loadDashboard();
  if (key === 'estate') loadEstate();
  if (key === 'validation') loadValidation();
  if (key === 'reports') loadReports();
  if (key === 'observability') loadObservability();
  if (key === 'system') loadSystem();
  if (key === 'marketplace') { loadMarketplace(); loadSavedConnections(); }
  if (key === 'governance') loadGovernanceExtras();
  // fillObjSelects/loadMovementSettings are async: a bare try/catch around the
  // call (no await) only catches a SYNCHRONOUS throw, never a rejection — the
  // failure became an unhandled rejection with nothing shown on screen.
  if (key === 'convert') fillObjSelects().catch(e => loadErr('convertErr', e, fillObjSelects));
  if (key === 'scaffold') loadMovementSettings().catch(e => loadErr('scaffoldErr', e, loadMovementSettings));
  if (key === 'settings') showSettings(curSetSec, true);
  return true;
}
// Writing the hash fires hashchange, which would re-run showPage and restart
// every loader; gHashLock makes our own writes a no-op for that listener.
let gHashLock = false;
// Which job the overlay is currently showing, so routeFromHash can tell an
// already-open job from one it needs to open (and avoid a reopen loop).
let gOpenJobId = null;
function setHash(h) {
  if (location.hash === '#' + h) return;
  gHashLock = true;
  history.pushState({page: h}, '', '#' + h);
  gHashLock = false;
}
// Only a nav entry that exists in THIS build is a valid route, so a stale or
// hand-typed link degrades to the default page instead of a blank console.
function routeFromHash() {
  const raw = decodeURIComponent((location.hash || '').replace(/^#/, ''));
  const [page, sec, arg] = raw.split('/');
  if (!page || !document.querySelector('nav a[data-page="' + page + '"]')) return false;
  if (page === 'settings' && sec && $('#sec-' + sec)) curSetSec = sec;
  if (page === 'reports' && sec && REPORT_TOOL_PANELS[sec]) reportTool = sec;
  if (page === 'dashboard' && sec && (sec === 'job' || sec === 'mod')) dashView = sec;
  // A job id in the route opens (or leaves open) its overlay; no id closes it,
  // so Back out of a job returns to the table instead of stranding the overlay.
  if (page === 'dashboard' && sec === 'job' && arg) {
    const want = decodeURIComponent(arg);
    if (gOpenJobId !== want) openJobDetail(want).catch(() => {});
  } else if (gOpenJobId) {
    gOpenJobId = null;
    if ($('#modalBg').style.display !== 'none') closeModal();
  }
  if (page === 'observability' && sec && ['all', 'attn', 'measured'].includes(sec)) gObsMonFilter = sec;
  return showPage(page, false);
}
// preventDefault: these now carry a real href (for focusability/keyboard
// activation), but navigation itself must still go through showPage/setHash
// so the browser's own hash jump can't race pushState and duplicate history.
document.querySelectorAll('nav a[data-page]').forEach(a =>
  a.onclick = ev => { ev.preventDefault(); showPage(a.dataset.page); });
window.addEventListener('popstate', () => routeFromHash() || showPage('dashboard', false));
window.addEventListener('hashchange', () => {   // typed/edited hash, not ours
  if (!gHashLock) routeFromHash();
});

/* global search: navigation + real jobs + saved connections (Cmd/Ctrl+K) */
// The field starts readonly so browser autofill can't push the login email
// into it on load or on SPA re-render; a real user interaction makes it
// editable, and it re-arms when left empty so autofill never gets another shot.
(function () {
  const gs = $('#gSearch');
  const enable = () => gs.removeAttribute('readonly');
  gs.addEventListener('focus', enable);
  gs.addEventListener('pointerdown', enable);
  gs.addEventListener('blur', () => { if (!gs.value) gs.setAttribute('readonly', ''); });
  window.gSearchFocus = () => { enable(); gs.focus(); };
})();
// Roving highlight for the listbox: gSearchResults/gSearchIdx track what's
// on screen so Arrow keys and Enter can drive it the same way a click does.
let gSearchResults = [];
let gSearchIdx = -1;
function gSearchClose() {
  $('#gHits').style.display = 'none';
  $('#gSearch').setAttribute('aria-expanded', 'false');
  $('#gSearch').removeAttribute('aria-activedescendant');
  gSearchIdx = -1;
}
function gSearchHighlight(idx) {
  gSearchIdx = idx;
  const gs = $('#gSearch');
  $('#gHits').querySelectorAll('[data-i]').forEach(el => {
    const on = +el.dataset.i === idx;
    el.setAttribute('aria-selected', on ? 'true' : 'false');
    if (on) el.scrollIntoView({block: 'nearest'});
  });
  if (idx >= 0) gs.setAttribute('aria-activedescendant', 'gHit-' + idx);
  else gs.removeAttribute('aria-activedescendant');
}
function gSearchActivate(idx) {
  const r = gSearchResults[idx];
  if (!r) return;
  gSearchClose();
  $('#gSearch').value = '';
  r.go();
}
document.addEventListener('keydown', ev => {
  if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === 'k') { ev.preventDefault(); window.gSearchFocus(); }
  if (ev.key === 'Escape') gSearchClose();
});
$('#gSearch').addEventListener('keydown', ev => {
  if (!gSearchResults.length || $('#gHits').style.display === 'none') return;
  if (ev.key === 'ArrowDown') { ev.preventDefault(); gSearchHighlight(Math.min(gSearchIdx + 1, gSearchResults.length - 1)); }
  else if (ev.key === 'ArrowUp') { ev.preventDefault(); gSearchHighlight(Math.max(gSearchIdx - 1, 0)); }
  else if (ev.key === 'Enter') { ev.preventDefault(); gSearchActivate(gSearchIdx >= 0 ? gSearchIdx : 0); }
});
let gJobsCache = null, gTwinSearchTried = false;
$('#gSearch').oninput = async ev => {
  const q = ev.target.value.trim().toLowerCase();
  const hits = $('#gHits');
  if (!q) { gSearchClose(); return; }
  const results = [];
  document.querySelectorAll('nav a[data-page]').forEach(a => {
    const name = a.querySelector('.lbl').textContent;
    if (name.toLowerCase().includes(q)) results.push({t: name, k: 'page', go: () => a.click()});
  });
  try {
    if (!gJobsCache) gJobsCache = (await api('/api/jobs')).jobs || [];
    // openJobDetail (status, artifacts, reports, actions) instead of dumping a
    // raw HTML report into a new tab with no way back — the search result
    // already named the job; landing anywhere but on it made the user search
    // again by eye for the thing they had just found.
    // Four runs of the same project used to render as four identical rows
    // ("Bank Data (agents)" x4) with nothing to choose between them. The kind
    // is now a display name and the timestamp + short id disambiguate.
    gJobsCache.filter(j => (j.project || '').toLowerCase().includes(q)).slice(0, 6)
      .forEach(j => results.push({t: j.project, k: jobKindLabel(j.kind),
        sub: (j.created || '').replace('T', ' ').slice(0, 16) + ' · ' + String(j.id).slice(0, 8),
        go: () => openJobDetail(j.id).catch(e => mbAlert(e.message))}));
  } catch (e) {}
  /* Estate assets. The placeholder has always promised "assets", but the corpus
     was jobs + connections + page names only, so `raw_orders` / `fct_sales` —
     tables plainly visible in the Data Estate graph — returned "No matches".
     The twin is fetched once and cached; a deployment with no twin built simply
     contributes nothing. */
  try {
    if (!gTwin && !gTwinSearchTried) {
      gTwinSearchTried = true;
      try { gTwin = await api('/api/twin'); } catch (e) {}
    }
    ((gTwin && gTwin.nodes) || []).filter(n =>
        (n.name || n.id || '').toLowerCase().includes(q)).slice(0, 6)
      .forEach(n => results.push({
        t: n.name || n.id, k: n.kind || 'asset',
        sub: [n.technology, n.domain].filter(Boolean).join(' · '),
        go: () => { document.querySelector('nav a[data-page="estate"]').click();
                    setTimeout(() => twinSelect(n.id).catch(() => {}), 500); }}));
  } catch (e) {}
  // Same reasoning for a connection hit: open ITS drawer, not the whole
  // catalogue the user then has to re-search by eye.
  (allConnections || []).filter(x => (x.name || '').toLowerCase().includes(q)).slice(0, 4)
    .forEach(x => results.push({t: x.name, k: 'connection',
      go: () => openConnector(x.connector, x.id)}));
  gSearchResults = results.slice(0, 12);
  hits.innerHTML = gSearchResults.map((r, i) =>
    '<div id="gHit-' + i + '" data-i="' + i + '" role="option" aria-selected="false">'
    + '<span class="gh-t">' + esc(r.t) + '</span>'
    + '<span class="k">' + esc(r.k) + '</span>'
    + (r.sub ? '<div class="gh-sub">' + esc(r.sub) + '</div>' : '')
    + '</div>').join('')
    || '<div style="color:var(--muted)">No matches</div>';
  hits.style.display = 'block';
  ev.target.setAttribute('aria-expanded', 'true');
  gSearchIdx = -1;
  hits.querySelectorAll('[data-i]').forEach(el =>
    el.onclick = () => gSearchActivate(+el.dataset.i));
};

/* ================= settings workspace ================= */
let myEmail = null, myPerms = [], myUser = null, gFirstRun = false;
let curSetSec = 'workspace', wsNameCache = '';
const ROLE_LABEL = {owner: 'Owner', admin: 'Admin', engineer: 'Engineer', viewer: 'Viewer'};
const roleColor = {owner: '#101d33', admin: 'var(--accent)', engineer: 'var(--green)', viewer: 'var(--muted)'};

const SET_LOADERS = {workspace: loadWorkspace, profile: loadProfileSec, ai: loadAiSettings,
                     secrets: loadSecrets, notifications: loadNotifSettings,
                     system: loadSystemInfo, members: loadMembers,
                     roles: renderRolesMatrix};

function settingsDirty() {
  return ['#wsSave', '#aiSave', '#pnSave', '#nfSave'].some(s => !$(s).disabled);
}
// A refresh or tab close bypasses showPage/showSettings entirely, so the same
// unsaved-edits protection they give against in-app navigation needs its own
// hook here — the browser's own "leave site?" prompt, not a MetaBridge dialog.
window.addEventListener('beforeunload', ev => {
  if (!settingsDirty()) return;
  ev.preventDefault();
  ev.returnValue = '';
});
function showSettings(sec, force) {
  if (!force && sec !== curSetSec && settingsDirty()) { confirmDiscard(() => showSettings(sec, true)); return; }
  curSetSec = sec;
  // Keep the sub-section in the URL too, so a refresh inside Settings returns
  // to this pane and not to the Workspace default.
  if ($('#page-settings') && $('#page-settings').classList.contains('visible'))
    setHash('settings/' + sec);
  document.querySelectorAll('#setNav button').forEach(b => b.classList.toggle('active', b.dataset.sec === sec));
  $('#setNavSel').value = sec;
  document.querySelectorAll('.set-sec').forEach(s => s.classList.toggle('visible', s.id === 'sec-' + sec));
  (SET_LOADERS[sec] || (() => {}))();
}
// `after` runs once the dirty state is cleared — either back into another
// Settings sub-section (showSettings) or out of Settings entirely (showPage).
function confirmDiscard(after) {
  $('#modalBody').innerHTML = '<h3 style="margin:0 0 6px">Discard unsaved changes?</h3>'
    + '<div style="color:var(--ink3);font-size:13.5px">Your edits in this section have not been saved.</div>'
    + '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:18px">'
    + '<button class="secondary" id="dcKeep">Keep editing</button>'
    + '<button id="dcGo" style="background:var(--red)">Discard</button></div>';
  $('#modalBody').className = 'modal'; $('#modalBg').style.display = 'flex';
  $('#dcKeep').onclick = closeModal;
  $('#dcGo').onclick = () => {
    closeModal();
    ['#wsSave', '#aiSave', '#pnSave', '#nfSave'].forEach(s => { $(s).disabled = true; });
    ['#wsDirty', '#aiDirty', '#pnDirty', '#nfDirty'].forEach(s => { $(s).hidden = true; });
    $('#aiKey').value = ''; $('#aiToken').value = ''; $('#nfPass').value = '';
    after();
  };
}
document.querySelectorAll('#setNav button').forEach(b => b.onclick = () => showSettings(b.dataset.sec));
$('#setNavSel').innerHTML = [['GENERAL', [['workspace', 'Workspace'], ['profile', 'Profile']]],
  ['PLATFORM', [['ai', 'AI Runtime'], ['secrets', 'Connections & Secrets'], ['notifications', 'Notifications'], ['system', 'System']]],
  ['ACCESS', [['members', 'Members'], ['roles', 'Roles & Permissions']]]]
  .map(g => '<optgroup label="' + g[0] + '">' + g[1].map(i =>
    '<option value="' + i[0] + '">' + i[1] + '</option>').join('') + '</optgroup>').join('');
$('#setNavSel').onchange = ev => showSettings(ev.target.value);

function canManageSettings() { return myPerms.includes('*') || myPerms.includes('settings:manage'); }
function canManageUsers() { return myPerms.includes('*') || myPerms.includes('users:manage'); }
// open mode (no accounts configured) leaves every action available; once a
// session exists the server-issued permission list is the single source
function can(p) {
  if (!myUser) return true;              // open mode / server still enforces
  return myPerms.includes('*') || myPerms.includes(p);
}
function permTitle(p) {
  return 'Your role (' + (myPerms._role || 'viewer') + ') does not allow this — needs ' + p + '. Ask a workspace admin.';
}
// Static run/config controls, disabled for roles that lack the permission
// the API will demand — the same map the server enforces (see web/app.py
// _required_permission). Dynamic renderers gate inline at render time.
const RBAC_UI = [
  // pipeline / analysis runs -> jobs:run
  ['#analyzeBtn', 'jobs:run'], ['#convertSelected', 'jobs:run'],
  ['#convForm button[type=submit]', 'jobs:run'],
  // NB: #autofixBtn is gated inline in showFindings() — it is created
  // dynamically in a modal, after this boot-time pass runs.
  ['#govForm button[type=submit]', 'jobs:run'],
  ['#scafForm button[type=submit]', 'jobs:run'],
  ['#assessRun', 'jobs:run'], ['#airRun', 'jobs:run'], ['#debtRun', 'jobs:run'],
  ['#finRun', 'jobs:run'], ['#secRun', 'jobs:run'], ['#docsRun', 'jobs:run'],
  ['#evtAnalyze', 'jobs:run'], ['#evtIntel', 'jobs:run'], ['#evtGenerate', 'jobs:run'],
  ['#orchAnalyze', 'jobs:run'], ['#orchGenerate', 'jobs:run'], ['#orchDepAdd', 'jobs:run'],
  ['#objInvRun', 'jobs:run'], ['#objGen', 'jobs:run'],
  ['#twinBuild', 'jobs:run'], ['#twinSimulate', 'jobs:run'],
  ['#agRun', 'jobs:run'], ['#plugScaffold', 'jobs:run'],
  // platform configuration -> settings:manage
  ['#aiTest', 'settings:manage'], ['#mktKeypair', 'settings:manage'],
];
function applyRbacUi() {
  // Only ever DISABLE controls the role can't use — never force-enable, or
  // we would clobber a button's own lifecycle state (e.g. #generateAllBtn
  // ships disabled until analysis, run buttons disable themselves in-flight).
  // When the role regains a permission the tooltip is cleared but the app's
  // own disabled state is left intact.
  RBAC_UI.forEach(([sel, perm]) => {
    document.querySelectorAll(sel).forEach(el => {
      if (!can(perm)) {
        el.disabled = true;
        el.dataset.rbacBlocked = '1';
        el.title = permTitle(perm);
      } else if (el.dataset.rbacBlocked) {
        delete el.dataset.rbacBlocked;
        el.disabled = false;
        if (el.title && el.title.startsWith('Your role')) el.title = '';
      }
    });
  });
}

/* ---- workspace ---- */
let wsSnapshot = '';
function wsState() { return $('#wsName').value.trim() + '|' + $('#wsTz').value; }
function wsDirtyCheck() {
  const dirty = canManageSettings() && wsState() !== wsSnapshot;
  $('#wsSave').disabled = !dirty; $('#wsDirty').hidden = !dirty;
}
async function loadWorkspace() {
  const err = $('#wsErr'); err.style.display = 'none';
  try {
    const d = await api('/api/settings/workspace');
    const sel = $('#wsTz');
    if (!sel.options.length) {
      let zones;
      try { zones = Intl.supportedValuesOf('timeZone'); }
      catch (e) { zones = ['Asia/Kolkata', 'Asia/Singapore', 'Europe/Berlin', 'Europe/London',
                           'America/New_York', 'America/Chicago', 'America/Los_Angeles', 'Australia/Sydney']; }
      /* 420 raw IANA ids in one ungrouped list, underscores intact
         (America/Port_of_Spain), no offsets and no way to search meant setting
         a basic workspace preference required knowing IANA naming and
         scrolling hundreds of rows. Now: the browser's detected zone first,
         then region optgroups, with a readable city name and a live UTC
         offset on every option. A <select> with optgroups is also type-ahead
         searchable by label in every browser. */
      const tzNow = new Date();
      const offsetOf = z => {
        try {
          const p = new Intl.DateTimeFormat('en', {timeZone: z, timeZoneName: 'longOffset'})
            .formatToParts(tzNow).find(x => x.type === 'timeZoneName');
          return p ? p.value.replace('GMT', 'UTC').replace(/^UTC$/, 'UTC+0:00') : '';
        } catch (e) { return ''; }
      };
      const optFor = z => {
        const city = z.split('/').slice(1).join(' / ').replace(/_/g, ' ') || z;
        const off = offsetOf(z);
        return '<option value="' + esc(z) + '">' + esc(city || z)
          + (off ? ' (' + esc(off) + ')' : '') + ' — ' + esc(z) + '</option>';
      };
      let detected = '';
      try { detected = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (e) {}
      const regions = new Map();
      zones.forEach(z => {
        const r = z.includes('/') ? z.split('/')[0] : 'Other';
        if (!regions.has(r)) regions.set(r, []);
        regions.get(r).push(z);
      });
      sel.innerHTML = '<option value="">Not set</option>'
        + (detected ? '<optgroup label="Detected">' + optFor(detected) + '</optgroup>' : '')
        + '<optgroup label="Common"><option value="UTC">UTC (UTC+0:00) — UTC</option></optgroup>'
        + [...regions.keys()].sort().map(r => '<optgroup label="' + esc(r.replace(/_/g, ' ')) + '">'
            + regions.get(r).slice().sort().map(optFor).join('') + '</optgroup>').join('');
    }
    $('#wsName').value = d.name || '';
    wsNameCache = d.name || '';
    $('#wsId').textContent = d.workspace_id || 'Assigned on first save';
    sel.value = d.timezone || '';
    $('#wsOwner').textContent = d.owner ? getUserDisplayName(d.owner) + ' · ' + d.owner.email : '—';
    const can = canManageSettings();
    $('#wsName').disabled = !can; sel.disabled = !can;
    wsSnapshot = wsState(); wsDirtyCheck();
  } catch (e) { err.textContent = e.message; err.style.display = 'block'; }
}
$('#wsName').oninput = wsDirtyCheck;
$('#wsTz').onchange = wsDirtyCheck;
$('#wsForm').onsubmit = async ev => {
  ev.preventDefault();
  const err = $('#wsErr'); err.style.display = 'none';
  try {
    const d = await api('/api/settings/workspace', {method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: $('#wsName').value, timezone: $('#wsTz').value})});
    $('#wsId').textContent = d.workspace_id;
    wsNameCache = d.name;
    wsSnapshot = wsState(); wsDirtyCheck();
  } catch (e) { err.textContent = e.message; err.style.display = 'block'; }
};

/* ---- profile: personal information ---- */
let pnSnapshot = '';
function pnDirtyCheck() {
  const dirty = !!myUser && $('#pnName').value.trim() !== pnSnapshot;
  $('#pnSave').disabled = !dirty; $('#pnDirty').hidden = !dirty;
}
function loadProfileSec() {
  if (!myUser) return;
  // the form edits the RAW stored name (myUser.name), never a prettified
  // rendering of it — what you save is exactly what reloads
  $('#pnName').value = myUser.name || getUserDisplayName(myUser);
  $('#pnEmail').textContent = myUser.email;
  pnSnapshot = $('#pnName').value.trim(); pnDirtyCheck();
}
$('#pnName').oninput = pnDirtyCheck;
$('#pnForm').onsubmit = async ev => {
  ev.preventDefault();
  const err = $('#pnErr'), okEl = $('#pnOk');
  err.style.display = 'none'; okEl.hidden = true;
  try {
    await api('/api/v1/me', {method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: $('#pnName').value})});
    await loadUser();               // header + dropdown update immediately
    loadProfileSec();
    okEl.hidden = false;            // explicit success feedback
    setTimeout(() => { okEl.hidden = true; }, 2500);
  } catch (e) { err.textContent = e.message; err.style.display = 'block'; }
};

/* ---- AI runtime ---- */
const AI_MODELS = {
  anthropic: [['claude-sonnet-5', 'Claude Sonnet 5 — recommended'],
              ['claude-opus-4-8', 'Claude Opus 4.8'],
              ['claude-haiku-4-5-20251001', 'Claude Haiku 4.5']],
  bedrock: [['global.anthropic.claude-sonnet-4-5-20250929-v1:0',
             'Claude Sonnet 4.5 (global) — recommended']],
};
const AI_STATES = {
  NOT_CONFIGURED: ['Not configured', '#8894a8'],
  NOT_TESTED: ['Configured — not tested', 'var(--amber)'],
  TESTING: ['Testing connection…', 'var(--amber)'],
  CONNECTED: ['Connected', 'var(--green)'],
  ERROR: ['Connection issue', '#b03a2e'],
  MODEL_UNAVAILABLE: ['Model unavailable', '#b03a2e'],
  AUTHENTICATION_ERROR: ['Authentication failed', '#b03a2e'],
};
let aiCfgCache = {provider: '', region: '', model: ''};
let aiSnapshot = '', aiLastTest = null;

function modelLabel(id) {
  for (const list of Object.values(AI_MODELS))
    for (const m of list) if (m[0] === id) return m[1].replace(' — recommended', '');
  return id || '';
}
function aiProv() {
  const b = document.querySelector('.prov[aria-checked="true"]');
  return b ? b.dataset.prov : '';
}
function bedAuthMode() {
  const r = document.querySelector('input[name=bedauth]:checked');
  return r ? r.value : 'iam';
}
function aiState() {
  return [aiProv(), $('#aiRegion').value.trim(), $('#aiModel').value.trim(), bedAuthMode()].join('|');
}
function aiDirtyCheck() {
  const dirty = canManageSettings()
    && (aiState() !== aiSnapshot || !!$('#aiKey').value || !!$('#aiToken').value);
  if ($('#aiSave').dataset.saving) return;   // mid-save: leave button as-is
  $('#aiSave').disabled = !dirty; $('#aiDirty').hidden = !dirty;
  // clear a lingering "✓ Saved" note once the user starts editing again
  const err = $('#aiErr');
  if (dirty && err.dataset.ok) { err.style.display = 'none'; delete err.dataset.ok; }
}
function aiSelectProv(p) {
  document.querySelectorAll('.prov').forEach(b =>
    b.setAttribute('aria-checked', String(b.dataset.prov === p)));
  $('#aiCfg').style.display = p ? '' : 'none';
  $('#aiBedrockCfg').style.display = p === 'bedrock' ? '' : 'none';
  $('#aiAnthCfg').style.display = p === 'anthropic' ? '' : 'none';
}
function aiModelOptions(p, current) {
  const sel = $('#aiModelSel');
  const known = AI_MODELS[p] || [];
  const opts = known.slice();
  if (current && !known.some(m => m[0] === current)) opts.push([current, current + ' — configured']);
  opts.push(['__custom', 'Custom model ID…']);
  sel.innerHTML = opts.map(m => '<option value="' + esc(m[0]) + '">' + esc(m[1]) + '</option>').join('');
  sel.value = current && opts.some(m => m[0] === current) ? current
            : (known.length ? known[0][0] : '__custom');
  if (sel.value !== '__custom') $('#aiModel').value = sel.value;
}
function bedAuthVisibility() {
  const key = bedAuthMode() === 'key';
  $('#bedKeyFld').hidden = !key;
  $('#bedIamHint').textContent = key
    ? 'A Bedrock API key is used instead of the deployment’s AWS credentials.'
    : 'MetaBridge uses the AWS credentials configured for this deployment.';
}
function agoLabel(ts) {
  const m = Math.round((Date.now() - ts) / 60000);
  return m < 1 ? 'just now' : m === 1 ? '1 minute ago' : m < 60 ? m + ' minutes ago'
       : Math.round(m / 60) + ' h ago';
}
function renderAiStatus(state, friendly) {
  const prov = aiProv();
  if (!state) state = !prov ? 'NOT_CONFIGURED' : (aiLastTest ? aiLastTest.state : 'NOT_TESTED');
  const s = AI_STATES[state];
  const provName = {bedrock: 'Amazon Bedrock', anthropic: 'Anthropic'}[prov] || '';
  const meta = prov
    ? [provName, $('#aiRegion').value.trim(), modelLabel($('#aiModel').value.trim())]
        .filter(Boolean).map(esc).join('<br>')
    : 'Choose a provider above to turn on MetaBridge AI.';
  $('#aiRt').innerHTML =
    '<div class="st-line"><i class="dot" style="background:' + s[1] + '"></i>' + s[0] + '</div>'
    + '<div class="st-meta">' + meta + '</div>'
    + (aiLastTest && state !== 'TESTING'
        ? '<div class="st-meta">Last tested ' + agoLabel(aiLastTest.at) + '</div>' : '')
    + (friendly ? '<div class="st-err">' + esc(friendly) + '</div>' : '');
}
async function loadAiSettings() {
  const err = $('#aiErr'); err.style.display = 'none';
  try {
    const d = await api('/api/settings/ai');
    aiCfgCache = d;
    aiSelectProv(d.provider || '');
    $('#aiRegion').value = d.region || '';
    $('#aiModel').value = d.model || '';
    aiModelOptions(d.provider || '', d.model || '');
    $('#aiKey').value = ''; $('#aiToken').value = '';
    $('#aiKey').placeholder = d.api_key_set ? 'Saved — leave blank to keep' : 'sk-ant-…';
    $('#aiToken').placeholder = d.bedrock_token_set ? 'Saved — leave blank to keep' : '';
    const authRadio = document.querySelector('input[name=bedauth][value='
      + (d.bedrock_token_set ? 'key' : 'iam') + ']');
    if (authRadio) authRadio.checked = true;
    bedAuthVisibility();
    const can = canManageSettings();
    ['#aiRegion', '#aiKey', '#aiToken', '#aiModel', '#aiModelSel'].forEach(x => { $(x).disabled = !can; });
    document.querySelectorAll('.prov, input[name=bedauth]').forEach(b => { b.disabled = !can; });
    aiSnapshot = aiState(); aiDirtyCheck();
    renderAiStatus();
  } catch (e) { err.textContent = e.message; err.style.display = 'block'; }
}
document.querySelectorAll('.prov').forEach(b => b.onclick = () => {
  aiSelectProv(b.dataset.prov);
  aiModelOptions(b.dataset.prov, aiCfgCache.provider === b.dataset.prov ? aiCfgCache.model : '');
  aiDirtyCheck(); renderAiStatus();
});
document.querySelectorAll('input[name=bedauth]').forEach(r => r.onchange = () => {
  bedAuthVisibility(); aiDirtyCheck();
});
$('#aiModelSel').onchange = () => {
  if ($('#aiModelSel').value === '__custom') { $('#aiAdv').open = true; $('#aiModel').focus(); }
  else $('#aiModel').value = $('#aiModelSel').value;
  aiDirtyCheck();
};
$('#aiModel').oninput = () => {
  if ($('#aiModelSel').value !== $('#aiModel').value) $('#aiModelSel').value = '__custom';
  aiDirtyCheck();
};
['#aiRegion', '#aiKey', '#aiToken'].forEach(x => { $(x).oninput = aiDirtyCheck; });
$('#aiSave').onclick = async () => {
  const err = $('#aiErr'); err.style.display = 'none'; err.style.color = '';
  const btn = $('#aiSave');
  if (btn.dataset.saving) return;            // guard against double-submit
  const label = btn.textContent;
  btn.dataset.saving = '1'; btn.disabled = true; btn.textContent = 'Saving…';
  try {
    const d = await api('/api/settings/ai', {method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({provider: aiProv(), api_key: $('#aiKey').value,
                            region: $('#aiRegion').value, model: $('#aiModel').value,
                            bedrock_token: bedAuthMode() === 'key' ? $('#aiToken').value : '',
                            clear_bedrock_token: aiProv() === 'bedrock' && bedAuthMode() === 'iam'})});
    aiCfgCache = d; aiLastTest = null;
    $('#aiKey').value = ''; $('#aiToken').value = '';
    $('#aiKey').placeholder = d.api_key_set ? 'Saved — leave blank to keep' : 'sk-ant-…';
    $('#aiToken').placeholder = d.bedrock_token_set ? 'Saved — leave blank to keep' : '';
    $('#aiModel').value = d.model || '';
    aiModelOptions(d.provider || '', d.model || '');
    delete btn.dataset.saving; btn.textContent = label;
    aiSnapshot = aiState(); aiDirtyCheck();  // now not dirty -> button disabled = "saved"
    renderAiStatus();
    err.style.color = 'var(--green)'; err.dataset.ok = '1';
    err.textContent = '✓ Configuration saved.'; err.style.display = 'block';
  } catch (e) {
    // validation (422) or API/network error — keep the edits, allow retry
    delete btn.dataset.saving; btn.textContent = label;
    err.style.color = ''; delete err.dataset.ok;
    err.textContent = e.message; err.style.display = 'block';
    aiDirtyCheck();                          // still dirty -> Save re-enabled
  }
};
function classifyAiFailure(detail) {
  const d = (detail || '').toLowerCase();
  if (/model/.test(d) && /(not |invalid|access|denied|exist|unavailable|on-demand|validation)/.test(d))
    return 'MODEL_UNAVAILABLE';
  if (/(auth|credential|token|expired|unauthorized|forbidden|signature|401|403)/.test(d))
    return 'AUTHENTICATION_ERROR';
  return 'ERROR';
}
// -- Notifications (outbound email) settings --------------------------------
let nfSnapshot = null, nfCache = null;
function nfTlsMode() {
  const r = document.querySelector('input[name="nftls"]:checked');
  return r ? r.value : 'starttls';
}
function nfState() {
  return JSON.stringify([$('#nfHost').value.trim(), $('#nfPort').value.trim(),
    $('#nfUser').value.trim(), $('#nfPass').value, $('#nfFrom').value.trim(),
    $('#nfFromName').value.trim(), nfTlsMode(), $('#nfEnabled').checked]);
}
function nfDirtyCheck() {
  if ($('#nfSave').dataset.saving) return;   // mid-save: leave button as-is
  const dirty = canManageSettings() && nfSnapshot !== null
    && nfState() !== nfSnapshot;
  $('#nfSave').disabled = !dirty; $('#nfDirty').hidden = !dirty;
}
function nfRenderStatus(d) {
  const box = $('#notifStatus');
  // Headline: one badge that states the situation, using the shared component
  // so it reads the same as every other status pill in the console.
  const badge = d.ready
    ? ['ok', 'Ready']
    : (d.configured ? ['warn', 'Action needed'] : ['', 'Not configured']);
  const head = '<div class="nf-head"><span class="'
    + (badge[0] ? 'badge ' + badge[0] : 'badge') + '">'
    + badge[1] + '</span><b>'
    + (d.ready ? 'Sending via ' + esc(d.provider)
       : (d.configured ? 'Configuration incomplete'
          : 'Email events stay in the console'))
    + '</b></div>';
  // Detail rows: only what actually has a value — an unconfigured transport
  // shows nothing rather than a column of em-dashes.
  const rows = [];
  if (d.host) rows.push(['Host', esc(d.host) + (d.port ? ':' + d.port : '')]);
  if (d.from) rows.push(['From', esc(d.from_name ? d.from_name + ' <' + d.from + '>' : d.from)]);
  if (d.user) rows.push(['Username', esc(d.user)]);
  if (d.host) rows.push(['Encryption', {ssl: 'Implicit TLS (SMTPS)',
    starttls: 'STARTTLS', none: 'None — plaintext'}[d.tls] || esc(d.tls)]);
  if (d.host) rows.push(['Credential', d.password_set ? 'Stored' : 'None set']);
  const dl = rows.length
    ? '<dl>' + rows.map(r => '<dt>' + r[0] + '</dt><dd>' + r[1] + '</dd>').join('') + '</dl>'
    : '<dl><dt>Transport</dt><dd class="empty">No SMTP relay configured yet</dd></dl>';
  const notes = (d.notes || []).length
    ? '<ul class="nf-notes">' + d.notes.map(n => '<li>' + esc(n) + '</li>').join('') + '</ul>'
    : '';
  box.innerHTML = head + dl + notes;
}
async function loadNotifSettings() {
  const box = $('#notifStatus'), msg = $('#notifTestMsg');
  if (msg) msg.textContent = '';
  $('#nfErr').style.display = 'none';
  $('#nfFromErr').hidden = true;             // clear stale field-level errors
  $('#nfFrom').setAttribute('aria-invalid', 'false');
  box.textContent = 'Loading…';
  try {
    const d = await api('/api/settings/notifications/email');
    nfCache = d;
    nfRenderStatus(d);
    // populate the form from the effective config
    const lock = d.env_locked || {}, can = canManageSettings();
    $('#nfHost').value = d.host || '';
    $('#nfPort').value = d.port || 587;
    $('#nfUser').value = d.user || '';
    $('#nfPass').value = '';
    $('#nfPass').placeholder = d.password_set ? 'Saved — leave blank to keep' : '';
    $('#nfFrom').value = d.from || '';
    $('#nfFromName').value = d.from_name || '';
    const tls = d.ssl ? 'ssl' : (d.starttls ? 'starttls' : 'none');
    document.querySelectorAll('input[name="nftls"]').forEach(r => {
      r.checked = r.value === tls;
      r.disabled = !can || !!(lock.ssl || lock.starttls);
    });
    $('#nfEnabled').checked = d.enabled !== false || !d.configured;
    // fields pinned by the server environment are authoritative -> read-only
    [['#nfHost', 'host'], ['#nfPort', 'port'], ['#nfUser', 'user'],
     ['#nfPass', 'password'], ['#nfFrom', 'from'],
     ['#nfFromName', 'from_name']].forEach(([sel, key]) => {
      const el = $(sel), locked = !!lock[key];
      el.readOnly = locked; el.disabled = !can;
      // readOnly (not disabled) keeps the value reachable by keyboard and
      // screen reader; the styling comes from input[readonly] in the sheet.
      el.title = locked ? 'Set by a server environment variable' : '';
      el.setAttribute('aria-readonly', locked ? 'true' : 'false');
    });
    $('#nfEnabled').disabled = !can || !!lock.enabled;
    const anyLock = Object.keys(lock).some(k => lock[k]);
    $('#nfHostHint').hidden = !anyLock;
    if (anyLock) $('#nfHostHint').textContent =
      'Some fields are set by server environment variables and are read-only here.';
    if (d.ssl || d.starttls === false) $('#nfAdv').open = true;
    nfSnapshot = nfState(); nfDirtyCheck();
    const t = $('#notifTest');
    if (t) {
      t.disabled = !d.ready || !can;
      t.title = d.ready ? '' : 'Configure and save email first';
    }
  } catch (e) {
    box.innerHTML = '<span style="color:var(--red)">' + esc((e && e.message)
      || 'Could not load email status.') + '</span>';
  }
}
// Inline validation: report a bad From address beside the field on blur,
// rather than making the operator round-trip a save to discover it.
function nfValidateFrom() {
  const el = $('#nfFrom'), box = $('#nfFromErr');
  const val = el.value.trim(), host = $('#nfHost').value.trim();
  let err = '';
  if (val && !/^[^@\s]+@[^@\s.]+\.[^@\s]+$/.test(val))
    err = 'Enter a complete email address, e.g. no-reply@your-domain.com';
  else if (host && !val)
    err = 'A From address is required when an SMTP host is set.';
  box.textContent = err; box.hidden = !err;
  el.setAttribute('aria-invalid', err ? 'true' : 'false');
  el.setAttribute('aria-describedby', err ? 'nfFromErr' : 'nfFromHint');
  return !err;
}
['#nfHost', '#nfPort', '#nfUser', '#nfPass', '#nfFrom', '#nfFromName']
  .forEach(s => { $(s).oninput = nfDirtyCheck; });
$('#nfFrom').onblur = nfValidateFrom;
$('#nfHost').onblur = () => { if ($('#nfFrom').value.trim() || $('#nfHost').value.trim()) nfValidateFrom(); };
document.querySelectorAll('input[name="nftls"]').forEach(r => {
  r.onchange = () => {
    // keep the port in step with the chosen transport unless env-pinned
    const p = $('#nfPort');
    if (!p.readOnly) {
      if (nfTlsMode() === 'ssl' && p.value === '587') p.value = '465';
      else if (nfTlsMode() !== 'ssl' && p.value === '465') p.value = '587';
    }
    nfDirtyCheck();
  };
});
$('#nfEnabled').onchange = nfDirtyCheck;
$('#nfSave').onclick = async () => {
  const err = $('#nfErr'); err.style.display = 'none'; err.style.color = '';
  const btn = $('#nfSave');
  if (btn.dataset.saving) return;            // guard against double-submit
  if (!nfValidateFrom()) {                   // catch it before the round-trip
    $('#nfFrom').focus();                    // move focus to the offending field
    return;
  }
  const label = btn.textContent;
  btn.dataset.saving = '1'; btn.disabled = true; btn.textContent = 'Saving…';
  $('#notifTestMsg').textContent = '';
  try {
    const tls = nfTlsMode();
    const d = await api('/api/settings/notifications/email', {method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({host: $('#nfHost').value.trim(),
                            port: $('#nfPort').value.trim() || '587',
                            user: $('#nfUser').value.trim(),
                            password: $('#nfPass').value,
                            from: $('#nfFrom').value.trim(),
                            from_name: $('#nfFromName').value.trim(),
                            starttls: tls === 'starttls', ssl: tls === 'ssl',
                            enabled: $('#nfEnabled').checked})});
    delete btn.dataset.saving; btn.textContent = label;
    await loadNotifSettings();               // re-read effective config
    err.style.color = 'var(--green)'; err.dataset.ok = '1';
    err.textContent = '✓ Configuration saved.'; err.style.display = 'block';
  } catch (e) {
    // validation (422) or API/network error — keep the edits, allow retry
    delete btn.dataset.saving; btn.textContent = label;
    err.style.color = ''; delete err.dataset.ok;
    err.textContent = e.message; err.style.display = 'block';
    nfDirtyCheck();                          // still dirty -> Save re-enabled
  }
};
$('#notifTest').onclick = async () => {
  const btn = $('#notifTest'), msg = $('#notifTestMsg'), lbl = btn.textContent;
  btn.disabled = true; btn.textContent = 'Sending…'; msg.textContent = '';
  try {
    const d = await api('/api/settings/notifications/email/test', {method: 'POST'});
    msg.style.color = 'var(--green)'; msg.textContent = '✓ Sent to ' + d.sent_to;
  } catch (e) {
    msg.style.color = 'var(--red)'; msg.textContent = (e && e.message) || 'Send failed';
  } finally {
    btn.textContent = lbl; btn.disabled = false;
  }
};

$('#aiTest').onclick = async () => {
  renderAiStatus('TESTING');
  try {
    const d = await api('/api/settings/ai/test', {method: 'POST'});
    if (d.ok) {
      aiLastTest = {state: 'CONNECTED', at: Date.now()};
      renderAiStatus('CONNECTED');
    } else if ((d.detail || '') === 'No provider configured') {
      aiLastTest = null; renderAiStatus('NOT_CONFIGURED');
    } else {
      const state = classifyAiFailure(d.detail);
      aiLastTest = {state, at: Date.now()};
      const provName = aiProv() === 'bedrock' ? 'Amazon Bedrock' : 'the Anthropic API';
      const region = $('#aiRegion').value.trim();
      const friendly = state === 'MODEL_UNAVAILABLE'
        ? 'MetaBridge could reach ' + provName + ', but the configured model is not available'
          + (region ? ' in ' + region : '') + '.'
        : state === 'AUTHENTICATION_ERROR'
        ? (aiProv() === 'bedrock'
            ? 'Authentication failed. Check the AWS credentials configured for this deployment.'
            : 'Authentication failed. Check the Anthropic API credential.')
        : 'MetaBridge AI could not complete a test call. ' + (d.detail || '').slice(0, 160);
      renderAiStatus(state, friendly);
    }
  } catch (e) {
    aiLastTest = {state: 'ERROR', at: Date.now()};
    renderAiStatus('ERROR', e.message);
  }
};

/* ---- connections & secrets ---- */
async function loadSecrets() {
  const rows = [];
  try {
    const ai = await api('/api/settings/ai');
    if (ai.provider === 'anthropic')
      rows.push({name: 'Anthropic API credential', used: 'MetaBridge AI',
                 status: ai.api_key_set ? 'Configured' : 'Not configured', go: 'ai'});
    else if (ai.provider === 'bedrock')
      rows.push(ai.bedrock_token_set
        ? {name: 'Amazon Bedrock API key', used: 'MetaBridge AI', status: 'Configured', go: 'ai'}
        : {name: 'AWS IAM role (server)', used: 'MetaBridge AI', status: 'Provided by deployment', go: 'ai'});
    else
      rows.push({name: 'AI credential', used: 'MetaBridge AI', status: 'Not configured', go: 'ai'});
  } catch (e) { /* section still renders connection rows */ }
  try {
    const d = await api('/api/v1/connections');
    (d.connections || []).forEach(c => rows.push({
      name: c.name || c.connector, used: (c.connector || '') + ' connection',
      status: c.has_secrets ? 'Stored on this server' : 'Server environment / per-use',
      go: 'marketplace'}));
  } catch (e) {}
  const stColor = s => s === 'Not configured' ? 'var(--amber)' : 'var(--green)';
  const secRows = rows.map(r => '<tr><td><b>' + esc(r.name) + '</b></td><td>' + esc(r.used) + '</td>'
    + '<td><span class="stat"><i style="background:' + stColor(r.status) + '"></i>'
    + esc(r.status) + '</span></td>'
    + '<td style="text-align:right"><a data-sgo="' + r.go + '" style="color:var(--accent);'
    + 'font-weight:600;cursor:pointer">Configure</a></td></tr>');
  const secHeader = '<tr><th>Credential</th><th>Used by</th><th>Status</th><th></th></tr>';
  const secEmpty = '<tr><td colspan=4 style="color:var(--muted)">No credentials yet.</td></tr>';
  const renderSecTable = () => {
    renderPage('secTable', secRows, secHeader, secEmpty, 'credentials', () => {
      $('#secTable').querySelectorAll('[data-sgo]').forEach(a => a.onclick = () => {
        if (a.dataset.sgo === 'ai') showSettings('ai');
        else document.querySelector('nav a[data-page="marketplace"]').click();
      });
    });
  };
  renderSecTable();
  bindPager('secTable', renderSecTable);
}

/* ---- members ---- */
async function loadMembers() {
  const err = $('#teamErr'); err.style.display = 'none';
  $('#memAddBtn').style.display = canManageUsers() ? '' : 'none';
  if (!canManageUsers()) {
    $('#memBody').innerHTML = '<div class="fhint">Members are managed by workspace owners and admins. '
      + 'Your role: <b>' + esc(ROLE_LABEL[myPerms._role] || 'Viewer') + '</b>.</div>';
    return;
  }
  let d;
  try { d = await api('/api/users'); }
  catch (e) { err.textContent = e.message; err.style.display = 'block'; return; }
  const owners = d.users.filter(u => u.role === 'owner').length;
  $('#memBody').innerHTML = '<div class="set-table-wrap"><table id="memTable"></table>'
    + pagerHtml('memTable') + '</div>';
  const memRows = d.users.map(u => {
    const isMe = u.email === myEmail;
    const soleOwner = u.role === 'owner' && owners <= 1;
    // mirrors the server rules: only owners may change/remove/reset
    // owner accounts; the sole owner can never be demoted or removed
    const canMutate = !soleOwner && (u.role !== 'owner' || myPerms._role === 'owner');
    const canReset = canResetFor(u);
    return '<tr data-email="' + esc(u.email) + '">'
      + '<td><div class="mem-cell"><span class="avatar" data-avatar></span>'
      + '<span><b>' + esc(getUserDisplayName(u)) + '</b>'
      + (isMe ? '<div class="you">You</div>' : '') + '</span></div></td>'
      + '<td style="color:var(--ink3)">' + esc(u.email) + '</td>'
      + '<td>' + badge(ROLE_LABEL[u.role] || u.role, roleColor[u.role] || 'var(--ink3)') + '</td>'
      + '<td style="color:var(--muted)">' + esc((u.created || '').slice(0, 10)) + '</td>'
      + '<td><span class="stat"><i style="background:var(--green)"></i>Active</span></td>'
      + '<td style="text-align:right">' + (isMe || !(canMutate || canReset) ? ''
          : '<div class="rowmenu"><button class="kebab" style="position:static" '
            + 'aria-haspopup="menu" aria-label="Member actions">&#8942;</button>'
            + '<div class="menu" role="menu">'
            + (canMutate ? '<button data-act="role" role="menuitem">Change role…</button>' : '')
            + (canReset ? '<button data-act="reset" role="menuitem">Send reset link…</button>' : '')
            + (canMutate ? '<button data-act="rm" class="signout" role="menuitem">Remove from workspace</button>' : '')
            + '</div></div>')
      + '</td></tr>';
  });
  const memHeader = '<tr><th>Member</th><th>Email</th><th>Role</th><th>Joined</th><th>Status</th><th></th></tr>';
  const memEmpty = '<tr><td colspan=6 style="color:var(--muted)">No members yet.</td></tr>';
  const renderMemTable = () => {
    renderPage('memTable', memRows, memHeader, memEmpty, 'members', () => {
      d.users.forEach(u => {
        const slot = document.querySelector('#memTable tr[data-email="'
          + (window.CSS && CSS.escape ? CSS.escape(u.email) : u.email) + '"] [data-avatar]');
        if (slot) renderAvatar(slot, u);
      });
      document.querySelectorAll('#memTable .kebab').forEach(k => k.onclick = ev => {
        ev.stopPropagation();
        const m = k.nextElementSibling;
        const open = m.style.display === 'block';
        document.querySelectorAll('#memTable .rowmenu .menu').forEach(x => { x.style.display = 'none'; });
        m.style.display = open ? 'none' : 'block';
        if (!open) document.addEventListener('click', () => { m.style.display = 'none'; }, {once: true});
      });
      document.querySelectorAll('#memTable [data-act]').forEach(b => b.onclick = () => {
        const email = b.closest('tr').dataset.email;
        const u = d.users.find(x => x.email === email);
        if (b.dataset.act === 'role') openRoleDialog(u);
        else if (b.dataset.act === 'reset') openResetLinkDialog(u);
        else openRemoveMemberDialog(u);
      });
    });
  };
  renderMemTable();
  bindPager('memTable', renderMemTable);
}
// resetting an owner's password would hand over the owner account, so only
// an owner may mint reset links for owner accounts (mirrors the server rule)
function canResetFor(u) {
  return u.role !== 'owner' || myPerms._role === 'owner';
}
function openResetLinkDialog(u) {
  $('#modalBody').innerHTML = '<h3 style="margin:0 0 6px">Send a password reset link</h3>'
    + '<div style="color:var(--ink3);font-size:13.5px;margin-bottom:12px">'
    + esc(getUserDisplayName(u)) + ' · ' + esc(u.email) + '</div>'
    + '<div style="color:var(--ink3);font-size:13px">Generates a <b>one-time</b> link (expires in 60 minutes) '
    + 'that lets them choose a new password. Share it with them directly — it is shown only once '
    + 'and never emailed or logged by MetaBridge.</div>'
    + '<div id="rlOut" style="display:none;margin-top:12px">'
    +   '<label>One-time reset link</label>'
    +   '<div style="display:flex;gap:8px;align-items:center">'
    +   '<input type="text" id="rlLink" readonly style="flex:1;font-size:12px">'
    +   '<button type="button" id="rlCopy" class="secondary" style="margin:0;white-space:nowrap">Copy</button></div>'
    +   '<div class="fhint">Minting a new link invalidates this one.</div></div>'
    + '<div class="err" id="rlErr"></div>'
    + '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:18px">'
    + '<button class="secondary" id="rlCancel">Close</button><button id="rlGo">Generate link</button></div>';
  $('#modalBody').className = 'modal'; $('#modalBg').style.display = 'flex';
  $('#rlCancel').onclick = closeModal;
  $('#rlGo').onclick = async () => {
    const err = $('#rlErr'); err.style.display = 'none';
    try {
      const d2 = await api('/api/users/' + encodeURIComponent(u.email) + '/reset-link', {method: 'POST'});
      $('#rlLink').value = d2.reset_link;
      $('#rlOut').style.display = 'block';
      $('#rlGo').textContent = 'Regenerate link';
    } catch (e) { err.textContent = e.message; err.style.display = 'block'; }
  };
  $('#rlCopy').onclick = async () => {
    try { await navigator.clipboard.writeText($('#rlLink').value); $('#rlCopy').textContent = 'Copied ✓'; }
    catch (e) { $('#rlLink').select(); document.execCommand('copy'); $('#rlCopy').textContent = 'Copied ✓'; }
    setTimeout(() => { $('#rlCopy').textContent = 'Copy'; }, 2000);
  };
}
function roleOptions(selectedRole) {
  // only an owner may hand out the owner role (mirrors the server rule)
  return Object.keys(ROLE_LABEL)
    .filter(r => r !== 'owner' || myPerms._role === 'owner' || selectedRole === 'owner')
    .map(r => '<option value="' + r + '"'
      + (r === selectedRole ? ' selected' : '') + '>' + ROLE_LABEL[r] + '</option>').join('');
}
function openRoleDialog(u) {
  $('#modalBody').innerHTML = '<h3 style="margin:0 0 6px">Change role</h3>'
    + '<div style="color:var(--ink3);font-size:13.5px;margin-bottom:12px">'
    + esc(getUserDisplayName(u)) + ' · ' + esc(u.email) + '</div>'
    + '<label for="roleSel2">Role</label><select id="roleSel2">'
    + roleOptions(u.role)
    + '</select><div class="err" id="roleErr2"></div>'
    + '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:18px">'
    + '<button class="secondary" id="roleCancel">Cancel</button><button id="roleGo">Save role</button></div>';
  $('#modalBody').className = 'modal'; $('#modalBg').style.display = 'flex';
  $('#roleCancel').onclick = closeModal;
  $('#roleGo').onclick = async () => {
    try {
      await api('/api/users/' + encodeURIComponent(u.email), {method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({role: $('#roleSel2').value})});
      closeModal(); loadMembers();
    } catch (e) {
      $('#roleErr2').textContent = e.message; $('#roleErr2').style.display = 'block';
    }
  };
}
function openRemoveMemberDialog(u) {
  $('#modalBody').innerHTML = '<h3 style="margin:0 0 6px">Remove '
    + esc(getUserDisplayName(u)) + '?</h3>'
    + '<div style="color:var(--ink3);font-size:13.5px">This member will lose access to '
    + esc(wsNameCache || 'this workspace') + ' immediately.</div>'
    + '<div class="err" id="rmErr2"></div>'
    + '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:18px">'
    + '<button class="secondary" id="rmCancel2">Cancel</button>'
    + '<button id="rmGo2" style="background:var(--red)">Remove member</button></div>';
  $('#modalBody').className = 'modal'; $('#modalBg').style.display = 'flex';
  $('#rmCancel2').onclick = closeModal;
  $('#rmGo2').onclick = async () => {
    try {
      await api('/api/users/' + encodeURIComponent(u.email), {method: 'DELETE'});
      closeModal(); loadMembers();
    } catch (e) { $('#rmErr2').textContent = e.message; $('#rmErr2').style.display = 'block'; }
  };
}
$('#memAddBtn').onclick = () => {
  $('#modalBody').innerHTML = '<h3 style="margin:0 0 6px">Add member</h3>'
    + '<div style="color:var(--ink3);font-size:13.5px;margin-bottom:8px">Creates an account on this '
    + 'workspace with a temporary password to share with them directly.</div>'
    + '<label for="amName">Name</label><input type="text" id="amName">'
    + '<label for="amEmail">Email</label><input type="text" id="amEmail" placeholder="person@company.com">'
    + '<label for="amPw">Temporary password</label><input type="text" id="amPw" placeholder="8+ characters — they should change it">'
    + '<label for="amRole">Role</label><select id="amRole">'
    + roleOptions('engineer')
    + '</select><div class="err" id="amErr"></div>'
    + '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:18px">'
    + '<button class="secondary" id="amCancel">Cancel</button><button id="amGo">Add member</button></div>';
  $('#modalBody').className = 'modal'; $('#modalBg').style.display = 'flex';
  $('#amCancel').onclick = closeModal;
  $('#amGo').onclick = async () => {
    try {
      await api('/api/users', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: $('#amName').value, email: $('#amEmail').value,
                              password: $('#amPw').value, role: $('#amRole').value})});
      closeModal(); loadMembers();
    } catch (e) { $('#amErr').textContent = e.message; $('#amErr').style.display = 'block'; }
  };
};

/* ---- roles & permissions (mirrors web/auth.py PERMISSIONS + route mapping) ---- */
function renderRolesMatrix() {
  const ROWS = [
    ['Manage workspace', ['owner', 'admin']],
    ['Manage AI runtime', ['owner', 'admin']],
    ['Manage members', ['owner', 'admin']],
    ['Manage integrations', ['owner', 'admin', 'engineer']],
    ['Analyze workloads', ['owner', 'admin', 'engineer']],
    ['Run modernizations', ['owner', 'admin', 'engineer']],
    ['Generate pipelines', ['owner', 'admin', 'engineer']],
    ['Run validation', ['owner', 'admin', 'engineer']],
    ['Approve AI recommendations', ['owner', 'admin', 'engineer']],
    ['Delete jobs & artifacts', ['owner', 'admin', 'engineer']],
    ['View governance', ['owner', 'admin', 'engineer', 'viewer']],
    ['View reports', ['owner', 'admin', 'engineer', 'viewer']],
  ];
  $('#rolesMatrix').innerHTML = '<tr><th>Capability</th>'
    + Object.values(ROLE_LABEL).map(l => '<th>' + l + '</th>').join('') + '</tr>'
    + ROWS.map(r => '<tr><td>' + r[0] + '</td>'
      + Object.keys(ROLE_LABEL).map(role => '<td>'
        + (r[1].includes(role)
            ? '<span class="y" role="img" aria-label="Allowed">✓</span>'
            : '<span class="n" role="img" aria-label="Not allowed">—</span>')
        + '</td>').join('') + '</tr>').join('');
}

/* file drops */
const chosen = {};
document.querySelectorAll('.drop').forEach(drop => {
  const input = drop.querySelector('input');
  const key = drop.dataset.drop;
  drop.onclick = () => input.click();
  input.onchange = () => setFile(input.files[0]);
  ['dragover','dragenter'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.add('hover'); }));
  ['dragleave','drop'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.remove('hover'); }));
  drop.addEventListener('drop', ev => setFile(ev.dataTransfer.files[0]));
  function setFile(f) { if (!f) return; chosen[key] = f;
    drop.querySelector('.txt').innerHTML = '<b>' + esc(f.name) + '</b> (' + Math.round(f.size/1024) + ' KB)'
      + '<span class="hint">Drop another file to replace it</span>';
    // a dropped table manifest previews like a handed-off one
    if (key === 'scafFile') renderScaffoldManifest(f);
    // the land-anyway choice only exists once there is ETL logic to exclude
    if (key === 'scafEtlFile') { const r = $('#scafLandRow'); if (r) r.style.display = 'inline-flex'; }
    if (key === 'convFile') updateConvHint(); }
});

/* ---- digital-twin dropzone -------------------------------------------------
   The twin takes MANY files but does not need the platform-sniffing list the
   orchestration/event zones render, and #twinFiles.files must stay the source
   of truth because twinBuild reads it directly. So this is the plain zone plus
   a filename summary, wired to the same drag/drop affordances. */
(function () {
  const drop = document.getElementById('twinDrop');
  if (!drop) return;
  const input = document.getElementById('twinFiles');
  const txt = drop.querySelector('.txt');
  const empty = txt.innerHTML;
  const render = () => {
    const fs = [...input.files];
    if (!fs.length) { txt.innerHTML = empty; return; }
    const names = fs.slice(0, 3).map(f => f.name).join(', ');
    txt.innerHTML = '<b>' + esc(fs.length) + ' file' + (fs.length === 1 ? '' : 's')
      + ' attached</b><span class="hint">' + esc(names)
      + (fs.length > 3 ? ' +' + (fs.length - 3) + ' more' : '')
      + ' · drop more to replace</span>';
  };
  drop.onclick = () => input.click();
  input.onchange = render;
  ['dragover', 'dragenter'].forEach(e => drop.addEventListener(e, ev => {
    ev.preventDefault(); drop.classList.add('hover'); }));
  ['dragleave', 'drop'].forEach(e => drop.addEventListener(e, ev => {
    ev.preventDefault(); drop.classList.remove('hover'); }));
  drop.addEventListener('drop', ev => {
    // assigning a DataTransfer keeps input.files authoritative for twinBuild
    if (ev.dataTransfer && ev.dataTransfer.files.length) {
      try { input.files = ev.dataTransfer.files; } catch (e) {}
      render();
    }
  });
})();

/* ---- multi-file dropzones (orchestration, events) ----------------------
   A bare <input type="file" multiple> renders "3 files" and throws the names
   away: nothing said WHAT was attached, and removing one meant re-picking the
   whole set. These keep their own array — the input is only a file picker —
   and render it as a list that replaces the zone once anything is held.

   The platform beside each row is a guess from the file's extension (and, for
   the ambiguous .json, from a bounded sniff of its head). It is labelled as
   such in each panel's ⓘ, because the analyzer is what actually decides. */
const mfiles = {};
const MDROP_EXT = {
  orch: {py:'Airflow', jil:'AutoSys', xml:'Control-M', cron:'crontab', txt:'crontab',
         dtsx:'SSIS', dsx:'DataStage', item:'Talend'},
  evt:  {avsc:'Avro schema', properties:'Kafka / Connect', mqsc:'IBM MQ',
         sql:'ksqlDB / Flink SQL', prm:'GoldenGate', xml:'NiFi template', conf:'Config'},
};
// Markers anchored on keys the format actually requires, so an unrecognised
// document stays "JSON export" rather than being mislabelled.
const MDROP_JSON = {
  orch: [[/"Microsoft\.DataFactory|"activities"\s*:/, 'ADF / Fabric'],
         [/"StartAt"\s*:/, 'Step Functions'],
         [/"taskflow|"INFA/i, 'IDMC taskflow'],
         [/"job_definition_id"|"dbt_version"|"steps_override"/, 'dbt Cloud job']],
  evt:  [[/"subject"\s*:[\s\S]*"schema"\s*:/, 'Schema Registry'],
         [/"connector\.class"/, 'Debezium / Connect'],
         [/"eventHubs?"|"consumerGroup"/i, 'Event Hubs']],
};
async function mdropLabel(kind, f) {
  const ext = (f.name.split('.').pop() || '').toLowerCase();
  if (ext !== 'json') return MDROP_EXT[kind] && MDROP_EXT[kind][ext] || '';
  try {
    const head = await f.slice(0, 4096).text();
    for (const [re, label] of (MDROP_JSON[kind] || [])) if (re.test(head)) return label;
  } catch (e) { /* unreadable head — say nothing rather than guess */ }
  return 'JSON export';
}
function mdropFiles(kind) { return mfiles[kind] || []; }
// Each panel's status line doubles as the "nothing attached yet" note beside
// its button, the way the empty footer reads in the rest of the console. Only
// that exact phrase is written or cleared, so a real status ("Analyzing…", an
// error) is never clobbered.
const MDROP_NOTE = {orch: 'orchStatus', evt: 'evtStatus'};
const MDROP_EMPTY = 'No files selected';
function mdropNote(kind, empty) {
  const el = document.getElementById(MDROP_NOTE[kind] || '');
  if (!el) return;
  if (empty && !el.textContent.trim()) el.textContent = MDROP_EMPTY;
  else if (!empty && el.textContent.trim() === MDROP_EMPTY) el.textContent = '';
}
function mdropRender(root) {
  const kind = root.dataset.mdrop;
  const list = root.querySelector('.flist'), zone = root.querySelector('.mzone');
  const sum = root.querySelector('.fsum'), exts = root.querySelector('.exts');
  const files = mdropFiles(kind);
  zone.hidden = files.length > 0;
  if (exts) exts.hidden = files.length > 0;
  list.hidden = !files.length;
  sum.hidden = !files.length;
  mdropNote(kind, !files.length);
  if (!files.length) { list.innerHTML = ''; return; }
  list.innerHTML = files.map((f, i) =>
    '<div class="frow"><span class="fn" title="' + esc(f.name) + '">' + esc(f.name) + '</span>'
    + '<span class="fp" data-plat="' + i + '"></span>'
    + '<button type="button" class="fx" data-rm="' + i + '" title="Remove ' + esc(f.name)
    + '" aria-label="Remove ' + esc(f.name) + '">&times;</button></div>').join('');
  const named = files.filter(f => f._mbPlat).map(f => f._mbPlat);
  const uniq = [...new Set(named)];
  sum.innerHTML = files.length + (files.length === 1 ? ' file' : ' files')
    + (uniq.length ? ' · ' + esc(uniq.slice(0, 3).join(', '))
        + (uniq.length > 3 ? ' +' + (uniq.length - 3) : '') : '')
    + ' · <button type="button" class="linkbtn" data-add="1">add more</button>';
  files.forEach((f, i) => {
    const cell = list.querySelector('[data-plat="' + i + '"]');
    if (cell) cell.textContent = f._mbPlat || '';
  });
}
document.querySelectorAll('.mdrop').forEach(root => {
  const kind = root.dataset.mdrop, input = root.querySelector('input[type="file"]');
  const zone = root.querySelector('.mzone');
  mfiles[kind] = [];
  async function add(fileList) {
    const incoming = [...(fileList || [])];
    if (!incoming.length) return;
    // same name + size twice is a re-pick, not a second file
    const seen = new Set(mfiles[kind].map(f => f.name + ':' + f.size));
    const fresh = incoming.filter(f => !seen.has(f.name + ':' + f.size));
    mfiles[kind] = mfiles[kind].concat(fresh);
    mdropRender(root);
    await Promise.all(fresh.map(async f => { f._mbPlat = await mdropLabel(kind, f); }));
    mdropRender(root);
  }
  zone.onclick = () => input.click();
  input.onchange = () => { add(input.files); input.value = ''; };
  ['dragover','dragenter'].forEach(e => zone.addEventListener(e, ev => { ev.preventDefault(); zone.classList.add('hover'); }));
  ['dragleave','drop'].forEach(e => zone.addEventListener(e, ev => { ev.preventDefault(); zone.classList.remove('hover'); }));
  zone.addEventListener('drop', ev => add(ev.dataTransfer.files));
  mdropRender(root);                       // paints the "No files selected" note
  root.addEventListener('click', ev => {
    const rm = ev.target.closest('[data-rm]');
    if (rm) { mfiles[kind].splice(+rm.dataset.rm, 1); mdropRender(root); return; }
    if (ev.target.closest('[data-add]')) input.click();
  });
});

/* ---- project dropzones (Reports) --------------------------------------
   Every report tool exposed a bare <input type="file" webkitdirectory>: an OS
   button with no drop target, no accepted-format line, and no feedback past
   the browser's own "42 files". These wrap the same input in the zone the
   rest of the console uses, and keep their own array so the summary can name
   what is actually attached.

   Browse REPLACES (that is what the native control does, and re-picking a
   project folder means a different project); a drop ADDS, because dragging a
   second batch in is an additive gesture. A dropped FOLDER is walked through
   the entries API and every file keeps the path it had inside the drop —
   the analyzers key on project structure (models/, mappings/), so flattening
   it would change their answers. */
const pfiles = {};
function pdropFiles(kind) { return pfiles[kind] || []; }
// the name the analyzers see: the path within the picked folder or the drop
function pdropPath(f) { return f._mbPath || f.webkitRelativePath || f.name; }
const PDROP_MAX = 4000;                    // a runaway drop cannot hang the tab

async function pdropWalk(entry, prefix, out) {
  if (!entry || out.length >= PDROP_MAX) return;
  if (entry.isFile) {
    const f = await new Promise(res => entry.file(res, () => res(null)));
    if (f) { f._mbPath = prefix + entry.name; out.push(f); }
    return;
  }
  if (!entry.isDirectory) return;
  const reader = entry.createReader();
  // readEntries returns a PAGE of children, not all of them — loop until empty
  for (;;) {
    const batch = await new Promise(res => reader.readEntries(res, () => res([])));
    if (!batch.length) break;
    for (const child of batch) await pdropWalk(child, prefix + entry.name + '/', out);
  }
}
function pdropRender(root) {
  const kind = root.dataset.pdrop;
  const zone = root.querySelector('.mzone'), sum = root.querySelector('.fsum');
  const files = pdropFiles(kind);
  zone.hidden = false;                     // the zone stays: re-dropping is normal
  sum.hidden = !files.length;
  if (!files.length) return;
  // the common root of the drop, which is the project the user thinks in
  const roots = new Set(files.map(f => {
    const p = pdropPath(f); const i = p.indexOf('/');
    return i > 0 ? p.slice(0, i) + '/' : '';
  }));
  const named = [...roots].filter(Boolean);
  sum.innerHTML = files.length + (files.length === 1 ? ' file' : ' files')
    + (named.length === 1 ? ' · ' + esc(named[0]) : '')
    + ' · <button type="button" class="linkbtn" data-preplace="1">replace</button>'
    + ' · <button type="button" class="linkbtn" data-pclear="1">clear</button>';
}
document.querySelectorAll('.pdrop').forEach(root => {
  const kind = root.dataset.pdrop, input = root.querySelector('input[type="file"]');
  const zone = root.querySelector('.mzone');
  pfiles[kind] = [];
  function dedupe(list) {
    const seen = new Set(); const out = [];
    for (const f of list) {
      const k = pdropPath(f) + ':' + f.size;
      if (!seen.has(k)) { seen.add(k); out.push(f); }
    }
    return out.slice(0, PDROP_MAX);
  }
  async function addDrop(dt) {
    const out = [];
    const items = [...(dt.items || [])];
    const entries = items.map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
    if (entries.length) for (const e of entries) await pdropWalk(e, '', out);
    else out.push(...(dt.files || []));    // no entries API: loose files only
    pfiles[kind] = dedupe(pfiles[kind].concat(out));
    pdropRender(root);
  }
  zone.onclick = () => input.click();
  input.onchange = () => { pfiles[kind] = dedupe([...input.files]); pdropRender(root); };
  ['dragover','dragenter'].forEach(e => zone.addEventListener(e, ev => { ev.preventDefault(); zone.classList.add('hover'); }));
  ['dragleave','drop'].forEach(e => zone.addEventListener(e, ev => { ev.preventDefault(); zone.classList.remove('hover'); }));
  zone.addEventListener('drop', ev => addDrop(ev.dataTransfer));
  root.addEventListener('click', ev => {
    if (ev.target.closest('[data-preplace]')) { input.value = ''; input.click(); return; }
    if (ev.target.closest('[data-pclear]')) { pfiles[kind] = []; input.value = ''; pdropRender(root); }
  });
});

/* "and N more": the tail of a reference list, folded away until asked for */
document.addEventListener('click', ev => {
  const b = ev.target.closest && ev.target.closest('[data-more]');
  if (!b) return;
  const rest = b.parentNode.querySelector('.rest');
  if (!rest) return;
  const open = rest.hidden;
  rest.hidden = !open;
  b.textContent = open ? 'show less' : 'and ' + b.dataset.more + ' more';
});

function card(n, l, color, click) {
  return '<div class="card"' + (click ? ' data-filter="' + click + '" style="cursor:pointer"' : '') + '>'
    + '<div class="n"' + (color ? ' style="color:'+color+'"' : '') + '>' + n + '</div><div class="l">' + l
    + (click ? ' ↓' : '') + '</div></div>';
}
// Badges are pill-style tokens (tinted bg + colored text + border) per the design
// system. Call sites still pass a legacy solid hex, so map it onto a tone class.
const BADGE_TONE = {
  'var(--green)':'ok', '#157F3D':'ok',
  'var(--red)':'bad', '#B42318':'bad',
  'var(--amber)':'warn', '#B45309':'warn',
  'var(--accent)':'info', '#1E62D0':'info',
};
function badge(text, color) {
  const tone = BADGE_TONE[color] || BADGE_TONE[String(color).toLowerCase()] || '';
  return '<span class="badge' + (tone ? ' ' + tone : '') + '">' + text + '</span>';
}

/* ---------------- findings explorer ---------------- */
const SEV_COLOR = {MANUAL:'var(--red)', ERROR:'#7b241c', WARNING:'var(--amber)', INFO:'var(--accent)', VIOLATION:'var(--red)'};
// Findings-drawer tones. MANUAL gets its own tone rather than sharing `bad` with
// ERROR: a run with 11 manual items and 1 blocker must not paint 12 red rows, or
// the one finding that actually stops the conversion is lost in the crowd.
const SEV_TONE = {MANUAL:'manual', ERROR:'bad', VIOLATION:'bad', WARNING:'warn', INFO:'info'};
let lastReport = null;

function collectFindings(report) {
  const out = [];
  (report.project_issues || []).forEach(i => out.push({obj: '(project)', ...i}));
  (report.mappings || []).forEach(m => (m.issues || []).forEach(i => out.push({...i, obj: i.object || m.name})));
  (report.policy_findings || []).forEach(f => out.push({severity: f.severity, code: f.code,
    message: f.message, object: f.mapping, detail: f.column ? (f.column + ' · ' + f.category) : '', suggestion: ''}));
  ((report.validation || {}).findings || []).forEach(f => out.push({severity: f.severity,
    code: 'XML_' + f.code, message: f.message, object: f.location || '(xml)', detail: '', suggestion: ''}));
  return out;
}

// "Rewrite reconciliation checks as dbt tests" + 5 -> "Rewrite 5 reconciliation
// checks as dbt tests". Only messages phrased as an imperative task are rewritten:
// the count is inserted after a known leading verb, before the plural noun it
// quantifies. Descriptive messages ("Model was converted as…", "QUALIFY is not
// decomposable") are left exactly as written — injecting a number into those
// produces "Model 5 was converted", which is worse than showing no count at all.
const COUNT_VERBS = /^(rewrite|convert|migrate|replace|review|port|refactor|remove|update|translate)$/i;
function countedMessage(msg, n) {
  const m = msg.match(/^(\w+)\s+((?:\w+\s+){0,2}?)(\w+s)\b/);
  if (!m || !COUNT_VERBS.test(m[1])) return msg;
  return m[1] + ' ' + n + ' ' + m[2] + m[3] + msg.slice(m[0].length);
}
function oneLine(s) {
  return s.split('\n').map(x => x.trim()).filter(Boolean).join(' ');
}
// Many codes interpolate the object name into the message, so grouping by code
// yields "Source BENEFICIARY has decimal column(s)…" as the headline for 50
// different tables. Drop the object name when the group covers more than one, so
// the card states the class of problem and the occurrence list names the tables.
function genericMessage(msg, objs) {
  const uniq = [...new Set(objs)];
  if (uniq.length < 2) return msg;
  let out = msg;
  uniq.forEach(o => {
    if (!o || o.length < 3) return;
    out = out.split(o).join('').replace(/\s{2,}/g, ' ');
  });
  // A message that was ONLY the object name collapses to nothing — keep the original.
  return out.replace(/\s+([.,;:])/g, '$1').trim() || msg;
}
// Gutter line numbers for a snippet. The snippet is an excerpt with no known
// offset into the source file, so these are 1-based positions WITHIN the excerpt.
function numberedLines(s) {
  return s.split('\n').slice(0, 40).map((ln, i) =>
    '<span class="ln"><i>' + (i + 1) + '</i>' + esc(ln) + '</span>').join('');
}

function showFindings(report, filter) {
  lastReport = report;
  const all = collectFindings(report);
  const sevs = ['MANUAL','ERROR','VIOLATION','WARNING','INFO'];
  const counts = {};
  sevs.forEach(s => counts[s] = all.filter(f => f.severity === s).length);
  const chip = (sev, label, n, on) =>
    '<button class="fchip' + (on ? ' on' : '') + ' t-' + (SEV_TONE[sev] || 'none') + '"'
    + ' data-sev="' + (sev || '') + '" aria-pressed="' + (on ? 'true' : 'false') + '">'
    + (sev ? '<i class="dot" aria-hidden="true"></i>' : '')
    + '<span class="lb">' + label.charAt(0) + label.slice(1).toLowerCase() + '</span>'
    + '<span class="n">' + n + '</span></button>';
  const chips = sevs.filter(s => counts[s]).map(s => chip(s, s, counts[s], filter === s)).join('')
    + chip('', 'All', all.length, !filter);
  const list = filter ? all.filter(f => f.severity === filter) : all;

  // Repeated findings collapse into ONE card whose body lists each occurrence —
  // the repeats are variations on a single piece of work, so they read as one task.
  // Grouping is by severity+code, NOT by message: many codes interpolate the object
  // name into the text ("Source BENEFICIARY has decimal column(s)…"), so a
  // message-keyed group leaves 50 near-identical cards that differ only by table.
  // The card headline is then the message with that object name factored out.
  const groups = [];
  const byKey = {};
  list.forEach((f, i) => {
    const k = f.severity + '|' + f.code;
    if (!byKey[k]) { byKey[k] = {f: f, i: i, items: []}; groups.push(byKey[k]); }
    byKey[k].items.push(f);
  });

  const rows = groups.map(g => {
    const f = g.f, n = g.items.length;
    const obj = f.object || f.obj || '';
    const tone = SEV_TONE[f.severity] || 'none';
    // ERROR means the object was skipped entirely — that is what actually stops a
    // conversion, so it earns the "blocker" label rather than a generic severity tag.
    const label = f.severity === 'ERROR' ? 'BLOCKER' : f.severity;
    const objs = g.items.map(x => x.object || x.obj || '').filter(o => o && o !== '(project)');
    // Headline: strip the per-object name out of the message so the card reads as
    // one class of problem, then state the count. Falls back to the raw message.
    const head = n > 1 ? countedMessage(genericMessage(f.message || '', objs), n)
                       : (f.message || '');
    // Each occurrence line names its object and, where present, its snippet —
    // that pairing is what tells you *which* table to go fix.
    const occ = g.items.map(x => {
      const o = x.object || x.obj || '';
      const d = oneLine(x.detail || '');
      return (o && o !== '(project)') ? (d ? o + ' — ' + d : o) : d;
    }).filter(Boolean);
    const snippets = g.items.map(x => (x.detail || '').trim()).filter(Boolean);
    return '<details class="fnd t-' + tone + '" data-i="' + g.i + '">'
      + '<summary>'
      + '<span class="fnd-sev">' + esc(label) + '</span>'
      + '<div class="fnd-head" title="' + esc(f.message || '') + '">' + esc(head) + '</div>'
      + '<span class="fnd-chev" aria-hidden="true">&#9662;</span>'
      + '<div class="fnd-meta">'
      + (n === 1 && obj && obj !== '(project)'
          ? '<span class="fnd-obj">' + esc(obj) + '</span>' : '')
      + (f.code ? '<span class="fnd-code">' + esc(f.code) + '</span>' : '')
      + (n > 1 ? '<span class="fnd-n">' + n + ' occurrences</span>' : '')
      + '</div>'
      + '</summary>'
      + '<div class="fnd-body">'
      // One occurrence -> show its snippet as the code block. Many -> the object/
      // snippet pairs become the occurrence list, which is the more useful view.
      + (n === 1 && snippets.length
          ? '<pre class="fnd-code-block">' + numberedLines(snippets[0]) + '</pre>' : '')
      + (n > 1 && occ.length
          ? '<ol class="fnd-occ">' + occ.slice(0, 50).map((s, j) =>
              '<li><span class="ix">' + (j + 1) + '/' + occ.length + '</span>'
              + '<span class="sn" title="' + esc(s) + '">' + esc(s) + '</span></li>').join('')
            + (occ.length > 50 ? '<li><span class="sn">… and ' + (occ.length - 50)
                                 + ' more</span></li>' : '')
            + '</ol>' : '')
      + (f.suggestion ? '<div class="fnd-fix"><span class="k">FIX</span><span>'
                        + esc(f.suggestion) + '</span></div>' : '')
      + '<div class="fnd-acts"><button class="copyf primary">Copy as task</button></div>'
      + '</div></details>';
  }).join('')
    || '<div class="empty"><b>Nothing in this category</b>'
       + (filter ? 'No ' + filter + ' findings in this run.' : 'This run produced no findings.') + '</div>';

  // Plain-language summary: lead with what actually blocks the conversion, then
  // the human work, then the advisory noise — severity order, not source order.
  const parts = [];
  if (counts.ERROR) parts.push('<b>' + counts.ERROR + ' blocker' + (counts.ERROR > 1 ? 's' : '')
                               + '</b> stop' + (counts.ERROR > 1 ? '' : 's') + ' the conversion');
  if (counts.MANUAL) parts.push('<b>' + counts.MANUAL + '</b> statement'
                                + (counts.MANUAL > 1 ? 's need' : ' needs') + ' a human');
  if (counts.VIOLATION) parts.push('<b>' + counts.VIOLATION + '</b> policy violation'
                                   + (counts.VIOLATION > 1 ? 's' : ''));
  if (counts.WARNING) parts.push('<b>' + counts.WARNING + '</b> warning' + (counts.WARNING > 1 ? 's' : ''));
  if (counts.INFO) parts.push('<b>' + counts.INFO + '</b> informational');
  const summary = parts.length ? parts.join(' &middot; ') : 'No findings in this run.';
  // Proportional severity bar — segment width is each severity's share of the total.
  const bar = all.length
    ? '<div class="fnd-bar" role="img" aria-label="' + esc(parts.join(', ').replace(/<\/?b>/g, '')) + '">'
      + sevs.filter(s => counts[s]).map(s =>
          '<i class="t-' + (SEV_TONE[s] || 'none') + '" style="flex:' + counts[s] + '"'
          + ' title="' + counts[s] + ' ' + s.toLowerCase() + '"></i>').join('')
      + '</div>' : '';
  const fixable = counts.MANUAL || 0;

  $('#modalBody').className = 'modal conn-drawer';
  $('#modalBody').innerHTML =
    '<div class="cd-head fnd-head-wrap">'
    + '<div class="fnd-top">'
    + '<div class="fnd-id">'
    + '<div class="fnd-eyebrow">' + esc(report.project || '') + ' &middot; '
    + esc(report.source_format || '') + ' &rarr; ' + esc(report.target_format || '') + '</div>'
    + '<h2>Findings</h2>'
    + '</div>'
    + (report._jobId ? '<button id="autofixBtn" class="fixbtn"'
       + (can('jobs:run') ? '' : ' disabled title="' + esc(permTitle('jobs:run')) + '"')
       + '><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M13 2 4.5 13.5H11l-1 8.5 8.5-11.5H12l1-8.5Z"/></svg>'
       + '<span>Auto-fix</span>'
       + (fixable ? '<span class="n">' + fixable + '</span>' : '') + '</button>' : '')
    + '</div>'
    + '<div class="fnd-sum">' + summary + '</div>'
    + bar
    + '<div class="fchips" role="group" aria-label="Filter findings by severity">' + chips + '</div>'
    + '</div>'
    + '<div class="cd-body fnd-list">' + rows + '</div>'
    + '<div class="cd-foot"><div class="cd-acts fnd-foot">'
    + '<button class="secondary" id="rescanBtn">Re-run scan</button>'
    + '<button class="secondary" id="closeModal">Close</button></div></div>';
  if (report._jobId) $('#autofixBtn').onclick = () => openAutofix(report._jobId);
  $('#modalBg').style.display = 'flex';
  $('#closeModal').onclick = () => $('#modalBg').style.display = 'none';
  $('#modalBody').querySelectorAll('.fchip').forEach(c =>
    c.onclick = () => showFindings(report, c.dataset.sev || null));
  // Index off data-i rather than NodeList position, and read the object from either
  // key — collectFindings() writes `obj` for mapping/project issues and `object` for
  // policy/validation ones, so a single lookup would paste "undefined" for half of them.
  // Re-runs the same job so the drawer reflects a fresh scan. Gated on the same
  // permission as auto-fix, since both re-execute the job.
  const rescan = $('#rescanBtn');
  if (rescan) {
    if (!report._jobId || !can('jobs:run')) {
      rescan.disabled = true;
      if (!can('jobs:run')) rescan.title = permTitle('jobs:run');
    } else {
      rescan.onclick = async () => {
        rescan.disabled = true; rescan.textContent = 'Re-running…';
        try {
          delete jobReportsCache[report._jobId];
          const fresh = await api('/api/jobs/' + report._jobId + '/report.json');
          fresh._jobId = report._jobId;
          showFindings(fresh, filter);
        } catch (e) {
          rescan.disabled = false; rescan.textContent = 'Re-run scan';
          mbAlert(e.message);
        }
      };
    }
  }
  // Each card now represents a GROUP, so copy the whole group: data-i points at
  // the group's first finding in `list`, and the occurrence snippets come from
  // the rendered list. Read the object from either key — collectFindings() writes
  // `obj` for mapping/project issues and `object` for policy/validation ones.
  $('#modalBody').querySelectorAll('.copyf').forEach(btn => btn.onclick = ev => {
    ev.stopPropagation();
    const card = btn.closest('.fnd');
    const f = list[+card.dataset.i];
    const occ = [...card.querySelectorAll('.fnd-occ .sn')].map(x => '  - ' + x.textContent);
    navigator.clipboard.writeText('[' + f.severity + '] ' + (f.object || f.obj || '') + ' — ' + f.code + '\n'
      + f.message
      + (occ.length ? '\nOccurrences:\n' + occ.join('\n') : (f.detail ? '\nDetail: ' + f.detail : ''))
      + (f.suggestion ? '\nFix: ' + f.suggestion : ''));
    btn.textContent = 'Copied ✓'; setTimeout(() => btn.textContent = 'Copy as task', 1500);
  });
}

async function openJobFindings(jobId, filter) {
  const report = await api('/api/jobs/' + jobId + '/report.json');
  report._jobId = jobId;
  showFindings(report, filter || null);
}

// Cross-job MANUAL drill-down. The Overview "Manual review items" tile aggregates
// workload.manual_queue over recent convert runs, but showFindings() is per-report,
// so this is the only view that lists every outstanding manual item together.
// `runs` is the same [{job, r}] shape loadDashboard() already assembled — reusing it
// keeps this free of extra report fetches.
function manualQueueItems(runs) {
  // Mirror reporter.py's manual_queue exactly (report/reporter.py:64):
  //   project-level MANUAL/ERROR issues + objects with status NEEDS_MANUAL_WORK/FAILED.
  // Note this counts OBJECTS, not findings — an object with three MANUAL issues is
  // one queue entry. Filtering collectFindings() by severity would disagree with the
  // headline number, and would also miss FAILED objects that carry only ERROR issues.
  const items = [];
  (runs || []).forEach(({job, r}) => {
    // Provenance travels with every row, so an item is traceable to the exact run
    // (and the exact source bytes) without opening anything else.
    const run = {id: job.id, project: job.project || r.project || job.id,
                 source: r.source_format || '', target: r.target_format || '',
                 when: (job.created || r.generated_at || '').replace('T', ' '),
                 migration_id: r.migration_id || '', snapshot: r.source_snapshot || ''};
    (r.project_issues || [])
      .filter(i => i.severity === 'MANUAL' || i.severity === 'ERROR')
      .forEach(i => items.push({job, run, object: '(project)', code: i.code, message: i.message,
                                detail: i.detail || '', suggestion: i.suggestion || '',
                                severity: i.severity}));
    (r.mappings || [])
      .filter(m => m.status === 'NEEDS_MANUAL_WORK' || m.status === 'FAILED')
      .forEach(m => {
        const blocking = (m.issues || []).filter(i => i.severity === 'MANUAL' || i.severity === 'ERROR');
        const lead = blocking[0] || {};
        items.push({job, run, object: m.name, code: lead.code || m.status,
          message: lead.message || (m.status === 'FAILED' ? 'Conversion failed.' : 'Needs manual work.'),
          detail: [lead.detail || '', blocking.length > 1
            ? '+ ' + (blocking.length - 1) + ' more blocking issue(s) on this object'
            : ''].filter(Boolean).join('\n'),
          suggestion: lead.suggestion || '', severity: m.status === 'FAILED' ? 'ERROR' : 'MANUAL'});
      });
  });
  return items;
}

// Which run produced this item, and against exactly which source bytes. migration_id
// and source_snapshot (a sha256 + file/object counts) come from the report and are what
// make a finding reproducible — quote them when filing a ticket.
function provenanceHtml(run) {
  const line = (k, v, mono) => v
    ? '<dt>' + k + '</dt><dd' + (mono ? ' class="mono"' : '') + '>' + esc(v) + '</dd>' : '';
  return '<div class="mq-prov"><div class="h">Produced by</div><dl>'
    + line('Run', run.project)
    + line('When', run.when)
    + line('Conversion', run.source && run.target ? run.source + ' → ' + run.target : '')
    + line('Job ID', run.id, true)
    + line('Migration ID', run.migration_id, true)
    + line('Snapshot', run.snapshot, true)
    + '</dl></div>';
}

function showManualQueue(runs) {
  const items = manualQueueItems(runs).map(x => ({f: x, job: x.job}));
  const blocked = items.filter(x => x.f.severity === 'ERROR').length;

  // Group by run: the run is the single most useful axis (it answers "which run
  // produced this?" structurally instead of repeating a chip on all 13 rows), and
  // it keeps the 540px row free for the message.
  const order = [], byRun = new Map();
  items.forEach((x, i) => {
    const k = x.f.run.id;
    if (!byRun.has(k)) { byRun.set(k, []); order.push(k); }
    byRun.get(k).push({x, i});
  });

  const rowHtml = ({x, i}, groupSev) => {
    const f = x.f;
    // Project-level items have no object name; the engine writes the literal
    // "(project)", which would otherwise dominate every row while saying nothing.
    // The message already names the real subject ("PROCEDURE 'refresh_sales_summary'"),
    // so promote it and drop the placeholder.
    // Project-level items carry the literal "(project)" as their object name. It would
    // head every row while saying nothing, and the message already names the real
    // subject ("PROCEDURE 'refresh_sales_summary'"), so drop it and let the message lead.
    const obj = f.object && f.object !== '(project)' ? f.object : '';
    // Severity is already stated by the group header when a group is uniform —
    // repeating an identical MANUAL chip 15x is noise, so only badge the exceptions.
    const showSev = !groupSev || f.severity !== groupSev;
    const sev = f.severity || 'MANUAL';
    const tone = SEV_TONE[sev] || 'manual';
    // Shares the .fnd card shape with the Findings drawer, so both severity lists
    // read identically: left rail + tag, clamped headline, meta line beneath.
    return '<details class="fnd t-' + tone + '" data-i="' + i + '">'
      + '<summary>'
      + (showSev ? '<span class="fnd-sev">' + esc(sev === 'ERROR' ? 'BLOCKER' : sev) + '</span>' : '')
      + '<div class="fnd-head' + (showSev ? '' : ' nosev') + '" title="' + esc(f.message || '') + '">'
      + esc(f.message || '') + '</div>'
      + '<span class="fnd-chev" aria-hidden="true">&#9662;</span>'
      + '<div class="fnd-meta' + (showSev ? '' : ' nosev') + '">'
      + (obj ? '<span class="fnd-obj">' + esc(obj) + '</span>' : '')
      + (f.code ? '<span class="fnd-code">' + esc(f.code) + '</span>' : '')
      + '</div>'
      + '</summary>'
      + '<div class="fnd-body">'
      + (f.detail ? '<pre class="fnd-code-block">' + numberedLines(f.detail) + '</pre>' : '')
      + (f.suggestion ? '<div class="fnd-fix"><span class="k">FIX</span><span>'
                        + esc(f.suggestion) + '</span></div>' : '')
      + provenanceHtml(f.run)
      + '<div class="fnd-acts">'
      + '<button class="copyf primary">Copy as task</button>'
      + '<button class="secondary mqj" data-id="' + esc(f.run.id) + '">Run findings</button>'
      + '<button class="secondary mqd" data-id="' + esc(f.run.id) + '">Artifacts</button>'
      + '</div></div></details>';
  };

  const body = order.map(k => {
    const g = byRun.get(k), run = g[0].x.f.run;
    // Uniform severity is stated once on the header; mixed groups badge each row.
    const sevs = [...new Set(g.map(e => e.x.f.severity))];
    const groupSev = sevs.length === 1 ? sevs[0] : null;
    return '<div class="mq-grp"><span>' + esc(run.project)
      + (run.source && run.target ? ' &middot; ' + esc(run.source) + ' &rarr; ' + esc(run.target) : '')
      + '</span>'
      + '<span class="mq-n' + (groupSev ? ' t-' + (SEV_TONE[groupSev] || 'manual') : '') + '">'
      + (groupSev ? esc(groupSev === 'ERROR' ? 'BLOCKER' : groupSev) + ' &middot; ' : '')
      + g.length + '</span>'
      + '</div>'
      + g.map(e => rowHtml(e, groupSev)).join('');
  }).join('')
    || '<div class="empty"><b>Nothing needs a human</b>Every asset in the recent runs converted automatically.</div>';

  // The three stat tiles used to live here; the drawer header now states the same
  // three numbers in one sentence, so repeating them as tiles was pure duplication.
  const summary = '';

  $('#modalBody').className = 'modal conn-drawer';
  $('#modalBody').innerHTML =
    '<div class="cd-head fnd-head-wrap">'
    + '<div class="fnd-top"><div class="fnd-id">'
    + '<div class="fnd-eyebrow">Across ' + (runs || []).length + ' recent run'
    + ((runs || []).length === 1 ? '' : 's') + '</div>'
    + '<h2>Manual review</h2>'
    + '</div></div>'
    + '<div class="fnd-sum"><b>' + items.length + '</b> item'
    + (items.length === 1 ? '' : 's') + ' need' + (items.length === 1 ? 's' : '') + ' a human'
    + (blocked ? ' &middot; <b>' + blocked + '</b> blocked' : '')
    + ' &middot; <b>' + order.length + '</b> run' + (order.length === 1 ? '' : 's') + '</div>'
    + '</div>'
    + '<div class="cd-body fnd-list">' + summary + body
    + (items.length ? '<div class="cd-note" style="margin:14px 0 0">Every item also ships as a code '
        + 'skeleton in <b>manual_workbook/</b>, with <b>manual_queue.csv</b> for Jira/Excel import, '
        + 'inside each run&rsquo;s download.</div>' : '')
    + '</div>'
    + '<div class="cd-foot"><div class="cd-acts">'
    + '<button class="secondary" id="closeModal">Close</button></div></div>';
  $('#modalBg').style.display = 'flex';
  $('#closeModal').onclick = () => $('#modalBg').style.display = 'none';
  // Both jump into existing per-run views; these replace the drawer contents, so the
  // user lands on the run that produced the item.
  $('#modalBody').querySelectorAll('.mqj').forEach(a => a.onclick = ev => {
    ev.stopPropagation();
    openJobFindings(a.dataset.id).catch(e => mbAlert(e.message));
  });
  $('#modalBody').querySelectorAll('.mqd').forEach(a => a.onclick = ev => {
    ev.stopPropagation();
    openJobDetail(a.dataset.id).catch(e => mbAlert(e.message));
  });
  // Index off data-i, not DOM order — rows are emitted grouped by run, so position
  // in the NodeList no longer tracks position in `items`.
  $('#modalBody').querySelectorAll('.copyf').forEach(btn => btn.onclick = ev => {
    ev.stopPropagation();
    const f = items[+btn.closest('.fnd').dataset.i].f, run = f.run;
    // Include provenance — a pasted ticket is useless if nobody can tell which run,
    // or which version of the source, produced the finding.
    navigator.clipboard.writeText('[' + f.severity + '] ' + (f.object || '') + ' — ' + f.code + '\n'
      + f.message + (f.detail ? '\nDetail: ' + f.detail : '') + (f.suggestion ? '\nFix: ' + f.suggestion : '')
      + '\n\nProduced by run: ' + run.project + (run.when ? ' (' + run.when + ')' : '')
      + (run.source && run.target ? '\nConversion: ' + run.source + ' → ' + run.target : '')
      + '\nJob ID: ' + run.id
      + (run.migration_id ? '\nMigration ID: ' + run.migration_id : '')
      + (run.snapshot ? '\nSource snapshot: ' + run.snapshot : ''));
    btn.textContent = 'Copied ✓'; setTimeout(() => btn.textContent = 'Copy as task', 1500);
  });
}

function fmtBytes(n) {
  n = +n || 0;
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(1) + ' MB';
}

// Job-detail view — an overlay, so the underlying reports/jobs list (and its
// pagination/filter) is preserved. Shows status, metadata, generated
// artifacts (each a job-scoped download link), report links and actions.
async function openJobDetail(jobId) {
  /* The overlay had no route at all: no URL represented it, so a job could not
     be linked, reloaded or restored. `#dashboard/job` is the Overview TAB
     route (dashView), so the job id becomes a third segment under it. */
  setHash('dashboard/job/' + encodeURIComponent(jobId));
  gOpenJobId = jobId;
  $('#modalBody').innerHTML = '<div style="color:var(--ink3);font-size:13px">Loading job…</div>';
  $('#modalBody').className = 'modal'; $('#modalBg').style.display = 'flex';
  let j;
  try { j = await api('/api/jobs/' + jobId); }
  catch (e) {
    $('#modalBody').innerHTML = '<h3 style="margin:0 0 8px">Job</h3>'
      + '<div class="err" style="display:block">' + esc(e.message) + '</div>'
      + '<button class="secondary" style="margin-top:14px" onclick="closeModal()">Close</button>';
    return;
  }
  const st = runStatusChip(j);
  const rows = [];
  const add = (k, v, mono) => { if (v !== undefined && v !== '' && v != null) rows.push(
    '<dt>' + esc(k) + '</dt><dd' + (mono ? ' class="mono"' : '') + '>' + v + '</dd>'); };
  add('Job ID', esc(j.id), true);
  add('Created', esc((j.created || '').replace('T', ' ')), true);
  add('Finished', esc((j.finished || '').replace('T', ' ')), true);
  const s = j.summary || {};
  if (s.objects_total != null) add('Objects', s.objects_total);
  if (j.autofix) add('Auto-fix', badge('&#9889; fixed', 'var(--accent)'));
  if (j.error) add('Error', '<span style="color:var(--red)">' + esc(j.error) + '</span>');
  const reports = (j.reports || []).length
    ? (j.reports || []).map(r => '<a class="jd-rep" href="' + withKey(r.url) + '" target="_blank">'
        + esc(r.label) + '</a>').join('')
    : '<div class="jd-none">No report artifacts were generated for this job.</div>';
  // Long artifact paths are the reason this drawer scrolled sideways: the file name
  // is the identifying part, so it stays legible and the directory prefix truncates.
  const arts = (j.artifacts || []).length
    ? (j.artifacts || []).map(a => {
        const p = String(a.path || ''), cut = p.lastIndexOf('/');
        return '<a class="jd-art" href="' + withKey(a.url) + '" title="' + esc(p) + '">'
          + '<span class="p">'
          + (cut > -1 ? '<span class="dir">' + esc(p.slice(0, cut + 1)) + '</span>' : '')
          + '<span class="f">' + esc(cut > -1 ? p.slice(cut + 1) : p) + '</span></span>'
          + '<span class="sz">' + fmtBytes(a.size) + '</span></a>';
      }).join('')
    : '<div class="jd-none">No artifacts generated.</div>';
  let actions = '<a class="linkbtn" href="' + withKey('/api/jobs/' + j.id + '/download')
    + '">Download all (.zip)</a>';
  if (j.report_url) actions += '<a class="linkbtn" href="' + withKey(j.report_url)
    + '" target="_blank">View report</a>';
  // Same drawer skeleton as Findings / Manual review: pinned header, scrolling
  // body, pinned footer. Previously a plain .modal with inline styles, which is
  // why the artifact list scrolled the whole panel and pushed Close out of reach.
  $('#modalBody').className = 'modal conn-drawer';
  $('#modalBody').innerHTML =
    '<div class="cd-head fnd-head-wrap">'
    + '<div class="fnd-top"><div class="fnd-id">'
    + '<div class="fnd-eyebrow">' + esc(reportKindLabel(j.kind))
    + (j.source_format && j.target_format
        ? ' &middot; ' + esc(j.source_format) + ' &rarr; ' + esc(j.target_format) : '')
    + '</div>'
    + '<h2>' + esc(jobLabel(j)) + '</h2>'
    + '</div>' + st + '</div>'
    + '</div>'
    + '<div class="cd-body fnd-list">'
    + '<dl class="jd-meta">' + rows.join('') + '</dl>'
    + '<div class="jd-sec">Reports</div>'
    + '<div class="jd-reports">' + reports + '</div>'
    + '<div class="jd-sec">Generated artifacts'
    + ((j.artifacts || []).length ? '<span class="c">' + j.artifacts.length + '</span>' : '')
    + '</div>'
    + '<div class="jd-arts">' + arts + '</div>'
    + '</div>'
    + '<div class="cd-foot"><div class="cd-acts">'
    + '<div class="jd-actions">' + actions
    + (j.kind === 'convert' ? '<a class="jfd linkbtn">Findings</a>' : '')
    + '</div>'
    + '<button class="secondary" id="closeModal">Close</button></div></div>';
  $('#closeModal').onclick = () => closeModal();
  const jf = $('.jfd');
  if (jf) jf.onclick = () => openJobFindings(j.id).catch(e => mbAlert(e.message));
}

/* ---------------- auto-fix: approve & apply ---------------- */
// Shell for the auto-fix drawer's simple states (loading, error, result), so
// every state gets the same head/body/foot structure instead of only the main
// one. `foot` is optional — the loading state has no action yet.
function afShell(title, sub, bodyHtml, footHtml) {
  $('#modalBody').className = 'modal conn-drawer af-drawer';
  $('#modalBody').innerHTML =
    '<div class="cd-head"><div class="cd-title">'
    + '<span class="af-ic" aria-hidden="true">&#9889;</span>'
    + '<div><h2>' + title + '</h2>'
    + (sub ? '<div class="v">' + sub + '</div>' : '') + '</div></div></div>'
    + '<div class="cd-body">' + bodyHtml + '</div>'
    + (footHtml ? '<div class="cd-foot"><div class="cd-acts">' + footHtml + '</div></div>' : '');
}

async function openAutofix(jobId) {
  afShell('Auto-fix', '', '<div class="af-load">Scanning the manual queue for automatic fixes…</div>');
  $('#modalBg').style.display = 'flex';
  let plan, meta;
  try {
    [plan, meta] = await Promise.all([api('/api/jobs/' + jobId + '/autofix'),
                                      api('/api/jobs/' + jobId)]);
  }
  catch (e) {
    afShell('Auto-fix', '', '<div class="err" style="display:block">' + esc(e.message) + '</div>',
            '<button class="secondary" id="closeModal2">Close</button>');
    $('#closeModal2').onclick = () => $('#modalBg').style.display = 'none';
    return;
  }

  const anyItems = ['key_fix','llm_expression','llm_statement']
    .some(k => { const g = plan.groups[k]; return (g.total != null ? g.total : g.items.length) > 0; });
  if (!anyItems) {
    const wl = (meta.summary || {}).workload || {};
    const remaining = wl.manual_queue || 0;
    afShell('Auto-fix', remaining ? remaining + ' item(s) still need a human' : 'Manual queue is empty',
      (meta.autofix ? autofixBannerHtml(meta.autofix)
          + '<div class="af-note ok"><b>All suggested fixes have been applied</b>'
          + '<span>The output and report below are the updated versions.</span></div>'
        : '<div class="af-note"><b>Nothing here is auto-fixable</b>'
          + '<span>No group in this queue can be resolved automatically.</span></div>')
      + (remaining
          ? '<div class="af-note"><b>' + remaining + ' item(s) need human conversion</b>'
            + '<span>Each has a generated skeleton in <code>manual_workbook/</code> inside the '
            + 'download, and <code>manual_queue.csv</code> tracks them for your team.</span></div>'
          : '<div class="af-note ok"><b>The manual queue is empty</b>'
            + '<span>Nothing left to do.</span></div>')
      + '<div class="links" style="margin-top:14px">'
      + '<a href="' + withKey('/api/jobs/' + jobId + '/report') + '" target="_blank">Open updated report</a>'
      + '<a href="' + withKey('/api/jobs/' + jobId + '/download') + '">Download output</a></div>',
      '<button class="secondary" id="closeModal2">Close</button>');
    $('#closeModal2').onclick = () => $('#modalBg').style.display = 'none';
    return;
  }
  const order = ['key_fix', 'llm_expression', 'llm_statement'];
  const rows = order.map(key => {
    const g = plan.groups[key];
    const n = g.total != null ? g.total : g.items.length;
    if (!n) return '';
    const needsAi = g.mechanism === 'llm';
    const blocked = needsAi && !plan.llm_available;
    let detail = '';
    if (key === 'key_fix') detail = g.items.map(i => i.proposed_key
      ? '<span class="af-item"><code>' + esc(i.model) + '</code> &rarr; unique key <b>'
        + esc((i.keys || [i.proposed_key]).join(', ')) + '</b></span>'
      : '<span class="af-item warn"><code>' + esc(i.model)
        + '</code> &rarr; no key detected (stays manual)</span>').join('');
    else if (needsAi) detail = '<span class="af-item">Drafted by Claude · every output flagged for review</span>';
    // A blocked group is not merely faded — it says why, and offers the fix.
    return '<div class="af-card' + (blocked ? ' off' : '') + '">'
      + '<label class="af-row">'
      + '<input type="checkbox" class="fixGroup" value="' + key + '"'
      + (blocked ? ' disabled' : (g.ready ? ' checked' : ' disabled')) + '>'
      + '<span class="af-main">'
      + '<span class="af-title">' + esc(g.label)
      + '<span class="af-count">' + n + '</span></span>'
      + (blocked ? '<span class="af-warn">Needs an AI provider — '
          + '<a class="gotoSettings">configure in Settings &rarr;</a>'
          + ' <span class="af-warn-sub">(Anthropic key or Amazon Bedrock via your AWS IAM role)</span></span>' : '')
      + '<span class="af-detail">' + detail + '</span>'
      + '</span></label></div>';
  }).join('');
  const anyFixable = order.some(k => { const g = plan.groups[k]; return g.ready; });
  // Same head / scrolling body / pinned footer shell as the connector and
  // job-detail drawers. Previously this reused the bare `.modal` class while
  // inheriting `conn-drawer` from whichever drawer opened it, so it got
  // padding:0 and no scroll container — long content ran off the viewport.
  const total = order.reduce((n, k) => {
    const g = plan.groups[k]; return n + (g.total != null ? g.total : g.items.length);
  }, 0);
  $('#modalBody').className = 'modal conn-drawer af-drawer';
  $('#modalBody').innerHTML =
    '<div class="cd-head">'
    + '<div class="cd-title"><span class="af-ic" aria-hidden="true">&#9889;</span>'
    + '<div><h2>Auto-fix</h2>'
    + '<div class="v">' + total + ' item(s) across ' + order.filter(k => {
        const g = plan.groups[k]; return (g.total != null ? g.total : g.items.length) > 0;
      }).length + ' group(s)</div></div></div>'
    + '<div class="af-lede">Nothing changes until you apply. The project is re-converted with the '
    + 'approved fixes; all AI output is flagged for review.</div>'
    + '</div>'
    + '<div class="cd-body">'
    + (rows || '<div class="empty"><b>Nothing to approve</b>No automatically fixable items in this queue.</div>')
    + '<div class="err" id="fixErr"></div>'
    + '</div>'
    + '<div class="cd-foot"><div class="cd-acts">'
    + (anyFixable ? '<button id="applyFix" class="wide2">Apply approved fixes &amp; re-convert</button>' : '')
    + '<button class="secondary" id="closeModal2">Close</button>'
    + '</div></div>';
  $('#closeModal2').onclick = () => $('#modalBg').style.display = 'none';
  document.querySelectorAll('.gotoSettings').forEach(a => a.onclick = () => {
    $('#modalBg').style.display = 'none';
    document.querySelector('nav a[data-page="settings"]').click();
    showSettings('ai', true);
  });
  const applyBtn = $('#applyFix');
  if (applyBtn) applyBtn.onclick = async () => {
    const groups = [...document.querySelectorAll('.fixGroup:checked')].map(c => c.value);
    if (!groups.length) { const e2 = $('#fixErr'); e2.textContent = 'Approve at least one fix group.'; e2.style.display = 'block'; return; }
    applyBtn.disabled = true; applyBtn.textContent = 'Applying & re-converting…';
    try {
      const d = await api('/api/jobs/' + jobId + '/autofix',
        {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({groups})});
      const af = d.autofix || {};
      const skipped = (af.skipped || []).map(s =>
        '<span class="af-item warn">' + esc(s.reason) + '</span>').join('');
      if (af.changed === false) {
        // A genuine no-op — never claim success. Explain why and offer retry.
        afShell('No changes applied', 'The job is unchanged',
          '<div class="af-note"><b>The approved option(s) produced no change</b>'
          + '<span>Nothing in this job matched what those fixes act on.</span></div>'
          + (skipped ? '<div class="af-detail" style="margin-top:10px">' + skipped + '</div>' : ''),
          '<button id="retryFix" class="wide2">Back to options</button>'
          + '<button class="secondary" id="closeModal3">Close</button>');
        $('#retryFix').onclick = () => openAutofix(jobId);
        $('#closeModal3').onclick = () => $('#modalBg').style.display = 'none';
        return;
      }
      const applied = [
        ...(af.key_overrides || []).map(k => 'Merge key set: <code>' + esc(k) + '</code>'),
        af.expressions_resolved ? af.expressions_resolved
          + ' expression(s) translated by Claude (flagged for review)' : '',
        af.drafts_written ? af.drafts_written
          + ' Claude draft(s) written to <code>manual_drafts/</code> (review required)' : '',
      ].filter(Boolean);
      afShell('Fixes applied', applied.length + ' change(s) written',
        '<div class="af-note ok"><b>Re-converted with your approved fixes</b>'
        + '<span>The report and download below are the updated versions.</span></div>'
        + '<div class="af-detail">' + applied.map(t =>
            '<span class="af-item ok">' + t + '</span>').join('') + '</div>'
        + (skipped ? '<div class="af-sec">Not applied</div>'
            + '<div class="af-detail">' + skipped + '</div>' : '')
        + '<div class="links" style="margin-top:14px">'
        + '<a href="' + withKey(d.report_url) + '" target="_blank">Open updated report</a>'
        + '<a href="' + withKey(d.download_url) + '">Download updated output</a></div>',
        '<button class="secondary" id="closeModal3">Close</button>');
      $('#closeModal3').onclick = () => $('#modalBg').style.display = 'none';
      showConvertResult({...d, validation: d.validation});
    } catch (e) {
      const e2 = $('#fixErr'); e2.textContent = e.message; e2.style.display = 'block';
      applyBtn.disabled = false; applyBtn.textContent = 'Apply approved fixes & re-convert';
    }
  };
}

/* Display label for a job row. Jobs recorded before every kind set a project
   have no name at all, and stored history is deliberately NOT rewritten — the
   meta files record what actually happened. So derive something identifying at
   render time: "twin · a3f9c1" beats a bare "—", which made every such row
   look identical and unclickable-by-eye. */
function jobLabel(j) {
  const p = (j.project || '').trim();
  if (p && p.toLowerCase() !== 'input') return p;
  const kind = (j.kind || 'job').replace(/_/g, ' ');
  return kind + ' · ' + String(j.id || '').slice(0, 6);
}

/* ---------------- dashboard ---------------- */
const STATUS = {
  DISCOVERED:{c:'var(--ink3)',l:'Discovered'}, ANALYZING:{c:'var(--amber)',l:'Analyzing'},
  ANALYZED:{c:'var(--accent)',l:'Analyzed'}, READY:{c:'var(--accent)',l:'Ready'},
  QUEUED:{c:'var(--ink3)',l:'Queued'},
  MODERNIZING:{c:'var(--amber)',l:'Running'},
  // Neutral, NOT green. GENERATED is what verdictToStatus() falls back to when a
  // run carries no validation verdict — "converted but never validated". Green
  // made it read identically to PASSED, while the Validation pass rate card
  // excludes these runs from its maths entirely: a run could look successful here
  // and be invisible to the number that judges success.
  GENERATED:{c:'var(--ink3)',l:'Generated'},
  VALIDATING:{c:'var(--amber)',l:'Validating'}, PASSED:{c:'var(--green)',l:'Passed'},
  WARNING:{c:'var(--amber)',l:'Warning'}, MANUAL_REVIEW:{c:'#b03a2e',l:'Manual review'},
  FAILED:{c:'var(--red)',l:'Failed'}, DEPLOYED:{c:'var(--green)',l:'Deployed'},
};
function statChip(k) {
  const s = STATUS[k] || {c:'var(--ink3)', l:k};
  return '<span class="stat"><i style="background:' + s.c + '"></i>' + s.l + '</span>';
}
function verdictToStatus(v) {
  return {PASS:'PASSED', PASS_WITH_WARNINGS:'WARNING', MANUAL_REVIEW:'MANUAL_REVIEW',
          FAIL:'FAILED'}[v] || 'GENERATED';
}
// Kinds that PRODUCE something a validation pass could later judge. For these,
// finishing is not success — it is "generated, not yet checked". Every other
// kind reads metadata and writes documents; there is nothing to validate, so
// finishing IS the terminal state.
const RUN_KIND_GENERATES = {convert:1, govern:1, scaffold:1, events:1,
                            orchestration:1, objects_convert:1};
// The ONE place that decides what state a run is in.
//
// Execution state (queued/running/failed) is plumbing and is shown only while a
// run is unfinished. Once it finishes, only the migration lifecycle is shown —
// so a converted-but-never-validated run reads GENERATED, in neutral grey.
// Three call sites used to answer this question independently and disagree:
// job detail and the Overview row both rendered a green `done` badge, while the
// Reports table rendered a neutral `Generated` chip for the very same run. Green
// is the strongest signal in the UI and it was being spent on runs nobody had
// checked — the exact misread the STATUS comment above was written to prevent.
function runStatus(j) {
  j = j || {};
  const st = String(j.status || '').toLowerCase();
  if (st === 'failed') return 'FAILED';
  if (st === 'queued') return 'QUEUED';
  if (st && st !== 'done') return 'MODERNIZING';
  const verdict = (j.migration_validation || {}).verdict;
  if (verdict) return verdictToStatus(verdict);
  return RUN_KIND_GENERATES[j.kind] ? 'GENERATED' : 'PASSED';
}
// A settled outcome is a solid dot; work still outstanding is a hollow ring.
// The label carries the meaning, the dot carries the state — no icons, no
// capsules, so a status can never be mistaken for a button.
const RUN_STATUS_PENDING = {QUEUED:1, MODERNIZING:1, VALIDATING:1, ANALYZING:1};
function runStatusChip(j) {
  const k = runStatus(j);
  const s = STATUS[k] || {c:'var(--ink3)', l:k};
  const dot = RUN_STATUS_PENDING[k]
    ? 'background:transparent;border:2px solid ' + s.c + ';border-radius:50%'
    : 'background:' + s.c;
  return '<span class="stat" title="' + esc(RUN_STATUS_HINT[k] || s.l) + '">'
    + '<i style="' + dot + '"></i>' + s.l + '</span>';
}
const RUN_STATUS_HINT = {
  GENERATED: 'Converted, but no validation has run against the output yet',
  PASSED: 'Validated with no blocking issues',
  WARNING: 'Validated, with non-blocking issues',
  MANUAL_REVIEW: 'Needs a human before this can be trusted',
  FAILED: 'Did not complete — the error is on the run',
  DEPLOYED: 'Applied to the target',
  QUEUED: 'Waiting for a worker',
  MODERNIZING: 'Running now',
  VALIDATING: 'Validation checks in flight',
};
let jobReportsCache = {};
async function jobReport(id) {
  if (!jobReportsCache[id]) {
    try { jobReportsCache[id] = await api('/api/jobs/' + id + '/report.json'); }
    catch (e) { jobReportsCache[id] = null; }
  }
  return jobReportsCache[id];
}
/* Warm the cache for many jobs in ONE request. The Overview summarises N recent
   conversions, which used to mean N separate /report.json calls on every load
   (~20 on a populated workspace) just to paint six tiles. Callers still go
   through jobReport() afterwards, so a batch failure degrades to the old
   per-job path rather than breaking the page. */
async function jobReportsWarm(ids) {
  const want = [...new Set(ids.filter(id => !(id in jobReportsCache)))];
  if (!want.length) return;
  for (let i = 0; i < want.length; i += 200) {
    const slice = want.slice(i, i + 200);
    try {
      const d = await api('/api/jobs/reports', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ids: slice})});
      const reps = d.reports || {};
      slice.forEach(id => { jobReportsCache[id] = reps[id] || null; });
    } catch (e) { return; }        // leave them uncached; jobReport() will fetch
  }
}
// `click` turns the tile into a drill-down: it carries data-drill (the caller binds
// the handler) and is keyboard-reachable, since these tiles are plain divs, not buttons.
/* A metric's VALUE and whether we have a BASIS for it are two different facts.
   `n || '--'` conflated them: 0 is falsy in JS, so "0 connected systems" —
   a true, actionable answer — rendered as "--", which reads as "unknown". The
   only case that deserves "--" is having no data to answer from, so say that
   explicitly via `known`. Callers that always have the data omit it. */
function mnum(n, known) {
  if (known === false) return '--';
  return (n === 0 || n) ? n : '--';
}
function mcard(n, l, d, click) {
  return '<div class="mcard"' + (click ? ' data-drill="' + click + '" role="button" tabindex="0"'
      + ' style="cursor:pointer" title="View items"' : '') + '>'
    + '<div class="l">' + l + (click ? ' &darr;' : '') + '</div><div class="n">' + n + '</div>'
    + (d ? '<div class="d">' + d + '</div>' : '') + '</div>';
}

/* ---- Overview summary window ----------------------------------------
   How many of the most recent COMPLETED modernizations are opened and
   summarized on the Overview. Only the report-derived figures depend on
   it (assets analyzed, validation pass rate, manual review items, and
   the Active-modernizations table) — the whole-estate counts
   (connected systems, modernizations, models scaffolded) always cover
   everything. 0 means "all". Persisted so the choice survives page
   navigation and reloads. */
const DASH_WINDOW_STEPS = [8, 10, 25, 50];
let dashWindow = (() => {
  const v = parseInt(localStorage.getItem('mb_dash_window'), 10);
  return Number.isFinite(v) && v >= 0 ? v : 8;
})();

/* Offer only the steps that would actually narrow the list, plus "all" —
   "Last 50" against 34 runs is a no-op the user shouldn't have to reason
   about. A persisted window that no longer narrows collapses to "all",
   so the label never over-promises the data behind it. */
function renderDashWindow(total) {
  const wrap = $('#dashWindowWrap'), sel = $('#dashWindow');
  const steps = DASH_WINDOW_STEPS.filter(n => n < total);
  // The preference itself is left alone — one that no longer narrows
  // anything just READS as "all", so it comes back if the estate grows.
  const eff = steps.indexOf(dashWindow) === -1 ? 0 : dashWindow;
  if (!sel) return eff;
  // Record whether this control is useful at all, then let applyDashView()
  // decide visibility — it must stay hidden while the other view is showing.
  if (wrap) {
    wrap.dataset.useful = steps.length ? '1' : '0';
    wrap.style.display = (steps.length && dashView === 'mod') ? 'inline-flex' : 'none';
  }
  // Summarize is a scope control, not a filter — but "All N runs" is its
  // neutral position, so treat a narrowed window as active for consistency.
  if (sel) sel.classList.toggle('act', !!(steps.length && dashWindow));
  sel.innerHTML = steps.map(n => '<option value="' + n + '">Last ' + n + ' runs</option>').join('')
    + '<option value="0">All ' + total + ' runs</option>';
  sel.value = String(eff);
  sel.onchange = () => {
    dashWindow = parseInt(sel.value, 10) || 0;
    localStorage.setItem('mb_dash_window', String(dashWindow));
    pagerState('modTable').page = 1;   // a shrunken window must not strand the table mid-pager
    sel.disabled = true;
    loadDashboard().finally(() => { sel.disabled = false; });
  };
  return eff;
}

/* Fetch with a concurrency ceiling — "All runs" can mean 100 report
   fetches, and firing them at once would stall the server that is also
   serving the console. Results stay index-aligned with `items`. */
async function mapLimit(items, limit, fn) {
  const out = new Array(items.length);
  let next = 0;
  await Promise.all(Array.from({length: Math.min(limit, items.length)}, async () => {
    while (next < items.length) { const i = next++; out[i] = await fn(items[i], i); }
  }));
  return out;
}

// Generic client-side table pager — same shape as the hand-rolled Data
// Estate pager (estatePage/estatePageSize/#estatePager), pulled out so
// every growing list (jobs, reports, governance queue, secrets, members)
// gets the same prev/next + "showing X-Y of N" treatment without
// reimplementing page-state math per table. `id` scopes state per table;
// `noun` is the plural label used in the "of N <noun>" summary.
const _pagerState = {};
function pagerState(id, pageSize) {
  if (!_pagerState[id]) _pagerState[id] = {page: 1, pageSize};
  return _pagerState[id];
}
function renderPage(id, allRows, headerHtml, emptyHtml, noun, onRender, pageSize) {
  const st = pagerState(id, pageSize || 10);
  const total = allRows.length;
  const pages = Math.max(1, Math.ceil(total / st.pageSize));
  if (st.page > pages) st.page = pages;
  if (st.page < 1) st.page = 1;
  const start = (st.page - 1) * st.pageSize;
  const pageRows = allRows.slice(start, start + st.pageSize);
  $('#' + id).innerHTML = headerHtml + (pageRows.join('') || emptyHtml);
  const pager = $('#' + id + 'Pager');
  if (pager) {
    pager.style.display = total > st.pageSize ? 'flex' : 'none';
    if (total) {
      const from = start + 1, to = Math.min(start + st.pageSize, total);
      $('#' + id + 'PageInfo').textContent = 'Showing ' + from + '–' + to
        + ' of ' + total.toLocaleString() + ' ' + (total === 1 ? noun.replace(/s$/, '') : noun)
        + ' · page ' + st.page + ' of ' + pages;
      $('#' + id + 'Prev').disabled = st.page <= 1;
      $('#' + id + 'Next').disabled = st.page >= pages;
    }
  }
  if (onRender) onRender();
}
function bindPager(id, rerender) {
  const st = pagerState(id);
  const prev = $('#' + id + 'Prev'), next = $('#' + id + 'Next');
  const navTop = () => {
    if (document.activeElement) document.activeElement.blur();
    const wa = document.querySelector('.workarea');
    if (wa) wa.scrollTop = 0;
  };
  if (prev) prev.onclick = () => { st.page--; navTop(); rerender(); navTop(); };
  if (next) next.onclick = () => { st.page++; navTop(); rerender(); navTop(); };
}
function pagerHtml(id) {
  return '<div id="' + id + 'Pager" style="display:none;align-items:center;gap:12px;margin-top:12px;flex-wrap:wrap">'
    + '<button type="button" class="secondary" id="' + id + 'Prev" style="margin:0;padding:5px 12px">&larr; Previous</button>'
    + '<span id="' + id + 'PageInfo" style="font-size:12.5px;color:var(--ink3)" role="status" aria-live="polite"></span>'
    + '<button type="button" class="secondary" id="' + id + 'Next" style="margin:0;padding:5px 12px">Next &rarr;</button>'
    + '</div>';
}

/* ---- Overview table view -------------------------------------------
   'mod' = completed modernizations (conversion metrics), 'job' = all
   activity (every kind & status). One panel shows one of them, so the
   page has a single table region instead of two stacked ones. Each view
   carries its own filter control; the other is hidden rather than
   disabled, since a control that cannot apply to what you are looking at
   is noise. Lives in the hash (#dashboard/mod), not localStorage — a shared
   link now shows the sender's view rather than whatever the recipient's own
   browser last remembered. */
let dashView = 'job';

function applyDashView() {
  const isMod = dashView === 'mod';
  const set = (id, on) => { const el = $('#' + id); if (el) el.style.display = on ? '' : 'none'; };
  set('dashViewMod', isMod);
  set('dashViewJob', !isMod);
  // Filters travel with their table.
  const jw = $('#jobFilterWrap');
  if (jw) jw.classList.toggle('on', !isMod);
  const mw = $('#modFilterWrap');
  if (mw) mw.classList.toggle('on', isMod);
  // The Summarize control is additionally gated by renderDashWindow(), which
  // hides it when no step would actually narrow the list — so only ever turn
  // it ON for the view that owns it, and let that function have the last word.
  const ww = $('#dashWindowWrap');
  if (ww) ww.style.display = (isMod && ww.dataset.useful === '1') ? 'inline-flex' : 'none';
  document.querySelectorAll('#dashViewSeg button').forEach(b =>
    b.setAttribute('aria-selected', String(b.dataset.view === dashView)));
  updateFilterReset();
}

// A select showing anything other than "All …" gets the active treatment, so a
// filtered table is obvious at a glance rather than only from the row count.
function markActiveFilters() {
  [['#jobKind', jobKindFilter], ['#jobStatus', jobStatusFilter],
   ['#modSource', modSourceFilter], ['#modTarget', modTargetFilter]]
    .forEach(([sel, val]) => {
      const el = $(sel);
      if (el) el.classList.toggle('act', !!val);
    });
}

/* Reset is offered only when something is actually filtered, and only clears
   the ACTIVE view's filters — clearing a filter you cannot see would be a
   change with no visible cause. */
function updateFilterReset() {
  const btn = $('#dashFilterReset');
  if (!btn) return;
  const active = dashView === 'mod'
    ? (modSourceFilter || modTargetFilter)
    : (jobStatusFilter || jobKindFilter);
  btn.classList.toggle('on', !!active);
  markActiveFilters();
}

function bindFilterReset() {
  const btn = $('#dashFilterReset');
  if (!btn) return;
  btn.onclick = () => {
    if (dashView === 'mod') {
      modSourceFilter = ''; modTargetFilter = '';
      localStorage.setItem('mb_mod_source', '');
      localStorage.setItem('mb_mod_target', '');
      pagerState('modTable').page = 1;
    } else {
      jobStatusFilter = ''; jobKindFilter = '';
      localStorage.setItem('mb_job_status', '');
      localStorage.setItem('mb_job_kind', '');
      pagerState('jobTable').page = 1;
    }
    loadDashboard();
  };
}

/* Build a "<All> (n) / value (n)" filter select from the rows themselves, so a
   value the backend starts emitting appears with no UI change. Returns the
   effective value: a persisted choice with nothing behind it right now falls
   back to "all" rather than showing an empty table with no explanation. */
/* labelFn maps a stored value to its display name (see jobKindLabel). Options
   are sorted by the LABEL when one is supplied, so the list reads
   alphabetically as shown rather than by the underlying slug. */
function buildFilterSelect(sel, rows, keyFn, current, allLabel, onPick, labelFn) {
  if (!sel) return current;
  const counts = {};
  rows.forEach(r => { const v = keyFn(r); if (v) counts[v] = (counts[v] || 0) + 1; });
  let eff = current && counts[current] ? current : '';
  const lbl = labelFn || (v => v);
  sel.innerHTML = '<option value="">' + allLabel + ' (' + rows.length + ')</option>'
    + Object.keys(counts).sort((a, b) => String(lbl(a)).localeCompare(String(lbl(b)))).map(v =>
        '<option value="' + esc(v) + '">' + esc(lbl(v)) + ' (' + counts[v] + ')</option>').join('');
  sel.value = eff;
  sel.onchange = () => onPick(sel.value);
  return eff;
}

function bindDashViewToggle() {
  document.querySelectorAll('#dashViewSeg button').forEach(b => {
    b.onclick = () => {
      if (dashView === b.dataset.view) return;
      dashView = b.dataset.view;
      setHash('dashboard/' + dashView);
      applyDashView();
    };
  });
}

/* ---- Recent activity status filter -----------------------------------
   '' = every status. Options are derived from the jobs actually present
   (with counts) rather than a hardcoded list, so a status the backend
   starts emitting shows up here without a UI change. Persisted like the
   summary window. */
let jobStatusFilter = localStorage.getItem('mb_job_status') || '';
let jobKindFilter = localStorage.getItem('mb_job_kind') || '';
let modSourceFilter = localStorage.getItem('mb_mod_source') || '';
let modTargetFilter = localStorage.getItem('mb_mod_target') || '';

// Each dropdown counts against the rows the OTHER filter already allows, so the
// numbers describe what you would actually get — picking "convert (17)" then
// seeing 3 rows because a status filter was also on would be a lie.
function renderJobFilters(jobs, rerender) {
  const pick = (setter, key) => v => {
    setter(v);
    localStorage.setItem(key, v);
    pagerState('jobTable').page = 1;  // page 8 of "done" is not page 8 of "failed"
    renderJobFilters(jobs, rerender);
    rerender();
    updateFilterReset();
  };
  const byKind = jobKindFilter
    ? jobs.filter(j => (j.kind || '') === jobKindFilter) : jobs;
  const byStatus = jobStatusFilter
    ? jobs.filter(j => (j.status || 'unknown') === jobStatusFilter) : jobs;
  jobKindFilter = buildFilterSelect($('#jobKind'), byStatus, j => j.kind || '',
    jobKindFilter, 'All types', pick(v => jobKindFilter = v, 'mb_job_kind'), jobKindLabel);
  jobStatusFilter = buildFilterSelect($('#jobStatus'), byKind, j => j.status || 'unknown',
    jobStatusFilter, 'All statuses', pick(v => jobStatusFilter = v, 'mb_job_status'));
}

function renderModFilters(rows, rerender) {
  const pick = (setter, key) => v => {
    setter(v);
    localStorage.setItem(key, v);
    pagerState('modTable').page = 1;
    renderModFilters(rows, rerender);
    rerender();
    updateFilterReset();
  };
  const bySrc = modSourceFilter
    ? rows.filter(x => (x.r.source_format || '') === modSourceFilter) : rows;
  const byTgt = modTargetFilter
    ? rows.filter(x => (x.r.target_format || '') === modTargetFilter) : rows;
  modSourceFilter = buildFilterSelect($('#modSource'), byTgt, x => x.r.source_format || '',
    modSourceFilter, 'All sources', pick(v => modSourceFilter = v, 'mb_mod_source'));
  modTargetFilter = buildFilterSelect($('#modTarget'), bySrc, x => x.r.target_format || '',
    modTargetFilter, 'All targets', pick(v => modTargetFilter = v, 'mb_mod_target'));
}

async function loadDashboard() {
  try { await loadDashboardBody(); }
  catch (e) { loadErr('dashErr', e, loadDashboard); }
}
async function loadDashboardBody() {
  // Each set is fetched with its own server-side filter rather than sliced out
  // of one capped list. /api/jobs caps at the newest 100 jobs of EVERY kind, so
  // filtering in the browser let unrelated scaffold/twin/analyze runs push real
  // conversions past the cap — the tiles then described "16 runs" when 19
  // existed. The activity table still wants the unfiltered list.
  const [activity, convertsRes, scaffoldsRes] = await Promise.all([
    api('/api/jobs'),
    api('/api/jobs?kind=convert&status=done'),
    api('/api/jobs?kind=scaffold&status=done'),
  ]);
  const jobs = activity.jobs;
  gJobsCache = jobs;
  let conns = [], connsOk = false;
  // connsOk distinguishes "no connections" from "could not ask" — both leave
  // conns empty, but only the second one is unknown (see mnum).
  try { conns = (await api('/api/v1/connections')).connections || []; connsOk = true; } catch (e) {}
  const converts = convertsRes.jobs;
  const scaffolds = scaffoldsRes.jobs;
  const win = renderDashWindow(converts.length);   // 0 = all
  const windowed = win ? converts.slice(0, win) : converts;
  await jobReportsWarm(windowed.map(j => j.id));
  const reports = await mapLimit(windowed, 8, j => jobReport(j.id));
  const withR = windowed.map((j, i) => ({job: j, r: reports[i]})).filter(x => x.r);
  const assets = withR.reduce((n, x) => n + (x.r.mappings ? x.r.mappings.length : 0), 0);
  const verdicts = withR.map(x => (x.r.migration_validation || {}).verdict).filter(Boolean);
  const passRate = verdicts.length
    ? Math.round(100 * verdicts.filter(v => v === 'PASS' || v === 'PASS_WITH_WARNINGS').length / verdicts.length) + '%'
    : '--';
  // Manual review items is WHOLE-ESTATE, unlike the other two report-derived
  // tiles. Outstanding work is the one figure that must not be a sample: a
  // windowed count read as the total and understated it (13 shown, 17 real),
  // and it is the tile most likely to drive action. Reports are memoized in
  // jobReportsCache and fetched 8-at-a-time, so covering every run costs the
  // delta only — ~3ms per uncached report.
  if (win) await jobReportsWarm(converts.map(j => j.id));
  const allReports = win
    ? await mapLimit(converts, 8, j => jobReport(j.id))
    : reports;                       // window already covered everything
  const allWithR = converts.map((j, i) => ({job: j, r: allReports[i]})).filter(x => x.r);
  const manual = allWithR.reduce((n, x) =>
    n + ((x.r.summary || {}).workload ? x.r.summary.workload.manual_queue || 0 : 0), 0);
  const pipelines = scaffolds.reduce((n, j) => n + ((j.summary || {}).objects_total || 0), 0);
  // In-flight = every non-terminal convert. Counted from the kind-filtered set
  // (not the capped mixed list) so a busy workspace cannot hide running work.
  let inFlightConverts = 0;
  try {
    const all = await api('/api/jobs?kind=convert');
    inFlightConverts = all.jobs.filter(j =>
      ['running', 'queued', 'pending', 'in_progress'].includes(j.status)).length;
  } catch (e) {
    inFlightConverts = jobs.filter(j => j.kind === 'convert'
      && ['running', 'queued', 'pending', 'in_progress'].includes(j.status)).length;
  }
  $('#dashCards').innerHTML =
    mcard(mnum(conns.filter(x => x.state === 'connected').length, connsOk), 'Connected systems',
          connsOk ? (conns.length ? conns.length + ' saved' : 'Connect under Integrations')
                  : 'could not read connections')
    + mcard(mnum(assets, withR.length > 0), 'Assets analyzed', withR.length ? 'across ' + withR.length + ' of ' + converts.length + ' runs' : '')
    + mcard(mnum(converts.length), 'Modernizations', inFlightConverts + ' in flight')
    + mcard(mnum(pipelines, scaffolds.length > 0), 'Models scaffolded', scaffolds.length ? scaffolds.length + ' scaffold runs' : '')
    + mcard(passRate, 'Validation pass rate', verdicts.length ? 'from ' + verdicts.length + ' of ' + converts.length + ' runs' : 'no validated runs yet')
    + mcard(manual || (allWithR.length ? 0 : '--'), 'Manual review items',
            // Whole-estate, so the sub-label names runs that CARRY items rather
            // than a window — no "N of M" here, the number is the real total.
            manual ? 'across ' + allWithR.filter(x =>
                ((x.r.summary || {}).workload || {}).manual_queue).length
              + ' of ' + converts.length + ' runs' : '', manual ? 'manual' : '');
  const manualTile = $('#dashCards').querySelector('[data-drill="manual"]');
  if (manualTile) {
    // Drill-down covers every run too, or it would list fewer items than the
    // tile counts.
    const open = () => showManualQueue(allWithR);
    manualTile.onclick = open;
    manualTile.onkeydown = ev => {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); open(); }
    };
  }
  const modRow = ({job, r}) => {
    const co = r.conversion_output || {};
    const s = r.summary || {};
    // Project links to job detail, like every other table on this page — a run
    // you can see the numbers for is a run you should be able to open.
    return '<tr><td><a class="md" data-id="' + esc(job.id) + '" title="Open job detail"'
      + ' role="link" tabindex="0"'
      + ' style="color:var(--accent);cursor:pointer;font-weight:600">'
      + esc(jobLabel(job)) + '</a></td>'
      + '<td>' + esc(r.source_format || '') + '</td><td>' + esc(r.target_format || '') + '</td>'
      + '<td>' + (r.mappings ? r.mappings.length : '--') + '</td>'
      + '<td>' + (s.automated_conversion_rate != null ? s.automated_conversion_rate + '%' : '--') + '</td>'
      + '<td>' + (co.conversion_confidence != null ? co.conversion_confidence + '%' : '--') + '</td>'
      + '<td>' + statChip(verdictToStatus((r.migration_validation || {}).verdict)) + '</td>'
      // Last activity is when the run FINISHED. _finish_job() records `finished`
      // precisely so wall-clock is measurable, and job detail shows both; using
      // `created` here understated the column by the whole run duration and
      // disagreed with the detail view. Falls back for pre-existing jobs whose
      // meta.json predates the `finished` field.
      + '<td style="color:var(--muted)">' + esc((job.finished || job.created || '').replace('T', ' ')) + '</td></tr>';
  };
  // Counts on the tabs, so the toggle says what is behind it before you click.
  // These count the UNFILTERED sets — the tab says how much exists, the pager
  // says how much the current filter shows.
  const modTab = $('#dashTabMod'), jobTab = $('#dashTabJob');
  if (jobTab) {
    // Say when the list is capped rather than presenting 100 as the whole
    // history — "100" and "100 of 216" are very different facts.
    jobTab.textContent = 'All activity (' + (activity.truncated
      ? jobs.length + ' of ' + activity.total : jobs.length) + ')';
    jobTab.title = activity.truncated
      ? 'Showing the ' + jobs.length + ' most recent of ' + activity.total + ' jobs'
      : '';
  }
  if (modTab) modTab.textContent = 'Modernization history (' + withR.length + ')';
  const modHeader = '<tr><th>Project</th><th>Source</th><th>Target</th><th>Assets</th><th>Automation</th><th>Confidence</th><th>Status</th><th>Last activity</th></tr>';
  // "Nothing ran yet" and "the filter hides everything" need different copy —
  // the second needs a way back, not onboarding buttons.
  const modEmpty = () => (modSourceFilter || modTargetFilter)
    ? '<tr><td colspan=8 style="color:var(--muted)">No runs match '
      + [modSourceFilter && 'source <b>' + esc(modSourceFilter) + '</b>',
         modTargetFilter && 'target <b>' + esc(modTargetFilter) + '</b>']
        .filter(Boolean).join(' and ') + '. '
      + '<a id="modFilterClear" style="color:var(--accent);cursor:pointer">Clear filters</a></td></tr>'
    : '<tr><td><div class="empty" style="border:none"><b>No modernization programs yet.</b>'
      + 'Connect a source or upload a legacy project to begin.<br>'
      + '<button class="secondary" style="margin:12px 6px 0 0" onclick="document.querySelector(\'nav a[data-page=marketplace]\').click()">Connect source</button>'
      + '<button style="margin-top:12px" onclick="document.querySelector(\'nav a[data-page=convert]\').click()">Upload project</button></div></td></tr>';
  // Rebind on every render — paging replaces the rows, so handlers attached once
  // would only ever work on page 1.
  const renderModTable = () => {
    const shown = withR.filter(x =>
      (!modSourceFilter || (x.r.source_format || '') === modSourceFilter) &&
      (!modTargetFilter || (x.r.target_format || '') === modTargetFilter));
    const noun = (modSourceFilter || modTargetFilter) ? 'matching programs' : 'programs';
    renderPage('modTable', shown.map(modRow), modHeader, modEmpty(), noun, () => {
      $('#modTable').querySelectorAll('.md').forEach(a => {
        a.onclick = () => openJobDetail(a.dataset.id);
        a.onkeydown = ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); openJobDetail(a.dataset.id); } };
      });
      const clear = $('#modFilterClear');
      if (clear) clear.onclick = () => $('#dashFilterReset').onclick();
    });
  };
  renderModFilters(withR, renderModTable);
  renderModTable();
  bindPager('modTable', renderModTable);

  // Only convert/govern/scaffold runs produce key figures. On a page of twin or
  // orchestration rows the column is empty for every row, so it is dropped
  // entirely rather than reserving ~120px of width for nothing.
  const jobFigures = j => {
    const s = j.summary || {};
    if (j.kind === 'convert') return (s.automated_conversion_rate||0) + '% automated';
    if (j.kind === 'govern') return (s.classified_columns||0) + ' classified, ' + (s.violations||0) + ' violations';
    if (j.kind === 'scaffold') return (s.objects_total||0) + ' pipelines';
    return '';
  };
  let jobShowFigures = true;   // set per render from the rows actually shown
  const jobRow = j => {
    const figures = jobFigures(j);
    // runStatusChip, not a green `done` badge: this row and the Reports table
    // describe the same runs and used to disagree about them.
    const st = runStatusChip(j)
      + (j.autofix ? ' ' + badge('⚡ fixed','var(--accent)') : '');
    // Availability is decided by the SERVER (has_findings / has_download from
    // /api/jobs), not guessed from status. Only convert & govern jobs write a
    // report.json, so "findings" on a twin/objects/analyze/scaffold row was a
    // guaranteed 404 — those now render disabled with a reason, rather than
    // looking identical to a working link right up until you click it.
    const done = j.status === 'done';
    const act = (label, ok, why, attrs) => ok
      ? '<a class="jact" ' + attrs + ' title="' + esc(why) + '">' + label + '</a>'
      : '<span class="jact off" title="' + esc(why) + '" aria-disabled="true">' + label + '</span>';
    const acts = '<div class="jacts">'
      + act('Findings', done && j.has_findings !== false && j.has_findings !== undefined,
            !done ? 'Available once the job finishes'
                  : j.has_findings ? 'Open conversion findings'
                  : 'This job type does not produce a findings report',
            'data-act="f" data-id="' + esc(j.id) + '"')
      // No "Detail" chip: the Project cell below is already a link to the very
      // same openJobDetail(), so it was a second control for an action the row
      // always had — a third of the Actions column carrying no new capability.
      + act('Download', done && j.has_download !== false && j.has_download !== undefined,
            !done ? 'Available once the job finishes'
                  : j.has_download ? 'Download all artifacts (.zip)'
                  : 'This job produced no downloadable artifacts',
            'href="' + withKey('/api/jobs/' + j.id + '/download') + '"')
      + '</div>';
    // Project leads: it is what a person scans for. When/Type follow.
    // A real href (not role="link" on a hrefless <a>) so the row supports
    // middle-click, ctrl-click, "copy link" and open-in-new-tab — comparing two
    // runs side by side was impossible while these were click handlers only.
    return '<tr><td><a class="jd" data-id="' + esc(j.id) + '" title="Open job detail"'
      + ' href="#dashboard/job/' + encodeURIComponent(j.id) + '"'
      + ' style="color:var(--accent);cursor:pointer;font-weight:600">'
      + esc(jobLabel(j)) + '</a></td>'
      + '<td>' + esc(jobKindLabel(j.kind)) + '</td><td>' + st + '</td>'
      + (jobShowFigures ? '<td>' + figures + '</td>' : '')
      + '<td style="color:var(--ink3);white-space:nowrap">' + esc((j.created||'').replace('T',' ')) + '</td>'
      + '<td class="jacts-cell">' + acts + '</td></tr>';
  };
  const jobHeader = () => '<tr><th>Project</th><th>Type</th><th>Status</th>'
    + (jobShowFigures ? '<th>Key figures</th>' : '')
    + '<th>When</th><th class="jacts-cell">Actions</th></tr>';
  const jobCols = () => jobShowFigures ? 6 : 5;
  // Distinguish "nothing ran yet" from "the filter hides everything" —
  // the second needs a way back, not onboarding copy.
  const jobEmpty = () => (jobStatusFilter || jobKindFilter)
    ? '<tr><td colspan=' + jobCols() + ' style="color:var(--ink3)">No jobs match '
      + [jobKindFilter && 'type <b>' + esc(jobKindFilter) + '</b>',
         jobStatusFilter && 'status <b>' + esc(jobStatusFilter) + '</b>']
        .filter(Boolean).join(' and ') + '. '
      + '<a id="jobStatusClear" style="color:var(--accent);cursor:pointer">Clear filters</a></td></tr>'
    : '<tr><td colspan=' + jobCols() + ' style="color:var(--ink3)">No jobs yet — run a conversion, scan, or scaffold.</td></tr>';
  const renderJobTable = () => {
    // Same normalization the option list uses — a job with no status is
    // bucketed as "unknown" in both places or selecting it matches nothing.
    const shown = jobs.filter(j =>
      (!jobStatusFilter || (j.status || 'unknown') === jobStatusFilter) &&
      (!jobKindFilter || (j.kind || '') === jobKindFilter));
    // Decide BEFORE mapping rows: header, cells and colspan must agree.
    jobShowFigures = shown.some(j => jobFigures(j) !== '');
    const noun = (jobStatusFilter || jobKindFilter) ? 'matching jobs' : 'jobs';
    renderPage('jobTable', shown.map(jobRow), jobHeader(), jobEmpty(), noun, () => {
      // Only enabled actions are anchors; disabled ones render as <span> and
      // are never matched here, so they cannot be clicked or tab-focused.
      $('#jobTable').querySelectorAll('a.jact[data-act="f"]').forEach(a =>
        a.onclick = () => openJobFindings(a.dataset.id).catch(e => mbAlert(e.message)));
      // data-act="d" is gone with the Detail chip; .jd (the Project link) is
      // now the sole route into the job-detail overlay from this table.
      $('#jobTable').querySelectorAll('.jd').forEach(a => {
        a.onclick = ev => {
          // let the browser handle new-tab/new-window intents natively
          if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button === 1) return;
          ev.preventDefault();
          openJobDetail(a.dataset.id);
        };
      });
      const clear = $('#jobStatusClear');
      if (clear) clear.onclick = () => $('#dashFilterReset').onclick();
    });
  };
  renderJobFilters(jobs, renderJobTable);
  renderJobTable();
  bindPager('jobTable', renderJobTable);
  // Both tables are rendered every load; the toggle only decides which one is
  // displayed. Switching views is therefore instant and needs no refetch.
  bindFilterReset();
  bindDashViewToggle();
  applyDashView();
}

/* ---------------- analyze + per-model plan ---------------- */
const STRATEGY_OPTS = [
  ['auto', 'Auto (detected)'], ['full', 'Full / batch reload'],
  ['incremental', 'Incremental (merge)'], ['append', 'Incremental (append)'],
  ['delete_insert', 'Delete + insert'], ['view', 'View'],
];
let planJob = null;

/* Dynamic compatibility: every pair flows source parser -> CIR -> target
   generator, so nothing is gated except same-format — the hint EXPLAINS
   the route instead of disabling choices. */
async function updateCompat() {
  const f = $('#convForm'), hint = $('#compatHint');
  const src = f.source.value;
  [...f.target.options].forEach(o => {
    if (!o.value) return;
    o.disabled = !!(src && o.value === src);
    o.title = o.disabled ? 'Same as source — nothing to convert' : '';
  });
  if (src && f.target.value === src) f.target.value = '';
  const tgt = f.target.value;
  if (!tgt) { hint.style.display = 'none'; return; }
  try {
    const r = await api('/api/v1/compatibility?source='
      + encodeURIComponent(src) + '&target=' + encodeURIComponent(tgt));
    hint.style.display = 'block';
    hint.style.background = r.supported ? 'var(--green-bg)' : 'var(--red-bg)';
    hint.style.color = r.supported ? '#1f5c38' : '#8d2f2f';
    hint.innerHTML = (r.supported && r.route.length
      ? '<b>' + r.route.map(esc).join('</b> &rarr; <b>') + '</b><br>' : '')
      + esc(r.reason);
  } catch (e) { hint.style.display = 'none'; }
}
$('#convForm').source.onchange = updateCompat;
$('#convForm').target.onchange = updateCompat;

let analyzeInFlight = false;
function switchInputTab(tab) {
  const isUpload = tab === 'upload';
  $('#inputPanelUpload').style.display = isUpload ? '' : 'none';
  $('#inputPanelConn').style.display   = isUpload ? 'none' : '';
  $('#inputTabUpload').setAttribute('aria-pressed', isUpload ? 'true' : 'false');
  $('#inputTabConn').setAttribute('aria-pressed', isUpload ? 'false' : 'true');
  // Target platform / mode / Analyze belong to the UPLOAD path only. The
  // connection path picks its objects, previews the manifest and hands off to
  // Pipeline Studio, which asks for the target itself — so showing them here
  // offered a choice nothing downstream reads, next to an Analyze button that
  // could only report a missing file.
  $('#convTargetBlock').style.display = isUpload ? '' : 'none';
  // a half-finished upload run must not leave its results under the picker
  if (!isUpload) {
    $('#planPanel').style.display = 'none';
    $('#convResult').style.display = 'none';
    $('#convErr').style.display = 'none';
  }
}

$('#analyzeBtn').onclick = async () => {
  if (analyzeInFlight) return;                 // no duplicate submissions
  const err = $('#convErr'); err.style.display = 'none';
  $('#planPanel').style.display = 'none'; $('#convResult').style.display = 'none';
  if (!chosen.convFile) {
    err.innerHTML = '📁 No file uploaded yet. Click <b>Upload Files / Project</b> above and drop your .sql, .xml, or .zip file — then click Analyze again.';
    err.style.display = 'block'; return;
  }
  const form = $('#convForm');
  const fd = new FormData();
  fd.set('file', chosen.convFile);
  fd.set('source', form.source.value);
  fd.set('dialect', form.dialect.value);
  fd.set('project', ($('#convProject').value || '').trim());
  const btn = $('#analyzeBtn');
  analyzeInFlight = true;
  btn.disabled = true; btn.textContent = 'Analyzing workload…';
  setStep(2, {busy: true, note: 'Reading the workload…'});
  try {
    // timeout so a hung/slow request can't strand the button (root cause of
    // the "stuck Analyze" bug); the finally below always restores the button
    const d = await api('/api/analyze', {method: 'POST', body: fd, timeoutMs: 120000});
    if (!d || !Array.isArray(d.models))
      throw new Error('The server returned an unexpected response. Please try again.');
    planJob = d;
    setStep(3, {note: d.models.length + ' model' + (d.models.length === 1 ? '' : 's') + ' found — choose what to convert'});
    $('#generateAllBtn').disabled = false;
    $('#generateAllBtn').title = '';
    updateConvHint();
    $('#planProject').textContent = d.project + ' (' + d.models.length + ' models)';
    const det = d.detection;
    $('#planDetect').innerHTML = det
      ? 'Detected format: <b>' + esc(det.detected_format) + '</b> · confidence '
        + Math.round(det.confidence_score * 100) + '%'
        + (det.detected_features.length ? ' · saw: ' + esc(det.detected_features.slice(0, 5).join(', ')) : '')
        + (det.alternative_formats.length ? ' · also possible: '
           + det.alternative_formats.map(a => esc(a.format) + ' (' + Math.round(a.confidence * 100) + '%)').join(', ') : '')
      : '';
    const strategyChip = s => badge(s, {FULL:'var(--accent)', MERGE:'var(--green)', APPEND:'var(--green)',
      DELETE_INSERT:'var(--amber)', VIEW:'#8b7cf7', EPHEMERAL:'var(--muted)'}[s] || 'var(--ink3)');
    $('#planTable').innerHTML =
      '<tr><th style="width:34px"><input type="checkbox" id="planAll" checked></th>'
      + '<th>Model</th><th>Detected load</th><th>Override load</th><th>Unique key</th>'
      + '<th style="text-align:center">Steps</th><th>Depends on</th></tr>'
      + d.models.map(m => '<tr data-model="' + m.name + '">'
        + '<td><input type="checkbox" class="planSel" checked></td>'
        + '<td><b>' + m.name + '</b></td>'
        + '<td>' + strategyChip(m.strategy) + '</td>'
        + '<td><select class="planStrat" style="padding:5px;border-radius:6px;border:1px solid #ccd3de">'
        + STRATEGY_OPTS.map(o => '<option value="' + o[0] + '">' + o[1] + '</option>').join('')
        + '</select></td>'
        + '<td><input class="planKey" value="' + (m.unique_key || []).join(', ')
        + '" placeholder="for merge" style="padding:5px;border-radius:6px;border:1px solid #ccd3de;width:130px"></td>'
        + '<td style="text-align:center">' + m.transformations + '</td>'
        + '<td style="font-size:12px;color:var(--ink3)">' + (m.depends_on.join(', ') || '—') + '</td></tr>').join('');
    $('#planAll').onchange = ev2 => { document.querySelectorAll('.planSel').forEach(c => c.checked = ev2.target.checked); updateCount(); };
    document.querySelectorAll('.planSel').forEach(c => c.onchange = updateCount);
    updateCount();
    $('#planPanel').style.display = 'block';
  } catch (e) {
    // stay on the step that failed: rewinding to 1 contradicted the error
    setStep(2, {fail: true, note: 'Analysis failed'});
    err.innerHTML = esc(e.message || 'Analysis failed — please try again.')
      + ' <a id="analyzeRetry" style="color:var(--accent);cursor:pointer;font-weight:600">Retry</a>';
    err.style.display = 'block';
    const rb = $('#analyzeRetry');
    if (rb) rb.onclick = () => { err.style.display = 'none'; $('#analyzeBtn').click(); };
  } finally {
    // ALWAYS restore the button — success, validation failure, API error,
    // timeout, cancellation or a malformed response all land here
    analyzeInFlight = false;
    btn.disabled = false; btn.textContent = 'Analyze workload';
  }
};

function updateCount() {
  const n = document.querySelectorAll('.planSel:checked').length;
  $('#planCount').textContent = n + ' of ' + document.querySelectorAll('.planSel').length + ' models selected';
  $('#convertSelected').textContent = 'Generate target (' + n + ' assets)';
}

$('#convertSelected').onclick = async () => {
  const err = $('#convErr'); err.style.display = 'none';
  const rows = [...document.querySelectorAll('#planTable tr[data-model]')];
  const selected = [], overrides = {};
  rows.forEach(r => {
    if (!r.querySelector('.planSel').checked) return;
    const name = r.dataset.model;
    selected.push(name);
    const strat = r.querySelector('.planStrat').value;
    const key = r.querySelector('.planKey').value.trim();
    if (strat !== 'auto' || key) overrides[name] = {strategy: strat, unique_key: key};
  });
  if (!selected.length) { err.textContent = 'Select at least one model.'; err.style.display = 'block'; return; }
  const form = $('#convForm');
  const fd = new FormData();
  fd.set('from_job', planJob.id);
  fd.set('source', planJob.source_format);
  fd.set('target', form.target.value);
  fd.set('dialect', form.dialect.value);
  fd.set('models', JSON.stringify(selected));
  fd.set('overrides', JSON.stringify(overrides));
  // Explicit name still wins over the label inherited from the analyze job.
  fd.set('project', ($('#convProject').value || '').trim());
  const btn = $('#convertSelected'); btn.disabled = true; btn.textContent = 'Generating…';
  setStep(4, {busy: true, note: 'Generating the selected models…'});
  try { showConvertResult(await api('/api/convert', {method: 'POST', body: fd})); }
  catch (e) { err.textContent = e.message; err.style.display = 'block'; }
  finally { btn.disabled = false; updateCount(); }
};

let lastConvertJob = null;

async function refreshJob() {
  if (!lastConvertJob) return;
  const btn = $('#convRefresh'); btn.disabled = true; btn.textContent = '↻ Refreshing…';
  try {
    const meta = await api('/api/jobs/' + lastConvertJob);
    showConvertResult(meta);
    $('#convRefreshed').textContent = 'checked ' + new Date().toLocaleTimeString();
  } catch (e) { $('#convErr').textContent = e.message; $('#convErr').style.display = 'block'; }
  finally { btn.disabled = false; btn.textContent = '↻ Refresh status'; }
}

function autofixBannerHtml(af) {
  if (!af) return '';
  const bits = [];
  (af.key_overrides || []).forEach(k => bits.push('merge key set: <b style="font-family:monospace">' + esc(k) + '</b>'));
  if (af.llm_assist_used) bits.push('expressions translated by Claude (flagged for review)');
  if (af.drafts_written) bits.push(af.drafts_written + ' code drafts in <b>manual_drafts/</b> (review required)');
  return '<div style="background:var(--green-bg);border:1px solid var(--green-line);border-radius:var(--radius);'
    + 'padding:11px 16px;margin-bottom:12px;font-size:13.5px">'
    + '<b style="color:var(--green)">⚡ Auto-fix applied</b>'
    + (af.applied_at ? ' <span style="color:var(--muted)">· ' + esc(af.applied_at).replace('T', ' ') + '</span>' : '')
    + (bits.length ? '<div style="margin-top:5px">' + bits.map(b => '· ' + b).join('<br>') + '</div>' : '')
    + '</div>';
}

function showConvertResult(d) {
  lastConvertJob = d.id;
  $('#convJobLabel').textContent = 'job ' + d.id;
  $('#convRefresh').onclick = refreshJob;
  $('#autofixBanner').innerHTML = autofixBannerHtml(d.autofix);
  $('#convFindings').onclick = () => openJobFindings(d.id);
  const s = d.summary;
  const wl = s.workload || {};
  const cov = wl.coverage_rate != null ? wl.coverage_rate : s.automated_conversion_rate;
  const covColor = cov >= 90 ? 'var(--green)' : cov >= 60 ? 'var(--amber)' : 'var(--red)';
  const wb = wl.workbook || {};
  $('#convCards').innerHTML =
    card(cov + '%', 'Workload coverage (' + (wl.automated_units||0) + '/' + (wl.total_units||s.objects_total) + ' units)', covColor)
    + card(s.objects_total,'Pipelines converted','', 'ALL')
    + card((wl.manual_queue!=null?wl.manual_queue:(s.issues_by_severity.MANUAL||0)),
           'Manual queue' + (wb.estimated_hours ? ' · ~' + wb.estimated_hours + 'h' : ''), 'var(--red)','MANUAL')
    + card(s.issues_by_severity.WARNING||0,'Warnings','var(--amber)','WARNING')
    + (d.validation ? card(d.validation.ok?'PASS':'FAIL','XML validation', d.validation.ok?'var(--green)':'var(--red)','ERROR') : '');
  if (wb.items) {
    $('#convCards').innerHTML += '<div style="flex-basis:100%;font-size:13px;background:var(--amber-bg);'
      + 'border:1px solid #eddaa0;border-radius:10px;padding:10px 16px">'
      + 'Every manual item has a generated code skeleton — <b>manual_workbook/</b> ('
      + wb.items + ' files) and <b>manual_queue.csv</b> are inside the download.</div>';
  }
  $('#convCards').querySelectorAll('[data-filter]').forEach(c =>
    c.onclick = () => openJobFindings(d.id, c.dataset.filter === 'ALL' ? null : c.dataset.filter));
  $('#convReport').href = withKey(d.report_url); $('#convDownload').href = withKey(d.download_url);
  $('#convResult').style.display = 'block'; loadDashboard();
}

/* ---------------- convert ---------------- */
$('#convForm').onsubmit = async ev => {
  ev.preventDefault();
  const err = $('#convErr'); err.style.display = 'none'; $('#convResult').style.display = 'none';
  if (!chosen.convFile) { err.textContent = 'Upload a workload first.'; err.style.display = 'block'; return; }
  if (!planJob) { err.textContent = 'Analyze the workload first — generation is enabled after analysis.'; err.style.display = 'block'; return; }
  const fd = new FormData(ev.target); fd.set('file', chosen.convFile);
  const btn = $('#generateAllBtn'); btn.disabled = true; btn.textContent = 'Generating…';
  setStep(4, {busy: true, note: 'Generating every asset…'});
  try {
    showConvertResult(await api('/api/convert', {method:'POST', body: fd}));
    setStep(5, {note: 'Done — open the report or download below'});
  } catch (e) { err.textContent = e.message; err.style.display = 'block'; }
  finally { btn.disabled = false; btn.textContent = 'Generate target'; }
};

/* ---------------- run stepper ------------------------------------------
   `setStep(n)` still means "the run is at step n". The options carry the two
   things the old five-colour bar could not say: that a step is IN FLIGHT, and
   that one FAILED — a failed analyze used to rewind the bar to step 1, which
   contradicted the error and its Retry link sitting on screen.

   A step stays clickable once reached, because the output of an earlier step
   (the detection line, the model table, the result cards) is what you want
   back after scrolling away from it. */
let stepMax = 1;
function setStep(n, opts) {
  const o = opts || {};
  if (!o.fail) stepMax = Math.max(stepMax, n);
  document.querySelectorAll('#modSteps .step').forEach(s => {
    const i = +s.dataset.step;
    s.classList.toggle('on', i === n && !o.fail);
    s.classList.toggle('done', i < n);
    s.classList.toggle('busy', i === n && !!o.busy);
    s.classList.toggle('fail', i === n && !!o.fail);
    if (i === n && !o.fail) s.setAttribute('aria-current', 'step');
    else s.removeAttribute('aria-current');
    const reached = i <= stepMax;
    s.setAttribute('aria-disabled', reached ? 'false' : 'true');
    s.title = reached ? 'Go to ' + s.textContent.replace(/^\d/, '').trim()
                      : 'Available once the run reaches this step';
  });
  document.querySelectorAll('#modSteps .bar').forEach(b =>
    b.classList.toggle('done', +b.dataset.bar < n));
  const note = $('#modStepNote');
  if (note) {
    note.classList.toggle('fail', !!o.fail);
    if (o.note !== undefined) note.textContent = o.note;
  }
}
// Reached steps scroll back to what they produced. Nothing is re-run and no
// state changes — this is navigation, not a wizard "back".
$('#modSteps').addEventListener('click', ev => {
  const b = ev.target.closest('.step');
  if (!b || b.getAttribute('aria-disabled') === 'true') return;
  const el = document.getElementById(b.dataset.go);
  if (el && el.offsetParent !== null) el.scrollIntoView({behavior: 'smooth', block: 'start'});
  else $('#convForm').scrollIntoView({behavior: 'smooth', block: 'start'});
});

/* Target platform cards drive the hidden target select. Grouped by the role
   the target plays — flat, a warehouse and a transformation framework read as
   interchangeable destinations, and they are not. One radiogroup still, so the
   grouping is presentation and the choice stays single. */
const TARGET_GROUPS = [
  ['Warehouses', [['snowflake','Snowflake'],['databricks','Databricks'],
                  ['bigquery','BigQuery'],['redshift','Redshift']]],
  ['Transformation and ETL', [['synapse','Fabric / Synapse'],['dbt','dbt'],
                  ['idmc','IDMC'],['powercenter','PowerCenter']]],
];
$('#targetSel').innerHTML = TARGET_GROUPS.map(([grp, items]) =>
  '<p class="tgrp">' + grp + '</p><div class="tsel">'
  + items.map(([v,l]) => '<button type="button" data-t="' + v + '" role="radio" '
      + 'aria-checked="false">' + l + '</button>').join('')
  + '</div>').join('');
$('#targetSel').querySelectorAll('button').forEach(b => b.onclick = () => {
  $('#targetSel').querySelectorAll('button').forEach(x => { x.classList.remove('on'); x.setAttribute('aria-checked','false'); });
  b.classList.add('on'); b.setAttribute('aria-checked','true');
  $('#convForm').target.value = b.dataset.t;
  updateCompat();
  updateConvHint();
});
document.querySelectorAll('input[name="tmode"]').forEach(r => r.onchange = () => {
  const analyzeOnly = document.querySelector('input[name="tmode"]:checked').value === 'analyze';
  $('#generateAllBtn').style.display = analyzeOnly ? 'none' : '';
  const cs = $('#convertSelected'); if (cs) cs.style.display = analyzeOnly ? 'none' : '';
  $('#tmodeSeg').querySelectorAll('label').forEach(l =>
    l.classList.toggle('on', l.querySelector('input').checked));
  updateConvHint();
});

/* The blocker on the primary action, stated in the row rather than hidden in a
   disabled button's tooltip — a tooltip on a disabled control is unreachable
   for anyone who is not hovering a mouse over it. */
function updateConvHint() {
  const note = $('#convHint');
  if (!note) return;
  const analyzeOnly = (document.querySelector('input[name="tmode"]:checked') || {}).value === 'analyze';
  let blocker = '';
  if (!chosen.convFile) blocker = 'Add a file to continue';
  else if (!$('#convForm').target.value) blocker = 'Choose a target platform';
  else if (!planJob && !analyzeOnly) blocker = 'Analyze first to generate';
  note.textContent = blocker;
}

/* Project name and the source/dialect overrides both default correctly almost
   always; each cost a permanent labelled field to say so. */
$('#convNameEdit').onclick = () => {
  const fld = $('#convNameFld'), btn = $('#convNameEdit');
  const open = fld.style.display === 'none';
  fld.style.display = open ? '' : 'none';
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  $('#convNameRO').style.display = open ? 'none' : '';
  btn.textContent = open ? 'Done' : 'Edit';
  if (open) $('#convProject').focus();
};
$('#convAdvBtn').onclick = () => {
  const adv = $('#convAdv'), btn = $('#convAdvBtn');
  const open = adv.style.display === 'none';
  adv.style.display = open ? '' : 'none';
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
};

// Hand a table manifest to Pipeline Studio. Every entry point goes through
// here — Modernize -> connected system, Integrations -> Scaffold, and Data
// Estate -> "Modernize this asset" — so the destination is always set up the
// same way: file attached, drop zone labelled, and the SOURCE SYSTEM selected
// to the system the manifest was introspected from. (Previously only the
// Integrations path selected the source, so arriving from Modernize left
// Pipeline Studio on its default source and the connector went undetected.)
async function selectScaffoldSource(connector) {
  const src = $('#scafSource');
  if (!src || !connector) return false;
  // the selects are populated at boot, but a handoff can win that race (or the
  // boot fetch failed) — make sure the options exist before selecting one
  if (!src.options.length) { try { await fillScaffoldSelects(); } catch (e) {} }
  if (![...src.options].some(o => o.value === connector)) return false;
  src.value = connector;
  syncMvChips();
  return true;
}
/* Render the attached manifest above the Source system row. Reads the File
   itself rather than taking the YAML as an argument, so a hand-dropped
   manifest previews exactly like a handed-off one. */
async function renderScaffoldManifest(file) {
  const box = $('#scafManifestPreview');
  if (!box) return;
  if (!file) { box.innerHTML = ''; return; }
  try {
    const yml = await file.text();
    box.innerHTML = manifestPreviewHtml(yml, file.name || 'tables_manifest.yml');
  } catch (e) {
    box.innerHTML = '<div style="font-size:12px;color:var(--muted);margin-top:6px">'
      + 'Manifest attached — preview unavailable (' + esc(e.message || e) + ')</div>';
  }
}
async function handoffToScaffold(file, label, connector) {
  chosen.scafFile = file;
  const dz = document.querySelector('[data-drop="scafFile"] .txt');
  if (dz) dz.innerHTML = label;
  await selectScaffoldSource(connector);
  await renderScaffoldManifest(file);
  document.querySelector('nav a[data-page="scaffold"]').click();
}

/* connected systems as a source entry (Option B) */
async function fillModConnList() {
  try {
    await loadConnections(true);
    if (!allConnectors.length) allConnectors = (await api('/api/v1/connectors')).connectors || [];
    // only systems that are started AND whose connector can actually be
    // introspected — otherwise "Analyze" would hit an unsupported path
    const canIntrospect = k => {
      const s = allConnectors.find(c => c.key === k);
      return !s || !s.supports || s.supports.introspect !== false;
    };
    const act = allConnections.filter(x => x.status === 'active' && canIntrospect(x.connector));
    $('#modConnList').innerHTML = act.length
      ? act.map(x => '<button type="button" class="secondary" style="margin:0 6px 6px 0;padding:7px 12px;font-size:12.5px" data-cid="' + x.id + '">' + esc(x.name) + '</button>').join('')
        + '<div style="font-size:11.5px;color:var(--muted);margin-top:2px">Analyzes the connected system and opens Pipeline Studio with its manifest.</div>'
      : 'No connectable systems yet — connect one that supports live introspection under Integrations.';
    $('#modConnList').querySelectorAll('[data-cid]').forEach(b => b.onclick = async () => {
      const label = b.textContent;
      b.disabled = true; b.textContent = 'Analyzing…';
      try {
        // timeoutMs guarantees the click SETTLES even when the target system
        // is unreachable (e.g. a security group silently dropping packets) —
        // without it the button hung on "Analyzing…" indefinitely
        const d = await api('/api/v1/connections/' + b.dataset.cid + '/introspect', {method:'POST', timeoutMs: 90000});
        if (!d.ok) throw new Error(d.error || 'analysis failed');
        const row = allConnections.find(x => x.id === b.dataset.cid);
        // Do NOT hand the whole estate straight to Pipeline Studio: an
        // introspection can return hundreds of tables across many schemas.
        // Open the picker so the user scopes it down first; the handoff
        // happens from there with a manifest filtered to the selection.
        openModPicker(d, row);
      } catch (e) { mbAlert((e && e.message) || 'Could not analyze this system — check its connection under Integrations.'); }
      finally { b.disabled = false; b.textContent = label; }
    });
  } catch (e) { $('#modConnList').textContent = '—'; }
}

/* ---------------- live-connection schema / object picker ----------------
   An introspection report covers the whole reachable estate. Modernizing all
   of it is almost never what is wanted, so the report is held here and the
   user narrows it: pick schemas, then pick the tables/views inside them. The
   manifest handed to Pipeline Studio is sliced down to that selection.

   The slice is done on the manifest TEXT rather than rebuilt from `tables`,
   because `manifest_yaml` carries generated content the console must not
   re-derive (column entries, declared unique_key, and the commented
   incremental_column / unique_key suggestions). Dropping whole `- name:`
   blocks keeps every one of those intact for the tables that survive. */
let modPick = null;

/* Keyed by KIND as well as name: a procedure very often shares its name with
   the table it loads (LOAD_ORDERS / ORDERS is the common shape), and a bare
   `schema.name` key would make ticking one silently tick the other. */
function modObjKey(o) { return o.kind + ':' + o.schema + '.' + o.name; }

function openModPicker(report, row) {
  // Split tables from views exactly the way the engine's `base_tables` does
  // ("VIEW" not in type) — an exact !== 'VIEW' test would let MATERIALIZED
  // VIEW through as a table and list it twice, since it is already in `views`.
  // Views have no manifest block (the manifest is table-only), but they are
  // still part of what gets modernized downstream, so they are selectable and
  // reported in the count — they simply do not filter any YAML.
  const tables = (report.tables || [])
    .filter(t => !String(t.type || '').toUpperCase().includes('VIEW'));
  const views = report.views || [];
  // Stored procedures carry the curated layer's LOGIC, and the manifest ships
  // them alongside the tables. Listing them here is what makes that visible
  // and controllable up front — otherwise the only sign they were converted
  // is the "N analyzed" line in the results, long after the choice is gone.
  const procedures = report.procedures || [];
  const objs = tables.map(t => ({schema: t.schema || '', name: t.name,
                                 kind: 'table', rows: t.rows}))
    .concat(views.map(v => ({schema: v.schema || '', name: v.name, kind: 'view'})))
    .concat(procedures.map(p => ({schema: p.schema || '', name: p.name,
                                  kind: 'procedure'})));
  const schemas = [...new Set(objs.map(o => o.schema))].sort();
  modPick = {
    report: report, row: row, objs: objs, schemas: schemas,
    // Default to everything selected: the picker is a way to NARROW an
    // analysis, so the no-interaction path must match the old behaviour.
    schemaSel: new Set(schemas),
    objSel: new Set(objs.map(modObjKey)),
    activeSchema: schemas[0] || '',
    filter: ''
  };
  $('#modPickTitle').textContent = 'Select what to modernize from ' + row.name;
  $('#modManifestPanel').style.display = 'none';
  $('#modPickPanel').style.display = '';
  $('#modObjFilter').value = '';
  renderModPicker();
  $('#modPickPanel').scrollIntoView({behavior:'smooth', block:'nearest'});
}

function closeModPicker() {
  modPick = null;
  $('#modPickPanel').style.display = 'none';
  $('#modManifestPanel').style.display = 'none';
}

/* Objects in the schemas currently ticked — the selection the manifest and
   the count are both derived from. An object in an unticked schema is out of
   scope even if its own box is still ticked, so unticking a schema hides its
   objects without destroying the choices made inside it. */
function modSelectedObjs() {
  if (!modPick) return [];
  return modPick.objs.filter(o => modPick.schemaSel.has(o.schema)
                              && modPick.objSel.has(modObjKey(o)));
}

function renderModPicker() {
  if (!modPick) return;
  const sl = $('#modSchemaList');
  sl.innerHTML = modPick.schemas.map(s => {
    const n = modPick.objs.filter(o => o.schema === s).length;
    const on = modPick.schemaSel.has(s);
    const active = s === modPick.activeSchema;
    return '<div data-schema="' + esc(s) + '" style="display:flex;gap:8px;'
      + 'align-items:center;padding:6px 12px;cursor:pointer;font-size:12.5px;'
      + (active ? 'background:var(--accent-soft);font-weight:600' : '') + '">'
      + '<input type="checkbox" data-schemabox="' + esc(s) + '"'
      + (on ? ' checked' : '') + ' style="width:auto;margin:0">'
      + '<span style="flex:1;overflow:hidden;text-overflow:ellipsis">'
      + esc(s || '(default)') + '</span>'
      + '<span style="color:var(--muted);font-size:11.5px">' + n + '</span></div>';
  }).join('') || '<div style="padding:8px 12px;font-size:12px;color:var(--muted)">No schemas found</div>';

  sl.querySelectorAll('[data-schema]').forEach(el => el.onclick = ev => {
    if (ev.target.matches('[data-schemabox]')) return;   // the box has its own handler
    modPick.activeSchema = el.dataset.schema;
    renderModPicker();
  });
  sl.querySelectorAll('[data-schemabox]').forEach(cb => cb.onchange = () => {
    const s = cb.dataset.schemabox;
    if (cb.checked) modPick.schemaSel.add(s); else modPick.schemaSel.delete(s);
    renderModPicker();
  });

  const q = (modPick.filter || '').toLowerCase();
  const shown = modPick.objs.filter(o => o.schema === modPick.activeSchema
    && (!q || o.name.toLowerCase().includes(q)));
  const schemaOn = modPick.schemaSel.has(modPick.activeSchema);
  $('#modObjList').innerHTML = shown.length
    ? shown.map(o => {
        const k = modObjKey(o);
        return '<label style="display:flex;gap:8px;align-items:center;padding:5px 12px;'
          + 'font-size:12.5px;cursor:pointer;text-transform:none;font-weight:400;margin:0'
          + (schemaOn ? '' : ';opacity:.45') + '">'
          + '<input type="checkbox" data-objbox="' + esc(k) + '"'
          + (modPick.objSel.has(k) ? ' checked' : '')
          + (schemaOn ? '' : ' disabled') + ' style="width:auto;margin:0">'
          + '<span style="flex:1;overflow:hidden;text-overflow:ellipsis">' + esc(o.name) + '</span>'
          + '<span style="color:var(--muted);font-size:11px">'
          + (o.kind === 'view' ? 'view'
             : o.kind === 'procedure' ? 'procedure'
             : (o.rows == null ? 'table' : Number(o.rows).toLocaleString() + ' rows'))
          + '</span></label>';
      }).join('')
    : '<div style="padding:10px 12px;font-size:12px;color:var(--muted)">'
      + (modPick.filter ? 'No objects match this filter.' : 'No objects in this schema.') + '</div>';

  $('#modObjList').querySelectorAll('[data-objbox]').forEach(cb => cb.onchange = () => {
    const k = cb.dataset.objbox;
    if (cb.checked) modPick.objSel.add(k); else modPick.objSel.delete(k);
    updateModPickCount();
  });
  updateModPickCount();
}

function updateModPickCount() {
  const sel = modSelectedObjs();
  const t = sel.filter(o => o.kind === 'table').length;
  const v = sel.filter(o => o.kind === 'view').length;
  const p = sel.filter(o => o.kind === 'procedure').length;
  const nSchemas = new Set(sel.map(o => o.schema)).size;
  $('#modPickCount').textContent = sel.length
    ? t + ' table' + (t === 1 ? '' : 's')
      + (v ? ' + ' + v + ' view' + (v === 1 ? '' : 's') : '')
      + (p ? ' + ' + p + ' procedure' + (p === 1 ? '' : 's') : '')
      + ' across ' + nSchemas + ' schema' + (nSchemas === 1 ? '' : 's')
      + ' selected'
    : 'Nothing selected';
  $('#modPickNext').disabled = !sel.length;
}

/* Keep only the table blocks whose (schema, name) is selected.

   Indentation alone cannot find the block boundaries: safe_dump emits a
   table entry as `- name: X` at indent 2, and a COLUMN entry inside that
   table's `columns:` list at the SAME indent 2 — so matching every
   `- name:` line splits each table at its first column and truncates it.

   The reliable discriminator is the key that follows: a table entry's next
   key is always `schema:` (_manifest_entry writes name then schema), while a
   column entry's is `type:`/`nullable:`. So a boundary is a `- name:` line
   whose FOLLOWING line is a `schema:` at the matching depth. Everything up to
   the next such boundary — columns, unique_key list items, and the generated
   `# incremental_column:` / `# unique_key:` suggestion comments — stays with
   the table it belongs to. Header comments before `tables:` pass through.

   `keepProc` is optional. Without it the trailing sections (`procedures:` and
   anything after) pass through verbatim, which is the behaviour every other
   caller depends on. With it, entries inside `procedures:` are filtered the
   same way tables are — that is what lets the picker's procedure checkboxes
   actually leave logic out instead of only appearing to. */
function filterManifestYaml(yml, keep, keepProc) {
  const lines = String(yml || '').split('\n');
  const out = [];
  let block = null, blockName = '', blockSchema = '', tail = false;
  const tailLines = [];
  const flush = () => {
    if (block && keep(blockSchema, blockName)) out.push(...block);
    block = null;
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    // A top-level key other than `tables:` ENDS the table list. `procedures:`
    // carries the stored-procedure bodies, and its entries look exactly like
    // table entries (`- name:` then `schema:`) — so without this they were
    // tested against the table selection, never matched it, and every one of
    // them was dropped. Worse, the section HEADER fell inside whichever table
    // block happened to be open last, so deselecting that one table silently
    // took the whole curated layer's logic with it.
    if (!tail && /^[A-Za-z_][\w-]*:/.test(line) && !/^tables:/.test(line)) {
      // indent-0 comments just above the key introduce the section, not the
      // table above it — carry them across rather than lose them with it
      const lead = [];
      while (block && block.length &&
             /^(#|\s*$)/.test(block[block.length - 1])) lead.unshift(block.pop());
      flush();
      tail = true;
      // into the TAIL, not `out`: these comments introduce the section, and
      // the procedure pass has to see them adjacent to their `procedures:`
      // key to drop them with it when nothing in it survives
      tailLines.push(...lead);
    }
    if (tail) { tailLines.push(line); continue; }
    const mo = line.match(/^(\s*)-\s+name:\s+(\S+)\s*$/);
    const nx = mo && (lines[i + 1] || '')
      .match(/^(\s*)schema:\s*(\S*)\s*$/);
    // the `schema:` key sits one level in from the `- ` bullet: two extra
    // columns for the dash and its space
    if (mo && nx && nx[1].length === mo[1].length + 2) {
      flush();
      block = [line]; blockName = mo[2];
      // a table with no schema is dumped as `schema: ''` — unquote it back to
      // the empty string so it compares equal to the report's own value
      blockSchema = (nx[2] || '').replace(/^(['"])(.*)\1$/, '$2');
      continue;
    }
    if (block) block.push(line); else out.push(line);
  }
  flush();
  return out.concat(keepProc ? filterProcedureSection(tailLines, keepProc)
                             : tailLines).join('\n');
}

/* Drop unselected entries from the manifest's `procedures:` list.

   A procedure entry cannot use the table trick of "the next key is `schema:`":
   `schema` is optional here, and the `definition` is a multi-line block scalar
   whose own text can contain anything, including lines that look like YAML
   keys. What IS reliable is indentation — every line belonging to an entry is
   indented deeper than the `- ` bullet that opens it, and the block scalar's
   body must be deeper still to remain part of it. So a new entry starts at a
   `- name:` at the SAME indent as the first bullet, and everything until the
   next one travels with it, body and all.

   If every procedure is deselected the section header and its comments go too,
   leaving no empty `procedures:` key for the scaffold to trip over. */
function filterProcedureSection(lines, keepProc) {
  const out = [];
  let i = 0;
  while (i < lines.length) {
    if (!/^procedures:\s*$/.test(lines[i])) { out.push(lines[i++]); continue; }
    // the comment block introducing the section belongs to it: if nothing
    // survives, these go as well rather than heading an absent list
    const lead = [];
    while (out.length && /^(#|\s*$)/.test(out[out.length - 1])) lead.unshift(out.pop());
    const header = lines[i++];
    const kept = [];
    let bulletIndent = null;
    while (i < lines.length) {
      const m = lines[i].match(/^(\s*)-\s+name:\s+(\S+)\s*$/);
      if (bulletIndent === null && m) bulletIndent = m[1].length;
      // a top-level key at indent 0 ends the section
      if (!m && /^[A-Za-z_][\w-]*:/.test(lines[i])) break;
      if (m && m[1].length === bulletIndent) {
        const entry = [lines[i++]];
        let schema = '';
        while (i < lines.length) {
          const nm = lines[i].match(/^(\s*)-\s+name:\s+(\S+)\s*$/);
          if (nm && nm[1].length === bulletIndent) break;
          if (/^[A-Za-z_][\w-]*:/.test(lines[i])) break;
          const sm = lines[i].match(/^\s*schema:\s*(\S*)\s*$/);
          // only the entry's OWN schema key, not one inside a body
          if (sm && !schema) schema = (sm[1] || '').replace(/^(['"])(.*)\1$/, '$2');
          entry.push(lines[i++]);
        }
        // trailing blanks separate entries — hold them aside so they don't
        // travel with a dropped entry, but keep them when it survives so a
        // kept-everything filter reproduces the input byte for byte
        const trail = [];
        while (entry.length && /^\s*$/.test(entry[entry.length - 1])) trail.unshift(entry.pop());
        if (keepProc(schema, m[2])) kept.push(...entry, ...trail);
        continue;
      }
      out.push(lines[i++]);
    }
    if (kept.length) out.push(...lead, header, ...kept);
  }
  return out;
}

$('#modPickCancel').onclick = closeModPicker;
$('#modManifestBack').onclick = () => {
  $('#modManifestPanel').style.display = 'none';
  $('#modPickPanel').style.display = '';
};
$('#modObjFilter').oninput = e => {
  if (!modPick) return;
  modPick.filter = e.target.value; renderModPicker();
};
$('#modObjAll').onclick = () => {
  if (!modPick) return;
  modPick.objs.filter(o => o.schema === modPick.activeSchema)
    .forEach(o => modPick.objSel.add(modObjKey(o)));
  renderModPicker();
};
$('#modObjNone').onclick = () => {
  if (!modPick) return;
  modPick.objs.filter(o => o.schema === modPick.activeSchema)
    .forEach(o => modPick.objSel.delete(modObjKey(o)));
  renderModPicker();
};

$('#modPickNext').onclick = () => {
  if (!modPick) return;
  const sel = modSelectedObjs();
  if (!sel.length) return;
  const keepSet = new Set(sel.filter(o => o.kind === 'table')
    .map(o => o.schema + '.' + o.name));
  // The manifest's `schema:` is only emitted when introspection knew one; fall
  // back to matching on the bare name so single-schema sources still filter.
  const byName = new Set(sel.filter(o => o.kind === 'table').map(o => o.name));
  const procSet = new Set(sel.filter(o => o.kind === 'procedure')
    .map(o => o.schema + '.' + o.name));
  const procByName = new Set(sel.filter(o => o.kind === 'procedure')
    .map(o => o.name));
  const yml = filterManifestYaml(modPick.report.manifest_yaml || '',
    (schema, name) => schema ? keepSet.has(schema + '.' + name) : byName.has(name),
    (schema, name) => schema ? procSet.has(schema + '.' + name)
                             : procByName.has(name));
  modPick.yml = yml;
  $('#modManifestPreview').innerHTML = manifestPreviewHtml(
    yml, modPick.row.connector + '_manifest.yml');
  $('#modPickPanel').style.display = 'none';
  $('#modManifestPanel').style.display = '';
  $('#modManifestPanel').scrollIntoView({behavior:'smooth', block:'nearest'});
};

$('#modManifestGo').onclick = async () => {
  if (!modPick) return;
  const row = modPick.row, sel = modSelectedObjs();
  const nTables = sel.filter(o => o.kind === 'table').length;
  // The preview is editable in place. Its container id keys the raw cache
  // that "Edit Manifest in UI" writes back to, so read the YAML from THAT
  // entry — hand edits (uncommenting an incremental_column, setting a
  // unique_key) must survive the handoff, not be replaced by the pre-edit text.
  const box = $('#modManifestPreview').querySelector('[id^="manifestBox_"]');
  const cached = box && (window._manifestRawCache || {})[box.id];
  const yml = (cached && cached.yml) || modPick.yml;
  const nProcs = sel.filter(o => o.kind === 'procedure').length;
  await handoffToScaffold(
    new File([yml], row.connector + '_manifest.yml', {type:'text/yaml'}),
    '<b>' + row.connector + '_manifest.yml</b> (' + nTables
      + ' selected table' + (nTables === 1 ? '' : 's')
      + (nProcs ? ' + ' + nProcs + ' procedure' + (nProcs === 1 ? '' : 's') : '')
      + ' from ' + esc(row.name) + ')',
    row.connector);
  closeModPicker();
};

/* ---------------- marketplace ---------------- */
let allConnectors = [], activeCat = '';
/* Covered 5 of the 8 real categories, so `etl`, `events` and `orchestration`
   reached the UI as raw storage keys — as optgroup labels in the platform
   pickers and as category filter chips on Integrations, sitting beside Title
   Case siblings. */
const CAT_NAMES = {cloud_dw:'Cloud data platforms', lakehouse:'Lakehouse',
  on_prem_db:'On-premises databases', sap:'SAP', app:'Business applications',
  etl:'ETL & integration tools', events:'Streaming & messaging',
  orchestration:'Schedulers & orchestration'};
const CAP_LABELS = {pipeline_scaffold:'Pipeline Scaffold', dbt:'dbt', idmc:'IDMC',
  powercenter:'PowerCenter', sql_modernization:'SQL Modernization',
  metadata_analysis:'Metadata Analysis', lineage:'Lineage', bidirectional:'Bidirectional'};
// Display states mirror the server's canonical connection_state() — the API
// is the single source of truth, the console never re-derives connectivity.
const STATE_LABELS = {NOT_CONNECTED:'Connect', CONNECTED:'Connected',
  FAILED:'Failed', TESTING:'Testing…', UNCONNECTED:'Unconnected',
  STOPPED:'Stopped', NEEDS_CREDENTIAL:'Needs credential'};
// dot colour class + accessible wording per state; only genuine failures are
// styled as errors — unconnected/stopped use a neutral indicator, and a
// missing credential is amber (actionable) rather than red (broken).
// The classes are namespaced (d-*): plain `err`/`warn` collide with the
// app-wide `.err` form-error banner, which hid the indicator and its label.
const STATE_DOT = {CONNECTED:'', FAILED:'d-err', TESTING:'d-warn',
  UNCONNECTED:'d-dis', STOPPED:'d-dis', NOT_CONNECTED:'d-dis', NEEDS_CREDENTIAL:'d-warn'};
// status pill tone per state, reusing the app's canonical .badge system so a
// connection's status reads the same as every other status chip in the console
const STATE_BADGE = {CONNECTED:'ok', FAILED:'bad', TESTING:'warn busy',
  NEEDS_CREDENTIAL:'warn', UNCONNECTED:'', STOPPED:'', NOT_CONNECTED:''};
let allConnections = [];
let mktFilters = {q:'', cap:'', status:''};

/* Connections are cached module-wide, but a connection created after this page
   loaded (another tab, the API, a teammate) is invisible to that cache. Any
   feature whose OUTPUT depends on a connection's settings must therefore read
   through here with force=true, not off the cache — a stale read silently
   generated artifacts with the connection details missing. */
async function loadConnections(force) {
  if (force || !allConnections.length) {
    try { allConnections = (await api('/api/v1/connections')).connections || []; }
    catch (e) { if (force) throw e; allConnections = allConnections || []; }
  }
  return allConnections;
}

// The server already computed x.state (connected|failed|testing|unconnected|
// stopped). Uppercase it for the label/dot maps; fall back defensively.
function connState(x) {
  const s = (x && x.state ? String(x.state) : 'unconnected').toUpperCase();
  return STATE_LABELS[s] ? s : 'UNCONNECTED';
}
// Timestamps written before the server emitted offsets are bare local time
// ('2026-07-28T14:19:08'), which the browser reads as ITS OWN local time — so
// a just-run test showed as "6 h ago" for a UTC server + IST viewer. Treat an
// offset-less string as UTC (what the container actually writes) instead.
function parseTs(iso) {
  if (!iso) return NaN;
  const s = String(iso);
  const bare = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?$/.test(s);
  return new Date(bare ? s.replace(' ', 'T') + 'Z' : s).getTime();
}
function fmtAgo(iso) {
  if (!iso) return '';
  const t = parseTs(iso);
  if (!isFinite(t)) return '';
  const s = (Date.now() - t) / 1000;
  // Clock skew (or a server slightly ahead) must never render "-2 h ago".
  if (s < 90) return 'just now';
  if (s < 3600) return Math.round(s/60) + ' min ago';
  if (s < 86400) return Math.round(s/3600) + ' h ago';
  return Math.round(s/86400) + ' d ago';
}
// Compact row counts for card metadata — "403.4M" instead of "403,375,603", which
// on its own pushed the analysis line onto a second line in a narrow card. Exact
// figures still belong in tables and tooltips, never here.
function fmtRows(n) {
  const v = Number(n||0);
  if (v >= 1e9) return (v/1e9).toFixed(1).replace(/\.0$/,'') + 'B';
  if (v >= 1e6) return (v/1e6).toFixed(1).replace(/\.0$/,'') + 'M';
  if (v >= 1e4) return Math.round(v/1e3) + 'K';
  return v.toLocaleString();
}
function connectionFor(key) {
  const mine = allConnections.filter(x => x.connector === key);
  // prefer a proven-connected one, else any saved connection for this key
  return mine.find(x => x.state === 'connected') || mine[0];
}

// A driver error can be long (a full SQL statement + caret underline) and the
// card has no room for it — wrap it in a scrollable box with an explicit ×
// so it doesn't feel stuck open with no way to dismiss it.
function connErrBox(msg) {
  return '<div style="position:relative;margin-top:6px">'
    + '<button type="button" title="Dismiss" onclick="this.parentElement.remove()" '
    + 'style="position:absolute;top:6px;right:6px;width:20px;height:20px;padding:0;'
    + 'margin:0;line-height:18px;border-radius:4px;background:var(--navy2);'
    + 'border-color:var(--navy-line);color:var(--navy-txt);font-size:13px">&times;</button>'
    + '<pre style="color:#FF9E93;padding-right:32px">' + esc(msg) + '</pre></div>';
}

async function loadSavedConnections() {
  // connector specs drive the status label, primary action and live-test
  // capability — load them first so a card never renders with a stale/blank
  // spec (which made status and actions look wrong).
  try { if (!allConnectors.length) allConnectors = (await api('/api/v1/connectors')).connectors || []; } catch (e) {}
  try {
    await loadConnections(true);
  } catch (e) { allConnections = []; }
  const el = $('#savedConns');
  if (!allConnections.length) {
    el.innerHTML = '<div class="empty"><b>No connections yet</b>'
      + 'Connect an enterprise system to analyze metadata and scaffold your first pipeline.<br>'
      + '<button class="secondary" style="margin-top:12px" onclick="document.getElementById(\'mktSearch\').focus()">Explore integrations</button></div>';
    loadMarketplace();
    return;
  }
  const savedCount = $('#savedCount');
  if (savedCount) savedCount.textContent = allConnections.length + (allConnections.length === 1 ? ' connection' : ' connections');
  // one labelled key/value row — dropped entirely when the value is empty, so a
  // connector without (say) a warehouse doesn't leave a blank line in the card.
  // `full` is the untruncated value for the tooltip when the label is abbreviated.
  const kvRow = (k, v, full) => v ? '<div><dt>' + esc(k) + '</dt><dd title="' + esc(full || v) + '">' + esc(v) + '</dd></div>' : '';
  el.innerHTML = '<div class="sc-row">' + allConnections.map(x => {
    const st = connState(x);
    const spec = allConnectors.find(k => k.key === x.connector) || {name: x.connector};
    const sup = (spec.supports) || {};
    const t = x.last_test;
    const label = STATE_LABELS[st];
    // health reads as a title + detail pair: the title states the condition, the
    // detail carries the (often very long) driver message
    const note = st === 'FAILED' ? {tone:'bad', title:'Connection failed', msg:(t && t.error) || 'The last test did not pass.'}
      : st === 'NEEDS_CREDENTIAL' ? {tone:'warn', title:'Password not stored', msg:'Add a credential to connect, or set MB_' + (x.connector||'').toUpperCase() + '_PASSWORD.'}
      : st === 'CONNECTED' ? {tone:'', title:'', msg:(t && t.ok ? 'Last tested ' + fmtAgo(t.at) + ' · connection healthy' : 'Reachable · analyzed ' + fmtAgo((x.last_analysis||{}).at))}
      : st === 'STOPPED' ? {tone:'', title:'', msg:'Stopped — start it to use this connection.'}
      : st === 'TESTING' ? {tone:'', title:'', msg:'Testing connection…'}
      : {tone:'', title:'', msg:(t && t.unsupported ? 'Live test not available for this connector.' : 'Not tested yet.')};
    const an = x.last_analysis;
    const anScope = an ? (an.database||'') + (an.schema && an.schema !== '(all)' ? '.' + an.schema : '') : '';
    // The analysis line only names its scope when it differs from the connection's
    // own database — repeating FINCORE_PROD from the row above just pushed the
    // line onto a second, ragged row. Row counts are abbreviated for the same
    // reason; the exact figure stays in the tooltip.
    const db = x.params.database || '';
    const anWhere = !anScope || anScope === db ? ''
      : (db && anScope.indexOf(db + '.') === 0 ? anScope.slice(db.length + 1) : anScope);
    const anLine = an ? [anWhere, an.tables + ' tables', fmtRows(an.total_rows) + ' rows',
      fmtAgo(an.at)].filter(Boolean).join(' · ') : '';
    const anFull = an ? [anScope, an.tables + ' tables',
      Number(an.total_rows||0).toLocaleString() + ' rows', fmtAgo(an.at)].filter(Boolean).join(' · ') : '';
    const badgeCls = STATE_BADGE[st] || '';
    // primary action depends on the real state AND what the connector supports.
    // A connector with no live driver can't be introspected, so its primary
    // action opens the modal to generate artifacts rather than a live probe.
    const canLive = sup.introspect !== false;   // undefined (old cache) => allow
    const primary = st === 'FAILED' ? '<button data-act="fix" data-id="' + x.id + '">Fix connection</button>'
      : st === 'NEEDS_CREDENTIAL' ? '<button data-act="fix" data-id="' + x.id + '">Add credential</button>'
      : st === 'STOPPED' ? '<button data-act="start" data-id="' + x.id + '">Start</button>'
      : (canLive ? '<button data-act="analyze" data-id="' + x.id + '">Analyze</button>'
                 : '<button data-act="edit" data-id="' + x.id + '">Generate artifacts</button>');
    const liveActs = canLive && (st === 'CONNECTED' || st === 'UNCONNECTED') && st !== 'STOPPED';
    return '<div class="sc" role="group" aria-label="Connection ' + esc(x.name) + ', ' + esc(label) + '">'
      + '<div class="hd"><img src="/static/logos/' + esc(x.connector) + '.svg" alt="" onerror="this.style.display=\'none\'">'
      + '<span class="nm"><strong title="' + esc(x.name) + ' · ' + esc(spec.name) + '">' + esc(x.name)
      +   '<span class="sub">' + esc(spec.name) + '</span></strong></span>'
      + '<span class="badge ' + badgeCls + '" role="status">'
      +   '<span class="dot" aria-hidden="true"></span>' + esc(label) + '</span></div>'
      + '<button class="kebab" data-menu="' + x.id + '" aria-label="More actions for ' + esc(x.name) + '" aria-haspopup="menu">&#8942;</button>'
      + '<div class="menu" id="menu_' + x.id + '" role="menu">'
      +   '<button data-act="' + (x.status === 'active' ? 'stop' : 'start') + '" data-id="' + x.id + '">' + (x.status === 'active' ? 'Stop' : 'Start') + '</button>'
      +   '<button data-act="config" data-id="' + x.id + '">View configuration</button>'
      +   '<button data-act="edit" data-id="' + x.id + '">Edit connection</button>'
      +   '<button class="danger" data-act="delete" data-id="' + x.id + '">Delete</button>'
      + '</div>'
      + '<dl class="kv">'
      +   kvRow('Host', x.params.account || x.params.host || x.params.project || '—')
      +   kvRow('Scope', [x.params.database, x.params.warehouse].filter(Boolean).join(' · '))
      +   kvRow('Scan', anLine, anFull)
      + '</dl>'
      + '<div class="note ' + note.tone + '">'
      +   '<div class="note-msg">' + (note.title ? '<b>' + esc(note.title) + '</b> — ' : '') + esc(note.msg) + '</div>'
      +   (note.title.length + note.msg.length > 96 ? '<button type="button" class="lnk" data-more="' + x.id + '">Show full message</button>' : '')
      + '</div>'
      + '<div class="acts">' + primary
      + (liveActs && canLive ? ' <button class="secondary ico" data-act="test" data-id="' + x.id + '" title="Test connection" aria-label="Test connection">'
        + '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 2.5a7.5 7.5 0 1 1-5.3 2.2"/><path d="M4.2 2.4v2.9h2.9"/></svg></button>'
        + ' <button class="secondary ico" data-act="load" data-id="' + x.id + '" title="Load data — accepts .xlsx, .csv, .tsv or .json" aria-label="Load data">'
        + '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 13V3"/><path d="M6.5 6.5 10 3l3.5 3.5"/><path d="M3 13v2.5a1.5 1.5 0 0 0 1.5 1.5h11a1.5 1.5 0 0 0 1.5-1.5V13"/></svg></button>'
        + '<input type="file" id="loadFile_' + x.id + '" accept=".xlsx,.csv,.tsv,.json" style="display:none">' : '')
      + '</div><div id="connOut_' + x.id + '"></div></div>';
  }).join('') + '</div>';

  // long driver errors are clamped to two lines — this expands them in place
  el.querySelectorAll('button[data-more]').forEach(b => b.onclick = ev => {
    ev.stopPropagation();
    const open = b.closest('.note').classList.toggle('open');
    b.textContent = open ? 'Show less' : 'Show full message';
  });

  const closeConnMenus = () => el.querySelectorAll('.menu').forEach(x => x.style.display = 'none');
  el.querySelectorAll('.kebab').forEach(b => b.onclick = ev => {
    ev.stopPropagation();
    const m = $('#menu_' + b.dataset.menu);
    const open = m.style.display === 'block';
    closeConnMenus();
    m.style.display = open ? 'none' : 'block';   // toggle
  });
  // Persistent outside-click + Escape close, bound ONCE for the whole app so
  // it never self-removes (the previous {once:true} handler stopped working
  // after the first click, leaving menus stuck open). Enterprise menu UX.
  if (!window._connMenuGlobalBound) {
    window._connMenuGlobalBound = true;
    document.addEventListener('click', ev => {
      if (ev.target.closest('#savedConns .menu, #savedConns .kebab')) return;
      document.querySelectorAll('#savedConns .menu').forEach(x => x.style.display = 'none');
    });
    document.addEventListener('keydown', ev => {
      if (ev.key === 'Escape') document.querySelectorAll('#savedConns .menu').forEach(x => x.style.display = 'none');
    });
  }

  el.querySelectorAll('button[data-act]').forEach(b => b.onclick = async ev => {
    ev.stopPropagation();
    closeConnMenus();                            // selecting any option closes the menu
    const id = b.dataset.id, act = b.dataset.act;
    const row = allConnections.find(x => x.id === id);
    const out = $('#connOut_' + id);
    // role gate mirroring the server: delete needs jobs:delete, every other
    // mutating action needs jobs:run; "config" is read-only
    const need = act === 'config' ? '' : act === 'delete' ? 'jobs:delete' : 'jobs:run';
    if (need && !can(need)) {
      if (out) out.innerHTML = connErrBox(permTitle(need));
      return;
    }
    try {
      if (act === 'delete') {
        if (!await mbConfirm('This removes the saved connection and its stored credentials. '
              + 'Pipelines that reference it will stop resolving.',
              {title: 'Delete “' + row.name + '”?', okText: 'Delete connection', danger: true})) return;
        await api('/api/v1/connections/' + id, {method:'DELETE'}); return loadSavedConnections();
      }
      if (act === 'start' || act === 'stop') { await api('/api/v1/connections/' + id + '/' + act, {method:'POST'}); return loadSavedConnections(); }
      if (act === 'fix' || act === 'edit') { return openConnector(row.connector, row.id); }
      if (act === 'config') {
        out.innerHTML = '<pre style="margin-top:8px">' + esc(JSON.stringify(row.params, null, 2))
          + '\n\ncredentials: ' + (row.has_secrets ? 'password stored on this host (file mode 0600)' : 'password from MB_* environment variable') + '</pre>';
        return;
      }
      if (act === 'scaffold') {
        b.disabled = true; b.textContent = 'Preparing…';
        out.innerHTML = '<div style="font-size:13px;padding:6px 0">Fetching table manifest…</div>';
        const d = await api('/api/v1/connections/' + id + '/introspect', {method:'POST'});
        if (!d.ok) { out.innerHTML = connErrBox(d.error||'failed'); return; }
        const f = new File([d.manifest_yaml], row.connector + '_manifest.yml', {type:'text/yaml'});
        chosen.scafFile = f;
        const dz = document.querySelector('[data-drop="scafFile"] .txt');
        if (dz) dz.innerHTML = '<b>' + esc(f.name) + '</b> (from connection \u201C' + esc(row.name) + '\u201D — ' + d.readiness.tables + ' tables)';
        await selectScaffoldSource(row.connector);
        await renderScaffoldManifest(f);
        document.querySelector('nav a[data-page="scaffold"]').click();
        out.innerHTML = '';
        return;
      }
      if (act === 'load') {
        const inp = $('#loadFile_' + id);
        inp.onchange = async () => {
          if (!inp.files.length) return;
          const f = inp.files[0];
          const tbl = await mbPrompt('Rows from “' + f.name + '” will be loaded into this table.',
            f.name.replace(/\.[^.]+$/, '').replace(/\W+/g, '_').toUpperCase(),
            {title: 'Target table name', okText: 'Load'});
          if (tbl === null) return;
          out.innerHTML = '<div style="font-size:13px;padding:6px 0">Loading ' + esc(f.name) + '…</div>';
          const fd = new FormData(); fd.set('file', f); fd.set('table', tbl);
          try {
            const d = await api('/api/v1/connections/' + id + '/load', {method:'POST', body: fd});
            if (d.mode === 'package') {
              const text = '-- ' + d.note + '\n' + d.ddl + '\n\n' + d.load + '\n';
              const url = URL.createObjectURL(new Blob([text], {type:'text/plain'}));
              out.innerHTML = '<pre style="margin-top:6px">' + esc(d.notes.join('; ')) + '\n\n' + esc(text) + '</pre>'
                + '<a href="' + url + '" download="load_' + esc(d.table) + '.sql"><button class="secondary">Download load script</button></a>';
            } else if (d.ok) {
              out.innerHTML = '<pre style="margin-top:6px;color:var(--green)">Loaded ' + d.rows_in_file.toLocaleString() + ' rows into ' + esc(d.table)
                + ' — table now has ' + d.rows_in_table.toLocaleString() + ' rows (' + d.elapsed_ms + 'ms)\n'
                + d.steps.map(s => 'OK   ' + esc(s.step) + ' (' + s.ms + 'ms)').join('\n') + '</pre>';
            } else { out.innerHTML = connErrBox(d.error || 'load failed'); }
          } catch (e) { out.innerHTML = connErrBox(e.message||e); }
          inp.value = '';
        };
        inp.click();
        return;
      }
      // test / analyze
      b.disabled = true;
      out.innerHTML = '<div style="font-size:13px;padding:6px 0">' + (act === 'test' ? 'Testing connection…' : 'Analyzing metadata…') + '</div>';
      const d = await api('/api/v1/connections/' + id + '/' + (act === 'test' ? 'test' : 'introspect'), {method:'POST'});
      if (act === 'test') {
        out.innerHTML = d.ok
          ? '<pre style="margin-top:6px">Connected — ' + d.latency_ms + 'ms\n' + esc(JSON.stringify(d.context||{}, null, 2)) + '</pre>'
          : connErrBox(d.error||'failed');
        loadSavedConnections();
      } else {
        if (!d.ok) { out.innerHTML = connErrBox(d.error||'failed'); }
        else {
          const r = d.readiness;
          // database/schema/verdict are echoed back from the connection the
          // user saved, so they are user-controlled and must be escaped.
          out.innerHTML = '<pre style="margin-top:6px">' + esc(d.database) + '.' + esc(d.schema) + ' — ' + esc(r.verdict) + ': '
            + r.tables + ' tables (' + r.total_rows.toLocaleString() + ' rows) · ' + r.views + ' views (' + r.views_convertible + ' convertible)</pre>'
            + manifestPreviewHtml(d.manifest_yaml, 'tables_manifest.yml');
          loadSavedConnections();
        }
      }
    } catch (e) { out && (out.innerHTML = connErrBox(e.message||e)); }
    finally { b.disabled = false; }
  });
  loadMarketplace();
}

async function loadPlugins() {
  if (!$('#plugPanel')) return;
  let d, h;
  try { d = await api('/api/plugins'); h = await api('/api/plugins/health'); }
  catch (e) { return; }
  $('#plugCount').textContent = d.plugins.length + ' plugins';
  $('#plugApi').textContent = 'Plugin API v' + d.api_version + '.';
  const hs = h.summary || {};
  $('#plugHealth').innerHTML = 'Health: '
    + ['ok', 'degraded', 'error'].map(k =>
        '<span style="color:' + ({ok:'var(--green)', degraded:'var(--amber)', error:'var(--red)'}[k])
        + ';font-weight:600">' + (hs[k] || 0) + ' ' + k + '</span>').join(' · ')
    + ' of ' + h.total + '.';
  // group by type
  const byType = {};
  d.plugins.forEach(p => (byType[p.type] = byType[p.type] || []).push(p));
  $('#plugByType').innerHTML = d.types.map(t => {
    const ps = byType[t] || [];
    return '<div style="margin-bottom:8px"><b>' + esc(t.replace(/_/g, ' '))
      + '</b> <span style="color:var(--muted)">(' + ps.length + ')</span>: '
      + ps.slice(0, 12).map(p => '<span title="' + esc((p.capabilities || []).join(', '))
        + (p.builtin ? ' · built-in' : ' · installed') + '" style="display:inline-block;background:'
        + (p.api_compatible ? 'var(--green-bg)' : 'var(--red-bg)') + ';border-radius:4px;padding:1px 7px;margin:2px;font-size:12px">'
        + esc(p.id) + '</span>').join('')
      + (ps.length > 12 ? ' <span style="color:var(--muted);font-size:12px">+' + (ps.length - 12) + ' more</span>' : '')
      + '</div>';
  }).join('');
}

$('#plugScaffold').onclick = async () => {
  try {
    const d = await api('/api/plugins/scaffold', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({type: 'source_connector', name: 'My Connector',
                            capabilities: ['describe', 'introspect']})});
    $('#plugScaffoldOut').style.display = 'block';
    $('#plugScaffoldOut').innerHTML = '<div style="font-size:12.5px;color:var(--ink3);margin-bottom:4px">'
      + 'Starter plugin <b>' + esc(d.id) + '</b> (API v' + esc(d.api_version)
      + '). Save these two files, edit the capability bodies, then POST to /api/plugins/load:</div>'
      + '<div style="font-size:11.5px;color:var(--muted);margin:4px 0">plugin.yml</div>'
      + '<pre style="background:#0f1728;color:#e8edf6;padding:10px;border-radius:6px;overflow-x:auto;font-size:11.5px">'
      + esc(d.plugin_yml) + '</pre>'
      + '<div style="font-size:11.5px;color:var(--muted);margin:4px 0">impl.py</div>'
      + '<pre style="background:#0f1728;color:#e8edf6;padding:10px;border-radius:6px;overflow-x:auto;font-size:11.5px">'
      + esc(d.impl_py) + '</pre>';
  } catch (e) { $('#plugScaffoldOut').style.display = 'block'; $('#plugScaffoldOut').innerHTML = '<div class="err" style="display:block">' + esc(e.message) + '</div>'; }
};

// ---- Enterprise Marketplace -------------------------------------------
const MKT_TYPE_LABELS = {
  connector: 'Connectors', validator: 'Validators', ai_skill: 'AI Skills',
  pipeline_template: 'Pipeline Templates',
  industry_accelerator: 'Industry Accelerators',
  migration_template: 'Migration Templates',
  business_rules: 'Business Rules',
  transformation_library: 'Transformation Libraries',
};
const MKT_SIG_STYLE = {
  verified: ['var(--green)', 'var(--green-bg)', 'Signed · verified'],
  unsigned: ['var(--amber)', '#fdf6e9', 'Unsigned'],
  untrusted_publisher: ['var(--red)', 'var(--red-bg)', 'Untrusted publisher'],
  checksum_mismatch: ['var(--red)', 'var(--red-bg)', 'Checksum mismatch'],
  signature_invalid: ['var(--red)', 'var(--red-bg)', 'Invalid signature'],
};

function mktBadge(text, color, bg, title) {
  return '<span title="' + esc(title || text) + '" style="display:inline-block;background:'
    + bg + ';color:' + color + ';border-radius:4px;padding:1px 7px;margin:2px 4px 2px 0;'
    + 'font-size:11.5px;font-weight:600">' + esc(text) + '</span>';
}

async function loadEnterpriseMarketplace() {
  if (!$('#mktStorePanel')) return;
  let cat, inst, ups;
  try {
    cat = await api('/api/marketplace');
    inst = await api('/api/marketplace/installed');
    ups = await api('/api/marketplace/updates');
  } catch (e) { return; }
  const installed = inst.installed || {};
  const updById = {};
  (ups.updates || []).forEach(u => { updById[u.id] = u; });
  $('#mktStoreCount').textContent = cat.items.length + ' packages · '
    + Object.keys(installed).length + ' installed';

  // health line
  const hs = (inst.health && inst.health.summary) || {};
  const hItems = (inst.health && inst.health.items) || {};
  const total = (inst.health && inst.health.total) || 0;
  $('#mktStoreHealth').innerHTML = total
    ? 'Installed health: ' + ['ok', 'degraded', 'error'].map(k =>
        '<span style="color:' + ({ok:'var(--green)', degraded:'var(--amber)', error:'var(--red)'}[k])
        + ';font-weight:600">' + (hs[k] || 0) + ' ' + k + '</span>').join(' · ')
      + ' of ' + total + ' installed package' + (total === 1 ? '' : 's') + '.'
    : 'No packages installed yet.';

  // available-updates banner
  const pending = (ups.updates || []).filter(u => u.update_available);
  const ub = $('#mktStoreUpdates');
  if (pending.length) {
    ub.style.display = 'block';
    ub.innerHTML = '<div style="background:var(--amber-bg);border:1px solid #f0dfae;border-radius:6px;'
      + 'padding:8px 12px;font-size:12.5px;color:var(--amber)">'
      + '<b>' + pending.length + '</b> update' + (pending.length === 1 ? '' : 's') + ' available: '
      + pending.map(u => esc(u.id) + ' (' + esc(u.installed) + ' → ' + esc(u.latest) + ')').join(', ')
      + '</div>';
  } else { ub.style.display = 'none'; ub.innerHTML = ''; }

  // catalog grouped by type
  const byType = {};
  cat.items.forEach(it => (byType[it.type] = byType[it.type] || []).push(it));
  const order = cat.item_types || Object.keys(byType);
  $('#mktStoreBody').innerHTML = order.filter(t => (byType[t] || []).length).map(t => {
    const cards = byType[t].map(it => mktCard(it, installed[it.id], updById[it.id], hItems[it.id])).join('');
    return '<div style="margin:14px 0 6px"><b style="font-size:13px">' + esc(MKT_TYPE_LABELS[t] || t)
      + '</b> <span style="color:var(--muted);font-size:12px">(' + byType[t].length + ')</span></div>'
      + '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px">'
      + cards + '</div>';
  }).join('');
}

function mktCard(it, installedRec, upd, hc) {
  const sig = MKT_SIG_STYLE[it.signature_status] || ['var(--ink3)', '#eef', it.signature_status || 'unknown'];
  const lic = (it.license && it.license.type) || 'Unspecified';
  const licAccept = it.license && it.license.requires_acceptance;
  const isInstalled = !!installedRec;
  const badges = [
    mktBadge('v' + it.version, sig[0] === 'var(--green)' ? 'var(--ink2)' : 'var(--ink2)', 'var(--line)', 'catalog version'),
    mktBadge(sig[2], sig[0], sig[1], 'signature status: ' + it.signature_status),
    mktBadge(lic, licAccept ? '#7a5c14' : 'var(--ink2)', licAccept ? '#fdf6e9' : 'var(--line)',
             licAccept ? 'requires license acceptance' : 'license'),
    mktBadge(it.compatible ? 'Compatible' : 'Incompatible', it.compatible ? 'var(--green)' : 'var(--red)',
             it.compatible ? 'var(--green-bg)' : 'var(--red-bg)', 'platform compatibility'),
  ];
  if ((it.dependencies || []).length)
    badges.push(mktBadge((it.dependencies.length) + ' dep' + (it.dependencies.length === 1 ? '' : 's'),
                'var(--ink2)', 'var(--line)', (it.dependencies || []).map(d => d.id).join(', ')));

  // installing/updating/removing packages reconfigures the platform —
  // settings:manage, mirrored from the server route map
  const mktDisabled = can('settings:manage') ? ''
    : ' disabled title="' + esc(permTitle('settings:manage')) + '"';
  let actions;
  if (isInstalled) {
    const updAvail = upd && upd.update_available;
    const hs = (hc && hc.status) || 'ok';
    const hStyle = hs === 'error' ? ['var(--red)', 'var(--red-bg)', 'var(--red-line)']
      : hs === 'degraded' ? ['var(--amber)', 'var(--amber-bg)', 'var(--amber-line)']
      : ['var(--green)', 'var(--green-bg)', 'var(--green-line)'];
    const hLabel = hs === 'ok' ? '✓ Installed v' + esc(installedRec.version)
      : (hs === 'error' ? '✕ ' : '! ') + hs + ' · v' + esc(installedRec.version);
    actions = '<span title="' + esc((hc && hc.detail) || 'content present')
      + '" style="color:' + hStyle[0] + ';background:' + hStyle[1]
      + ';border:1px solid ' + hStyle[2] + ';border-radius:999px;padding:2px 9px;'
      + 'font-size:12px;font-weight:600">' + hLabel + '</span>'
      + (updAvail ? '<button type="button" class="secondary mkt-update" data-id="' + esc(it.id)
          + '" data-accept="' + (licAccept ? '1' : '') + '" style="margin:0;padding:3px 10px;font-size:12px"'
          + mktDisabled + '>'
          + 'Update → v' + esc(upd.latest) + '</button>' : '')
      + '<button type="button" class="secondary mkt-uninstall" data-id="' + esc(it.id)
      + '" style="margin:0;padding:3px 10px;font-size:12px"' + mktDisabled + '>Uninstall</button>';
  } else {
    actions = '<button type="button" class="mkt-install" data-id="' + esc(it.id)
      + '" data-name="' + esc(it.name) + '" data-accept="' + (licAccept ? '1' : '')
      + '" data-lic="' + esc(lic) + '" data-url="' + esc((it.license && it.license.url) || '')
      + '" style="margin:0;padding:3px 12px;font-size:12px"'
      + (it.compatible ? mktDisabled : ' disabled title="incompatible with this platform"')
      + '>Install</button>';
  }
  return '<div style="border:1px solid var(--border);border-radius:var(--radius);padding:11px 12px;background:var(--surface)">'
    + '<div style="font-weight:600;font-size:13px">' + esc(it.name) + '</div>'
    + '<div style="color:var(--muted);font-size:11px;margin:1px 0 6px">' + esc(it.id)
    + ' · by ' + esc(it.publisher || 'unknown') + '</div>'
    + '<div style="color:var(--ink3);font-size:12px;min-height:32px">' + esc(it.description || '') + '</div>'
    + '<div style="margin:6px 0">' + badges.join('') + '</div>'
    + '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:4px">' + actions + '</div>'
    + '</div>';
}

document.addEventListener('click', async (ev) => {
  const inst = ev.target.closest('.mkt-install');
  const unin = ev.target.closest('.mkt-uninstall');
  const upd = ev.target.closest('.mkt-update');
  if (!inst && !unin && !upd) return;
  ev.preventDefault();
  try {
    if (inst) {
      const id = inst.dataset.id;
      let accept = false;
      if (inst.dataset.accept === '1') {
        accept = await mbConfirm('“' + inst.dataset.name + '” is licensed under ' + inst.dataset.lic
          + (inst.dataset.url ? ' (' + inst.dataset.url + ')' : '')
          + '.\n\nInstalling requires accepting this license.',
          {title: 'Accept license?', okText: 'Accept & install'});
        if (!accept) return;
      }
      inst.disabled = true; inst.textContent = 'Installing…';
      const r = await api('/api/marketplace/install', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({item_id: id, accept_license: accept})});
      if ((r.installed || []).length > 1)
        await mbAlert('Dependencies were resolved automatically:\n\n' + r.installed.join(', '),
          {title: 'Installed ' + r.installed.length + ' packages', tone: 'info', okText: 'Done'});
    } else if (unin) {
      if (!await mbConfirm('This removes the package from this workspace. '
            + 'Anything depending on it may stop working.',
            {title: 'Uninstall ' + unin.dataset.id + '?', okText: 'Uninstall', danger: true})) return;
      await api('/api/marketplace/uninstall', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({item_id: unin.dataset.id})});
    } else if (upd) {
      await api('/api/marketplace/update', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({item_id: upd.dataset.id,
          accept_license: upd.dataset.accept === '1'})});
    }
    await loadEnterpriseMarketplace();
    loadPlugins();
  } catch (e) {
    mbAlert(e.message, {title: 'Marketplace error'});
    await loadEnterpriseMarketplace();
  }
});

if ($('#mktKeypair')) $('#mktKeypair').onclick = async () => {
  try {
    const kp = await api('/api/marketplace/keypair', {method: 'POST'});
    $('#mktKeypairOut').style.display = 'block';
    $('#mktKeypairOut').innerHTML =
      '<div style="font-size:12.5px;color:var(--ink3);margin-bottom:4px">Save the private key now — it is '
      + '<b>never stored</b> by MetaBridge. Register the public key with your marketplace admin to '
      + 'have your published packages trusted.</div>'
      + '<div style="font-size:11.5px;color:var(--muted);margin:4px 0">private key (keep secret)</div>'
      + '<pre style="background:#0f1728;color:#e8edf6;padding:10px;border-radius:6px;overflow-x:auto;font-size:11.5px">'
      + esc(kp.private_key) + '</pre>'
      + '<div style="font-size:11.5px;color:var(--muted);margin:4px 0">public key (share for trust)</div>'
      + '<pre style="background:#0f1728;color:#e8edf6;padding:10px;border-radius:6px;overflow-x:auto;font-size:11.5px">'
      + esc(kp.public_key) + '</pre>';
  } catch (e) {
    $('#mktKeypairOut').style.display = 'block';
    $('#mktKeypairOut').innerHTML = '<div class="err" style="display:block">' + esc(e.message) + '</div>';
  }
};

async function loadMarketplace() {
  try { await loadMarketplaceBody(); }
  catch (e) { loadErr('mktErr', e, loadMarketplace); }
}
async function loadMarketplaceBody() {
  loadPlugins();
  loadEnterpriseMarketplace();
  if (!allConnectors.length) allConnectors = (await api('/api/v1/connectors')).connectors;
  // metrics
  const byCat = k => allConnectors.filter(c => c.category === k).length;
  const connected = allConnections.filter(x => x.state === 'connected').length;
  $('#mktMetrics').innerHTML =
    '<span><b>' + allConnectors.length + '</b> Integrations</span>'
    + '<span><b>' + connected + '</b> Connected system' + (connected === 1 ? '' : 's') + '</span>'
    + '<span><b>' + (byCat('cloud_dw') + byCat('lakehouse')) + '</b> Cloud Platforms</span>'
    + '<span><b>' + byCat('on_prem_db') + '</b> Legacy / On-Prem Systems</span>'
    + '<span><b>' + byCat('sap') + '</b> SAP Connectors</span>';
  // capability filter options
  const caps = [...new Set(allConnectors.flatMap(c => c.capabilities || []))];
  $('#fltCap').innerHTML = '<option value="">All capabilities</option>'
    + caps.map(k => '<option value="' + k + '">' + (CAP_LABELS[k]||k) + '</option>').join('');
  const cats = [...new Set(allConnectors.map(c => c.category))];
  $('#catBar').innerHTML = '<button class="' + (activeCat?'off':'') + '" data-cat="">All integrations</button>'
    + cats.map(cc => '<button class="' + (activeCat===cc?'':'off') + '" data-cat="' + cc + '">' + (CAT_NAMES[cc]||cc) + '</button>').join('');
  $('#catBar').querySelectorAll('button').forEach(b => b.onclick = () => { activeCat = b.dataset.cat; loadMarketplace(); });
  renderMarketplace();
}

function renderMarketplace() {
  if (!allConnectors.length) return;
  const q = mktFilters.q.toLowerCase();
  const list = allConnectors.filter(c => {
    if (activeCat && c.category !== activeCat) return false;
    if (mktFilters.cap && !(c.capabilities||[]).includes(mktFilters.cap)) return false;
    const conn = connectionFor(c.key);
    if (mktFilters.status === 'connected' && !(conn && connState(conn) === 'CONNECTED')) return false;
    if (mktFilters.status === 'not_connected' && conn && connState(conn) === 'CONNECTED') return false;
    if (mktFilters.status === 'issue' && !(conn && connState(conn) === 'FAILED')) return false;
    if (!q) return true;
    const syn = {on_prem_db: 'legacy on-prem hybrid database sql', cloud_dw: 'cloud warehouse',
                 lakehouse: 'cloud lakehouse spark delta', sap: 'sap erp', app: 'saas business application'};
    const hay = [c.name, c.vendor, c.category, CAT_NAMES[c.category]||'', c.platform_type,
                 c.deployment_model, c.dialect, syn[c.category]||'',
                 (c.capabilities||[]).map(k => CAP_LABELS[k]||k).join(' ')]
                .join(' ').toLowerCase();
    return hay.includes(q);
  });
  $('#mktCount').textContent = list.length + ' integration' + (list.length === 1 ? '' : 's');
  if (!list.length) {
    $('#connGrid').innerHTML = '<div class="empty" style="grid-column:1/-1"><b>No integrations found</b>Try searching by platform, vendor, or capability.</div>';
    return;
  }
  $('#connGrid').innerHTML = list.map(c => {
    const conn = connectionFor(c.key);
    const st = conn ? connState(conn) : 'NOT_CONNECTED';
    const capBadges = (c.capabilities||[]).filter(k => k !== 'metadata_analysis' && k !== 'lineage');
    const shown = capBadges.slice(0, 3);
    const extra = capBadges.length - shown.length;
    const foot = conn
      ? '<span class="dot ' + (STATE_DOT[st] || 'd-dis') + '" role="img" aria-label="Status: ' + esc(STATE_LABELS[st]) + '" title="' + esc(STATE_LABELS[st]) + '"></span> ' + esc(STATE_LABELS[st])
        + '<span class="go">Configure &rarr;</span>'
      : '<span class="go" style="margin-left:auto">Connect &rarr;</span>';
    /* The card was role="listitem" AND the click target, so assistive tech
       announced "list item" for something that behaves as a button (the visible
       "Connect ->" affordance is a non-interactive span). role="button" makes
       the behaviour match the announcement; the wrapper keeps the list
       semantics its role="list" parent requires. */
    const cardLabel = esc(c.name) + ', ' + (conn ? STATE_LABELS[st] : 'not connected')
      + (conn ? ' — edit connection' : ' — connect');
    return '<div role="listitem"><div class="conn" role="button" tabindex="0" data-key="' + c.key + '"'
      + ' aria-label="' + cardLabel + '">'
      + '<div class="hd"><img class="logo" src="/static/logos/' + c.key + '.svg" alt="" onerror="this.style.display=\'none\'">'
      + '<div><h3>' + esc(c.name) + '</h3><div class="v">' + esc(c.vendor) + ' · ' + esc(CAT_NAMES[c.category]||c.category) + '</div></div></div>'
      + '<div class="tags">' + shown.map(k => '<span class="tag">' + (CAP_LABELS[k]||k) + '</span>').join('')
      + (extra > 0 ? '<span class="tag more" title="' + capBadges.map(k => CAP_LABELS[k]||k).join(', ') + '">+' + extra + '</span>' : '')
      + '</div><div class="foot">' + foot + '</div></div></div>';
  }).join('');
  $('#connGrid').querySelectorAll('.conn').forEach(el => {
    // a connected tile ("Configure") edits the existing connection; an
    // unconnected tile creates a new one
    const open = () => { const cn = connectionFor(el.dataset.key);
      openConnector(el.dataset.key, cn && cn.id); };
    el.onclick = open;
    el.onkeydown = ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); open(); } };
  });
}

$('#mktSearch').oninput = ev => { mktFilters.q = ev.target.value; renderMarketplace(); };
$('#fltCap').onchange = ev => { mktFilters.cap = ev.target.value; renderMarketplace(); };
$('#fltStatus').onchange = ev => { mktFilters.status = ev.target.value; renderMarketplace(); };
// "New connection" opens a connector picker modal (the intended creation
// entry point) — it never navigates away or redirects to another page.
async function openNewConnectionPicker() {
  if (!allConnectors.length) {
    try { allConnectors = (await api('/api/v1/connectors')).connectors || []; } catch (e) {}
  }
  const render = (q) => {
    const ql = (q || '').toLowerCase();
    const list = allConnectors.filter(c => !ql
      || [c.name, c.vendor, CAT_NAMES[c.category] || c.category,
          (c.capabilities || []).join(' ')].join(' ').toLowerCase().includes(ql));
    $('#npList').innerHTML = list.length ? list.map(c =>
      '<button type="button" class="np-item" data-key="' + esc(c.key) + '" style="display:flex;gap:10px;align-items:center;width:100%;text-align:left;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:9px 11px;margin:0 0 6px;cursor:pointer">'
      + '<img src="/static/logos/' + esc(c.key) + '.svg" alt="" style="width:22px;height:22px" onerror="this.style.display=\'none\'">'
      + '<span><b style="font-size:13px">' + esc(c.name) + '</b>'
      + '<span style="display:block;color:var(--muted);font-size:11.5px">' + esc(c.vendor || '') + ' · ' + esc(CAT_NAMES[c.category] || c.category) + '</span></span></button>').join('')
      : '<div style="color:var(--muted);font-size:13px;padding:8px 0">No integrations match “' + esc(q) + '”.</div>';
    $('#npList').querySelectorAll('.np-item').forEach(b =>
      b.onclick = () => { $('#modalBg').style.display = 'none'; openConnector(b.dataset.key); });
  };
  $('#modalBody').innerHTML = '<h3 style="margin:0 0 4px">New connection</h3>'
    + '<div style="color:var(--ink3);font-size:13px;margin-bottom:10px">Choose an integration to connect.</div>'
    + '<input type="search" id="npSearch" placeholder="Search integrations..." style="width:100%;margin-bottom:10px" aria-label="Search integrations">'
    + '<div id="npList" style="max-height:52vh;overflow-y:auto"></div>'
    + '<div style="display:flex;justify-content:flex-end;margin-top:12px"><button class="secondary" id="npClose">Close</button></div>';
  $('#modalBody').className = 'modal'; $('#modalBg').style.display = 'flex';
  $('#npClose').onclick = () => { $('#modalBg').style.display = 'none'; };
  $('#npSearch').oninput = ev => render(ev.target.value);
  render('');
  $('#npSearch').focus();
}
$('#newConnBtn').onclick = openNewConnectionPicker;

async function openConnector(key, connId) {
  const c = allConnectors.find(x => x.key === key);
  const sup = c.supports || {};
  // EDIT mode is entered ONLY with an explicit connId (the saved-connection
  // card and connected marketplace tiles pass it). Create entry points (the
  // New Connection picker) pass no id, so they never preload or overwrite an
  // existing row — they always create a new connection.
  const editRow = connId ? allConnections.find(x => x.id === connId) : null;
  const saved = (editRow && editRow.params) || {};
  // Fields split into two labelled groups (identity/credentials vs. where the
  // data lives) so a long connector form reads as a short list of sections
  // rather than one undifferentiated column.
  const TARGET_FIELDS = /^(warehouse|database|schema|catalog|project|dataset|bucket|path|region)$/i;
  const mkField = f => {
    const req = f.required
      ? ' <span class="req" title="Required" aria-label="required">*</span>' : ' <span class="opt">(optional)</span>';
    const val = f.secret ? '' : String(saved[f.name] != null ? saved[f.name] : (f.default || ''));
    const ph = f.secret && editRow && editRow.has_secrets
      ? ' placeholder="•••••••• (leave blank to keep stored)"' : '';
    // Connector credentials are NOT the user's MetaBridge login. Without these
    // hints Chromium autofills the saved sign-in email/password into the
    // username+password pair here (and refills it on re-render), silently
    // clobbering typed values. autocomplete="new-password"/"off" + a 1Password/
    // LastPass opt-out keeps managers out of the drawer.
    const hint = f.hint ? '<div class="fhint">' + esc(f.hint) + '</div>'
      : (f.secret ? '<div class="fhint">Stored on this host — never shown in full.</div>' : '');
    const input = '<input type="' + (f.secret ? 'password' : 'text') + '" data-field="' + esc(f.name) + '"'
      + ' data-required="' + (f.required ? '1' : '') + '" data-secret="' + (f.secret ? '1' : '') + '"'
      + ' autocomplete="' + (f.secret ? 'new-password' : 'off') + '"'
      + ' autocorrect="off" autocapitalize="off" spellcheck="false"'
      + ' data-1p-ignore data-lpignore="true" data-form-type="other"'
      + ' value="' + esc(val) + '"' + ph + '>';
    return '<div><label>' + esc(f.label) + req + (f.secret ? ' 🔒' : '') + '</label>'
      // Secrets get a reveal toggle so a mistyped credential can be checked before
      // saving — the usual cause of an auth failure you cannot see.
      + (f.secret ? '<div class="pw-wrap">' + input + revealBtnHtml() + '</div>' : input)
      + hint + '</div>';
  };
  const targetFields = c.fields.filter(f => TARGET_FIELDS.test(f.name));
  const accessFields = c.fields.filter(f => !TARGET_FIELDS.test(f.name));
  const groupHtml = (title, list) => list.length
    ? '<div class="cd-sec">' + title + '</div><div class="cd-grid">' + list.map(mkField).join('') + '</div>'
    : '';
  const existing = editRow || connectionFor(key);
  const auth = c.fields.some(f => f.secret) ? 'Username / password'
    : c.fields.some(f => /token/i.test(f.name)) ? 'Token' : 'Configured per platform';
  const capRow = (c.capabilities||[]).map(k => '<span class="tag">' + (CAP_LABELS[k]||k) + '</span>').join('');
  $('#modalBody').className = 'modal conn-drawer';
  $('#modalBody').innerHTML =
    // ---- header (fixed) ----
      '<div class="cd-head">'
    +   '<div class="cd-title">'
    +     '<img class="logo" src="/static/logos/' + c.key + '.svg" alt="" onerror="this.style.display=\'none\'">'
    +     '<div><h2>' + c.name + '</h2>'
    +     '<div class="v">' + c.vendor + ' \u00b7 ' + (CAT_NAMES[c.category]||c.category) + ' \u00b7 ' + c.deployment_model + '</div></div>'
    +   '</div>'
    +   (existing
        ? '<div class="cd-status ' + (connState(existing) === 'CONNECTED' ? 'ok' : 'dis') + '">'
          + '<span class="dot ' + (STATE_DOT[connState(existing)]||'d-dis') + '" role="img" aria-label="Status" style="margin-top:4px"></span>'
          + '<div>' + STATE_LABELS[connState(existing)] + ' \u2014 saved as <b>' + esc(existing.name) + '</b>'
          + '<br>Analyze / Scaffold / Load are available under Saved connections.</div></div>'
        : '')
    + '</div>'
    // ---- body (scrolls) ----
    + '<div class="cd-body">'
    +   '<details class="cd-about">'
    +     '<summary>About this connector \u2014 capabilities, modes, regions</summary>'
    +     '<div class="cd-about-in">'
    +       '<div class="tags" style="margin:0 0 12px">' + capRow + '</div>'
    +       '<dl class="cd-facts">'
    +         '<dt>Source modes</dt><dd>Full load \u00b7 Incremental (watermark) \u00b7 Metadata only</dd>'
    +         '<dt>Targets</dt><dd>' + ([c.dbt_adapter && 'dbt', c.idmc_type && 'IDMC', c.powercenter_dbtype && 'PowerCenter'].filter(Boolean).join(' \u00b7 ') || 'source system (extract / analyze)') + '</dd>'
    +         '<dt>Authentication</dt><dd>' + auth + '</dd>'
    +         '<dt>Live actions</dt><dd>' + (sup.live_test ? 'Test &amp; Analyze supported' : 'Declarative only (artifacts &amp; scaffold)') + '</dd>'
    +         '<dt>Regions</dt><dd>' + c.regions.join(', ') + '</dd>'
    +       '</dl>'
    +     '</div>'
    +   '</details>'
    +   (c.notes ? '<div class="cd-status dis" style="margin:0 0 18px">' + c.notes + '</div>' : '')
    +   '<div class="cd-lead"><h3>' + (editRow ? 'Connection details' : 'Connect') + '</h3>'
    +     '<span class="req-key"><span class="req">*</span> required</span></div>'
    +   '<div class="cd-note">Credentials for the ' + esc(c.name) + ' instance you want to connect to.</div>'
    +   '<div class="cd-grid" style="margin-bottom:18px"><div class="wide">'
    +     '<label>Connection name <span class="opt">(optional)</span></label>'
    +     '<input type="text" id="connName" autocomplete="off" autocorrect="off" autocapitalize="off"'
    +     ' spellcheck="false" data-1p-ignore data-lpignore="true" data-form-type="other"'
    +     ' placeholder="' + esc(c.name) + '" value="' + esc(editRow ? editRow.name : '') + '">'
    +     '<div class="fhint">A label for this connection in Saved connections.</div>'
    +   '</div></div>'
    +   groupHtml(accessFields.some(f => f.secret) ? 'Account &amp; credentials' : 'Connection settings', accessFields)
    +   (targetFields.length ? '<div class="cd-rule"></div>' + groupHtml('Target location', targetFields) : '')
    +   '<div id="connFieldErr" style="display:none;color:var(--red);font-size:13px;margin:6px 0"></div>'
    +   (!sup.live_test ? '<div class="fhint" style="max-width:none">Live test and introspection are not available for this connector yet \u2014 save it, generate connection artifacts, or analyze a table manifest in Pipeline Studio.</div>' : '')
    +   '<div id="artOut"></div>'
    + '</div>'
    // ---- footer (pinned: primary actions stay reachable) ----
    + '<div class="cd-foot">'
    +   '<label class="savepw"><input type="checkbox" id="savePw"' + (editRow && editRow.has_secrets ? ' checked' : '') + '> Remember password on this host</label>'
    +   ((sup.live_test || sup.introspect)
        ? '<div class="cd-acts" style="margin-bottom:8px">'
          + (sup.live_test ? '<button class="secondary" id="testConn">Test connection</button>' : '')
          + (sup.introspect ? '<button class="secondary" id="analyzeDb">Analyze database</button>' : '')
          + '</div>'
        : '')
    +   '<div class="cd-acts primary-row">'
    +     (sup.artifacts ? '<button id="genArt" class="wide2">Generate artifacts</button>' : '')
    +     '<button class="secondary" id="saveConn">' + (editRow ? 'Update connection' : 'Save connection') + '</button>'
    +     '<button class="secondary" id="closeModal">Close</button>'
    +   '</div>'
    + '</div>';
  $('#modalBg').style.display = 'flex';
  gOverlayClose = closeDrawer;
  ['#genArt', '#testConn', '#analyzeDb', '#saveConn'].forEach(sel => {
    const el = $(sel);
    if (el && !can('jobs:run')) { el.disabled = true; el.title = permTitle('jobs:run'); }
  });
  // client-side required-field check mirroring the server (a secret field is
  // exempt when editing a connection that already has a stored secret)
  // Single reader for the drawer's fields. Trimming here (not only on blur) covers
  // the case where a value is pasted and the button clicked without the field ever
  // losing focus — a pasted credential with a trailing newline is exactly how the
  // "wrong password" failures arise.
  function fieldParams(skipStoredSecrets) {
    const params = {};
    $('#modalBody').querySelectorAll('input[data-field]').forEach(i => {
      const v = i.value.replace(/^[\s ]+|[\s ]+$/g, '');
      if (v !== i.value) i.value = v;
      // don't overwrite a stored secret with an empty box on edit
      if (skipStoredSecrets && i.dataset.secret && !v && editRow && editRow.has_secrets) return;
      params[i.dataset.field] = v;
    });
    return params;
  }
  function validateFields(requireSecrets) {
    const missing = [];
    $('#modalBody').querySelectorAll('input[data-field]').forEach(i => {
      if (i.dataset.required && !i.value.trim()) {
        // secret fields are exempt from the required check on Save/Generate
        // (requireSecrets=false) — the server's _missing_required only
        // demands non-secret fields (secrets resolve from MB_* env vars at
        // use time). Live probes (Test/Analyze) pass requireSecrets=true and
        // still demand the actual secret value.
        if (i.dataset.secret && !requireSecrets) return;
        missing.push((i.previousElementSibling.textContent || '').replace('*', '').replace('🔒', '').replace('(optional)', '').trim());
      }
    });
    const err = $('#connFieldErr');
    if (missing.length) { err.textContent = 'Fill required field(s): ' + missing.join(', '); err.style.display = 'block'; return false; }
    err.style.display = 'none'; return true;
  }
  $('#closeModal').onclick = () => closeDrawer();
  const testBtn = $('#testConn');
  if (testBtn) testBtn.onclick = async () => {
    if (!validateFields(true)) return;   // live test needs the secret too
    const params = fieldParams(false);
    $('#artOut').innerHTML = '<div style="padding:8px 0">Connecting…</div>';
    try {
      const d = await api('/api/v1/connectors/' + key + '/test',
        {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({params})});
      if (d.ok) {
        const probes = (d.probes || []).map(pr => pr.probe + ': ' + pr.result + ' (' + pr.ms + 'ms)').join('\n');
        $('#artOut').innerHTML = '<label style="color:var(--green)">Connected — ' + d.latency_ms + 'ms round trip</label>'
          + '<pre>' + esc(JSON.stringify(d.context, null, 2)) + '\n' + esc(probes)
          + (d.objects ? '\ntables visible: ' + d.objects.tables_visible : '') + '</pre>';
      } else {
        let head = d.authenticated ? 'Authenticated — but the session context failed' : 'Connection failed';
        let extra = '';
        if (d.steps) extra += d.steps.map(s => (s.ok ? 'OK   ' : 'FAIL ') + s.step + (s.error ? ' — ' + s.error : '')).join('\n') + '\n';
        if (d.databases_visible) extra += '\nDatabases this role CAN see:\n  ' + d.databases_visible.join('\n  ') + '\n';
        $('#artOut').innerHTML = '<label style="color:var(--red)">' + head + '</label><pre>' + esc(extra) + esc(d.error || '') + '</pre>';
      }
    } catch (e) { $('#artOut').innerHTML = '<label style="color:var(--red)">Connection failed</label><pre>' + esc(e.message || e) + '</pre>'; }
  };
  const analyzeBtn = $('#analyzeDb');
  if (analyzeBtn) analyzeBtn.onclick = async () => {
    if (!validateFields(true)) return;
    const params = fieldParams(false);
    $('#artOut').innerHTML = '<div style="padding:8px 0">Analyzing database (read-only)…</div>';
    try {
      const d = await api('/api/v1/connectors/' + key + '/introspect',
        {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({params})});
      if (!d.ok) { $('#artOut').innerHTML = '<label style="color:var(--red)">Analysis failed</label><pre>' + esc(d.error || '') + '</pre>'; return; }
      const r = d.readiness;
      const top = d.tables.slice(0, 15).map(t =>
        t.schema + '.' + t.name + '  (' + t.rows.toLocaleString() + ' rows, ' + t.columns.length + ' cols)').join('\n');
      const reviews = (r.views_needing_review || []).map(v => v.view + ' — ' + v.reason).join('\n');
      $('#artOut').innerHTML =
        '<label style="color:var(--green)">Database analyzed — ' + r.verdict + ' (' + d.elapsed_ms + 'ms)</label>'
        + '<pre>' + d.database + '.' + d.schema + '\n'
        + r.tables + ' tables (' + r.total_rows.toLocaleString() + ' rows) · '
        + r.views + ' views (' + r.views_convertible + ' parse clean' + ((r.views_needing_review||[]).length ? ', ' + r.views_needing_review.length + ' need review' : '') + ')\n\n'
        + esc(top) + (d.tables.length > 15 ? '\n… ' + (d.tables.length - 15) + ' more' : '')
        + (reviews ? '\n\nViews needing review:\n' + esc(reviews) : '') + '</pre>'
        + manifestPreviewHtml(d.manifest_yaml, key + '_tables_manifest.yml')
        + '<div style="font-size:13px;color:var(--ink3);margin-top:6px">Upload this manifest in <strong>Pipeline Studio</strong> to generate dbt + IDMC + PowerCenter pipelines for these ' + r.tables + ' tables.</div>';
    } catch (e) { $('#artOut').innerHTML = '<label style="color:var(--red)">Analysis failed</label><pre>' + esc(e.message || e) + '</pre>'; }
  };
  const saveBtn = $('#saveConn');
  saveBtn.onclick = async () => {
    if (!validateFields(false)) return;   // secret may stay stored on edit
    if (saveBtn.dataset.saving) return;   // guard double-submit
    const params = fieldParams(true);
    const payload = {connector: key, params, save_secrets: $('#savePw').checked,
                     name: ($('#connName').value || '').trim()};
    if (editRow) payload.id = editRow.id;
    const lbl = saveBtn.textContent;
    saveBtn.dataset.saving = '1'; saveBtn.disabled = true;
    saveBtn.textContent = editRow ? 'Updating...' : 'Saving...';
    try {
      const d = await api('/api/v1/connections', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify(payload)});
      // Server-verify the SAVED config (its stored secret / MB_* env) so the
      // Saved Connections card shows the real, current status immediately
      // after create/update — the modal's transient test never reached the
      // store, which is why a "Connected" test didn't move the saved card.
      if (sup.live_test && ($('#savePw').checked || d.has_secrets)) {
        saveBtn.textContent = 'Verifying...';
        try { await api('/api/v1/connections/' + d.id + '/test', {method:'POST'}); } catch (e) {}
      }
      $('#artOut').innerHTML = '<label style="color:var(--green)">' + (editRow ? 'Updated \u201C' : 'Saved as \u201C') + esc(d.name) + '\u201D — it stays available under Marketplace \u2192 Saved connections until you stop or delete it'
        + (d.has_secrets ? ' (password stored on this host, file mode 0600).' : ' (password NOT stored — supply MB_* env var or re-enter when asked).') + '</label>';
      // values are persisted now — rebaseline so closing doesn't warn
      $('#modalBody').querySelectorAll('input[data-field]')
        .forEach(i => i.setAttribute('value', i.value));
      loadSavedConnections();
    } catch (e) { $('#artOut').innerHTML = '<label style="color:var(--red)">Save failed</label><pre>' + esc(e.message||e) + '</pre>'; }
    finally { delete saveBtn.dataset.saving; saveBtn.disabled = false; saveBtn.textContent = lbl; }
  };
  const genBtn = $('#genArt');
  if (genBtn) genBtn.onclick = async () => {
    if (!validateFields(false)) return;
    const params = fieldParams(false);
    const cname = ($('#connName').value || '').trim() || ('conn_' + key);
    const d = await api('/api/v1/connectors/' + key + '/artifacts',
      {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({params, name: cname})});
    $('#artOut').innerHTML =
      '<label>Secrets — set these env vars on the runtime host (values are never stored)</label><pre>' + (d.secret_env_vars.join('\n') || '(none)') + '</pre>'
      + '<label>dbt profiles.yml</label><pre>' + esc(d.dbt_profile) + '</pre>'
      + '<label>IDMC connection JSON</label><pre>' + esc(JSON.stringify(d.idmc_connection, null, 2)) + '</pre>'
      + '<label>PowerCenter (pmrep)</label><pre>' + esc(d.pmrep_command) + '</pre>';
  };
}
// True when the connector drawer holds user-entered values that aren't saved.
// Compared against the value the field was rendered with, so preloaded values
// in edit mode don't count as edits.
function drawerDirty() {
  const body = $('#modalBody');
  if (!body) return false;
  return [...body.querySelectorAll('input[data-field]')]
    .some(i => i.value !== (i.getAttribute('value') || ''));
}

// Single exit path for the drawer — confirms before discarding a part-filled
// form. Callers that already persisted (Save connection) pass force=true.
async function closeDrawer(force) {
  if (!force && drawerDirty()) {
    const go = await mbConfirm('This connection has unsaved details. Closing will discard them.',
      {title: 'Discard this connection?', okText: 'Discard', danger: true});
    if (!go) return;
  }
  $('#modalBg').style.display = 'none';
}

// Backdrop dismiss: require mousedown AND mouseup to both land on the backdrop.
// A plain click handler fires whenever the two share a common ancestor, so
// drag-selecting text inside the drawer and releasing a few px outside it (easy
// with a 540px right-pinned panel) counted as a backdrop click and discarded a
// half-filled connection form.
// Was hardcoded to closeDrawer() regardless of which overlay was mounted, so
// e.g. dismissing the "Discard this connection?" confirm by clicking beside it
// ran the CONNECTOR drawer's close path instead of that dialog's own Cancel —
// overlayClose() runs whatever the currently-open overlay actually set.
(function () {
  const bg = $('#modalBg');
  let downOnBg = false;
  bg.addEventListener('mousedown', ev => { downOnBg = ev.target === bg; });
  bg.addEventListener('mouseup', ev => {
    const dismiss = downOnBg && ev.target === bg;
    downOnBg = false;
    if (dismiss) overlayClose();
  });
})();
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

/* ---------------- table manifest preview ---------------- */
function manifestStats(yml) {
  const lines = String(yml || '').split('\n');
  let tables = 0, keys = 0, suggested = 0, missingKey = 0, active = 0;
  lines.forEach((l, i) => {
    // A TABLE entry and a COLUMN entry are both `- name: X`, and safe_dump
    // indents them identically — so `- name:` alone counted every column as a
    // table (a 3-table manifest with 33 columns reported "36 tables"). The
    // discriminator is the following key: _manifest_entry writes name then
    // schema for a table, whereas a column is followed by type:/nullable:.
    const mo = l.match(/^(\s*)-\s+name:/);
    const nx = mo && (lines[i + 1] || '').match(/^(\s*)schema:/);
    if (mo && nx && nx[1].length === mo[1].length + 2) tables++;
    else if (/^\s*#\s*incremental_column:/.test(l)) suggested++;
    else if (/^\s*#\s*unique_key:/.test(l)) missingKey++;
    else if (/^\s*unique_key:/.test(l)) keys++;
    else if (/^\s*incremental_column:\s*\S/.test(l)) active++;   // uncommented
  });
  return {tables, keys, suggested, missingKey, active};
}

function renderYamlBody(yml) {
  return String(yml || '').split('\n').map(l => {
    const e = esc(l);
    if (/^\s*#\s*(incremental_column|unique_key):/.test(l))
      return '<span style="color:var(--amber);font-weight:600">' + e + '</span>';
    if (/^\s*#/.test(l)) return '<span style="color:var(--muted)">' + e + '</span>';
    const kv = l.match(/^(\s*-?\s*)([A-Za-z_][\w]*)(:)(.*)$/);
    if (kv) return esc(kv[1])
      + '<span style="color:#1F6FB2">' + esc(kv[2]) + '</span>'
      + '<span style="color:#51606F">' + esc(kv[3]) + '</span>'
      + '<span style="color:var(--ink)">' + esc(kv[4]) + '</span>';
    return '<span style="color:var(--ink)">' + e + '</span>';
  }).join('\n');
}

window._manifestRawCache = window._manifestRawCache || {};

function manifestPreviewHtml(yml, filename) {
  const containerId = 'manifestBox_' + Math.random().toString(36).substr(2, 9);
  window._manifestRawCache[containerId] = { yml: yml, filename: filename, isEditing: false };
  // Update chosen.scafFile with initial/current content
  if (typeof chosen !== 'undefined') {
    chosen.scafFile = new File([yml], filename || 'tables_manifest.yml', {type: 'text/yaml'});
  }
  return renderManifestContainer(containerId);
}

function renderManifestContainer(containerId) {
  const cached = window._manifestRawCache[containerId];
  if (!cached) return '';
  const yml = cached.yml;
  const filename = cached.filename;
  const s = manifestStats(yml);
  const blob = new Blob([yml], {type:'text/yaml'});
  const NEUTRAL = ['#EDF0F4', '#33404E', '#D9DFE7'];
  const GOOD    = ['#E9F6EE', '#0F6B33', '#BFE4CC'];
  const WARN    = ['#FCF3E3', '#8A4B08', '#EFD9AC'];
  const chip = (txt, bg, fg, bd) => '<span style="display:inline-block;'
    + 'padding:3px 10px;border-radius:11px;font-size:12px;font-weight:600;'
    + 'margin:0 6px 4px 0;background:' + bg + ';color:' + fg
    + ';border:1px solid ' + bd + '">' + esc(txt) + '</span>';

  let chips = chip(s.tables + ' tables', ...NEUTRAL);
  chips += s.keys
    ? chip(s.keys + ' with primary key', ...GOOD)
    : chip('no primary keys declared', ...WARN);
  if (s.suggested) chips += chip(s.suggested + ' watermark suggestion'
    + (s.suggested === 1 ? '' : 's'), ...WARN);

  const full = s.tables - s.active;
  const warn = (s.tables > 0 && full > 0) || (s.tables === 0)
    ? '<div style="font-size:12px;color:var(--amber);margin-top:8px;line-height:1.5">'
      + (s.tables === 0
          ? 'No tables detected in manifest.'
          : full + ' of ' + s.tables + ' table' + (s.tables === 1 ? '' : 's')
            + ' ha' + (full === 1 ? 's' : 've') + ' no active '
            + '<code>incremental_column</code> and will FULL reload on every run.'
            + (s.missingKey ? ' ' + s.missingKey + ' also lack'
                + (s.missingKey === 1 ? 's' : '') + ' a declared PRIMARY KEY, so a '
                + 'merge key must be set by hand.' : '')
            + ' Uncomment and verify the highlighted lines to get MERGE loads.')
      + '</div>'
    : '<div style="font-size:12px;color:#0F6B33;margin-top:8px;line-height:1.5">'
      + 'Every table has an active incremental_column — MERGE loads.</div>';

  const isEditing = cached.isEditing || false;
  // Presentation only: the manifest runs to hundreds of lines and a 320px
  // window makes it a keyhole. Kept in a map rather than on the element so
  // the choice survives the re-render that Edit/Done triggers.
  window._manifestExpanded = window._manifestExpanded || {};
  window._manifestHidden = window._manifestHidden || {};
  const isBig = !!window._manifestExpanded[containerId];
  const isHidden = !!window._manifestHidden[containerId];
  const boxH = isBig ? '75vh' : '320px';

  return '<div id="' + containerId + '" style="margin-top:10px">'
    + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">'
    + '<div>' + chips + '</div>'
    + '<div style="display:flex;gap:6px;align-items:center">'
    + '<button type="button" class="secondary" data-mb-hide="1" title="' + (isHidden ? 'Show the manifest' : 'Hide the manifest') + '" style="font-size:12px;padding:4px 10px;cursor:pointer" onclick="toggleManifestBox(\'' + containerId + '\')">'
    + (isHidden ? '▸ Show' : '▾ Hide') + '</button>'
    + '<button type="button" class="secondary" data-mb-size="1" title="' + (isBig ? 'Shrink the preview' : 'Make the preview taller') + '" style="font-size:12px;padding:4px 10px;cursor:pointer;display:' + (isHidden ? 'none' : 'inline-block') + '" onclick="toggleManifestSize(\'' + containerId + '\')">'
    + (isBig ? '⤡ Shrink' : '⤢ Expand') + '</button>'
    + '<button type="button" class="secondary" data-mb-edit="1" style="font-size:12px;padding:4px 10px;cursor:pointer;display:' + (isHidden ? 'none' : 'inline-block') + '" onclick="toggleManifestEdit(\'' + containerId + '\')">'
    + (isEditing ? '✔ Done Editing' : '✎ Edit Manifest in UI') + '</button>'
    + '</div>'
    + '</div>'
    + '<div data-mb-body="1" style="display:' + (isHidden ? 'none' : 'block') + '">'
    + (isEditing
        ? '<textarea id="txt_' + containerId + '" style="width:100%;height:' + boxH + ';background:var(--header-bg);color:var(--ink);border:1px solid var(--border);border-radius:6px;padding:12px;font-family:monospace;font-size:12px;line-height:1.55;box-sizing:border-box;resize:vertical;" oninput="onManifestInput(\'' + containerId + '\')">' + esc(yml) + '</textarea>'
        : '<pre style="max-height:' + boxH + ';overflow:auto;background:var(--header-bg);color:var(--ink);border:1px solid var(--border);border-radius:6px;padding:12px;font-size:12px;line-height:1.55;margin-top:0;white-space:pre-wrap;overflow-wrap:anywhere">' + renderYamlBody(yml) + '</pre>'
      )
    + warn
    + '<div style="margin-top:8px">'
    + '<a href="' + URL.createObjectURL(blob) + '" download="' + esc(filename) + '"><button type="button" class="secondary">Download table manifest</button></a>'
    + '</div>'
    + '</div>'
    + '</div>';
}

function toggleManifestBox(containerId) {
  // Collapses the whole preview — YAML, warning and download — leaving only
  // the summary chips, which are the part worth keeping on screen. Presentation
  // only: the manifest text is untouched, so an unsaved edit is still there
  // when it is shown again.
  window._manifestHidden = window._manifestHidden || {};
  const hide = !window._manifestHidden[containerId];
  window._manifestHidden[containerId] = hide;
  const wrap = document.getElementById(containerId);
  if (!wrap) return;
  const body = wrap.querySelector('[data-mb-body]');
  if (body) body.style.display = hide ? 'none' : 'block';
  ['[data-mb-size]', '[data-mb-edit]'].forEach(function (sel) {
    const el = wrap.querySelector(sel);
    if (el) el.style.display = hide ? 'none' : 'inline-block';
  });
  const btn = wrap.querySelector('[data-mb-hide]');
  if (btn) {
    btn.textContent = hide ? '▸ Show' : '▾ Hide';
    btn.title = hide ? 'Show the manifest' : 'Hide the manifest';
  }
}

function toggleManifestSize(containerId) {
  // Resizes the preview and nothing else — no re-render, so an in-progress
  // edit keeps its cursor, its scroll position and its unsaved text.
  window._manifestExpanded = window._manifestExpanded || {};
  const big = !window._manifestExpanded[containerId];
  window._manifestExpanded[containerId] = big;
  const wrap = document.getElementById(containerId);
  if (!wrap) return;
  const box = wrap.querySelector('textarea, pre');
  if (box) {
    if (box.tagName === 'TEXTAREA') box.style.height = big ? '75vh' : '320px';
    else box.style.maxHeight = big ? '75vh' : '320px';
  }
  const btn = wrap.querySelector('[data-mb-size]');
  if (btn) {
    btn.textContent = big ? '⤡ Shrink' : '⤢ Expand';
    btn.title = big ? 'Shrink the preview' : 'Make the preview taller';
  }
}

function toggleManifestEdit(containerId) {
  const cached = window._manifestRawCache[containerId];
  if (!cached) return;
  if (cached.isEditing) {
    const txtEl = document.getElementById('txt_' + containerId);
    if (txtEl) {
      cached.yml = txtEl.value;
      if (typeof chosen !== 'undefined') {
        chosen.scafFile = new File([cached.yml], cached.filename || 'tables_manifest.yml', {type: 'text/yaml'});
      }
    }
  }
  cached.isEditing = !cached.isEditing;
  const el = document.getElementById(containerId);
  if (el) el.outerHTML = renderManifestContainer(containerId);
}

function onManifestInput(containerId) {
  const cached = window._manifestRawCache[containerId];
  if (!cached) return;
  const txtEl = document.getElementById('txt_' + containerId);
  if (txtEl) {
    cached.yml = txtEl.value;
    if (typeof chosen !== 'undefined') {
      chosen.scafFile = new File([cached.yml], cached.filename || 'tables_manifest.yml', {type: 'text/yaml'});
    }
  }
}

/* ---------------- governance ---------------- */
$('#govForm').onsubmit = async ev => {
  ev.preventDefault();
  const err = $('#govErr'); err.style.display = 'none'; $('#govResult').style.display = 'none';
  if (!chosen.govFile) { err.textContent = 'Choose a .zip first.'; err.style.display = 'block'; return; }
  const fd = new FormData(ev.target); fd.set('file', chosen.govFile);
  const btn = ev.target.querySelector('button'); btn.disabled = true; btn.textContent = 'Scanning…';
  try {
    const d = await api('/api/govern', {method:'POST', body: fd});
    const s = d.summary;
    $('#govCards').innerHTML = card(s.classified_columns,'Classified columns')
      + card(s.special_category_columns,'Special categories','var(--red)')
      + card(s.violations,'Violations', s.violations?'var(--red)':'var(--green)')
      + card(s.warnings,'Warnings','var(--amber)');
    $('#govReport').href = withKey(d.report_url); $('#govDownload').href = withKey(d.download_url);
    const v = (d.result.policy_findings||[]).filter(f => f.severity === 'VIOLATION').slice(0,8);
    $('#govFindings').innerHTML = v.length ? '<table><tr><th>Column</th><th>Category</th><th>Finding</th></tr>'
      + v.map(f => '<tr><td><b>' + f.column + '</b></td><td>' + f.category + '</td><td>' + f.message + '</td></tr>').join('') + '</table>' : '';
    $('#govResult').style.display = 'block'; loadDashboard();
  } catch (e) { err.textContent = e.message; err.style.display = 'block'; }
  finally { btn.disabled = false; btn.textContent = 'Run governance scan'; }
};

/* Options grouped by the same category taxonomy the Integrations catalogue
   uses, so a 50-item platform picker is scannable. Categories missing from
   CAT_NAMES fall back to a de-slugged label rather than the raw key, and any
   uncategorised connector lands in a trailing "Other" group instead of
   vanishing. */
function groupedConnectorOptions(list, selectedKey) {
  const groups = new Map();
  list.forEach(c => {
    const cat = c.category || '_other';
    if (!groups.has(cat)) groups.set(cat, []);
    groups.get(cat).push(c);
  });
  const label = cat => cat === '_other' ? 'Other'
    : (CAT_NAMES[cat] || String(cat).replace(/_/g, ' ').replace(/\b\w/g, m => m.toUpperCase()));
  const keys = [...groups.keys()].sort((a, b) =>
    a === '_other' ? 1 : b === '_other' ? -1 : label(a).localeCompare(label(b)));
  return keys.map(cat => '<optgroup label="' + esc(label(cat)) + '">'
    + groups.get(cat).slice().sort((x, y) => (x.name || '').localeCompare(y.name || ''))
        .map(c => '<option value="' + esc(c.key) + '"'
          + (c.key === selectedKey ? ' selected' : '') + '>' + esc(c.name) + '</option>').join('')
    + '</optgroup>').join('');
}

/* ---------------- scaffold ---------------- */
async function fillScaffoldSelects() {
  if (!allConnectors.length) allConnectors = (await api('/api/v1/connectors')).connectors;
  /* Was a single flat list of 50 options — Salesforce, Kafka, Control-M, SAP
     HANA and Teradata in one undifferentiated scroll with no optgroups, while
     Modernize's equivalent picker next door was already grouped. Same taxonomy
     as the Integrations catalogue (CAT_NAMES), so the two agree. The sap_s4
     default is deliberate and kept. */
  $('#scafSource').innerHTML = groupedConnectorOptions(allConnectors, 'sap_s4');
  $('#scafTarget').innerHTML = groupedConnectorOptions(
    allConnectors.filter(c => c.dbt_adapter || c.category === 'cloud_dw' || c.category === 'lakehouse'),
    'snowflake');
  syncMvChips();          // the From/To chips name whatever is selected here
}
/* ---------------- Reports: migration assessment ------------------------- */
$('#assessRun').onclick = async () => {
  const err = $('#assessErr'); err.style.display = 'none';
  $('#assessResult').style.display = 'none';
  $('#assessStatus').textContent = 'Assessing…';
  try {
    const files = await Promise.all(pdropFiles('assess').map(f =>
      new Promise((res, rej) => {
        const r = new FileReader();
        // keep the relative path when a folder is picked so project
        // structure (models/ etc.) survives — same as AI readiness
        r.onload = () => res({name: pdropPath(f), content: r.result});
        r.onerror = rej;
        r.readAsText(f);
      })));
    if (!files.length) throw new Error('Choose project files first.');
    const d = await api('/api/assessment', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({files})});
    $('#assessStatus').textContent = '';
    if (d.empty) {
      err.innerHTML = 'No assessable objects were found in those files. '
        + 'Pick the <b>project folder</b> (so models/mappings are included), '
        + 'or check the source format.';
      err.style.display = 'block';
      return;
    }
    const es = d.executive_summary;
    $('#assessHeadline').textContent = es.headline
      + ' Source: ' + d.source_label + '.';
    const card = (n, l) => '<div class="mcard"><div class="n">' + n
      + '</div><div class="l">' + l + '</div></div>';
    $('#assessCards').innerHTML =
      card(es.objects_total, 'Objects')
      + card(es.automation_potential + '%', 'Automation potential')
      + card(es.migration_complexity + ' · ' + esc(es.complexity_level), 'Complexity')
      + card(es.average_confidence, 'Confidence')
      + card(es.manual_review_items, 'Manual review')
      + card(es.technical_debt_score + '/100', 'Technical debt')
      + card('~' + es.estimated_weeks + ' wks', 'Timeline')
      + card('$' + Math.round(es.estimated_labor_usd).toLocaleString(), 'Labor estimate');
    const apps = d.application_inventory || [];
    $('#assessRisks').innerHTML =
      '<b>Application groups</b> (' + apps.length + '): '
      + (apps.map(x => esc(x.application) + ' <span style="color:var(--muted)">('
          + x.objects + ' obj)</span>').join(' · ') || 'Unassigned')
      + '<div style="font-size:11.5px;color:var(--muted);margin-top:2px">Grouped from '
      + 'project/module metadata — never from SQL comments.</div>'
      + '<b style="display:block;margin-top:10px">Risks:</b><ul style="margin:4px 0 0 18px">'
      + d.migration_risks.map(r => '<li><b>' + esc(r.risk) + '</b> ['
        + esc(r.level) + '] — ' + esc(r.evidence) + ' → ' + esc(r.mitigation)
        + '</li>').join('') + '</ul>';
    $('#assessLinks').innerHTML = ['pdf', 'pptx', 'xlsx', 'docx', 'json']
      .map(f => '<a href="' + withKey('/api/assessment/' + d.assessment_id
        + '/export?format=' + f) + '">'
        + {pdf: 'PDF report', pptx: 'Board PowerPoint',
           xlsx: 'Excel inventory', docx: 'Word summary',
           json: 'JSON'}[f] + '</a>').join('');
    $('#assessResult').style.display = 'block';
  } catch (e) {
    $('#assessStatus').textContent = '';
    err.textContent = e.message; err.style.display = 'block';
  }
};

/* ---------------- Enterprise AI readiness assessment ---------------- */
const AIR_DIM_ORDER = ['metadata_quality', 'business_glossary', 'lineage',
  'data_quality', 'master_data', 'security', 'access_controls', 'pii',
  'freshness', 'vectorization_readiness', 'document_quality',
  'knowledge_graph_readiness', 'rag_readiness', 'llm_readiness',
  'agent_readiness'];
function airBandColor(band) {
  return {'Advanced': '#0e7c7b', 'Ready': 'var(--green)', 'Developing': 'var(--amber)',
          'Foundational': 'var(--red)', 'Not ready': '#8a2020'}[band] || 'var(--muted)';
}
function airGauge(score, band) {
  const r = 52, c = 2 * Math.PI * r, off = c * (1 - score / 100);
  const col = airBandColor(band);
  return '<svg width="130" height="130" viewBox="0 0 130 130">'
    + '<circle cx="65" cy="65" r="' + r + '" fill="none" stroke="#e8ecf2" stroke-width="12"/>'
    + '<circle cx="65" cy="65" r="' + r + '" fill="none" stroke="' + col
    + '" stroke-width="12" stroke-linecap="round" stroke-dasharray="' + c
    + '" stroke-dashoffset="' + off + '" transform="rotate(-90 65 65)"/>'
    + '<text x="65" y="60" text-anchor="middle" font-size="30" font-weight="700" fill="#16233c">'
    + score + '</text>'
    + '<text x="65" y="80" text-anchor="middle" font-size="11" fill="' + col
    + '" font-weight="600">' + esc(band) + '</text></svg>';
}

$('#airRun').onclick = async () => {
  const err = $('#airErr'); err.style.display = 'none';
  $('#airResult').style.display = 'none';
  $('#airStatus').textContent = 'Assessing AI readiness…';
  try {
    const files = await Promise.all(pdropFiles('air').map(f =>
      new Promise((res, rej) => {
        const r = new FileReader();
        // keep the relative path when a folder was picked so dbt/project
        // structure survives (the parsers need models/ etc.)
        r.onload = () => res({name: pdropPath(f), content: r.result});
        r.onerror = rej;
        r.readAsText(f);
      })));
    if (!files.length) throw new Error('Choose project files first.');
    const d = await api('/api/ai-readiness', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({files})});
    $('#airStatus').textContent = '';
    if (d.empty) {
      err.innerHTML = 'No assessable metadata was found. Pick your '
        + '<b>project folder</b> (dbt / PowerCenter / SAP…) so its models '
        + 'and schemas are included.';
      err.style.display = 'block';
      return;
    }
    const sc = d.ai_readiness_score;
    $('#airGauge').innerHTML = airGauge(sc.score, sc.band);
    $('#airHeadline').innerHTML = '<b>' + esc(sc.headline) + '</b>'
      + '<div style="color:var(--muted);margin-top:6px">Source: ' + esc(d.source_label)
      + '. ' + esc(sc.weights_note) + '</div>'
      + '<div style="margin-top:6px">Top blockers: '
      + sc.top_blockers.slice(0, 3).map(b => esc(b.dimension.replace(/_/g, ' '))
        + ' (' + b.score + ')').join(', ') + '</div>';
    // dimension bars
    $('#airDims').innerHTML = AIR_DIM_ORDER.filter(k => d.dimensions[k]).map(k => {
      const dm = d.dimensions[k], col = airBandColor(dm.level);
      return '<div style="display:flex;align-items:center;gap:10px;margin-bottom:3px" title="'
        + esc((dm.findings || [])[0] || '') + '">'
        + '<span style="width:210px;font-size:12.5px">' + esc(k.replace(/_/g, ' ')) + '</span>'
        + '<div style="flex:1;max-width:360px;background:var(--line);border-radius:5px;height:14px">'
        + '<div style="width:' + dm.score + '%;height:14px;border-radius:5px;background:' + col + '"></div></div>'
        + '<span style="width:96px;font-size:12px;color:' + col + '">' + dm.score + ' · ' + esc(dm.level) + '</span></div>';
    }).join('');
    // strategy cards
    const vdb = d.recommended_vector_database, emb = d.embedding_strategy,
          ch = d.chunking_strategy, kg = d.knowledge_graph_strategy,
          arch = d.recommended_llm_architecture, cost = d.estimated_ai_implementation_cost;
    const scard = (title, val, sub) => '<div class="mcard" style="text-align:left;min-width:220px" title="'
      + esc(sub || '') + '"><div class="l">' + esc(title) + '</div><div style="font-size:13.5px;font-weight:600;margin-top:4px">'
      + esc(val) + '</div></div>';
    $('#airStrategyCards').innerHTML =
      scard('Vector database', vdb.recommended, vdb.rationale)
      + scard('Embedding', emb.model, emb.rationale)
      + scard('Chunking', ch.primary, ch.rationale)
      + scard('Knowledge graph', kg.build_now ? 'Build now: ' + kg.store : 'Defer', kg.rationale)
      + scard('LLM architecture', arch.pattern, arch.rationale)
      + scard('Estimated year-1 cost', '$' + Math.round(cost.total_year_one_usd).toLocaleString(),
              'range $' + Math.round(cost.range_year_one_usd[0]).toLocaleString() + '–$'
              + Math.round(cost.range_year_one_usd[1]).toLocaleString() + ' · ' + cost.basis);
    // RAG gates
    $('#airGates').innerHTML = d.rag_readiness.gates.map(g =>
      '<div style="margin-bottom:4px"><span style="color:' + (g.pass ? 'var(--green)' : 'var(--red)')
      + ';font-weight:700">' + (g.pass ? '✓' : '✗') + '</span> ' + esc(g.gate)
      + ' <span style="color:var(--muted)">— ' + esc(g.detail) + '</span></div>').join('')
      + '<div style="margin-top:6px;color:var(--muted)">RAG readiness: <b>' + d.rag_readiness.score
      + '</b> (' + esc(d.rag_readiness.band) + ')</div>';
    // roadmap
    $('#airRoadmap').innerHTML = d.executive_ai_roadmap.phases.map(p =>
      '<div style="margin-bottom:8px"><b>' + esc(p.phase) + '</b> '
      + (p.weeks ? '<span style="color:var(--green)">· ' + p.weeks + ' wk</span>'
                 : '<span style="color:var(--red)">· gated</span>')
      + '<div style="color:var(--muted);font-size:12px">gate: ' + esc(p.entry_gate) + '</div></div>').join('')
      + '<div style="color:var(--muted);font-size:12px">' + esc(d.executive_ai_roadmap.sequencing_note) + '</div>';
    // architecture guardrails
    $('#airArch').innerHTML = '<b>LLM guardrails:</b><ul style="margin:4px 0 0 18px">'
      + arch.guardrails.map(g => '<li>' + esc(g) + '</li>').join('') + '</ul>'
      + '<div style="margin-top:6px;color:var(--muted)">' + esc(arch.fine_tuning_stance) + '</div>'
      + '<div style="margin-top:8px;color:var(--muted);font-size:12px">' + esc(d.determinism_note) + '</div>';
    $('#airLinks').innerHTML = ['pdf', 'xlsx', 'json'].map(f =>
      '<a href="' + withKey('/api/ai-readiness/' + d.assessment_id + '/export?format=' + f) + '">'
      + {pdf: 'PDF report', xlsx: 'Excel workbook', json: 'JSON'}[f] + '</a>').join('');
    $('#airResult').style.display = 'block';
  } catch (e) {
    $('#airStatus').textContent = '';
    err.textContent = e.message; err.style.display = 'block';
  }
};

/* ---------------- Technical debt intelligence ---------------- */
function debtBandColor(band) {
  return {'Low':'var(--green)', 'Moderate':'var(--amber)', 'High':'var(--red)', 'Severe':'#8a2020'}[band] || 'var(--muted)';
}
function debtGauge(score, band) {
  const r = 52, c = 2 * Math.PI * r, off = c * (1 - score / 100);
  const col = debtBandColor(band);
  return '<svg width="130" height="130" viewBox="0 0 130 130">'
    + '<circle cx="65" cy="65" r="' + r + '" fill="none" stroke="#e8ecf2" stroke-width="12"/>'
    + '<circle cx="65" cy="65" r="' + r + '" fill="none" stroke="' + col
    + '" stroke-width="12" stroke-linecap="round" stroke-dasharray="' + c
    + '" stroke-dashoffset="' + off + '" transform="rotate(-90 65 65)"/>'
    + '<text x="65" y="60" text-anchor="middle" font-size="30" font-weight="700" fill="#16233c">' + score + '</text>'
    + '<text x="65" y="80" text-anchor="middle" font-size="11" fill="' + col + '" font-weight="600">' + esc(band) + '</text></svg>';
}

$('#debtRun').onclick = async () => {
  const err = $('#debtErr'); err.style.display = 'none';
  $('#debtStatus').textContent = 'Analyzing technical debt…';
  try {
    const files = await Promise.all(pdropFiles('debt').map(f =>
      new Promise((res, rej) => {
        const r = new FileReader();
        r.onload = () => res({name: pdropPath(f), content: r.result});
        r.onerror = rej;
        r.readAsText(f);
      })));
    const d = await api('/api/tech-debt', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(files.length ? {files} : {})});
    $('#debtStatus').textContent = '';
    const sc = d.technical_debt_score;
    $('#debtGauge').innerHTML = debtGauge(sc.score, sc.band);
    $('#debtHeadline').innerHTML = '<b>' + esc(sc.headline) + '</b>'
      + '<div style="color:var(--muted);margin-top:6px">' + esc(sc.index_note) + '</div>';
    const cost = d.cloud_cost_savings, eff = d.estimated_refactoring_effort;
    const card = (n, l, s) => '<div class="mcard" title="' + esc(s || '') + '"><div class="n">' + n
      + '</div><div class="l">' + esc(l) + '</div></div>';
    $('#debtCards').innerHTML =
      card(sc.debt_objects, 'Debt objects', sc.total_objects + ' estate objects')
      + card('$' + Math.round(cost.annual_usd).toLocaleString(), 'Annual cloud saving', cost.basis)
      + card(eff.total_hours + ' hrs', 'Refactoring effort', '~' + eff.engineer_weeks + ' engineer-weeks')
      + card('$' + Math.round(eff.labor_usd).toLocaleString(), 'Cleanup labor', eff.basis);
    // category bars (only non-zero)
    const cats = Object.entries(sc.by_category).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]);
    const max = Math.max(1, ...cats.map(c => c[1]));
    $('#debtCats').innerHTML = cats.length ? cats.map(([k, v]) =>
      '<div style="display:flex;align-items:center;gap:10px;margin-bottom:3px">'
      + '<span style="width:200px;font-size:12.5px">' + esc(k) + '</span>'
      + '<div style="flex:1;max-width:340px;background:var(--line);border-radius:5px;height:14px">'
      + '<div style="width:' + Math.round(100 * v / max) + '%;height:14px;border-radius:5px;background:var(--red)"></div></div>'
      + '<span style="width:40px;font-size:12px;color:var(--red)">' + v + '</span></div>').join('')
      : '<div style="color:var(--green);font-size:13px">No technical debt detected in scope.</div>';
    // cleanup plan
    $('#debtPlan').innerHTML = d.engineering_cleanup_plan.length ? d.engineering_cleanup_plan.map(p =>
      '<div style="margin-bottom:8px"><b>' + esc(p.action) + '</b> — ' + p.count
      + ' item(s), ~' + p.estimated_hours + ' hr'
      + '<div style="color:var(--muted);font-size:12px">' + p.examples.map(esc).join(', ')
      + (p.count > p.examples.length ? ' …' : '') + '</div></div>').join('')
      : '<div style="color:var(--muted)">Nothing to clean up.</div>';
    // roadmap
    $('#debtRoadmap').innerHTML = d.prioritized_remediation_roadmap.phases.map(p =>
      '<div style="margin-bottom:8px"><b>' + esc(p.phase) + '</b> '
      + '<span class="badge ' + ({LOW:'ok', MEDIUM:'warn', HIGH:'bad'}[p.risk] || '') + '">' + esc(p.risk) + '</span>'
      + ' · ' + p.effort_hours + ' hr · $' + Math.round(p.monthly_savings_usd).toLocaleString() + '/mo'
      + '<div style="color:var(--muted);font-size:12px">' + esc(p.note) + '</div>'
      + '<div style="font-size:12px">' + p.items.map(i => esc(i.category) + ' (' + i.count + ')').join(', ') + '</div></div>').join('')
      || '<div style="color:var(--muted)">No remediation needed.</div>';
    $('#debtCoverage').textContent = d.coverage_note + ' ' + d.assumptions.note;
    $('#debtLinks').innerHTML = ['pdf', 'xlsx', 'json'].map(f =>
      '<a href="' + withKey('/api/tech-debt/' + d.debt_id + '/export?format=' + f) + '">'
      + {pdf: 'PDF report', xlsx: 'Excel cleanup workbook', json: 'JSON'}[f] + '</a>').join('');
    $('#debtResult').style.display = 'block';
  } catch (e) {
    $('#debtStatus').textContent = '';
    err.textContent = e.message; err.style.display = 'block';
  }
};

/* ---------------- Enterprise FinOps ---------------- */
$('#finRun').onclick = async () => {
  const err = $('#finErr'); err.style.display = 'none';
  $('#finStatus').textContent = 'Modeling FinOps…';
  try {
    const files = await Promise.all(pdropFiles('fin').map(f =>
      new Promise((res, rej) => {
        const r = new FileReader();
        r.onload = () => res({name: pdropPath(f), content: r.result});
        r.onerror = rej;
        r.readAsText(f);
      })));
    const body = {};
    if (files.length) body.files = files;
    const tel = ($('#finTelemetry').value || '').trim();
    if (tel) {
      try { body.telemetry = JSON.parse(tel); }
      catch (e) { throw new Error('Telemetry is not valid JSON: ' + e.message); }
    }
    const d = await api('/api/finops', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    $('#finStatus').textContent = '';
    const cc = d.current_cost, fc = d.future_cost, roi = d.roi;
    const card = (n, l, s) => '<div class="mcard" title="' + esc(s || '') + '"><div class="n">' + n
      + '</div><div class="l">' + esc(l) + '</div></div>';
    const usd = v => '$' + Math.round(v).toLocaleString();
    $('#finCards').innerHTML =
      card(usd(cc.monthly_usd) + '/mo', 'Current cost', usd(cc.annual_usd) + '/yr')
      + card(usd(fc.monthly_usd) + '/mo', 'Optimized cost', fc.reduction_pct + '% lower')
      + card(usd(fc.annual_savings_usd), 'Annual savings', '')
      + card(usd(d.migration_cost.one_time_usd), 'Migration cost', d.migration_cost.basis)
      + card(d.payback_period.months != null ? d.payback_period.months + ' mo' : 'N/A', 'Payback', d.payback_period.text)
      + card((roi.first_year_pct != null ? roi.first_year_pct + '%' : 'n/a') + ' / '
             + (roi.three_year_pct != null ? roi.three_year_pct + '%' : 'n/a'), 'ROI yr1 / 3yr', roi.note);
    // cost breakdown + levers
    $('#finCost').innerHTML = '<b>Current, by component ($/mo):</b><div style="margin:4px 0 8px">'
      + Object.entries(cc.by_component_monthly_usd).map(([k, v]) =>
        '<span style="display:inline-block;min-width:150px">' + esc(k.replace(/_/g, ' ')) + ': <b>' + usd(v) + '</b></span>').join('')
      + '</div><b>Savings levers ($/mo):</b><ul style="margin:4px 0 0 18px">'
      + fc.savings_levers.filter(l => l.monthly_usd > 0).map(l => '<li>' + esc(l.lever) + ' — <b>' + usd(l.monthly_usd) + '</b></li>').join('')
      + '</ul>';
    // analyses
    const an = d.analyses;
    const rows = [
      ['Warehouse utilization', an.warehouse_utilization.utilization_pct + '% — ' + an.warehouse_utilization.assessment],
      ['Cloud storage', usd(an.cloud_storage.monthly_usd) + '/mo · ' + an.cloud_storage.storage_gb + ' GB'],
      ['Streaming', usd(an.streaming_cost.monthly_usd) + '/mo · ' + an.streaming_cost.topics + ' topics'],
      ['Compute', usd(an.compute_cost.monthly_usd) + '/mo · ' + an.compute_cost.pipelines + ' pipelines'],
      ['Data movement', usd(an.data_movement.monthly_usd) + '/mo · ' + an.data_movement.cross_platform_hops + ' hops'],
      ['Idle resources', usd(an.idle_resources.monthly_usd) + '/mo (' + an.idle_resources.pct_of_current + '% of spend)'],
      ['Query history', usd(an.query_history.monthly_usd) + '/mo'],
      ['ETL runtime', an.etl_runtime.runtime_hours_month + ' hrs/mo'],
      ['Pipeline efficiency', an.pipeline_efficiency.efficiency_score + '/100 · ' + an.pipeline_efficiency.incremental_adoption_pct + '% incremental'],
    ];
    $('#finAnalyses').innerHTML = rows.map(([k, v]) =>
      '<div style="display:flex;gap:8px;margin-bottom:2px"><span style="width:160px;color:var(--muted)">' + esc(k) + '</span><span>' + esc(v) + '</span></div>').join('');
    // recommendations
    const rc = d.reserved_capacity_recommendations, sz = d.warehouse_sizing, cl = d.cluster_recommendations;
    $('#finRecs').innerHTML =
      '<div style="margin-bottom:6px"><b>Reserved capacity:</b> ' + esc(rc.mechanism) + ' — commit ~' + rc.recommended_commit_pct
      + '% of the steady baseline, save ~' + usd(rc.estimated_saving_usd_month) + '/mo (' + rc.estimated_saving_pct + '%). <span style="color:var(--muted)">' + esc(rc.note) + '</span></div>'
      + '<div style="margin-bottom:6px"><b>Warehouse sizing:</b> ' + esc(sz.recommended_size) + (sz.multi_cluster ? ' (multi-cluster)' : '') + ' — ' + esc(sz.basis) + '. <span style="color:var(--muted)">' + esc(sz.note) + '</span></div>'
      + '<div><b>Cluster:</b> autoscale ' + cl.min_workers + '–' + cl.max_workers + ' workers, auto-terminate ' + cl.auto_termination_min + ' min. '
      + cl.recommendations.map(esc).join('; ') + '</div>';
    // platform playbooks
    const plats = [['snowflake_optimization', 'Snowflake'], ['databricks_optimization', 'Databricks'], ['bigquery_optimization', 'BigQuery'], ['fabric_optimization', 'Microsoft Fabric']];
    $('#finPlatforms').innerHTML = plats.map(([key, label]) => {
      const p = d[key];
      return '<div style="margin-bottom:8px"><b>' + esc(label) + '</b> '
        + '<span class="badge ' + (p.in_estate ? 'ok' : '') + '">'
        + (p.in_estate ? 'in your estate' : 'target-state') + '</span>'
        + '<ul style="margin:4px 0 0 18px">' + p.recommendations.map(r => '<li>' + esc(r) + '</li>').join('') + '</ul></div>';
    }).join('');
    const basis = d.data_basis;
    $('#finBasis').textContent = 'Measured from telemetry: ' + (basis.measured_from_telemetry.join(', ') || 'none')
      + '. Modeled from metadata: ' + basis.modeled_from_metadata.join(', ') + '. ' + d.assumptions.note;
    $('#finLinks').innerHTML = ['pdf', 'xlsx', 'json'].map(f =>
      '<a href="' + withKey('/api/finops/' + d.finops_id + '/export?format=' + f) + '">'
      + {pdf: 'PDF report', xlsx: 'Excel model', json: 'JSON'}[f] + '</a>').join('');
    $('#finResult').style.display = 'block';
  } catch (e) {
    $('#finStatus').textContent = '';
    err.textContent = e.message; err.style.display = 'block';
  }
};

/* ---------------- Security & compliance intelligence ---------------- */
const SEC_FW_LABEL = {gdpr:'GDPR', hipaa:'HIPAA', pci_dss:'PCI-DSS', sox:'SOX', iso27001:'ISO 27001', nist_csf:'NIST CSF'};
function secBandColor(band) {
  return {'Strong':'var(--green)', 'Adequate':'var(--amber)', 'At risk':'var(--red)', 'Critical':'#8a2020'}[band] || 'var(--muted)';
}
function secGauge(score, band, label) {
  const r = 46, c = 2 * Math.PI * r, off = c * (1 - score / 100), col = secBandColor(band);
  return '<div style="text-align:center"><svg width="116" height="116" viewBox="0 0 116 116">'
    + '<circle cx="58" cy="58" r="' + r + '" fill="none" stroke="#e8ecf2" stroke-width="11"/>'
    + '<circle cx="58" cy="58" r="' + r + '" fill="none" stroke="' + col + '" stroke-width="11" stroke-linecap="round" stroke-dasharray="' + c
    + '" stroke-dashoffset="' + off + '" transform="rotate(-90 58 58)"/>'
    + '<text x="58" y="54" text-anchor="middle" font-size="27" font-weight="700" fill="#16233c">' + score + '</text>'
    + '<text x="58" y="72" text-anchor="middle" font-size="10" fill="' + col + '" font-weight="600">' + esc(band) + '</text></svg>'
    + '<div style="font-size:11.5px;color:var(--muted);font-weight:600">' + esc(label) + '</div></div>';
}
function secStatusChip(st) {
  const c = {met:'var(--green)', partial:'var(--amber)', gap:'var(--red)'}[st] || 'var(--muted)';
  return '<span style="padding:0 7px;border-radius:9px;font-size:10.5px;color:#fff;background:' + c + '">' + esc(st) + '</span>';
}

$('#secRun').onclick = async () => {
  const err = $('#secErr'); err.style.display = 'none';
  $('#secStatus').textContent = 'Analyzing security & compliance…';
  try {
    const files = await Promise.all(pdropFiles('sec').map(f =>
      new Promise((res, rej) => {
        const r = new FileReader();
        r.onload = () => res({name: pdropPath(f), content: r.result});
        r.onerror = rej;
        r.readAsText(f);
      })));
    const d = await api('/api/security', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(files.length ? {files} : {})});
    $('#secStatus').textContent = '';
    const ss = d.security_score, cs = d.compliance_score;
    $('#secGaugeS').innerHTML = secGauge(ss.score, ss.band, 'Security');
    $('#secGaugeC').innerHTML = secGauge(cs.score, cs.band, 'Compliance');
    $('#secHeadline').innerHTML = '<b>' + esc(ss.headline) + '</b>';
    // dimension bars
    $('#secDims').innerHTML = Object.entries(d.analyses).map(([k, v]) =>
      '<div style="display:flex;align-items:center;gap:10px;margin-bottom:3px" title="' + esc((v.findings || [])[0] || '') + '">'
      + '<span style="width:170px;font-size:12.5px">' + esc(k.replace(/_/g, ' ')) + '</span>'
      + '<div style="flex:1;max-width:320px;background:var(--line);border-radius:5px;height:14px">'
      + '<div style="width:' + v.score + '%;height:14px;border-radius:5px;background:' + secBandColor(v.level) + '"></div></div>'
      + '<span style="width:96px;font-size:12px;color:' + secBandColor(v.level) + '">' + v.score + ' · ' + esc(v.level) + '</span></div>').join('');
    // framework coverage
    $('#secFrameworks').innerHTML = Object.entries(cs.by_framework).map(([k, v]) => {
      const band = d.frameworks[k].band;
      return '<div style="display:flex;align-items:center;gap:10px;margin-bottom:3px">'
        + '<span style="width:100px;font-size:12.5px">' + esc(SEC_FW_LABEL[k] || k) + '</span>'
        + '<div style="flex:1;max-width:320px;background:var(--line);border-radius:5px;height:14px">'
        + '<div style="width:' + v + '%;height:14px;border-radius:5px;background:' + secBandColor(band) + '"></div></div>'
        + '<span style="width:60px;font-size:12px;color:' + secBandColor(band) + '">' + v + '%</span></div>';
    }).join('');
    // risk matrix
    $('#secRisks').innerHTML = d.risk_matrix.map(x =>
      '<div style="margin-bottom:6px"><span class="badge '
      + ({Critical:'bad', High:'bad', Medium:'warn', Low:'ok'}[x.severity] || '') + '">' + esc(x.severity) + '</span> '
      + '<b>' + esc(x.risk) + '</b> <span style="color:var(--muted)">(L:' + esc(x.likelihood) + ' × I:' + esc(x.impact) + ')</span>'
      + '<div style="color:var(--muted);font-size:12px">' + esc(x.evidence) + (x.frameworks.length ? ' — ' + x.frameworks.map(esc).join(', ') : '') + '</div></div>').join('');
    // controls
    $('#secControls').innerHTML = d.recommended_controls.length ? d.recommended_controls.map(c =>
      '<div style="margin-bottom:5px"><b>' + esc(c.priority) + '</b> ' + esc(c.control)
      + '<div style="color:var(--muted);font-size:12px">' + c.frameworks.map(esc).join(', ') + ' · effort ' + esc(c.effort) + '</div></div>').join('')
      : '<div style="color:var(--green)">No control gaps flagged in scanned metadata.</div>';
    // plans
    const mask = d.data_masking_plan, tok = d.tokenization_plan;
    $('#secPlans').innerHTML =
      '<div><b>Masking (' + mask.length + '):</b> ' + (mask.map(m => esc(m.object) + ' → ' + esc(m.technique)).join('; ') || '—') + '</div>'
      + '<div style="margin-top:4px"><b>Tokenization (' + tok.length + '):</b> ' + (tok.map(t => esc(t.object) + ' → ' + esc(t.technique)).join('; ') || '—') + '</div>'
      + '<div style="margin-top:4px"><b>Encryption:</b> ' + d.encryption_recommendations.at_rest.slice(0, 2).map(esc).join('; ') + '</div>';
    $('#secDisclaimer').textContent = d.audit_evidence.disclaimer;
    $('#secLinks').innerHTML = ['pdf', 'xlsx', 'json'].map(f =>
      '<a href="' + withKey('/api/security/' + d.security_id + '/export?format=' + f) + '">'
      + {pdf: 'PDF report', xlsx: 'Excel audit pack', json: 'JSON'}[f] + '</a>').join('');
    $('#secResult').style.display = 'block';
  } catch (e) {
    $('#secStatus').textContent = '';
    err.textContent = e.message; err.style.display = 'block';
  }
};

/* ---------------- Documentation generator ---------------- */
let docsCatalog = null;
const DOC_FORMATS = ['pdf', 'docx', 'md', 'html'];
const DOC_FMT_LABEL = {pdf: 'PDF', docx: 'Word', md: 'Markdown', html: 'HTML'};

async function loadDocsCatalog() {
  if (docsCatalog || !$('#docsChecklist')) return;
  try { docsCatalog = await api('/api/docs/catalog'); } catch (e) { return; }
  $('#docsChecklist').innerHTML = docsCatalog.documents.map(d =>
    '<label style="display:block;margin:2px 0;font-weight:400"><input type="checkbox" class="docChk" value="'
    + esc(d.slug) + '" checked style="width:auto"> ' + esc(d.title) + '</label>').join('');
  $('#docsFormats').innerHTML = DOC_FORMATS.map(f =>
    '<label style="margin:0 10px 0 0;font-weight:400"><input type="checkbox" class="docFmt" value="'
    + f + '" checked style="width:auto"> ' + DOC_FMT_LABEL[f] + '</label>').join('');
}

$('#docsRun').onclick = async () => {
  const err = $('#docsErr'); err.style.display = 'none';
  $('#docsStatus').textContent = 'Generating documentation…';
  try {
    const files = await Promise.all(pdropFiles('docs').map(f =>
      new Promise((res, rej) => {
        const r = new FileReader();
        r.onload = () => res({name: pdropPath(f), content: r.result});
        r.onerror = rej;
        r.readAsText(f);
      })));
    const documents = [...document.querySelectorAll('.docChk:checked')].map(c => c.value);
    const formats = [...document.querySelectorAll('.docFmt:checked')].map(c => c.value);
    if (!documents.length) throw new Error('Select at least one document.');
    if (!formats.length) throw new Error('Select at least one format.');
    if (!files.length) throw new Error('Choose your project folder first — documentation is generated from the uploaded project, never from empty or previously-cached data.');
    const body = {documents, formats, files};
    const d = await api('/api/docs', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    $('#docsStatus').textContent = '';
    const rows = d.documents.map(doc =>
      '<tr><td><b>' + esc(doc.title) + '</b></td><td>'
      + Object.keys(doc.files).map(f =>
          '<a href="' + withKey('/api/docs/' + d.docs_id + '/download?doc=' + encodeURIComponent(doc.slug)
          + '&format=' + f) + '">' + DOC_FMT_LABEL[f] + '</a>').join(' · ')
      + '</td></tr>').join('');
    $('#docsTable').innerHTML = '<tr><th>Document</th><th>Download</th></tr>' + rows;
    $('#docsResult').style.display = 'block';
  } catch (e) {
    $('#docsStatus').textContent = '';
    err.textContent = e.message; err.style.display = 'block';
  }
};

/* ---------------- Modernize: event & streaming modernization ------------ */
let evtId = null;

$('#evtAnalyze').onclick = async () => {
  const err = $('#evtErr'); err.style.display = 'none';
  $('#evtStatus').textContent = 'Analyzing…';
  try {
    const files = await Promise.all(mdropFiles('evt').map(f =>
      new Promise((res, rej) => {
        const r = new FileReader();
        r.onload = () => res({name: f.name, content: r.result});
        r.onerror = rej;
        r.readAsText(f);
      })));
    if (!files.length) throw new Error('Choose event platform export files first.');
    const d = await api('/api/events/analyze', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({files})});
    evtId = d.event_id;
    $('#evtStatus').textContent = '';
    const card = (n, l) => '<div class="mcard"><div class="n">' + n
      + '</div><div class="l">' + l + '</div></div>';
    $('#evtCards').innerHTML =
      card(esc(d.detected_platform), 'Detected platform')
      + card(d.topics.length, 'Topics')
      + card(d.queues.length, 'Queues')
      + card(d.consumers.length, 'Consumers')
      + card(d.producers.length, 'Producers')
      + card(d.streaming_jobs.length, 'Streaming jobs')
      + card(d.automation_score + '%', 'Automation score')
      + card(d.semantic_confidence + '%', 'Semantic confidence')
      + card(d.manual_review_items.length, 'Manual review');
    const li = (label, arr) => arr.length
      ? '<div><b>' + label + ':</b> ' + arr.slice(0, 12).map(esc).join(', ')
        + (arr.length > 12 ? ' …' : '') + '</div>' : '';
    $('#evtLists').innerHTML = li('Topics', d.topics) + li('Queues', d.queues)
      + li('Consumers', d.consumers) + li('Streaming jobs', d.streaming_jobs)
      + li('CDC sources', d.cdc_sources) + li('IoT sources', d.iot_sources)
      + '<div><b>Event flow:</b> ' + d.lineage.event_flow.length
      + ' edge(s) · validation ' + esc(d.validation_verdict.replace(/_/g, ' '))
      + '</div>';
    $('#evtResult').style.display = 'block';
  } catch (e) {
    $('#evtStatus').textContent = '';
    err.textContent = e.message; err.style.display = 'block';
  }
};

$('#evtIntel').onclick = async () => {
  const err = $('#evtErr'); err.style.display = 'none';
  $('#evtIntelStatus').textContent = 'Analyzing topology…';
  try {
    const d = await api('/api/events/intelligence', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({event_id: evtId})});
    $('#evtIntelStatus').textContent = '';
    // readiness dashboard
    $('#evtScoreCards').innerHTML = Object.entries(d.readiness).map(([k, v]) =>
      '<div class="mcard" title="' + esc(v.explanation) + '"><div class="n">'
      + v.value + '</div><div class="l">' + esc(k.replace(/_/g, ' '))
      + '</div></div>').join('');
    // topology graph: columns by node type
    const g = d.topology.execution_graph;
    const cols = ['producer', 'channel', 'transform', 'consumer'];
    const pos = {}, colY = [0, 0, 0, 0];
    g.nodes.forEach(n => {
      const ci = Math.max(0, cols.indexOf(n.type));
      pos[n.id] = {x: 20 + ci * 230, y: 16 + colY[ci] * 58};
      colY[ci]++;
    });
    const W = 190, H = 40;
    const width = 40 + cols.length * 230,
          height = 40 + Math.max(...colY, 1) * 58;
    const ecolor = {dead_letter: 'var(--red)', route: 'var(--amber)',
                    binding: 'var(--amber)', iot_rule: '#7a4bb8'};
    let svg = '<svg width="' + width + '" height="' + height + '" xmlns="http://www.w3.org/2000/svg">';
    g.edges.forEach(e => {
      const a = pos[e.from], b = pos[e.to];
      if (!a || !b) return;
      svg += '<path d="M' + (a.x + W) + ' ' + (a.y + H/2) + ' C ' + (a.x + W + 30)
        + ' ' + (a.y + H/2) + ', ' + (b.x - 30) + ' ' + (b.y + H/2) + ', '
        + b.x + ' ' + (b.y + H/2) + '" fill="none" stroke="'
        + (ecolor[e.kind] || '#8aa2c0') + '" stroke-width="1.5"'
        + (e.kind !== 'produce' && e.kind !== 'consume' ? ' stroke-dasharray="5,4"' : '') + '/>';
    });
    const ncolor = {producer: 'var(--green)', channel: '#2f6fb4',
                    transform: '#7a4bb8', consumer: 'var(--amber)'};
    g.nodes.forEach(n => {
      const p = pos[n.id];
      svg += '<g><rect x="' + p.x + '" y="' + p.y + '" width="' + W + '" height="' + H
        + '" rx="9" fill="var(--surface)" stroke="' + (ncolor[n.type] || 'var(--muted)') + '" stroke-width="1.4"/>'
        + '<text x="' + (p.x + W/2) + '" y="' + (p.y + 17) + '" text-anchor="middle" font-size="11" font-weight="600" fill="#16233c">'
        + esc(String(n.label).slice(0, 26)) + '</text>'
        + '<text x="' + (p.x + W/2) + '" y="' + (p.y + 31) + '" text-anchor="middle" font-size="9.5" fill="var(--muted)">'
        + esc(n.type + (n.partitions ? ' · ' + n.partitions + 'p' : '')
        + (n.delivery ? ' · ' + n.delivery.replace(/_/g, '-') : '')) + '</text></g>';
    });
    $('#evtTopoGraph').innerHTML = svg + '</svg>';
    // partition heatmap
    $('#evtHeatmap').innerHTML = d.partitions.channels.map(c => {
      const cells = [];
      for (let i = 0; i < Math.min(c.partitions, 48); i++)
        cells.push('<span style="display:inline-block;width:14px;height:14px;margin:1px;border-radius:3px;background:'
          + (i < c.consumers_in_group ? 'var(--green)' : '#e3e7ee') + '"></span>');
      return '<div style="margin-bottom:6px"><span style="display:inline-block;width:200px;font-size:12.5px">'
        + esc(c.channel) + '</span>' + cells.join('')
        + ' <span style="color:var(--muted);font-size:11.5px">' + c.idle_partitions
        + ' idle · key: ' + esc(c.key_quality) + '</span></div>';
    }).join('') || '<span style="color:var(--muted)">no partitioned channels</span>';
    // consumer coverage chart
    const groups = d.topology.consumer_groups;
    $('#evtCoverage').innerHTML = Object.entries(groups).map(([grp, members]) => {
      const chans = d.partitions.channels.filter(c => c.consumers_in_group > 0);
      const parts = chans.reduce((a, c) => a + c.partitions, 0) || 1;
      const pct = Math.min(100, Math.round(100 * members.length / parts));
      return '<div style="margin-bottom:6px;font-size:12.5px">' + esc(grp)
        + ' — ' + members.length + ' consumer(s) / ' + parts + ' partition(s)'
        + '<div style="background:var(--line);border-radius:6px;height:10px;max-width:420px;margin-top:3px">'
        + '<div style="width:' + pct + '%;height:10px;border-radius:6px;background:'
        + (pct >= 100 ? 'var(--green)' : pct >= 50 ? 'var(--amber)' : 'var(--red)') + '"></div></div></div>';
    }).join('') || '<span style="color:var(--muted)">no consumer groups in the import</span>';
    // schema evolution timeline
    $('#evtSchemas').innerHTML = d.schema_evolution.subjects.map(su => {
      const chips = su.versions.map((v, i) => {
        const ch = su.changes[i - 1];
        const broken = ch && ch.breaking_changes.length;
        return '<span class="badge ' + (broken ? 'bad' : 'ok') + '" style="margin:2px"'
          + (ch ? ' title="' + esc((ch.breaking_changes.join('; ')
            || ('added: ' + ch.added.join(', ')) || 'compatible')) + '"' : '')
          + '>v' + v + (broken ? ' ⚠' : '') + '</span>';
      }).join('<span style="color:#aab">→</span>');
      return '<div style="margin-bottom:6px"><b>' + esc(su.schema) + '</b> ('
        + esc(su.compatibility) + ') ' + chips + '</div>';
    }).join('') || '<span style="color:var(--muted)">no schemas in the import</span>';
    // recommendations
    const recsResp = await api('/api/events/' + evtId + '/recommendations');
    const section = (label, arr) => arr.length
      ? '<div><b>' + label + '</b><ul style="margin:4px 0 10px 18px">'
        + arr.map(x => '<li>' + esc(typeof x === 'string' ? x
          : (x.object || x.source || '') + ': '
            + (x.remediation || x.recommendation || '')) + '</li>').join('')
        + '</ul></div>' : '';
    $('#evtRecs').innerHTML =
      section('Partitions', recsResp.partitions)
      + section('Schema', recsResp.schema)
      + section('Event quality', recsResp.quality)
      + section('CDC', recsResp.cdc)
      + section('IoT', recsResp.iot)
      + section('Security', recsResp.security)
      || '<span style="color:var(--muted)">no recommendations</span>';
    $('#evtIntelPanel').style.display = 'block';
  } catch (e) {
    $('#evtIntelStatus').textContent = '';
    err.textContent = e.message; err.style.display = 'block';
  }
};

/* The chosen target's role, stated before the run rather than discovered
   in the artifacts: a sink or processor leaves the source broker in place. */
const EVT_TARGET_ROLE = {
  flink: 'processor', spark_streaming: 'processor',
  databricks_streaming: 'sink', snowflake_streaming: 'sink',
  dbt_streaming: 'sink', scaffold: 'spec',
};
const EVT_ROLE_TEXT = {
  processor: 'Stream processor — computes on top of the estate. Your source '
    + 'broker (or a broker replacement) must keep running to feed it.',
  sink: 'Analytics sink — lands events into tables. It has no publish/subscribe, '
    + 'so your source broker (or a replacement) must keep running to feed it.',
  spec: 'Specification artifact, not a running platform.',
};
function evtShowTargetRole() {
  const el = $('#evtTargetRole');
  if (!el) return;
  const role = EVT_TARGET_ROLE[$('#evtTarget').value] || 'broker';
  el.textContent = EVT_ROLE_TEXT[role]
    || 'Broker replacement — the source platform can be decommissioned after cutover.';
}
if ($('#evtTarget')) { $('#evtTarget').onchange = evtShowTargetRole; evtShowTargetRole(); }

$('#evtGenerate').onclick = async () => {
  const err = $('#evtErr'); err.style.display = 'none';
  $('#evtGenStatus').textContent = 'Generating…';
  try {
    const d = await api('/api/events/convert', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({event_id: evtId, target: $('#evtTarget').value})});
    $('#evtGenStatus').textContent = d.generated.length + ' file(s) · verdict '
      + d.validation_verdict.replace(/_/g, ' ')
      + ' · coverage ' + (d.automation_score != null ? d.automation_score + '%' : '--');
    // Surface what the target could NOT express. Reporting only the file
    // count and a green verdict is how a run that dropped a whole
    // streaming job still looked like a complete success.
    const skipped = d.unemitted || [], adj = d.adjustments || [];
    let warn = '';
    if (skipped.length || adj.length) {
      const li = (t, arr, f) => arr.length ? '<div style="margin-top:6px"><b>' + t
        + ' (' + arr.length + ')</b><ul style="margin:4px 0 0 18px">'
        + arr.slice(0, 8).map(f).join('')
        + (arr.length > 8 ? '<li>… ' + (arr.length - 8) + ' more</li>' : '')
        + '</ul></div>' : '';
      warn = '<div style="background:var(--accent-soft);border:1px solid var(--border);'
        + 'border-radius:8px;padding:10px 12px;margin-top:10px;font-size:12.5px">'
        + '<b>Read before deploying</b> — see <code>_metabridge_generation_notes.md</code> in the package.'
        + li('Not emitted', skipped,
             s => '<li><b>' + esc(s.kind) + '</b> <code>' + esc(s.name) + '</code> — ' + esc(s.reason) + '</li>')
        + li('Changed to fit the target', adj,
             n => '<li><code>' + esc(n.object) + '</code> — ' + esc(n.note) + '</li>')
        + '</div>';
    }
    $('#evtLinks').innerHTML =
      '<a href="' + withKey(d.download_url) + '">Download package</a>' + warn;
  } catch (e) {
    $('#evtGenStatus').textContent = '';
    err.textContent = e.message; err.style.display = 'block';
  }
};

/* ---------------- Modernize: object inventory & feasibility ------------- */
let objInvId = null, objRecords = [];
const OBJ_STATUS = {
  AUTOMATED: ['Automated', 'var(--green)'],
  PARTIAL: ['Needs review', 'var(--amber)'],
  MANUAL: ['Manual', 'var(--red)'],
  NO_EQUIVALENT: ['No equivalent', 'var(--ink3)'],
};

async function fillObjSelects() {
  try {
    await loadConnections(true);
    if (!allConnectors.length) allConnectors = (await api('/api/v1/connectors')).connectors || [];
    const live = allConnections.filter(x => {
      if (x.status !== 'active') return false;
      const spec = allConnectors.find(c => c.key === x.connector);
      return spec && spec.supports && spec.supports.introspect;
    });
    $('#objConn').innerHTML = live.length
      ? live.map(x => '<option value="' + x.id + '">' + esc(x.name)
          + ' (' + esc(x.connector) + ')</option>').join('')
      : '<option value="">No live connection yet - add one in Integrations</option>';
    /* Silently defaulted to `redshift` (the second option). Unlike a source
       default, the TARGET decides what every object is scored against — so a
       feasibility scan could be run against the wrong platform with nothing on
       screen suggesting a choice had been made. Require an explicit pick. */
    $('#objTarget').innerHTML = '<option value="">Choose a target platform…</option>'
      + groupedConnectorOptions(allConnectors
          .filter(c => c.dbt_adapter || c.category === 'cloud_dw' || c.category === 'lakehouse'), null);
  } catch (e) { /* selects stay empty; Run reports the real error */ }
}

function objRenderList() {
  const q = ($('#objFilter').value || '').toLowerCase();
  const rows = objRecords.filter(r => r.status !== 'AUTOMATED')
    .filter(r => !q || (r.name + ' ' + r.kind + ' ' + r.status + ' '
      + (r.schema || '') + ' ' + (r.language || '') + ' '
      + (r.target_equivalent || '')).toLowerCase().includes(q));
  $('#objList').innerHTML =
    '<tr><th>Kind</th><th>Object</th><th>Status</th><th>Target pattern</th>'
    + '<th>Why</th><th>Effort</th></tr>'
    + (rows.slice(0, 400).map(r => {
        const st = OBJ_STATUS[r.status] || [r.status, 'var(--ink3)'];
        return '<tr><td>' + esc(r.kind.replace(/_/g, ' ')) + '</td>'
          + '<td><code>' + esc((r.schema ? r.schema + '.' : '') + r.name) + '</code>'
          + (r.language ? ' <span style="color:var(--muted)">(' + esc(r.language) + ')</span>' : '')
          + '</td><td>' + badge(st[0], st[1]) + '</td>'
          + '<td>' + esc(r.target_equivalent || '') + '</td>'
          + '<td style="max-width:380px;font-size:12px">' + esc(r.reason || '') + '</td>'
          + '<td>' + r.effort_hours + 'h</td></tr>';
      }).join('') || '<tr><td colspan="6">Nothing matches.</td></tr>')
    + (rows.length > 400 ? '<tr><td colspan="6" style="color:var(--muted)">Showing 400 of '
        + rows.length + ' - refine the filter or open the full report.</td></tr>' : '');
}
$('#objFilter') && ($('#objFilter').oninput = () => objRenderList());

$('#objInvRun').onclick = async () => {
  const err = $('#objErr'); err.style.display = 'none';
  $('#objResult').style.display = 'none'; $('#objLinks').innerHTML = '';
  const cid = $('#objConn').value, target = $('#objTarget').value;
  if (!cid) { err.textContent = 'Connect a live system first (Integrations).';
    err.style.display = 'block'; return; }
  if (!target) { err.textContent = 'Choose the target platform to score against.';
    err.style.display = 'block'; $('#objTarget').focus(); return; }
  // The button used to stay enabled with unchanged text for the whole run — no
  // aria-busy, no spinner — so a second click queued a duplicate scan.
  const runBtn = $('#objInvRun');
  const runLabel = runBtn.textContent;
  runBtn.disabled = true; runBtn.setAttribute('aria-busy', 'true');
  runBtn.textContent = 'Running inventory…';
  $('#objStatus').textContent = 'Enumerating every object the connected role can see…';
  try {
    const d = await api('/api/objects/inventory', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({connection_id: cid, target})});
    objInvId = d.inventory_id; objRecords = d.records || [];
    $('#objStatus').textContent = '';
    const card = (n, l, dd) => '<div class="mcard"><div class="n">' + n
      + '</div><div class="l">' + l + '</div>'
      + (dd ? '<div class="d">' + esc(dd) + '</div>' : '') + '</div>';
    const bs = d.by_status || {};
    $('#objCards').innerHTML =
      card(d.objects_total, 'Objects', (d.database || '') + (d.schema ? ' / ' + d.schema : ''))
      + card((bs.AUTOMATED || 0), 'Automated', d.automation_pct + '% of estate')
      + card((bs.PARTIAL || 0), 'Needs review')
      + card((bs.MANUAL || 0), 'Manual port')
      + card((bs.NO_EQUIVALENT || 0), 'No equivalent')
      + card(d.estimated_effort_hours + 'h', 'Est. effort', 'deterministic per-object estimate');
    const kinds = d.kinds || {};
    $('#objKinds').innerHTML =
      '<tr><th>Kind</th><th>Count</th><th>Automated</th><th>Needs review</th>'
      + '<th>Manual</th><th>No equiv.</th><th>Target equivalent</th><th>Effort</th></tr>'
      + Object.keys(kinds).map(k => {
          const v = kinds[k];
          return '<tr><td><b>' + esc(k.replace(/_/g, ' ')) + '</b></td><td>' + v.count
            + '</td><td>' + v.AUTOMATED + '</td><td>' + v.PARTIAL + '</td><td>' + v.MANUAL
            + '</td><td>' + v.NO_EQUIVALENT + '</td><td style="font-size:12px">'
            + esc(v.target_equivalent || '') + '</td><td>' + v.effort_hours + 'h</td></tr>';
        }).join('');
    $('#objRevN').textContent = objRecords.filter(r => r.status !== 'AUTOMATED').length;
    objRenderList();
    const un = d.unreadable || [];
    if (un.length) {
      $('#objUnread').innerHTML = '<b>' + un.length + ' categor'
        + (un.length === 1 ? 'y' : 'ies') + ' not readable with this role</b> - the counts are a floor, not a total: '
        + un.map(u => esc(u.category)).join(', ')
        + '. Re-run with a role that can read them.';
      $('#objUnread').style.display = 'block';
    } else { $('#objUnread').style.display = 'none'; }
    $('#objLinks').innerHTML =
      '<a href="' + withKey(d.report_html_url + '&inline=true') + '" target="_blank">View full report</a>'
      + '<a href="' + withKey(d.report_json_url) + '">Download JSON</a>';
    /* An inventory that read NOTHING used to render as a clean success: six
       zeroed cards, "Nothing matches.", job status Passed, and Generate
       migration package still enabled — which reads as "this estate has
       nothing to migrate" rather than "we could not see anything". The usual
       cause is a connection with no schema set (the API returns schema: "")
       and it is not something the user can spot from zeros. `unreadable`
       above only covers a PARTIAL read, so it never fired here. */
    const empty = !d.objects_total;
    $('#objGen').disabled = empty;
    if (empty) {
      const noSchema = !d.schema;
      $('#objEmpty').innerHTML = '<b>No objects were readable, so there is nothing to score.</b>'
        + (noSchema
            ? ' This connection has no schema set, so the scan had nothing to enumerate —'
              + ' set a schema on the connection under Integrations, then re-run.'
            : ' The schema <code>' + esc(d.schema) + '</code> in <code>' + esc(d.database || '')
              + '</code> returned no objects the connected role can see. Check the role’s'
              + ' grants, or pick a different schema.')
        + ' This is not the same as an empty estate.';
      $('#objEmpty').style.display = 'block';
      $('#objGen').title = 'Nothing to package — the inventory is empty';
    } else {
      $('#objEmpty').style.display = 'none';
      if ($('#objGen').title.startsWith('Nothing to package')) $('#objGen').title = '';
    }
    $('#objResult').style.display = 'block';
  } catch (e) {
    $('#objStatus').textContent = '';
    err.textContent = e.message; err.style.display = 'block';
  } finally {
    runBtn.disabled = false; runBtn.removeAttribute('aria-busy');
    runBtn.textContent = runLabel;
    applyRbacUi();          // re-assert role gating after restoring the button
  }
};

$('#objGen').onclick = async () => {
  if (!objInvId) return;
  const err = $('#objErr'); err.style.display = 'none';
  $('#objGenStatus').textContent = 'Generating…';
  try {
    const d = await api('/api/objects/convert', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({inventory_id: objInvId})});
    $('#objGenStatus').innerHTML = d.files.length + ' file(s) · '
      + d.views_converted + ' view(s) converted'
      + (d.views_manual ? ' · ' + d.views_manual + ' view(s) in the manual queue' : '')
      + ' · ' + d.manual_objects + ' object(s) need a human';
    $('#objLinks').innerHTML +=
      '<a href="' + withKey(d.download_url) + '">Download migration package</a>';
  } catch (e) {
    $('#objGenStatus').textContent = '';
    err.textContent = e.message; err.style.display = 'block';
  }
};

/* ---------------- Pipeline Studio: data movement settings ---------------- */
async function loadMovementSettings() {
  try {
    const d = await api('/api/settings/movement');
    $('#mvStageUri').value = d.stage_uri || '';
    $('#mvRegion').value = d.region || '';
    $('#mvSourceStage').value = d.source_stage || '';
    $('#mvSourceCredential').value = d.source_credential || '';
    $('#mvTargetStage').value = d.target_stage || '';
    $('#mvIamRole').value = d.iam_role || '';
    const set = !!(d.stage_uri || d.iam_role || d.source_stage || d.region
                   || d.source_credential || d.target_stage);
    // A status badge on the collapsed head, so "will my scripts come out with
    // <placeholders>?" is answerable without opening the section.
    const st = $('#mvState');
    st.className = 'badge ' + (set ? 'ok' : 'warn');
    st.textContent = set ? 'Configured' : 'Not set';
    st.title = set ? 'Generated scripts come out fully substituted'
                   : 'Generated scripts will carry <placeholders>';
  } catch (e) { /* panel stays usable; Save reports the real error */ }
}

/* The settings are usually left alone after the first run — collapsed by
   default, with the badge above carrying the only bit that matters closed. */
$('#mvHead').onclick = () => {
  const body = $('#mvBody'), head = $('#mvHead');
  const open = body.hidden;
  body.hidden = !open;
  head.setAttribute('aria-expanded', String(open));
};
/* The From/To chips name the systems these fields actually move between,
   instead of leaving the reader to hold the selects above in their head. */
function syncMvChips() {
  [['#scafSource', '#mvFromChip'], ['#scafTarget', '#mvToChip']].forEach(([s, c]) => {
    const sel = $(s), chip = $(c);
    if (!sel || !chip) return;
    const opt = sel.selectedOptions[0];
    if (opt) chip.textContent = opt.text;
  });
}

$('#mvSave').onclick = async () => {
  $('#mvStatus').textContent = 'Saving…';
  try {
    await api('/api/settings/movement', {method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({stage_uri: $('#mvStageUri').value.trim(),
                            region: $('#mvRegion').value.trim(),
                            source_stage: $('#mvSourceStage').value.trim(),
                            source_credential: $('#mvSourceCredential').value.trim(),
                            target_stage: $('#mvTargetStage').value.trim(),
                            iam_role: $('#mvIamRole').value.trim()})});
    $('#mvStatus').textContent = 'Saved - the next Generate uses these values.';
    loadMovementSettings();
  } catch (e) { $('#mvStatus').textContent = e.message; }
};

/* ---------------- Pipeline Studio: orchestration modernization ---------- */
let orchId = null, orchGraphs = null, orchResil = null;

async function orchReadFiles() {
  const files = mdropFiles('orch');
  if (!files.length) throw new Error('Choose orchestration export files first.');
  return Promise.all(files.map(f => new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res({name: f.name, content: r.result});
    r.onerror = rej;
    r.readAsText(f);
  })));
}

$('#orchAnalyze').onclick = async () => {
  const err = $('#orchErr'); err.style.display = 'none';
  $('#orchStatus').textContent = 'Analyzing…';
  try {
    const files = await orchReadFiles();
    const d = await api('/api/orchestration/analyze', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({files})});
    orchId = d.orchestration_id;
    orchResil = d.resilience || null;
    $('#orchStatus').textContent = '';
    const card = (n, l, dd) => '<div class="mcard"><div class="n">' + n
      + '</div><div class="l">' + l + '</div>'
      + (dd ? '<div class="d">' + esc(dd) + '</div>' : '') + '</div>';
    $('#orchCards').innerHTML =
      card(esc(d.detected_platform), 'Detected platform')
      + card(d.workflows.length, 'Workflows')
      + card(d.tasks_total, 'Tasks')
      + card(d.dependencies_total, 'Dependencies')
      + card(d.automation_score + '%', 'Automation score')
      + card(esc(d.validation_verdict.replace(/_/g, ' ')), 'Validation',
             d.manual_review_items.length + ' manual review item(s)')
      + (orchResil && orchResil.resilience_score !== undefined
         ? card(orchResil.resilience_score + '/100 ' + esc(orchResil.grade),
                'Runtime resilience',
                orchResil.findings.length + ' runtime risk(s)')
         : '');
    const g = await api('/api/orchestration/' + orchId + '/graph');
    orchGraphs = g.workflows;
    $('#orchWfSel').innerHTML = Object.keys(orchGraphs).map(w =>
      '<option>' + esc(w) + '</option>').join('');
    orchRenderWorkflow();
    $('#orchResult').style.display = 'block';
  } catch (e) {
    $('#orchStatus').textContent = '';
    err.textContent = e.message; err.style.display = 'block';
  }
};
$('#orchWfSel') && ($('#orchWfSel').onchange = () => orchRenderWorkflow());

function orchRenderWorkflow() {
  const wf = $('#orchWfSel').value;
  const g = orchGraphs[wf].graph;
  orchRenderSvg(g);
  const rows = g.edges.map((e, i) =>
    '<tr><td>' + esc(e.from) + '</td><td>' + esc(e.to) + '</td>'
    + '<td>' + esc(e.kind) + (e.condition ? ' <span style="color:var(--muted)">'
    + esc(String(e.condition).slice(0, 60)) + '</span>' : '') + '</td>'
    + '<td><a data-rmdep="' + i + '" style="color:var(--red);cursor:pointer;'
    + 'font-size:12.5px">remove</a></td></tr>').join('');
  $('#orchDepTable').innerHTML =
    '<tr><th>From</th><th>To</th><th>Kind</th><th></th></tr>' + rows;
  const opts = g.nodes.map(n => '<option>' + esc(n.id) + '</option>').join('');
  $('#orchDepFrom').innerHTML = opts;
  $('#orchDepTo').innerHTML = opts;
  $('#orchDepTable').querySelectorAll('[data-rmdep]').forEach(a =>
    a.onclick = () => orchEditDeps(wf,
      {remove: [{from: g.edges[+a.dataset.rmdep].from,
                 to: g.edges[+a.dataset.rmdep].to}]}));
  orchRenderResilience(wf);
}

/* Critical path, blast radius and cutover risk for one workflow — the
   operational read a migration lead needs before signing off a cutover. */
function orchRenderResilience(wf) {
  const box = $('#orchResil');
  if (!box) return;
  if (!orchResil || !orchResil.workflows) { box.style.display = 'none'; return; }
  const r = orchResil.workflows.find(x => x.workflow === wf);
  if (!r) { box.style.display = 'none'; return; }
  const mine = (orchResil.findings || []).filter(f => f.workflow === wf);
  const stat = (l, v, d) => '<div style="min-width:150px">'
    + '<div style="font-size:11px;letter-spacing:.4px;text-transform:uppercase;'
    + 'color:var(--ink3)">' + l + '</div>'
    + '<div style="font-size:15px;font-weight:650;color:var(--ink)">' + v + '</div>'
    + (d ? '<div style="font-size:11.5px;color:var(--ink3)">' + d + '</div>' : '')
    + '</div>';
  let html = '<div class="panel" style="margin:0;padding:14px">'
    + '<div style="display:flex;justify-content:space-between;align-items:center;'
    + 'flex-wrap:wrap;gap:8px"><b style="font-size:13.5px">Runtime resilience — '
    + esc(wf) + '</b>'
    + badge(orchResil.resilience_score + '/100 · grade ' + esc(orchResil.grade),
            orchResil.resilience_score >= 90 ? 'var(--green)'
            : orchResil.resilience_score >= 75 ? 'var(--amber)' : 'var(--red)')
    + '</div>'
    + '<div style="display:flex;gap:22px;flex-wrap:wrap;margin-top:12px">'
    + stat('Critical path', r.critical_path_length + ' of ' + r.tasks + ' tasks',
           (r.critical_path_declared_timeout_seconds
            ? (r.critical_path_unbounded_tasks ? '\u2265 ' : '')
              + r.critical_path_declared_timeout + ' declared'
            : 'no timeouts declared')
           + (r.critical_path_unbounded_tasks
              ? ' \u00b7 ' + r.critical_path_unbounded_tasks + ' unbounded' : ''))
    + stat('Sequential stages', r.serial_stages,
           'parallelism ' + r.parallelism_ratio + '×')
    + stat('Widest parallel step', r.max_parallel_width + ' task(s)',
           r.schedule_interval_seconds
             ? 'every ' + Math.round(r.schedule_interval_seconds / 60) + ' min' : '')
    + '</div>';
  if (r.critical_path.length) {
    html += '<div style="margin-top:12px;font-size:12.5px;color:var(--ink3)">'
      + 'Longest chain</div><div style="font-size:12.5px;margin-top:3px">'
      + r.critical_path.map(k => '<code>' + esc(k) + '</code>').join(' → ')
      + '</div>';
  }
  if ((r.single_points_of_failure || []).length) {
    html += '<div style="margin-top:14px;font-size:12.5px;color:var(--ink3)">'
      + 'Single points of failure — one failure blocks:</div>'
      + '<table style="margin-top:6px"><tr><th>Task</th><th>Type</th>'
      + '<th>Blocks downstream</th><th>Retries</th><th>Timeout</th></tr>'
      + r.single_points_of_failure.map(s => '<tr><td><code>' + esc(s.task)
        + '</code></td><td>' + esc(s.type) + '</td><td>' + s.blocks_downstream
        + ' task(s)</td><td>' + (s.retries
            ? s.retries : badge('none', 'var(--red)'))
        + '</td><td>' + (s.has_timeout ? 'set'
            : badge('unbounded', 'var(--amber)')) + '</td></tr>').join('')
      + '</table>';
  }
  if (mine.length) {
    html += '<div style="margin-top:14px">' + mine.map(f =>
      '<div style="padding:8px 0;border-top:1px solid var(--border)">'
      + badge(f.severity, SEV_COLOR[f.severity] || 'var(--ink3)')
      + ' <b style="font-size:12.5px">' + esc(f.code) + '</b>'
      + '<div style="font-size:12.5px;margin-top:3px">' + esc(f.message) + '</div>'
      + (f.suggestion ? '<div style="font-size:12px;color:var(--ink3);'
         + 'margin-top:2px">' + esc(f.suggestion) + '</div>' : '')
      + '</div>').join('') + '</div>';
  }
  if ((orchResil.cutover_checklist || []).length) {
    html += '<details style="margin-top:12px"><summary style="font-size:13px;'
      + 'color:var(--accent);cursor:pointer;font-weight:600">Cutover checklist ('
      + orchResil.cutover_checklist.length + ')</summary><ul style="margin:8px 0 0 18px;'
      + 'font-size:12.5px;line-height:1.75">'
      + orchResil.cutover_checklist.map(c => '<li>' + esc(c) + '</li>').join('')
      + '</ul></details>';
  }
  box.innerHTML = html + '</div>';
  box.style.display = 'block';
}

function orchRenderSvg(g) {
  const X = 190, Y = 62, W = 160, H = 40;
  const pos = {};
  g.waves.forEach((wave, wi) => wave.forEach((k, i) => {
    pos[k] = {x: 20 + wi * X, y: 16 + i * Y};
  }));
  const width = 40 + g.waves.length * X, height = 40 + Math.max(
    ...g.waves.map(w => w.length), 1) * Y;
  const color = {failure: 'var(--red)', conditional: 'var(--amber)',
                 always: 'var(--ink3)', event: '#7a4bb8'};
  let svg = '<svg width="' + width + '" height="' + height
    + '" xmlns="http://www.w3.org/2000/svg" style="font-family:inherit">';
  g.edges.forEach(e => {
    const a = pos[e.from], b = pos[e.to];
    if (!a || !b) return;
    svg += '<path d="M' + (a.x + W) + ' ' + (a.y + H / 2) + ' C '
      + (a.x + W + 40) + ' ' + (a.y + H / 2) + ', ' + (b.x - 40) + ' '
      + (b.y + H / 2) + ', ' + b.x + ' ' + (b.y + H / 2)
      + '" fill="none" stroke="' + (color[e.kind] || '#8aa2c0')
      + '" stroke-width="1.6"'
      + (e.kind !== 'success' ? ' stroke-dasharray="5,4"' : '') + '/>';
  });
  g.nodes.forEach(n => {
    const p = pos[n.id];
    if (!p) return;
    svg += '<g><rect x="' + p.x + '" y="' + p.y + '" width="' + W
      + '" height="' + H + '" rx="9" fill="#f6f9fd" stroke="#2f6fb4"'
      + ' stroke-width="1.2"/>'
      + '<text x="' + (p.x + W / 2) + '" y="' + (p.y + 17)
      + '" text-anchor="middle" font-size="11.5" font-weight="600" '
      + 'fill="#16233c">' + esc(String(n.label).slice(0, 22)) + '</text>'
      + '<text x="' + (p.x + W / 2) + '" y="' + (p.y + 31)
      + '" text-anchor="middle" font-size="10" fill="var(--muted)">'
      + esc(n.type) + (n.retries ? ' · ' + n.retries + ' retries' : '')
      + '</text></g>';
  });
  $('#orchGraph').innerHTML = svg + '</svg>';
}

async function orchEditDeps(wf, body) {
  const err = $('#orchErr'); err.style.display = 'none';
  try {
    await api('/api/orchestration/' + orchId + '/dependencies',
      {method: 'POST', headers: {'Content-Type': 'application/json'},
       body: JSON.stringify({workflow: wf, ...body})});
    const g = await api('/api/orchestration/' + orchId + '/graph');
    orchGraphs = g.workflows;
    orchRenderWorkflow();
  } catch (e) { err.textContent = e.message; err.style.display = 'block'; }
}
$('#orchDepAdd').onclick = () => orchEditDeps($('#orchWfSel').value, {
  add: [{from: $('#orchDepFrom').value, to: $('#orchDepTo').value,
         kind: $('#orchDepKind').value}]});

$('#orchGenerate').onclick = async () => {
  const err = $('#orchErr'); err.style.display = 'none';
  $('#orchGenStatus').textContent = 'Generating…';
  try {
    const d = await api('/api/orchestration/convert', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({orchestration_id: orchId,
                            target: $('#orchTarget').value})});
    $('#orchGenStatus').textContent = d.generated.length
      + ' file(s) · verdict ' + d.validation_verdict;
    $('#orchLinks').innerHTML =
      '<a href="' + withKey(d.download_url) + '">Download package</a>'
      + '<a href="' + withKey('/api/orchestration/' + d.orchestration_id
      + '/report?format=md') + '" target="_blank">Execution documentation</a>';
  } catch (e) {
    $('#orchGenStatus').textContent = '';
    err.textContent = e.message; err.style.display = 'block';
  }
};

// The governance report is OPT-OUT. Source/target region only feed its
// residency check, so the region row is shown only while it is enabled —
// asking for regions you don't need is friction, not governance.
function syncScafGovern() {
  const on = $('#scafGovern').checked;
  $('#scafRegionRow').style.display = on ? '' : 'none';
  // the footer says what pressing Generate will actually produce
  $('#scafHint').textContent = on ? 'Pipelines and a governance report' : 'Pipelines only';
}
$('#scafGovern').onchange = syncScafGovern;
syncScafGovern();
$('#scafSource').onchange = syncMvChips;
$('#scafTarget').onchange = syncMvChips;

$('#scafForm').onsubmit = async ev => {
  ev.preventDefault();
  const err = $('#scafErr'); err.style.display = 'none'; $('#scafResult').style.display = 'none';
  $('#scafBanner').style.display = 'none';
  if (!chosen.scafFile) { err.textContent = 'Choose a tables .yml first.'; err.style.display = 'block'; return; }
  const fd = new FormData(ev.target); fd.set('tables', chosen.scafFile);
  // an empty file part still arrives as an upload with a blank filename, so
  // only attach it when one was actually chosen
  if (chosen.scafEtlFile) fd.set('etl', chosen.scafEtlFile); else fd.delete('etl');
  const govOn = $('#scafGovern').checked;
  fd.set('governance', govOn ? 'true' : 'false');
  if (!govOn) { fd.delete('source_region'); fd.delete('target_region'); }
  // if a saved connection exists for the chosen source/target connector,
  // pass its id so generated dbt/IDMC/PowerCenter connection artifacts carry
  // that system's real connection settings (secrets stay as env-var refs)
  try { await loadConnections(true); } catch (e) { /* offline: env-var refs */ }
  const connFor = k => { const m = allConnections.filter(x => x.connector === k);
    return (m.find(x => x.state === 'connected') || m[0] || {}).id; };
  const sc = connFor($('#scafSource').value), tc = connFor($('#scafTarget').value);
  if (sc) fd.set('source_conn', sc);
  if (tc) fd.set('target_conn', tc);
  // say plainly which side will carry real settings and which falls back to
  // env-var placeholders, instead of shipping a half-filled artifact silently
  $('#scafConnNote').innerHTML = [['Source', sc, $('#scafSource')],
                                  ['Target', tc, $('#scafTarget')]]
    .map(([lbl, id, sel]) => {
      const nm = esc(sel.selectedOptions[0].text);
      const c = id && allConnections.find(x => x.id === id);
      return c ? lbl + ': <b>' + nm + '</b> \u2192 settings from saved '
                 + 'connection <code>' + esc(c.name) + '</code>'
               : lbl + ': <b>' + nm + '</b> \u2192 no saved connection, '
                 + 'artifacts use env-var placeholders';
    }).join('<br>');
  $('#scafConnNote').style.display = 'block';
  // by id, not querySelector('button') — the first button in this form is
  // "Save settings" inside the movement card, which is what used to get
  // disabled and relabelled "Generating…" on every run
  const btn = $('#scafGenerate'); btn.disabled = true; btn.textContent = 'Generating…';
  try {
    const d = await api('/api/scaffold', {method:'POST', body: fd});
    const banner = $('#scafBanner');
    banner.innerHTML = '<strong>&#10003; Scaffold created</strong> — job <code>' + d.id
      + '</code> · project <strong>' + esc(d.project || '') + '</strong> · '
      + esc($('#scafSource').selectedOptions[0].text) + ' &rarr; ' + esc($('#scafTarget').selectedOptions[0].text)
      + ' · ' + esc(d.created || '')
      + '<div style="margin-top:6px;font-size:13px"><strong>Scaffolded models:</strong> '
      + (d.pipelines || []).map(esc).join(', ')
      + '</div><div style="font-size:13px"><strong>Generated:</strong> '
      + (d.artifacts || []).map(esc).join(' · ') + '</div>'
      + ((d.manifest_notes && d.manifest_notes.length) ? '<div style="font-size:13px;color:var(--ink3)">' + d.manifest_notes.map(esc).join('; ') + '</div>' : '')
      // What the supplied ETL contributed, and — the part worth seeing — which
      // tables were therefore NOT landed. Silently dropping a table from the
      // landing layer is the one thing here a user must be told about.
      + (d.etl ? '<div style="margin-top:6px;font-size:13px"><strong>'
         + esc(d.etl.label || 'ETL') + ' logic:</strong> '
         + esc(String(d.etl.converted || 0)) + ' mapping(s) converted into the '
         + 'curated layer'
         + ((d.etl.not_landed && d.etl.not_landed.length)
            ? '<div style="color:var(--ink3)">Not landed (built by this logic, '
              + 'so not also copied as raw): '
              + d.etl.not_landed.map(t => '<code>'
                  + esc((t.schema ? t.schema + '.' : '') + t.table)
                  + '</code>').join(', ') + '</div>'
            : '')
         + ((d.etl.unresolved_sources && d.etl.unresolved_sources.length)
            ? '<div style="color:var(--warn,#b45309)">Reads tables the manifest '
              + 'does not carry: ' + d.etl.unresolved_sources.map(esc).join(', ')
              + '</div>' : '')
         + '</div>' : '')
      // dbt transforms inside the target; it cannot read the source. Say so
      // here, because it is the step users skip and then hit "relation does
      // not exist" on the first model.
      + ((d.ddl && d.ddl.tables) ? '<div style="margin-top:6px;font-size:13px">'
         + '<strong>Run <code>ddl/</code> first:</strong> landing DDL + bulk '
         + 'export/import for ' + d.ddl.tables + ' table(s). dbt transforms '
         + 'inside ' + esc($('#scafTarget').selectedOptions[0].text)
         + ' \u2014 it does not read '
         + esc($('#scafSource').selectedOptions[0].text)
         + ', so the tables must land there first. See <code>ddl/README.md'
         + '</code>.</div>' : '')
      // Stored-procedure logic: say what became a model AND what did not, so
      // "procedures: 12" is never read as "all the logic came across".
      + (d.procedures ? '<div style="margin-top:6px;font-size:13px">'
         + '<strong>Stored-procedure logic:</strong> ' + d.procedures.analyzed
         + ' analyzed, ' + d.procedures.with_models + ' produced '
         + d.procedures.models + ' transformation model(s)'
         + (d.procedures.statements_not_converted
            ? ' — ' + d.procedures.statements_not_converted
              + ' statement(s) (cursors, dynamic SQL, control flow) need a '
              + 'human' : '')
         + (d.procedures.skipped ? '; ' + d.procedures.skipped
            + ' not analyzed' : '')
         + '. See <code>procedures/PROCEDURE_LOGIC.md</code>.</div>' : '')
      + (d.governance_enabled === false ? '<div style="font-size:13px;color:var(--ink3)">Governance report skipped — pipelines only.</div>' : '')
      + '<div style="font-size:13px;color:var(--ink3)">Also listed under Dashboard &rarr; recent jobs. Use the links below to open the reports or download everything.</div>';
    banner.style.display = 'block';
    // with governance opted out there are no classification/violation numbers
    // to show — report that honestly instead of printing a misleading 0
    $('#scafCards').innerHTML = card(d.summary.objects_total,'Scaffolded models')
      + card((d.artifacts || []).filter(a => /\/\s*\(/.test(a)).length || '3','Stacks emitted','var(--accent)')
      + (d.governance
        ? card(d.governance.classified_columns,'Classified columns')
          + card(d.governance.violations,'Violations', d.governance.violations ? 'var(--red)' : 'var(--green)')
        : card('--','Classified columns','var(--muted)')
          + card('--','Violations','var(--muted)'));
    $('#scafReport').href = withKey(d.report_url);
    const govLink = $('#scafGov');
    if (d.gov_report_url) { govLink.href = withKey(d.gov_report_url); govLink.style.display = ''; }
    else { govLink.removeAttribute('href'); govLink.style.display = 'none'; }
    $('#scafDownload').href = withKey(d.download_url);
    $('#scafResult').style.display = 'block'; loadDashboard();
  } catch (e) { err.textContent = e.message; err.style.display = 'block'; }
  finally { btn.disabled = false; btn.textContent = 'Generate pipelines'; }
};

/* ---------------- Data Estate ---------------- */
// Estate statistics, search and assets are ALWAYS scoped to the selected
// system. The page LANDS on "All systems" (the aggregate, from stored stats);
// choosing a single system is what triggers live introspection. Selecting the
// explicit "Select a system…" blank returns to the selection-required state,
// where Search is hidden — never a stale all-systems total.
let estateSystem = 'all';
let estateSummary = [];
async function loadEstate() {
  loadTwin();
  // loadConnections(true) re-throws on a genuine API failure (see its own
  // force-flag branch) — that must not be folded into an empty array here,
  // or a broken backend renders as a fully-formed, entirely empty estate
  // with nothing to tell the two states apart.
  try { await loadConnections(true); }
  catch (e) { loadErr('estateErr', e, loadEstate); return; }
  if (!allConnectors.length) { try { allConnectors = (await api('/api/v1/connectors')).connectors || []; } catch (e) {} }
  const startable = allConnections.filter(x => x.status === 'active');
  // only offer systems that can actually be introspected live
  const introspectable = startable.filter(x => {
    const s = allConnectors.find(c => c.key === x.connector);
    return !s || !s.supports || s.supports.introspect !== false;
  });
  // "All systems" is the DEFAULT landing state: it is the only option that says
  // something useful before you know what is here, and the aggregate is served
  // from stored stats (no live introspection), so defaulting to it costs one
  // cheap call rather than hitting a source system uninvited. Picking a single
  // system — which does introspect live — stays an explicit act.
  $('#estateSystem').innerHTML = '<option value="all">All systems (aggregate)</option>'
    + '<option value="">Select a system…</option>'
    + introspectable.map(x => '<option value="' + x.id + '">' + esc(x.name) + '</option>').join('');
  $('#estateSystem').value = 'all';
  $('#estateSystem').onchange = ev => estateSelect(ev.target.value);
  $('#estateSearch').oninput = () => estateReRender(true);
  await estateSelect('all');
}

async function estateSelect(id) {
  estateSystem = id;
  window._estate = null;
  window._estatePicker = null;      // {id, d} while a database list is in play
  window._estateDbName = '';        // the database we drilled into
  window._estateFilter = '';        // Level 3: the active object type
  window._estateView = '';          // 'databases' | 'objects'
  $('#estateSchema').style.display = 'none';
  $('#estateContext').innerHTML = '';
  estateSummary = [];
  // Search is gated on a selection and cleared whenever the system changes, so
  // results never carry over from a previously selected system. It stays
  // HIDDEN until a system is picked — an always-visible box that does nothing
  // reads as broken, and there is nothing to search across before then.
  const search = $('#estateSearch');
  search.value = '';
  search.disabled = !id;
  search.style.display = id ? '' : 'none';
  search.placeholder = id === 'all' ? 'Search all systems…' : 'Search this system…';
  estatePage = 1;
  let stats = null;
  try { stats = await api('/api/estate/stats?system=' + encodeURIComponent(id)); }
  catch (e) { stats = null; }
  if (estateSystem !== id) return;   // a newer selection superseded this one
  renderEstateCards(stats);
  const table = $('#estateTable');
  $('#estatePager').style.display = 'none';
  if (!id) {
    $('#estateHint').textContent = 'Pick a system to explore its assets (read-only), or “All systems” for the aggregate.';
    table.innerHTML = '<tr><td style="color:var(--muted)">Select a system to view its statistics and assets.</td></tr>';
    return;
  }
  if (stats && stats.known === false) {          // unknown / removed system
    $('#estateHint').textContent = stats.message || 'Unknown system.';
    table.innerHTML = '<tr><td style="color:var(--red)">' + esc(stats.message || 'Unknown system.') + '</td></tr>';
    return;
  }
  if (id === 'all') {
    $('#estateHint').textContent = 'Aggregate across all systems — pick a single system for live asset browsing.';
    estateSummary = (stats && stats.systems) || [];
    renderEstateSummaryTable();
    return;
  }
  // a specific system: its live assets only
  $('#estateHint').textContent = '';
  table.innerHTML = '<tr><td style="color:var(--muted)">Analyzing metadata (read-only)…</td></tr>';
  try {
    const d = await api('/api/v1/connections/' + id + '/introspect', {method:'POST'});
    if (!d.ok) throw new Error(d.error || 'analysis failed');
    if (estateSystem !== id) return;             // selection changed mid-flight
    if (d.mode === 'databases') {
      // Level 1: this connection has no database set, so there is nothing to
      // inventory yet — offer the databases it CAN see instead of an error.
      window._estatePicker = {id: id, d: d};
      window._estateView = 'databases';
      renderDatabaseList();
    } else {
      window._estate = d;
      window._estateView = 'objects';
      populateSchemaFilter(d);
      renderEstateTable();
    }
  } catch (e) {
    if (estateSystem === id) table.innerHTML = '<tr><td style="color:var(--red)">' + esc(e.message) + '</td></tr>';
  }
}

// Level 1 — the database picker. Reuses the object table's column shape so
// the layout never jumps between the two views.
function renderDatabaseList() {
  const p = window._estatePicker;
  if (!p) return;
  const q = ($('#estateSearch').value || '').toLowerCase();
  $('#estateSchema').style.display = 'none';
  $('#estatePager').style.display = 'none';
  const ctx = p.d.context || {};
  $('#estateContext').innerHTML = estateRoleBanner(ctx)
    + '<div style="font-size:12.5px;color:var(--ink3);margin:2px 0 12px">'
    + 'No database is set on this connection — pick one to explore its objects.</div>';
  const dbs = (p.d.databases || []).filter(x => !q || (x.name || '').toLowerCase().includes(q));
  $('#estateTable').innerHTML = '<tr><th>Database</th><th>Type</th><th>System</th><th>Owner</th><th>Rows</th><th>Columns</th><th>Status</th></tr>'
    + (dbs.map(db => '<tr data-db="' + esc(db.name) + '" style="cursor:pointer"><td><b>' + esc(db.name)
        + '</b> <span style="color:var(--muted);font-size:11px">&rarr; explore</span></td>'
        + '<td>Database</td><td>' + esc(p.d.connector || '') + '</td>'
        + '<td>' + esc(db.owner || db.kind || '—') + '</td><td>—</td><td>—</td>'
        + '<td>' + statChip('DISCOVERED') + '</td></tr>').join('')
      || '<tr><td colspan=7 style="color:var(--muted)">No databases are visible to this login.</td></tr>');
  $('#estateTable').querySelectorAll('tr[data-db]').forEach(tr =>
    tr.onclick = () => introspectDatabase(p.id, tr.getAttribute('data-db')));
}

// Level 1 drill-in. Only this level re-fetches; every filter below it runs
// in the browser over the one result.
async function introspectDatabase(id, dbName) {
  $('#estateContext').innerHTML = '';
  $('#estatePager').style.display = 'none';
  $('#estateTable').innerHTML = '<tr><td style="color:var(--muted)">Analyzing ' + esc(dbName) + ' (read-only)…</td></tr>';
  try {
    const d = await api('/api/v1/connections/' + id + '/introspect?database=' + encodeURIComponent(dbName),
                        {method:'POST'});
    if (!d.ok) throw new Error(d.error || 'analysis failed');
    if (estateSystem !== id) return;              // selection changed mid-flight
    window._estate = d;
    window._estateDbName = dbName;
    window._estateView = 'objects';
    window._estateFilter = '';
    populateSchemaFilter(d);
    estateReRender(true);
  } catch (e) {
    if (estateSystem === id) $('#estateTable').innerHTML = '<tr><td style="color:var(--red)">' + esc(e.message) + '</td></tr>';
  }
}

// Level 2 — the schema dropdown. A database has many schemas; this narrows to
// one. Hidden when there is nothing to choose between.
function populateSchemaFilter(d) {
  const sel = $('#estateSchema');
  const schemas = [...new Set(estateAssets(d).map(a => a.schema).filter(Boolean))].sort();
  sel.innerHTML = '<option value="">All schemas</option>'
    + schemas.map(s => '<option value="' + esc(s) + '">' + esc(s) + '</option>').join('');
  sel.style.display = schemas.length > 1 ? '' : 'none';
  sel.onchange = () => estateReRender(true);
}

// Flatten the whole introspect result into one typed asset list. Driven
// entirely by which arrays came back, so a platform that reports fewer
// object types simply produces fewer rows and fewer pills — no per-platform
// UI code. Roles and grants are account-level and are NOT rows.
function estateAssets(d) {
  const A = [];
  (d.tables || []).forEach(o => A.push({
    name: o.name, type: String(o.type || '').includes('VIEW') ? 'View' : 'Table',
    schema: o.schema || '',
    // rows_known === false means the platform publishes no free row count and
    // nobody has collected statistics — NOT that the table is empty. Passing 0
    // through renders "0" and reads as eleven empty tables; null renders the
    // same em-dash the UI already uses for objects that have no row concept.
    // Absent flag = the connector always knows (Snowflake, Postgres), so the
    // value is trusted as-is.
    rows: o.rows_known === false ? null : o.rows,
    columns: o.columns || [],
    definition: (d.view_definitions && d.view_definitions[o.name]) || '', _o: o}));
  const push = (arr, type) => (arr || []).forEach(o => A.push({
    name: o.name, type: type, schema: o.schema || '', rows: null, columns: null,
    definition: o.definition || '', language: o.language || '',
    returns: o.returns || '', source: o.source || '',
    secret_findings: o.secret_findings || null}));
  push(d.materialized_views, 'Materialized view');
  push(d.dynamic_tables, 'Dynamic table');
  push(d.sequences, 'Sequence');
  push(d.file_formats, 'File format');
  push(d.functions, 'Function');
  push(d.procedures, 'Stored procedure');
  push(d.streams, 'Stream');
  push(d.tasks, 'Task');
  push(d.pipes, 'Pipe');
  push(d.stages, 'Stage');
  push(d.volumes, 'Volume');
  push(d.packages, 'Package');
  push(d.triggers, 'Trigger');
  push(d.types, 'Object type');
  push(d.rules, 'Rule');
  push(d.indexes, 'Index');
  push(d.scheduler_programs, 'Scheduler program');
  push(d.scheduler_schedules, 'Schedule');
  push(d.scheduler_chains, 'Scheduler chain');
  push(d.synonyms, 'Synonym');
  push(d.db_links, 'Database link');
  push(d.queues, 'Queue');
  // A grant has no name of its own — it is a (role, privilege, object)
  // fact — so it gets its own branch rather than a push(). It was returned
  // by every connector and rendered by none.
  (d.grants || []).forEach(o => A.push({
    name: o.privilege + ' on ' + o.object, type: 'Grant',
    schema: String(o.object || '').split('.')[0], rows: null, columns: null,
    definition: 'GRANT ' + o.privilege + ' ON ' + (o.granted_on || 'TABLE')
      + ' ' + o.object + ' TO ' + o.role}));
  // a constraint's identity is the table it guards, so `table` is what the
  // schema/name pair means here — the estate list keys off `name`
  (d.constraints || []).forEach(o => A.push({
    name: o.name, type: o.type === 'PRIMARY KEY' ? 'Primary key'
      : o.type === 'FOREIGN KEY' ? 'Foreign key'
      : o.type === 'UNIQUE' ? 'Unique constraint' : 'Check constraint',
    schema: o.schema || '', rows: null, columns: null,
    definition: o.expression || (o.ref_table
      ? (o.columns || []).join(', ') + ' → ' + o.ref_table
        + ' (' + (o.ref_columns || []).join(', ') + ')'
      : (o.columns || []).join(', '))}));
  push(d.scheduler_jobs, 'Scheduler job');
  push(d.masking_policies, 'Masking policy');
  push(d.row_access_policies, 'Row access policy');
  push(d.tags, 'Tag');
  (d.shares || []).forEach(o => A.push({name: o.name, type: 'Share',
    schema: o.database || '', rows: null, columns: null}));
  return A;
}

function estateRoleBanner(ctx) {
  if (!ctx || !ctx.current_role) return '';
  const bits = [];
  if (ctx.edition && ctx.edition !== 'unknown' && ctx.edition !== 'n/a')
    bits.push('<b>' + esc(ctx.edition) + '</b> edition');
  if (ctx.warehouse) bits.push('warehouse <b>' + esc(ctx.warehouse) + '</b>');
  if (ctx.account) bits.push('account ' + esc(ctx.account));
  // A platform that calls this level something else says so (Oracle: the
  // pluggable database, which is the SERVICE you dial — not the namespace
  // holding your tables, which is a schema there).
  if (ctx.database) bits.push(esc(ctx.database_label || 'database')
    + ' <b>' + esc(ctx.database) + '</b>');
  return '<div style="background:var(--accent-soft);border:1px solid var(--accent-line);'
    + 'border-radius:var(--radius);padding:9px 13px;margin-bottom:10px;font-size:13px;color:var(--ink2)">'
    + 'Connected as <b>' + esc(ctx.current_role) + '</b>'
    + (bits.length ? ' · ' + bits.join(' · ') : '') + '</div>';
}

// Classes the connected role or this platform could not give us. Reporting a
// blocked class as a plain zero would read as "you have none of these".
function estateGatedNote(d) {
  const caps = d.capabilities || {};
  const blocked = Object.keys(caps).filter(k => caps[k].status === 'blocked_privilege');
  const absent = Object.keys(caps).filter(k => caps[k].status === 'not_applicable');
  const capped = Object.keys(caps).filter(k => caps[k].truncated);
  const label = k => k.replace(/_/g, ' ');
  const out = [];
  if (blocked.length) out.push('<b>' + blocked.map(label).join(', ') + '</b> could not be read with this role');
  if (absent.length) out.push('<b>' + absent.map(label).join(', ') + '</b> are not available on this platform');
  if (capped.length) out.push('<b>' + capped.map(label).join(', ') + '</b> were capped — the list is partial');
  if (!out.length) return '';
  return '<div style="font-size:11.5px;color:var(--amber);background:var(--amber-bg);'
    + 'border:1px solid var(--amber-line);border-radius:var(--radius);padding:7px 11px;margin-bottom:10px">'
    + out.join(' · ') + '</div>';
}

// Level 3 — breadcrumb, role banner and the scope-aware "filter by type"
// count pills.
const ESTATE_TYPES = ['Table', 'View', 'Materialized view', 'Dynamic table',
  'Sequence', 'File format', 'Function', 'Stored procedure', 'Stream', 'Task',
  'Pipe', 'Stage', 'Volume', 'Masking policy', 'Row access policy', 'Tag',
  'Share'];
// "Masking policy" pluralises to "policies", not "policys"
const estatePlural = t => t.endsWith('s') ? t
  : t.endsWith('y') ? t.slice(0, -1) + 'ies' : t + 's';

function renderEstateContext(d) {
  const el = $('#estateContext');
  const active = window._estateFilter || '';
  // Counts follow the CURRENT schema + search scope but deliberately ignore
  // the type filter itself — otherwise every pill but the active one would
  // read 0 and you could never switch between them.
  const q = ($('#estateSearch').value || '').toLowerCase();
  const sf = $('#estateSchema').value || '';
  const scoped = estateAssets(d).filter(a => estateInScope(a, sf, q));
  const byType = {};
  scoped.forEach(a => byType[a.type] = (byType[a.type] || 0) + 1);
  const base = 'display:inline-block;border-radius:20px;padding:2px 11px;margin:2px 4px 2px 0;font-size:12px;';
  const pill = (label, ftype, count, isActive, clickable) => '<span'
    + (clickable ? ' data-ftype="' + esc(ftype) + '"' : '')
    + ' style="' + base + (isActive
        ? 'background:var(--accent);border:1px solid var(--accent);color:#fff;cursor:pointer'
        : clickable
          ? 'background:var(--header-bg);border:1px solid var(--border);color:var(--ink2);cursor:pointer'
          : 'background:var(--header-bg);border:1px solid var(--line);color:var(--muted)')
    + '"><b>' + count + '</b> ' + esc(label) + '</span>';
  // an active pill stays visible at count 0 so it can always be un-clicked
  const pills = pill('All', '', scoped.length, !active, true)
    + ESTATE_TYPES.filter(t => byType[t] || active === t)
        .map(t => pill(estatePlural(t), t, byType[t] || 0, active === t, true)).join('')
    // account-level objects belong to no schema, so they are counts, not rows
    + ((d.roles || []).length ? pill('Roles', null, d.roles.length, false, false) : '')
    + ((d.grants || []).length ? pill('Grants', null, d.grants.length, false, false) : '');
  // Level 1 navigation. Connectors that answer with a picker get a
  // breadcrumb back to it; a connector that always resolves a default
  // database (Databricks) reports the list instead and gets a switcher, so
  // it can still change database without a dead end.
  const crumb = window._estatePicker
    ? '<div style="margin-bottom:8px;font-size:12.5px"><a href="#" id="estateBack" style="color:var(--accent);text-decoration:none">&larr; All databases</a>'
      + (window._estateDbName ? ' <span style="color:var(--muted)">/ ' + esc(window._estateDbName) + '</span>' : '') + '</div>'
    : ((d.available_databases || []).length > 1
       ? '<div style="margin-bottom:8px;font-size:12.5px;color:var(--ink3)">Database '
         + '<select id="estateDbSwitch" style="width:auto;min-width:150px;display:inline-block;margin-left:4px">'
         + d.available_databases.map(n => '<option' + (n === (d.database || '') ? ' selected' : '')
             + '>' + esc(n) + '</option>').join('')
         + '</select></div>'
       : '');
  el.innerHTML = crumb + estateRoleBanner(d.context || {}) + estateGatedNote(d)
    + '<div style="margin:2px 0 6px"><span style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-right:6px">Filter by type</span>'
    + pills + '</div>';
  if ($('#estateDbSwitch')) $('#estateDbSwitch').onchange = ev =>
    introspectDatabase($('#estateSystem').value, ev.target.value);
  if ($('#estateBack')) $('#estateBack').onclick = ev => {
    ev.preventDefault();
    window._estate = null;
    window._estateDbName = '';
    window._estateFilter = '';
    window._estateView = 'databases';
    estateReRender(true);
  };
  el.querySelectorAll('[data-ftype]').forEach(p => p.onclick = () => {
    const ft = p.getAttribute('data-ftype');
    window._estateFilter = (window._estateFilter === ft) ? '' : ft;
    estateReRender(true);
  });
}

function estateInScope(a, schemaFilter, q) {
  return (!schemaFilter || a.schema === schemaFilter)
    && (!q || a.name.toLowerCase().includes(q)
      || a.type.toLowerCase().includes(q)
      || (a.columns || []).some(cc => cc.name.toLowerCase().includes(q)));
}

function renderEstateCards(stats) {
  if (!stats || !stats.cards || !stats.cards.length) {
    $('#estateCards').innerHTML = '<div style="grid-column:1/-1;color:var(--muted);font-size:13.5px">'
      + esc((stats && stats.message) || 'Select a system to view its statistics.') + '</div>';
    return;
  }
  $('#estateCards').innerHTML = stats.cards.map(c => mcard(c.n, c.label, c.sub || '')).join('');
}

function renderEstateSummaryTable() {
  const q = ($('#estateSearch').value || '').toLowerCase();
  const rows = estateSummary.filter(s => !q
    || (s.name || '').toLowerCase().includes(q)
    || (s.connector || '').toLowerCase().includes(q));
  // Distinguish "your search hid everything" from "nothing is connected yet" —
  // the second is the first-load state for a new workspace (this view is the
  // default), so it gets onboarding rather than a dead-end "no match".
  const empty = estateSummary.length
    ? '<tr><td colspan=3 style="color:var(--muted)">No systems match “' + esc(q) + '”.</td></tr>'
    : '<tr><td colspan=3><div class="empty" style="border:none"><b>No systems connected yet.</b>'
      + 'Connect a source under Integrations to see it here.<br>'
      + '<button class="secondary" style="margin:12px 0 0" onclick="document.querySelector(\'nav a[data-page=marketplace]\').click()">Connect source</button>'
      + '</div></td></tr>';
  $('#estateTable').innerHTML = '<tr><th>System</th><th>Connector</th><th>Analyzed</th></tr>'
    + (rows.map(s => '<tr><td><b>' + esc(s.name) + '</b></td><td>' + esc(s.connector) + '</td><td>'
        + (s.analyzed ? badge('analyzed', 'var(--green)') : badge('not analyzed', 'var(--muted)')) + '</td></tr>').join('')
      || empty);
  $('#estatePager').style.display = 'none';
}
// Estate "Analyze" results can run to hundreds of tables. The full result
// arrives in one live introspection call (window._estate.tables), so we
// paginate client-side over the filtered rows — stable ordering (the
// introspect's -rows,name order is preserved), a page size, total count,
// prev/next controls and current-page info. The search filter is applied
// before paging and resets to page 1 (see the oninput handler).
let estatePage = 1;
let estatePageSize = 25;
// All four levels compose here: database (already fetched) -> schema -> type
// -> search. Filtering happens BEFORE paging, so the pager always reports the
// narrowed total rather than the whole database.
function _estateFiltered() {
  const d = window._estate;
  if (!d) return [];
  const ft = window._estateFilter || '';
  const sf = $('#estateSchema').value || '';
  const q = ($('#estateSearch').value || '').toLowerCase();
  return estateAssets(d).filter(a => (!ft || a.type === ft)
    && estateInScope(a, sf, q));
}
function renderEstateTable() {
  const d = window._estate;
  const conn = allConnections.find(x => x.id === $('#estateSystem').value) || {connector: ''};
  renderEstateContext(d);
  const filtered = _estateFiltered();
  const total = filtered.length;
  const pages = Math.max(1, Math.ceil(total / estatePageSize));
  if (estatePage > pages) estatePage = pages;
  if (estatePage < 1) estatePage = 1;
  const start = (estatePage - 1) * estatePageSize;
  const pageRows = filtered.slice(start, start + estatePageSize);
  $('#estateTable').innerHTML = '<tr><th>Asset</th><th>Type</th><th>System</th><th>Schema</th><th>Rows</th><th>Columns</th><th>Status</th></tr>'
    + (pageRows.map((a, i) => '<tr data-i="' + (start + i) + '" style="cursor:pointer"><td><b>' + esc(a.name) + '</b></td>'
      + '<td>' + esc(a.type) + '</td>'
      + '<td>' + esc(conn.connector) + '</td><td>' + esc(a.schema || '—') + '</td>'
      // rows/columns are table facts; every other object type has neither
      + '<td>' + (a.rows != null ? a.rows.toLocaleString() : '—') + '</td>'
      + '<td>' + (a.columns ? a.columns.length : '—') + '</td>'
      + '<td>' + statChip('DISCOVERED') + '</td></tr>').join('')
      || '<tr><td colspan=7 style="color:var(--muted)">No assets match.</td></tr>');
  // pager
  const pager = $('#estatePager');
  pager.style.display = total > estatePageSize ? 'flex' : 'none';
  if (total) {
    const from = start + 1, to = Math.min(start + estatePageSize, total);
    $('#estPageInfo').textContent = 'Showing ' + from + '–' + to
      + ' of ' + total.toLocaleString() + ' asset' + (total === 1 ? '' : 's')
      + ' · page ' + estatePage + ' of ' + pages;
    $('#estPrev').disabled = estatePage <= 1;
    $('#estNext').disabled = estatePage >= pages;
  }
  $('#estateTable').querySelectorAll('tr[data-i]').forEach(tr => tr.onclick = () => {
    const a = filtered[+tr.dataset.i];
    if (!a) return;
    // tables and views carry columns and convert today; everything else gets
    // the generic object drawer with its (redacted) definition
    if (a._o) openAssetDrawer(a._o, conn, d);
    else openObjectDrawer(a, conn);
  });
}
function estateReRender(resetPage) {
  if (resetPage) estatePage = 1;
  if (estateSystem === 'all') renderEstateSummaryTable();
  else if (window._estateView === 'databases') renderDatabaseList();
  else if (window._estate) renderEstateTable();
}
const estateNavTop = () => {
  if (document.activeElement) document.activeElement.blur();
  const wa = document.querySelector('.workarea');
  if (wa) wa.scrollTop = 0;
};
$('#estPrev').onclick = () => { estatePage--; estateNavTop(); renderEstateTable(); estateNavTop(); };
$('#estNext').onclick = () => { estatePage++; estateNavTop(); renderEstateTable(); estateNavTop(); };
$('#estPageSize').onchange = ev => { estatePageSize = parseInt(ev.target.value, 10) || 25; estateReRender(true); };
// Every object type that is not a table or a view: a sequence, a function, a
// stored procedure, a stream. Bodies arrive REDACTED from the backend — this
// only says so where a credential was found and removed.
function openObjectDrawer(a, conn) {
  const cols = a.columns || [];
  const meta = [esc(a.type), esc(conn.connector || '')];
  if (a.language) meta.push(esc(a.language));
  if (a.returns) meta.push('returns ' + esc(a.returns));
  if (a.source) meta.push('reads ' + esc(a.source));
  $('#modalBody').innerHTML = '<h2 style="font-size:18px">' + esc(a.schema ? a.schema + '.' : '') + esc(a.name) + '</h2>'
    + '<div class="v" style="color:var(--ink3);font-size:13px;margin-bottom:10px">' + meta.join(' · ') + '</div>'
    + ((a.secret_findings || []).length
       ? '<div style="background:var(--amber-bg);border:1px solid var(--amber-line);border-radius:var(--radius);'
         + 'padding:8px 12px;margin-bottom:10px;font-size:12.5px;color:var(--amber)">'
         + a.secret_findings.length + ' credential' + (a.secret_findings.length === 1 ? '' : 's')
         + ' found in this definition and redacted — the value was never stored.</div>' : '')
    + (cols.length ? '<div class="drawer-meta"><b>Columns (' + cols.length + ')</b></div><table>'
        + cols.map(cc => '<tr><td>' + esc(cc.name) + '</td><td style="color:var(--ink3)">' + esc(cc.type) + '</td></tr>').join('')
        + '</table>' : '')
    + (a.definition
       ? '<div class="drawer-meta"><b>Definition</b></div><pre>' + esc(String(a.definition).slice(0, 2000)) + '</pre>'
       : '<div style="color:var(--muted);font-size:12.5px">No definition is visible to this role.</div>')
    + '<button class="secondary" id="closeModal">Close</button>';
  $('#modalBody').className = 'modal'; $('#modalBg').style.display = 'flex';
  $('#closeModal').onclick = () => $('#modalBg').style.display = 'none';
}
function openAssetDrawer(t, conn, d) {
  $('#modalBody').innerHTML = '<h2 style="font-size:18px">' + esc(t.schema) + '.' + esc(t.name) + '</h2>'
    + '<div class="v" style="color:var(--ink3);font-size:13px;margin-bottom:10px">' + (t.type.includes('VIEW') ? 'View' : 'Table')
    + ' · ' + esc(conn.connector) + ' · '
    + (t.rows_known === false
       ? 'row count not collected'
       : Number(t.rows || 0).toLocaleString() + ' rows') + '</div>'
    + '<div class="drawer-meta"><b>Columns (' + t.columns.length + ')</b></div>'
    + '<table>' + t.columns.map(cc => '<tr><td>' + esc(cc.name) + '</td><td style="color:var(--ink3)">' + esc(cc.type) + '</td></tr>').join('') + '</table>'
    + (d.view_definitions && d.view_definitions[t.name]
       ? '<div class="drawer-meta"><b>Transformation logic</b></div><pre>' + esc(d.view_definitions[t.name].slice(0, 2000)) + '</pre>' : '')
    + '<button id="modAsset">Modernize this asset</button> <button class="secondary" id="closeModal">Close</button>';
  $('#modalBody').className = 'modal'; $('#modalBg').style.display = 'flex';
  $('#closeModal').onclick = () => $('#modalBg').style.display = 'none';
  $('#modAsset').onclick = async () => {
    const manifest = 'tables:\n  - name: ' + t.name + '\n    schema: ' + t.schema + '\n    columns:\n'
      + t.columns.map(cc => '      - {name: ' + cc.name + ', type: ' + cc.type.toLowerCase() + '}').join('\n') + '\n';
    $('#modalBg').style.display = 'none';
    await handoffToScaffold(
      new File([manifest], t.name.toLowerCase() + '_manifest.yml', {type:'text/yaml'}),
      '<b>' + t.name.toLowerCase() + '_manifest.yml</b> (1 asset from '
        + esc(conn.name || conn.connector) + ')',
      conn.connector);
  };
}

/* ---------------- Digital Twin ---------------- */
let gTwin = null, twinAffected = null, twinSelected = null;
const TWIN_KIND_COLOR = {application:'#2f6fb4', table:'var(--green)', topic:'var(--amber)',
  pipeline:'#7a4bb8', streaming_job:'#7a4bb8', workflow:'#5b6b84',
  database:'#16233c', warehouse:'#16233c', connection:'var(--muted)',
  api:'var(--red)', dashboard:'var(--red)', data_product:'#0e7c7b',
  consumer:'var(--amber)', producer:'var(--amber)'};
const TWIN_LAYERS = ['Systems', 'Applications', 'Data', 'Processing',
  'Orchestration', 'Consumption', 'Products', 'Governance'];
const TWIN_FLOW_KINDS = ['table', 'pipeline', 'topic', 'streaming_job', 'api', 'dashboard'];

async function loadTwin() {
  try { gTwin = await api('/api/twin'); renderTwin(); }
  catch (e) { /* no twin built yet — panel stays in build mode */ }
}

$('#twinBuild').onclick = async () => {
  const err = $('#twinErr'); err.style.display = 'none';
  $('#twinStatus').textContent = 'Discovering your estate…';
  try {
    const files = await Promise.all([...$('#twinFiles').files].map(f =>
      new Promise((res, rej) => {
        const r = new FileReader();
        r.onload = () => res({name: f.name, content: r.result});
        r.onerror = rej;
        r.readAsText(f);
      })));
    const d = await api('/api/twin/build', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({files,
        include_connections: $('#twinUseConns').checked,
        include_jobs: $('#twinUseJobs').checked})});
    $('#twinStatus').textContent = '';
    gTwin = d; twinAffected = null; twinSelected = null;
    renderTwin();
  } catch (e) {
    $('#twinStatus').textContent = '';
    err.textContent = e.message; err.style.display = 'block';
  }
};

function renderTwin() {
  if (!gTwin) return;
  $('#twinResult').style.display = 'block';
  const c = gTwin.counts || {};
  const endpoints = (c.api || 0) + (c.dashboard || 0) + (c.data_product || 0);
  $('#twinCards').innerHTML =
    mcard(gTwin.nodes.length, 'Objects', (gTwin.built_from || []).length + ' source(s)')
    + mcard(c.edges || 0, 'Dependencies', '')
    + mcard(c.application || 0, 'Applications', '')
    + mcard((c.pipeline || 0) + (c.streaming_job || 0), 'Pipelines & jobs', '')
    + mcard((c.table || 0) + (c.topic || 0), 'Tables & topics', '')
    + mcard(mnum(endpoints), 'Business endpoints', 'APIs · dashboards · products')
    + mcard(mnum(c.domain || 0), 'Domains', '');
  const techs = [...new Set(gTwin.nodes.map(n => n.technology).filter(Boolean))].sort();
  $('#twinTech').innerHTML = '<option value="">Simulate technology…</option>'
    + techs.map(t => '<option>' + esc(t) + '</option>').join('');
  twinSyncSimulateBtn();
  gTwinCanvas = gTwinCanvas || new TwinCanvas($('#twinWrap'), $('#twinSidePanel'));
  gTwinCanvas.setGraph(twinGraphNodes(), gTwin.edges || []);
  twinTab('landscape');
}

function twinGraphNodes() {
  let nodes = gTwin.nodes.filter(n => n.kind !== 'domain' && n.kind !== 'owner');
  if ($('#twinView').value === 'flow')
    nodes = nodes.filter(n => TWIN_FLOW_KINDS.includes(n.kind));
  return nodes;
}
$('#twinView').onchange = () => gTwinCanvas && gTwinCanvas.setGraph(twinGraphNodes(), gTwin.edges || []);
$('#twinSearch').oninput = e => gTwinCanvas && gTwinCanvas.setQuery(e.target.value);
$('#twinLayout').onchange = e => gTwinCanvas && gTwinCanvas.relayout(e.target.value);
$('#twinRelayout').onclick = () => gTwinCanvas && gTwinCanvas.relayout($('#twinLayout').value);

/* ---------------- Digital Twin — infinite-canvas graph ----------------
 * Hand-rolled pan/zoom/drag/minimap SVG canvas (no charting library —
 * same "plain DOM/SVG string" technique used everywhere else in this
 * console). Mirrors the old renderTwinGraph()'s data contract (gTwin
 * nodes/edges, TWIN_KIND_COLOR, twinAffected/twinSelected) but renders
 * as a live, draggable, zoomable scene instead of a static flat SVG. */
class TwinCanvas {
  constructor(wrapEl, panelEl) {
    this.wrap = wrapEl; this.panel = panelEl;
    this.NW = 190; this.NH = 46;
    this.view = {x: 60, y: 60, k: .85};
    this.pos = {}; this.q = ''; this.layoutMode = 'lr';
    this.userMoved = false;
    this.simIds = null; this.simLevels = null; this.simWave = -1; this.simTimer = null;

    this.svgNS = 'http://www.w3.org/2000/svg';
    wrapEl.innerHTML =
      '<div class="twc-layer" style="position:absolute;left:0;top:0;transform-origin:0 0">'
      + '<svg class="twc-svg" width="1" height="1" style="position:absolute;left:0;top:0;overflow:visible"></svg>'
      + '<div class="twc-nodes" style="position:absolute;left:0;top:0"></div></div>'
      + '<div class="twc-legend" style="position:absolute;left:12px;top:12px;display:flex;gap:6px;flex-wrap:wrap;max-width:70%;pointer-events:none"></div>'
      + '<div class="twc-zoom" style="position:absolute;right:12px;bottom:12px;display:flex;flex-direction:column;gap:6px">'
      + '<div style="display:flex;flex-direction:column;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);overflow:hidden;box-shadow:var(--shadow)">'
      + '<button type="button" class="twc-zi" style="all:unset;box-sizing:border-box;width:28px;height:26px;text-align:center;border-bottom:1px solid var(--line);cursor:pointer;color:var(--ink2)">+</button>'
      + '<button type="button" class="twc-zo" style="all:unset;box-sizing:border-box;width:28px;height:26px;text-align:center;border-bottom:1px solid var(--line);cursor:pointer;color:var(--ink2)">−</button>'
      + '<button type="button" class="twc-fit" style="all:unset;box-sizing:border-box;width:28px;height:26px;text-align:center;cursor:pointer;color:var(--ink2)" title="Fit to screen">⤢</button></div>'
      + '<div class="twc-zoompct" style="text-align:center;font:500 10px var(--mono);color:var(--muted);background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:2px 0"></div></div>'
      + '<div class="twc-minimap" style="position:absolute;left:12px;bottom:12px;width:180px;height:120px;background:rgba(255,255,255,.94);border:1px solid var(--border);border-radius:6px;overflow:hidden;cursor:crosshair;box-shadow:var(--shadow)">'
      + '<svg width="180" height="120" style="display:block"></svg>'
      + '<button type="button" class="twc-maphide" title="Hide the minimap"'
      + ' style="position:absolute;right:2px;top:2px;margin:0;height:18px;width:18px;padding:0;line-height:1;'
      + 'font-size:13px;background:rgba(255,255,255,.9);color:var(--muted);border:1px solid var(--border);'
      + 'border-radius:3px;cursor:pointer">×</button></div>'
      + '<button type="button" class="twc-maptoggle" title="Show/hide the overview minimap"'
      + ' style="position:absolute;left:12px;bottom:12px;margin:0;height:24px;padding:0 8px;font-size:11px;'
      + 'background:var(--surface);color:var(--ink2);border:1px solid var(--border);border-radius:4px;cursor:pointer;'
      + 'box-shadow:var(--shadow);display:none">Map</button>';

    this.layerEl = wrapEl.querySelector('.twc-layer');
    this.svgEl = wrapEl.querySelector('.twc-svg');
    this.nodesEl = wrapEl.querySelector('.twc-nodes');
    this.legendEl = wrapEl.querySelector('.twc-legend');
    this.mapEl = wrapEl.querySelector('.twc-minimap');
    this.mapToggleEl = wrapEl.querySelector('.twc-maptoggle');
    // The minimap sits over the lower-left of the canvas and can cover real
    // nodes — let it be dismissed, collapsing to a small "Map" chip.
    this.mapToggleEl.onclick = () => this.setMapOpen(true);
    wrapEl.querySelector('.twc-maphide').onpointerdown = e => {
      e.stopPropagation(); e.preventDefault(); this.setMapOpen(false);
    };
    this.mapSvg = this.mapEl.querySelector('svg');
    this.zoomPctEl = wrapEl.querySelector('.twc-zoompct');

    wrapEl.querySelector('.twc-zi').onclick = () => this.zoomBy(1.22);
    wrapEl.querySelector('.twc-zo').onclick = () => this.zoomBy(1 / 1.22);
    wrapEl.querySelector('.twc-fit').onclick = () => { this.userMoved = false; this.fit(); };
    this.mapEl.onpointerdown = e => this.onMapDown(e);
    wrapEl.onpointerdown = e => this.onBgDown(e);
    wrapEl.addEventListener('wheel', e => this.onWheel(e), {passive: false});

    this._move = e => this.onMove(e);
    this._up = () => this.onUp();
    window.addEventListener('pointermove', this._move);
    window.addEventListener('pointerup', this._up);

    this.ro = new ResizeObserver(() => { if (!this.userMoved) this.fit(); else this.renderTransform(); });
    this.ro.observe(wrapEl);
  }

  setGraph(nodes, edges) {
    this.nodes = nodes;
    this.byId = {}; nodes.forEach(n => this.byId[n.id] = n);
    const ids = new Set(nodes.map(n => n.id));
    const hide = {belongs_to: 1, owns: 1, includes: 1};
    this.edges = edges.filter(e => !hide[e.kind] && ids.has(e.from) && ids.has(e.to));
    this.out = {}; this.in = {};
    nodes.forEach(n => { this.out[n.id] = []; this.in[n.id] = []; });
    this.edges.forEach(e => { this.out[e.from].push(e.to); this.in[e.to].push(e.from); });
    this.pos = this.computeLayout(this.layoutMode);
    this.render();
    setTimeout(() => this.fit(), 30);
  }

  relayout(mode) {
    this.layoutMode = mode;
    this.pos = this.computeLayout(mode);
    this.render();
    setTimeout(() => this.fit(), 30);
  }

  setQuery(q) { this.q = q || ''; this.render(); }

  computeLayout(mode) {
    const nodes = this.nodes || [];
    const byLayer = {};
    nodes.forEach(n => (byLayer[n.layer || 0] = byLayer[n.layer || 0] || []).push(n.id));
    const layers = Object.keys(byLayer).map(Number).sort((a, b) => a - b);
    const NW = this.NW, NH = this.NH;
    const pos = {};
    if (mode === 'depth') {
      const indeg = {}; nodes.forEach(n => indeg[n.id] = this.in[n.id].length);
      const q = nodes.filter(n => !indeg[n.id]).map(n => n.id);
      const order = []; const seen = new Set(q);
      while (q.length) { const c = q.shift(); order.push(c);
        (this.out[c] || []).forEach(o => { if (!seen.has(o)) { seen.add(o); q.push(o); } }); }
      nodes.forEach(n => { if (!seen.has(n.id)) order.push(n.id); });
      const depth = {};
      order.forEach(id => { const ups = this.in[id] || [];
        depth[id] = ups.length ? Math.max(...ups.map(p => (depth[p] ?? 0) + 1)) : 0; });
      const buckets = {};
      order.forEach(id => (buckets[depth[id]] = buckets[depth[id]] || []).push(id));
      const keys = Object.keys(buckets).map(Number).sort((a, b) => a - b);
      const max = Math.max(1, ...keys.map(k => buckets[k].length));
      keys.forEach(k => {
        const arr = buckets[k].sort((a, b) => (this.byId[a].layer || 0) - (this.byId[b].layer || 0));
        const off = (max - arr.length) * (NH + 22) / 2;
        arr.forEach((id, i) => pos[id] = {x: k * (NW + 110), y: off + i * (NH + 22)});
      });
      return pos;
    }
    const stepY = NH + 24;
    // Wrap over-long layers into sub-columns. A layer with 50 tables in a
    // single column is ~3500px tall against a ~900px-wide graph, and fit()
    // then scales the whole thing to ~12% — a unreadable sliver with dead
    // space below it. Wrapping keeps the graph roughly screen-shaped.
    const PERCOL = 14;
    const wrap = ids => {
      const sub = [];
      for (let i = 0; i < ids.length; i += PERCOL) sub.push(ids.slice(i, i + PERCOL));
      return sub;
    };
    if (mode === 'tb') {
      // rows instead of columns: wrap on the horizontal axis
      const stepX = NW + 30;
      const rows = layers.map(L => wrap(byLayer[L]));
      const maxLen = Math.max(1, ...rows.flat().map(r => r.length));
      let y = 0;
      rows.forEach(subRows => {
        subRows.forEach(row => {
          const off = (maxLen - row.length) * stepX / 2;
          row.forEach((id, i) => pos[id] = {x: off + i * stepX, y});
          y += stepY + 24;
        });
        y += 90;                     // gap between layers
      });
    } else {
      const stepX = NW + 130;
      const colsOf = layers.map(L => wrap(byLayer[L]));
      const maxLen = Math.max(1, ...colsOf.flat().map(c => c.length));
      let x = 0;
      colsOf.forEach(subCols => {
        subCols.forEach(col => {
          const off = (maxLen - col.length) * stepY / 2;
          col.forEach((id, i) => pos[id] = {x, y: off + i * stepY});
          x += stepX;
        });
      });
    }
    return pos;
  }

  bbox() {
    const p = this.pos, ids = Object.keys(p);
    if (!ids.length) return {x: 0, y: 0, w: 1, h: 1};
    let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    ids.forEach(id => { x0 = Math.min(x0, p[id].x); y0 = Math.min(y0, p[id].y);
      x1 = Math.max(x1, p[id].x + this.NW); y1 = Math.max(y1, p[id].y + this.NH); });
    return {x: x0 - 60, y: y0 - 60, w: x1 - x0 + 120, h: y1 - y0 + 120};
  }

  // Size the canvas to the height the graph actually needs, so there is no
  // dead band under a short graph AND no squeezed-to-12% sliver under a
  // tall one.
  //
  // The scale fit() will settle on is min(aw/b.w, ah/b.h) — it depends on
  // the height we're choosing here, so solve it directly instead of
  // guessing from the horizontal fit alone:
  //   * wide graph  -> width is the binding constraint; height just needs
  //                    b.h*kw, which is less than the cap. Use it.
  //   * tall graph  -> height binds; growing the box only helps up to the
  //                    cap, so take the cap and let fit() use the full box
  //                    rather than leaving slack below a shrunken graph.
  autoHeight() {
    if (twinFullscreen) return;                 // fullscreen always fills
    const b = this.bbox();
    const r = this.wrap.getBoundingClientRect();
    const vw = r.width || 600;
    if (vw < 50) return;
    const L = 24, R = 60, T = 20, B = 20;
    const aw = Math.max(120, vw - L - R);
    const capPx = Math.max(260, window.innerHeight * 0.72);
    // Height needed at the scale fit() will actually use — including the
    // MIN_FIT_K floor. Without the floor a big graph computed a tiny scale,
    // reported a small "needed" height, and the canvas ended up both short
    // AND zoomed out to an unreadable sliver.
    const kw = Math.max(Math.min(aw / b.w, 1.4) * 0.96, this.MIN_FIT_K);
    const needed = b.h * kw + T + B;
    // Below the cap the graph fits at kw with no leftover space. At or above
    // it, the box is capped and fit() will scale down to match — either way
    // the box is exactly as tall as the content will occupy.
    const h = Math.max(260, Math.min(capPx, needed));
    this.wrap.style.height = Math.round(h) + 'px';
    // Pin the whole canvas/panel row to the canvas height. Otherwise the
    // side panel (which is often taller than the graph) stretches the row
    // and leaves a band of empty canvas beside it — the white gap under
    // the graph when a node is selected.
  }

  // Below ~55% the 12px node labels stop being readable, so "fit
  // everything on screen" becomes worthless — you get a wall of grey
  // slivers. Past that point stop zooming out: hold a legible scale and
  // let the user pan/scroll the canvas instead (standard infinite-canvas
  // behaviour — Figma/Miro never auto-zoom you into illegibility either).
  MIN_FIT_K = 0.55;

  fit() {
    this.autoHeight();
    const b = this.bbox();
    const r = this.wrap.getBoundingClientRect();
    const vw = r.width || 600, vh = r.height || 400;
    const L = 24, R = 60, T = 20, B = 20;
    const aw = Math.max(120, vw - L - R), ah = Math.max(120, vh - T - B);
    const raw = Math.min(aw / b.w, ah / b.h, 1.4) * 0.96;
    const k = Math.max(raw, this.MIN_FIT_K);
    // When clamped the graph is larger than the viewport: anchor to the
    // top-left of the content instead of centring it, so you start reading
    // at the sources rather than in the middle of nowhere.
    const clamped = k > raw + 1e-9;
    this.view = clamped
      ? {k, x: L - b.x * k, y: T - b.y * k}
      : {k, x: L + (aw - b.w * k) / 2 - b.x * k, y: T + (ah - b.h * k) / 2 - b.y * k};
    this.render();
  }

  centerOn(id, k) {
    const p = this.pos[id]; if (!p) return;
    const r = this.wrap.getBoundingClientRect();
    const kk = k || Math.max(this.view.k, .85);
    this.view = {k: kk, x: r.width / 2 - (p.x + this.NW / 2) * kk, y: r.height / 2 - (p.y + this.NH / 2) * kk};
    this.userMoved = true;
    this.render();
  }

  zoomBy(f) {
    const r = this.wrap.getBoundingClientRect();
    const v = this.view, k = Math.min(2.5, Math.max(.18, v.k * f));
    this.view = {k, x: r.width / 2 - (r.width / 2 - v.x) * (k / v.k), y: r.height / 2 - (r.height / 2 - v.y) * (k / v.k)};
    this.userMoved = true;
    this.render();
  }

  onWheel(e) {
    e.preventDefault();
    const v = this.view, r = this.wrap.getBoundingClientRect();
    this.userMoved = true;
    if (e.ctrlKey || e.metaKey || e.altKey) {
      const f = Math.exp(-e.deltaY * 0.0022), k = Math.min(2.5, Math.max(.18, v.k * f));
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      this.view = {k, x: mx - (mx - v.x) * (k / v.k), y: my - (my - v.y) * (k / v.k)};
    } else this.view = {...v, x: v.x - e.deltaX, y: v.y - e.deltaY};
    this.render();
  }

  onBgDown(e) {
    if (e.target.closest('.twc-node')) return;
    this.pan = {vx: this.view.x, vy: this.view.y, mx: e.clientX, my: e.clientY, moved: false};
  }
  onMove(e) {
    if (this.pan) { this.userMoved = true;
      this.pan.moved = this.pan.moved || Math.abs(e.clientX - this.pan.mx) + Math.abs(e.clientY - this.pan.my) > 3;
      this.view = {...this.view, x: this.pan.vx + e.clientX - this.pan.mx, y: this.pan.vy + e.clientY - this.pan.my};
      this.render();
    } else if (this.dragN) { this.userMoved = true;
      const k = this.view.k;
      const p = {x: this.dragN.px + (e.clientX - this.dragN.mx) / k, y: this.dragN.py + (e.clientY - this.dragN.my) / k};
      this.dragN.moved = this.dragN.moved || Math.abs(e.clientX - this.dragN.mx) + Math.abs(e.clientY - this.dragN.my) > 3;
      this.pos[this.dragN.id] = p;
      this.render();
    }
  }
  onUp() {
    if (this.dragN && !this.dragN.moved) twinSelect(this.dragN.id);
    if (this.pan && !this.pan.moved) twinClearSelection();
    this.pan = null; this.dragN = null;
  }
  setMapOpen(open) {
    this.mapOpen = open;
    this.mapEl.style.display = open ? 'block' : 'none';
    this.mapToggleEl.style.display = open ? 'none' : 'block';
    if (open) this.render();
  }

  onMapDown(e) {
    e.stopPropagation();
    const r = this.mapEl.getBoundingClientRect();
    const wx = ((e.clientX - r.left) - this._mox) / this._mk, wy = ((e.clientY - r.top) - this._moy) / this._mk;
    const wr = this.wrap.getBoundingClientRect();
    this.view = {k: this.view.k, x: wr.width / 2 - wx * this.view.k, y: wr.height / 2 - wy * this.view.k};
    this.userMoved = true;
    this.render();
  }

  renderTransform() {
    this.layerEl.style.transform = 'translate3d(' + this.view.x + 'px,' + this.view.y + 'px,0) scale(' + this.view.k + ')';
    this.zoomPctEl.textContent = Math.round(this.view.k * 100) + '%';
  }

  anchor(id, side) {
    const p = this.pos[id] || {x: 0, y: 0};
    const tb = this.layoutMode === 'tb';
    if (tb) return side === 'out' ? {x: p.x + this.NW / 2, y: p.y + this.NH} : {x: p.x + this.NW / 2, y: p.y};
    return side === 'out' ? {x: p.x + this.NW, y: p.y + this.NH / 2} : {x: p.x, y: p.y + this.NH / 2};
  }
  edgePath(a, b) {
    const tb = this.layoutMode === 'tb';
    if (tb) { const d = Math.max(36, Math.abs(b.y - a.y) * 0.5); return 'M' + a.x + ',' + a.y + ' C' + a.x + ',' + (a.y + d) + ' ' + b.x + ',' + (b.y - d) + ' ' + b.x + ',' + b.y; }
    const d = Math.max(40, Math.abs(b.x - a.x) * 0.45); return 'M' + a.x + ',' + a.y + ' C' + (a.x + d) + ',' + a.y + ' ' + (b.x - d) + ',' + b.y + ' ' + b.x + ',' + b.y;
  }

  // Coalesces bursts of render requests onto a single rAF so a rebuild
  // of the node layer happens at most once per frame.
  scheduleRender() {
    if (this._renderQueued) return;
    this._renderQueued = true;
    requestAnimationFrame(() => { this._renderQueued = false; this.render(); });
  }

  render() {
    if (!this.nodes) return;
    this.renderTransform();
    const P = this.pos, NW = this.NW, NH = this.NH;
    const q = (this.q || '').trim().toLowerCase();
    const matches = q ? new Set(this.nodes.filter(n =>
      String(n.name).toLowerCase().includes(q) || String(n.technology || '').toLowerCase().includes(q)
      || String(n.domain || '').toLowerCase().includes(q)).map(n => n.id)) : null;

    // Relationship highlighting is selection-driven only. Hover used to
    // drive it too, but with large graphs every pointerenter rebuilt the
    // whole node+edge layer, and the rebuilt node landing back under the
    // cursor re-fired enter/leave — a visible flicker loop.
    const focusId = twinSelected;
    let rel = null, up = null, down = null;
    if (focusId) {
      up = twinClosure(this, focusId, 'up'); down = twinClosure(this, focusId, 'down');
      rel = new Set([focusId, ...up, ...down]);
    }
    const simOn = this.simIds && this.simWave >= 0;

    // edges
    let svg = '<defs>'
      + '<marker id="twcArrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 z" fill="#9aa6bb"></path></marker>'
      + '<marker id="twcArrowHot" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 z" fill="var(--accent)"></path></marker>'
      + '<marker id="twcArrowSim" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 z" fill="var(--red)"></path></marker>'
      + '</defs>';
    this.edges.forEach(e => {
      const a = this.anchor(e.from, 'out'), b = this.anchor(e.to, 'in');
      if (!P[e.from] || !P[e.to]) return;
      const inFocus = rel && rel.has(e.from) && rel.has(e.to);
      const onPath = focusId && ((up && up.has(e.from) && rel.has(e.to)) || (down && down.has(e.to) && rel.has(e.from)) || e.from === focusId || e.to === focusId) && inFocus;
      // Highlight edges wholly inside the simulated selection AND edges
      // feeding into it from outside (the "inbound boundary") — a
      // single-node migration selection has no internal edge at all, so
      // restricting to both-ends-selected left the flow with nothing to
      // animate even though real inbound dependencies exist.
      // Gated on simWave so the wave-by-wave progression is actually
      // visible: an edge lights up only once its TARGET's wave has been
      // reached (inbound-boundary edges ride their target's wave).
      const simHit = simOn && this.simReached(e.to) && (
        this.simIds.has(e.from) || this.simIds.has(e.to));
      let stroke = '#b9c1cf', w = 1.3, op = .85, dash = '0', marker = 'url(#twcArrow)', cls = '';
      if (simHit) { stroke = 'var(--red)'; w = 2; op = 1; dash = '7 6'; marker = 'url(#twcArrowSim)'; cls = ' class="twc-flow"'; }
      else if (focusId) { if (onPath) { stroke = 'var(--accent)'; w = 2.1; op = 1; marker = 'url(#twcArrowHot)'; } else { op = .12; w = 1.1; } }
      svg += '<path' + cls + ' d="' + this.edgePath(a, b) + '" fill="none" stroke="' + stroke + '" stroke-width="' + w
        + '" stroke-opacity="' + op + '" stroke-linecap="round" stroke-dasharray="' + dash + '" marker-end="' + marker
        + '" style="transition:stroke-opacity .25s ease"></path>';
    });
    this.svgEl.innerHTML = svg;

    // nodes
    let html = '';
    this.nodes.forEach(n => {
      const p = P[n.id]; if (!p) return;
      const col = TWIN_KIND_COLOR[n.kind] || 'var(--muted)';
      const dim = (focusId && !(rel && rel.has(n.id))) || (matches && !matches.has(n.id));
      const isSel = twinSelected === n.id;
      const simHit = simOn && this.simIds.has(n.id) && this.simReached(n.id);
      const isSeed = simOn && this.simLevels && this.simLevels[n.id] === 0;
      const border = simHit ? 'var(--red)' : (isSel ? 'var(--accent)' : (matches && matches.has(n.id) ? 'var(--amber)' : col));
      const bg = simHit ? 'var(--red-bg)' : 'var(--surface)';
      const shadow = isSel ? '0 0 0 3px var(--accent-line), 0 8px 20px rgba(16,24,40,.14)' : '0 1px 2px rgba(16,24,40,.06)';
      const flag = simHit ? (isSeed ? 'SOURCE' : 'IMPACTED') : '';
      html += '<div class="twc-node" data-nid="' + esc(n.id) + '" style="'
        + 'position:absolute;left:0;top:0;display:flex;align-items:stretch;box-sizing:border-box;'
        + 'width:' + NW + 'px;height:' + NH + 'px;'
        + 'transform:translate3d(' + p.x + 'px,' + p.y + 'px,0);'
        + 'background:' + bg + ';border:1.5px solid ' + border + ';border-radius:9px;overflow:hidden;'
        + 'box-shadow:' + shadow + ';opacity:' + (dim ? .22 : 1) + ';cursor:grab;user-select:none;'
        + 'transition:' + (this.dragN && this.dragN.id === n.id ? 'none' : 'transform .25s cubic-bezier(.22,.7,.25,1)') + ', opacity .22s ease, box-shadow .18s ease'
        + '">'
        + '<div style="width:4px;flex:none;background:' + (simHit ? 'var(--red)' : col) + '"></div>'
        + '<div style="flex:1;min-width:0;padding:6px 8px 6px 10px">'
        + '<div style="font:600 12px inherit;color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + esc(String(n.name)) + '</div>'
        + '<div style="font:400 10px var(--mono);color:var(--muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'
        + esc(n.kind.replace(/_/g, ' ') + (n.technology ? ' · ' + n.technology : '')) + '</div></div>'
        + (flag ? '<div style="align-self:center;margin-right:8px;padding:2px 5px;border-radius:4px;background:' + (isSeed ? 'var(--red)' : 'var(--red-bg)') + ';color:' + (isSeed ? '#fff' : 'var(--red)') + ';font:700 8px inherit;letter-spacing:.06em;flex:none">' + flag + '</div>' : '')
        + '</div>';
    });
    this.nodesEl.innerHTML = html;
    this.nodesEl.querySelectorAll('.twc-node').forEach(el => {
      const nid = el.dataset.nid;
      el.onpointerdown = ev => { ev.stopPropagation(); const p = this.pos[nid];
        this.dragN = {id: nid, px: p.x, py: p.y, mx: ev.clientX, my: ev.clientY, moved: false}; };
    });

    // legend
    const counts = {};
    this.nodes.forEach(n => counts[n.kind] = (counts[n.kind] || 0) + 1);
    this.legendEl.innerHTML = Object.keys(TWIN_KIND_COLOR).filter(k => counts[k]).map(k =>
      '<div style="display:flex;align-items:center;gap:5px;padding:3px 8px;border-radius:14px;background:rgba(255,255,255,.9);border:1px solid var(--line);font:500 10.5px inherit;color:var(--ink3)">'
      + '<span style="width:6px;height:6px;border-radius:99px;background:' + TWIN_KIND_COLOR[k] + ';display:inline-block"></span>'
      + k.replace(/_/g, ' ') + ' <span style="color:var(--muted);font:500 10px var(--mono)">' + counts[k] + '</span></div>').join('');

    // minimap — skipped entirely while collapsed (render() runs on pan,
    // zoom and drag, so there's no reason to rebuild an invisible SVG)
    if (this.mapOpen === false) { this.applyFlowOffset(); if (simOn) this.startFlow(); else this.stopFlow(); return; }
    const b = this.bbox(); const MW = 180, MH = 120;
    const mk = Math.min(MW / b.w, MH / b.h) * 0.9;
    const mox = (MW - b.w * mk) / 2 - b.x * mk, moy = (MH - b.h * mk) / 2 - b.y * mk;
    this._mk = mk; this._mox = mox; this._moy = moy;
    const r = this.wrap.getBoundingClientRect();
    let mapSvg = '';
    this.nodes.forEach(n => { const p = P[n.id]; if (!p) return;
      const fill = simOn && this.simIds.has(n.id) ? 'var(--red)' : (TWIN_KIND_COLOR[n.kind] || 'var(--muted)');
      const op = (focusId && !(rel && rel.has(n.id))) ? .2 : .85;
      mapSvg += '<rect x="' + (p.x * mk + mox) + '" y="' + (p.y * mk + moy) + '" width="' + Math.max(2.5, NW * mk) + '" height="' + Math.max(2, NH * mk) + '" rx="1" fill="' + fill + '" fill-opacity="' + op + '"></rect>'; });
    mapSvg += '<rect x="' + ((-this.view.x / this.view.k) * mk + mox) + '" y="' + ((-this.view.y / this.view.k) * mk + moy)
      + '" width="' + ((r.width / this.view.k) * mk) + '" height="' + ((r.height / this.view.k) * mk)
      + '" fill="rgba(30,98,208,.1)" stroke="var(--accent)" stroke-width="1.1" rx="2"></rect>';
    this.mapSvg.innerHTML = mapSvg;

    // Paths are rebuilt from scratch on every render (innerHTML), which
    // would restart a CSS keyframe animation from 0 on each hover/pan/
    // wave-tick — the flow visibly stuttered. Drive the offset from one
    // persistent clock instead, so motion is continuous across renders.
    this.applyFlowOffset();
    if (simOn) this.startFlow(); else this.stopFlow();
  }

  applyFlowOffset() {
    const paths = this.svgEl.querySelectorAll('.twc-flow');
    if (!paths.length) return;
    const off = -((performance.now() / 1000 * 30) % 26);
    paths.forEach(p => p.style.strokeDashoffset = off + 'px');
  }
  startFlow() {
    if (this._flowRaf) return;
    const tick = () => {
      if (!this.simIds) { this._flowRaf = null; return; }
      this.applyFlowOffset();
      this._flowRaf = requestAnimationFrame(tick);
    };
    this._flowRaf = requestAnimationFrame(tick);
  }
  stopFlow() {
    if (this._flowRaf) { cancelAnimationFrame(this._flowRaf); this._flowRaf = null; }
  }

  // Has this node's migration wave been reached by the running animation?
  // Nodes outside the selection (inbound boundary) have no wave of their
  // own — they count as reached so their feeding edge can light up with
  // whichever selected node they point at.
  simReached(id) {
    if (!this.simLevels) return false;
    const lvl = this.simLevels[id];
    if (lvl === undefined) return true;
    return lvl <= this.simWave;
  }

  runSimulation(ids, levels) {
    clearInterval(this.simTimer);
    this.simIds = ids; this.simLevels = levels;
    const max = Math.max(0, ...Object.values(levels));
    this.simWave = 0; this.render();
    let w = 0;
    this.simTimer = setInterval(() => { w++; if (w > max) { clearInterval(this.simTimer); return; }
      this.simWave = w; this.render(); }, 700);
  }
  clearSimulation() { clearInterval(this.simTimer); this.stopFlow(); this.simIds = null; this.simLevels = null; this.simWave = -1; this.render(); }
}

function twinClosure(canvas, id, dir) {
  const seen = new Set(), q = [id];
  const map = dir === 'down' ? canvas.out : canvas.in;
  while (q.length) { const c = q.shift(); (map[c] || []).forEach(n => { if (!seen.has(n)) { seen.add(n); q.push(n); } }); }
  return seen;
}

let gTwinCanvas = null;

// Fullscreen takeover for the canvas — matches how established lineage tools
// (e.g. Atlan) handle this: embedded by default, with an explicit escape
// hatch to a focused view, rather than always taking the full viewport.
let twinFullscreen = false;
function twinSetFullscreen(on) {
  twinFullscreen = on;
  const root = $('#twinCanvasRoot'), wrap = $('#twinWrap'), btn = $('#twinFullscreen');
  document.body.classList.toggle('twin-fullscreen', on);
  root.classList.toggle('twin-canvas-fs', on);
  wrap.style.height = on ? '' : '72vh';
  btn.innerHTML = on ? '⤢ Exit fullscreen' : '⤢ Fullscreen';
  if (gTwinCanvas) setTimeout(() => { gTwinCanvas.userMoved = false; gTwinCanvas.fit(); }, 60);
}
$('#twinFullscreen').onclick = () => twinSetFullscreen(!twinFullscreen);
document.addEventListener('keydown', e => { if (e.key === 'Escape' && twinFullscreen) twinSetFullscreen(false); });

// Reset — back to the graph's initial state: default layout, no search
// filter, no simulation, no selection, view re-fit to content. Does NOT
// exit fullscreen (that's Escape/the fullscreen button's own job).
$('#twinReset').onclick = () => {
  if (!gTwinCanvas) return;
  $('#twinSearch').value = ''; gTwinCanvas.setQuery('');
  $('#twinLayout').value = 'lr'; $('#twinView').value = 'full';
  $('#twinTech').value = ''; twinSyncSimulateBtn();
  gTwinCanvas.clearSimulation();
  twinPinned = false;
  twinClearSelection(true);
  gTwinCanvas.layoutMode = 'lr';
  gTwinCanvas.setGraph(twinGraphNodes(), gTwin.edges || []);
  $('#twinDetail').innerHTML = '';
};

// Compact facts line from a node's metadata (rows/columns/schema for tables,
// object counts for systems) — surfaces the introspected detail on click.
function twinFacts(m) {
  if (!m) return '';
  const f = [];
  if (m.system) f.push('in ' + esc(String(m.system)));
  if (m.schema) f.push('schema ' + esc(String(m.schema)));
  if (m.object_type && m.object_type !== 'BASE TABLE') f.push(esc(String(m.object_type)));
  if (m.database) f.push('db ' + esc(String(m.database)));
  if (m.rows != null) f.push(Number(m.rows).toLocaleString() + ' rows');
  if (m.columns) f.push(m.columns + ' cols');
  if (m.tables != null && !('schema' in m)) f.push(m.tables + ' tables');
  if (m.views) f.push(m.views + ' views');
  return f.join(' · ');
}
let twinPinned = false;

function twinClearSelection(force) {
  if (twinPinned && !force) return;
  document.getElementById('twinSimInPanel')?.remove();
  twinSelected = null; twinAffected = null;
  $('#twinSidePanel').style.width = '0'; $('#twinSidePanel').style.borderLeftWidth = '0';
  $('#twinSidePanel').innerHTML = '';
  // panel is absolutely positioned — give the canvas its width back
  $('#twinWrap').style.marginRight = '0';
  if (gTwinCanvas) { gTwinCanvas.userMoved = false; gTwinCanvas.fit(); }
}

async function twinSelect(nid) {
  twinSelected = nid;
  const n = gTwin.nodes.find(x => x.id === nid);
  const panel = $('#twinSidePanel');
  panel.style.width = '320px'; panel.style.borderLeftWidth = '1px';
  // reserve the canvas width the overlaid panel covers
  $('#twinWrap').style.marginRight = '320px';
  try {
    const d = await api('/api/twin/impact?node=' + encodeURIComponent(nid));
    twinAffected = new Set(d.affected.map(a => a.id));
    if (gTwinCanvas) gTwinCanvas.render();
    const lvl = {HIGH: 'var(--red)', MEDIUM: 'var(--amber)', LOW: 'var(--green)'}[d.impact_level];
    const lvlBg = {HIGH: 'var(--red-bg)', MEDIUM: 'var(--amber-bg)', LOW: 'var(--green-bg)'}[d.impact_level];
    const ups = (gTwinCanvas.in[nid] || []).map(id => gTwin.nodes.find(x => x.id === id)).filter(Boolean);
    const downs = (gTwinCanvas.out[nid] || []).map(id => gTwin.nodes.find(x => x.id === id)).filter(Boolean);
    // max-width caps a long identifier (e.g. "snowflake (XNLRKUP-BZ56043)")
    // so it ellipsises inside the 320px panel instead of forcing the whole
    // panel to scroll sideways. title= keeps the full name reachable.
    const pill = m => '<button type="button" class="secondary twin-pill" data-nid="' + esc(m.id)
      + '" title="' + esc(m.name) + '"'
      + ' style="margin:0;height:auto;padding:4px 8px;font-size:11px;font-weight:500;white-space:nowrap;'
      + 'max-width:100%;display:inline-block;overflow:hidden;text-overflow:ellipsis;vertical-align:top">'
      + esc(m.name) + '</button>';
    panel.innerHTML =
      '<div style="padding:14px 16px 12px;border-bottom:1px solid var(--line)">'
      + '<div style="display:flex;align-items:flex-start;gap:9px">'
      + '<div style="width:9px;height:30px;border-radius:3px;background:' + (TWIN_KIND_COLOR[n.kind] || 'var(--muted)') + ';flex:none;margin-top:2px"></div>'
      + '<div style="flex:1;min-width:0"><div style="font:600 14.5px inherit;color:var(--ink);word-break:break-word">' + esc(n.name) + '</div>'
      + '<div style="font:400 11px var(--mono);color:var(--muted);margin-top:2px">' + esc(n.kind.replace(/_/g, ' '))
      + (n.technology ? ' · ' + esc(n.technology) : '') + '</div></div>'
      + '<button type="button" id="twinPanelPin" title="' + (twinPinned ? 'Unpin — clicking another node or the background will close this panel' : 'Pin — keep this panel open when clicking elsewhere')
      + '" style="all:unset;cursor:pointer;color:' + (twinPinned ? 'var(--accent)' : 'var(--muted)') + ';font-size:14px;padding:2px 6px">📌</button>'
      + '<button type="button" id="twinPanelClose" style="all:unset;cursor:pointer;color:var(--muted);font-size:15px;padding:2px 6px">×</button>'
      + '</div></div>'
      + '<div style="padding:12px 16px;border-bottom:1px solid var(--line)">'
      + '<div style="font:600 10px inherit;letter-spacing:.09em;color:var(--muted);margin-bottom:8px">BLAST RADIUS</div>'
      + '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:7px">'
      + twinStatBox(d.affected_total, 'affected')
      + twinStatBox(ups.length, 'upstream')
      + twinStatBox(d.business_endpoints.length, 'endpoints')
      + '</div>'
      + '<div style="margin-top:9px;padding:8px 9px;border-radius:6px;background:' + lvlBg + ';color:' + lvl + ';font:500 11.5px/1.4 inherit">'
      + '<b>IMPACT ' + esc(d.impact_level) + '</b> — ' + esc(d.summary) + '</div>'
      + (d.business_endpoints.length ? '<div style="margin-top:8px;font:400 11.5px inherit;color:var(--ink2)"><b>Endpoints hit:</b> '
         + d.business_endpoints.map(e => esc(e.name) + ' (' + esc(e.kind) + ')').join(', ') + '</div>' : '')
      + '</div>'
      + (twinFacts(n.metadata) ? '<div style="padding:10px 16px;border-bottom:1px solid var(--line);font:400 11.5px inherit;color:var(--ink3)">' + twinFacts(n.metadata) + '</div>' : '')
      + '<div style="padding:12px 16px;border-bottom:1px solid var(--line)">'
      + '<div style="font:600 10px inherit;letter-spacing:.09em;color:var(--muted);margin-bottom:7px">DIRECT DEPENDENCIES</div>'
      + '<div style="font:500 10.5px inherit;color:var(--ink3);margin:2px 0 5px">Upstream</div>'
      + '<div style="display:flex;flex-wrap:wrap;gap:5px;min-width:0">' + (ups.length ? ups.map(pill).join('') : '<span style="font:400 11.5px inherit;color:var(--muted)">Source node — no upstream.</span>') + '</div>'
      + '<div style="font:500 10.5px inherit;color:var(--ink3);margin:10px 0 5px">Downstream</div>'
      + '<div style="display:flex;flex-wrap:wrap;gap:5px;min-width:0">' + (downs.length ? downs.map(pill).join('') : '<span style="font:400 11.5px inherit;color:var(--muted)">Leaf node — nothing consumes it.</span>') + '</div>'
      + '</div>'
      + '<div style="padding:12px 16px 18px">'
      + '<div style="font:600 10px inherit;letter-spacing:.09em;color:var(--muted);margin-bottom:8px">ROOT CAUSE</div>'
      + '<button type="button" class="secondary" id="twinRC" style="margin:0">Root cause candidates</button>'
      + '<div id="twinRCOut"></div>'
      + '<button type="button" id="twinSimFromSel" style="width:100%;margin-top:12px"'
      + ' title="Simulate migrating ' + esc(n.name) + ' together with everything downstream of it">'
      + 'Simulate migrating this node + downstream</button>'
      + '</div>';
    panel.querySelectorAll('.twin-pill').forEach(b => b.onclick = () => { twinSelect(b.dataset.nid); gTwinCanvas.centerOn(b.dataset.nid); });
    // Gated inline (like #autofixBtn): this button is created after the
    // boot-time applyRbacUi() pass, so the RBAC_UI table can't reach it —
    // but it runs a job, exactly like the toolbar's #twinSimulate.
    if (!can('jobs:run')) {
      const sb = $('#twinSimFromSel');
      sb.disabled = true; sb.title = permTitle('jobs:run');
    }
    $('#twinPanelPin').onclick = () => { twinPinned = !twinPinned; twinSelect(nid); };
    $('#twinPanelClose').onclick = () => twinClearSelection(true);
    $('#twinRC').onclick = async () => {
      const rc = await api('/api/twin/root-cause?node=' + encodeURIComponent(nid));
      $('#twinRCOut').innerHTML = rc.candidates.length
        ? '<table style="margin-top:8px"><tr><th>Upstream object</th><th>Kind</th><th>Distance</th><th>Fan-out</th><th>Score</th></tr>'
          + rc.candidates.map(cd => '<tr><td><b>' + esc(cd.name) + '</b></td><td>' + esc(cd.kind)
            + '</td><td>' + cd.distance + '</td><td>' + cd.fan_out + '</td><td>' + cd.score + '</td></tr>').join('')
          + '</table><div style="color:var(--muted);font-size:12px;margin-top:4px">' + esc(rc.note) + '</div>'
        : '<div style="color:var(--muted);font-size:13px;margin-top:6px">Nothing upstream — this is a source.</div>';
    };
    // Migrating a node means migrating it AND everything downstream that
    // depends on it — that's what yields a real dependency-ordered wave
    // plan. Sending only the node itself always came back as one wave
    // with nothing to sequence.
    $('#twinSimFromSel').onclick = () => {
      const ids = [n.id, ...twinClosure(gTwinCanvas, n.id, 'down')];
      const names = ids.map(id => (gTwin.nodes.find(x => x.id === id) || {}).name).filter(Boolean);
      twinRunSimulation({selection: names}, n.name + ' + downstream');
    };
  } catch (e) { panel.innerHTML = '<div class="err" style="display:block;margin:16px">' + esc(e.message) + '</div>'; }
}

function twinStatBox(n, label) {
  return '<div style="background:var(--header-bg);border:1px solid var(--line);border-radius:6px;padding:7px 8px">'
    + '<div style="font:600 17px var(--mono);color:var(--ink)">' + n + '</div>'
    + '<div style="font:400 9.5px inherit;color:var(--muted);margin-top:1px">' + label + '</div></div>';
}

// Drives both the canvas's animated wave highlight and the #twinDetail
// summary from ONE real backend call — the canvas never re-derives waves
// client-side, it only visualizes what /api/twin/simulate returned.
async function twinRunSimulation(body, label) {
  $('#twinDetail').innerHTML = '<div style="color:var(--muted);font-size:13px">Simulating…</div>';
  try {
    const d = await api('/api/twin/simulate', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const nameToId = {}; gTwin.nodes.forEach(n => nameToId[n.name] = n.id);
    const ids = new Set(), levels = {};
    d.waves.forEach(w => w.objects.forEach(name => {
      const id = nameToId[name]; if (!id) return;
      ids.add(id); levels[id] = w.wave - 1;
    }));
    if (gTwinCanvas) gTwinCanvas.runSimulation(ids, levels);
    $('#twinDetail').innerHTML =
      '<div style="background:var(--green-bg);border:1px solid var(--green-line);border-radius:var(--radius);padding:12px 14px;font-size:13.5px">'
      + '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">'
      + '<b>Migration simulation — ' + esc(label) + '</b> · ' + d.selection_size
      + ' object(s) in ' + d.estimated_waves + ' wave(s)'
      + '<button type="button" class="secondary" id="twinSimClear" style="margin:0;margin-left:auto">Clear</button></div>'
      + d.waves.map(w => '<div style="margin-top:8px"><b>Wave ' + w.wave + '</b>: '
        + w.objects.map(esc).join(', ')
        + (w.external_consumers_affected.length
           ? '<br><span style="color:var(--red)">External consumers affected: ' + w.external_consumers_affected.map(esc).join(', ') + '</span>' : '')
        + '<br><span style="color:var(--muted);font-size:12.5px">' + esc(w.cutover_note) + '</span></div>').join('')
      + (d.inbound_boundary.length ? '<div style="margin-top:8px"><b>Inbound boundary (stays put, feeds the selection):</b> '
         + d.inbound_boundary.map(esc).join(', ') + '</div>' : '')
      + '<div style="color:var(--muted);font-size:12px;margin-top:6px">' + esc(d.co_migration_note) + '</div></div>';
    $('#twinSimClear').onclick = () => { if (gTwinCanvas) gTwinCanvas.clearSimulation(); $('#twinDetail').innerHTML = ''; };
    // #twinDetail lives outside the canvas container, so in fullscreen it is
    // hidden behind the overlay — surface the wave plan in the side panel
    // instead, and otherwise scroll it into view so the result is never
    // silently off-screen after a click.
    if (twinFullscreen) {
      const host = $('#twinSidePanel');
      if (host && host.style.width !== '0' && host.style.width !== '') {
        const box = document.createElement('div');
        box.id = 'twinSimInPanel';
        box.style.cssText = 'padding:12px 16px;border-top:1px solid var(--line);background:var(--green-bg)';
        // strip IDs from the clone — duplicating #twinSimClear would break
        // $('#twinSimClear') for both copies
        box.innerHTML = $('#twinDetail').innerHTML;
        box.querySelectorAll('[id]').forEach(el => {
          if (el.id === 'twinSimClear') el.dataset.role = 'simclear';
          el.removeAttribute('id');
        });
        const old = document.getElementById('twinSimInPanel'); if (old) old.remove();
        host.appendChild(box);
        box.querySelector('[data-role="simclear"]')?.addEventListener('click', () => {
          if (gTwinCanvas) gTwinCanvas.clearSimulation();
          $('#twinDetail').innerHTML = ''; box.remove();
        });
      }
    } else {
      $('#twinDetail').scrollIntoView({behavior: 'smooth', block: 'nearest'});
    }
  } catch (e) { $('#twinDetail').innerHTML = '<div class="err" style="display:block">' + esc(e.message) + '</div>'; }
}

// The technology simulate button is a no-op without a technology, so keep
// it disabled until one is chosen rather than letting a click dead-end in
// an error message far below the canvas. (RBAC may also disable it — never
// re-enable something the role isn't allowed to run.)
function twinSyncSimulateBtn() {
  const btn = $('#twinSimulate');
  if (btn.dataset.rbacBlocked) return;
  const tech = $('#twinTech').value;
  btn.disabled = !tech;
  btn.title = tech
    ? 'Simulate migrating every asset built on ' + tech
    : 'Pick a technology first — then simulate migrating every asset built on it';
}
$('#twinTech').onchange = twinSyncSimulateBtn;

$('#twinSimulate').onclick = () => {
  const tech = $('#twinTech').value;
  if (!tech) return;
  twinRunSimulation({technology: tech}, tech);
};

$('#twinTabs').querySelectorAll('button').forEach(b => b.onclick = () => twinTab(b.dataset.tab));
async function twinTab(tab) {
  $('#twinTabs').querySelectorAll('button').forEach(b =>
    b.style.background = b.dataset.tab === tab ? 'var(--accent-soft)' : '');
  const body = $('#twinTabBody');
  try {
    const d = await api('/api/twin/' + tab);
    if (tab === 'landscape')
      body.innerHTML = '<table><tr><th>Application</th><th>Technology</th><th>Domain</th><th>Owner</th><th>Pipelines</th><th>Tables</th><th>Topics</th><th>Workflows</th></tr>'
        + d.applications.map(a => '<tr><td><b>' + esc(a.application) + '</b></td><td>' + esc(a.technology || '—')
          + '</td><td>' + esc(a.domain) + '</td><td>' + esc(a.owner || '—') + '</td><td>' + a.pipelines
          + '</td><td>' + a.tables + '</td><td>' + a.topics + '</td><td>' + a.workflows + '</td></tr>').join('')
        + '</table>';
    else if (tab === 'capability-map')
      body.innerHTML = '<table><tr><th>Domain</th><th>Owner</th><th>Objects</th><th>Applications</th><th>Data products</th><th>Basis</th></tr>'
        + d.domains.map(x => '<tr><td><b>' + esc(x.domain) + '</b></td><td>' + esc(x.owner || '—')
          + '</td><td>' + x.objects + '</td><td>' + (x.applications.map(esc).join(', ') || '—')
          + '</td><td>' + (x.data_products.map(esc).join(', ') || '—')
          + '</td><td>' + (x.inferred ? statChip('INFERRED') : statChip('DECLARED')) + '</td></tr>').join('')
        + '</table>';
    else if (tab === 'inventory')
      body.innerHTML = '<table><tr><th>Technology</th><th>Total</th><th>Breakdown</th></tr>'
        + d.technologies.map(t => '<tr><td><b>' + esc(t.technology) + '</b></td><td>' + t.total
          + '</td><td>' + Object.entries(t.by_kind).map(([k, v]) => esc(k.replace(/_/g, ' ')) + ': ' + v).join(' · ')
          + '</td></tr>').join('') + '</table>';
    else
      body.innerHTML = d.dependencies.length
        ? '<table><tr><th>Application</th><th>Depends on</th><th>Via</th></tr>'
          + d.dependencies.map(x => '<tr><td><b>' + esc(x.application) + '</b></td><td><b>' + esc(x.depends_on)
            + '</b></td><td style="font-size:12px;color:var(--muted)">' + x.via.map(esc).join('<br>') + '</td></tr>').join('')
          + '</table>'
        : '<div style="color:var(--muted);font-size:13px">No cross-application dependencies detected — applications exchange nothing the twin can see.</div>';
  } catch (e) { body.innerHTML = '<div class="err" style="display:block">' + esc(e.message) + '</div>'; }
}

/* ---------------- Validation ---------------- */
async function loadValidation() {
  try { await loadValidationBody(); }
  catch (e) { loadErr('valErr', e, loadValidation); }
}
async function loadValidationBody() {
  // Filtered server-side: the 100-job cap is kind-blind, so picking converts
  // out of the mixed list here hid real runs behind unrelated jobs.
  const [activity, convertsRes] = await Promise.all([
    api('/api/jobs'), api('/api/jobs?kind=convert&status=done')]);
  const jobs = activity.jobs;
  gJobsCache = jobs;
  const converts = convertsRes.jobs;
  // Every run, not a hardcoded first 8. These tiles COUNT PROBLEMS ("Failed",
  // "Manual review"), so a sample is not a defensible answer — 8 of 19 runs
  // could report 0 failures while a failure sat in run 12. Unrelated to the
  // Overview's Summarize window, which never applied here. Reports are
  // memoized and throttled, so this costs the uncached delta only.
  await jobReportsWarm(converts.map(j => j.id));
  const reports = await mapLimit(converts, 8, j => jobReport(j.id));
  const verdicts = reports.filter(Boolean).map(r => (r.migration_validation || {}).verdict).filter(Boolean);
  const scope = verdicts.length + ' of ' + converts.length + ' runs';
  $('#valCards').innerHTML =
    mcard(mnum(verdicts.length, converts.length > 0), 'Validation runs', converts.length ? scope : 'from modernization runs')
    + mcard(verdicts.length ? Math.round(100 * verdicts.filter(v => v === 'PASS' || v === 'PASS_WITH_WARNINGS').length / verdicts.length) + '%' : '--', 'Pass rate', verdicts.length ? 'of validated runs' : '')
    + mcard(verdicts.filter(v => v === 'FAIL').length, 'Failed', '')
    + mcard(verdicts.filter(v => v === 'MANUAL_REVIEW').length, 'Manual review', '');
  const valOptLabel = j => {
    const route = [j.source, j.target].filter(Boolean).join(' → ');
    const when = (j.created || '').replace('T', ' ').slice(0, 16);
    return [j.project || j.id, route, when, String(j.id).slice(0, 8)]
      .filter(Boolean).join(' · ');
  };
  $('#valJob').innerHTML = '<option value="">Select…</option>'
    + converts.map(j => '<option value="' + j.id + '">' + esc(valOptLabel(j)) + '</option>').join('');
  $('#valJob').onchange = async ev => {
    if (!ev.target.value) return;
    $('#valBody').innerHTML = '<div style="color:var(--muted);font-size:13px">Loading validation results…</div>';
    const r = await jobReport(ev.target.value);
    if (!r) { $('#valBody').innerHTML = '<div class="empty"><b>No report available</b></div>'; return; }
    window._valCtx = {migration_id: ev.target.value, project: r.project};
    const mv = r.migration_validation || {};
    const layers = mv.layers || {};
    const layerRow = (name, label) => layers[name]
      ? '<tr><td>' + label + '</td><td>' + statChip(verdictToStatus(layers[name])) + '</td></tr>' : '';
    const vt = r.validation_tests || {};
    $('#valBody').innerHTML =
      '<div style="margin-bottom:10px">Overall verdict: ' + statChip(verdictToStatus(mv.verdict)) + '</div>'
      + '<table style="max-width:640px">'
      + layerRow('syntax_validation', 'Schema &amp; syntax')
      + layerRow('dependency_validation', 'Dependencies')
      + layerRow('semantic_transformation_validation', 'Transformation semantics')
      + layerRow('source_target_reconciliation_validation', 'Source-target reconciliation (row counts, nulls, aggregates, checksums)')
      + '</table>'
      + (vt.by_type ? '<div class="drawer-meta" style="margin-top:14px"><b>Generated validation checks</b></div>'
        + '<table style="max-width:640px">' + Object.entries(vt.by_type).filter(([k, n]) => n)
          .map(([k, n]) => '<tr><td>' + esc(k.replace(/_/g, ' ')) + '</td><td>' + n + '</td></tr>').join('') + '</table>' : '')
      + '<div class="drawer-meta" style="margin-top:14px"><b>AI Semantic Review</b> <span style="color:var(--muted)">(semantic reasoning by MetaBridge AI — not deterministic validation)</span></div>'
      + (layers.ai_semantic_review && layers.ai_semantic_review !== 'SKIPPED'
         ? '<div>' + statChip(verdictToStatus(layers.ai_semantic_review)) + '</div>'
         : '<div style="font-size:13px;color:var(--muted)">Not run for this migration — enable ai_review in the conversion options, or ask MetaBridge AI below.</div>');
  };
}

/* ---------------- Reports ---------------- */
const REPORT_KINDS = {convert: 'Migration report', analyze: 'Analysis report',
                      govern: 'Governance report', scaffold: 'Pipeline generation report',
                      upload: 'Uploaded workload'};
/* REPORT_KINDS covers 5 kinds; the Reports table renders ~18. The missing ones
   fell through to the raw slug, so one column mixed "Pipeline generation
   report" with `objects`, `agents` and `docs`. Fall back to the shared job-kind
   label instead of the storage key. */
function reportKindLabel(k) { return REPORT_KINDS[k] || jobKindLabel(k); }
/* ---------------- System (MetaBridge OS) ---------------- */
const SYS_HEALTH_COLOR = {available: 'var(--green)', external: '#2d6cdf',
  degraded: 'var(--amber)', error: 'var(--red)'};
function sysHc(s) { return SYS_HEALTH_COLOR[s] || 'var(--ink3)'; }

/* The one display-name map for job kinds. It used to be SYS_KIND_LABELS, used
   only by the System page — so System said "Agent run" / "Object inventory"
   while the Overview type filter, the Reports table, global search and the job
   detail header all showed the raw storage slugs (`agents`, `objects`,
   `events_convert`, `orchestration_convert`). Reports even mixed both registers
   in one column. Everything now goes through jobKindLabel(). */
const KIND_LABELS = {convert: 'Modernization', analyze: 'Analysis', govern: 'Governance scan',
  scaffold: 'Pipeline scaffold', twin: 'Digital twin build', assessment: 'Assessment',
  ai_readiness: 'AI readiness', debt: 'Tech-debt scan', finops: 'FinOps', security: 'Security scan',
  docs: 'Documentation', events: 'Event estate', sap: 'SAP analysis', orchestration: 'Orchestration',
  objects: 'Object inventory', objects_convert: 'Object migration package',
  events_convert: 'Event stream conversion',
  orchestration_convert: 'Orchestration conversion',
  agents: 'Agent run'};
const SYS_KIND_LABELS = KIND_LABELS;     // legacy alias
/* Unknown kinds fall back to a de-slugged form rather than the raw key, so a
   new engine shipped before this map is updated still reads as prose. */
function jobKindLabel(k) {
  if (!k) return 'Job';
  return KIND_LABELS[k] || String(k).replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}
const SYS_VERDICTS = {
  HEALTHY:   {c: '#1e8449', bg: '#eaf6ef', label: 'All systems healthy', icon: '●'},
  ATTENTION: {c: '#c77d0a', bg: '#fdf6e9', label: 'Needs attention', icon: '●'},
  DEGRADED:  {c: '#c0392b', bg: 'var(--red-bg)', label: 'Action required', icon: '●'}};

function sysNavigate(page, sec) {
  const a = document.querySelector('nav a[data-page="' + page + '"]');
  if (a) a.click();
  if (sec) showSettings(sec, true);
}

async function loadSystem() {
  if (!$('#sysEngines')) return;
  const err = $('#sysErr'); err.style.display = 'none';

  // 1) the command center — workspace health, attention, activity, usage
  let ins = null;
  try { ins = await api('/api/system/insights'); }
  catch (e) { err.style.display = 'block'; err.textContent = e.message; }
  if (ins) {
    const wsName = (ins.workspace || {}).name;
    $('#osName').textContent = 'System' + (wsName ? ' · ' + wsName : '');
    const v = SYS_VERDICTS[(ins.health || {}).verdict] || SYS_VERDICTS.HEALTHY;
    const banner = $('#sysHealthBanner');
    banner.style.background = v.bg;
    banner.style.border = '1px solid ' + v.c + '33';
    $('#sysVerdict').innerHTML = '<span style="color:' + v.c + ';font-size:17px">' + v.icon + '</span>'
      + '<span style="color:' + v.c + '">' + v.label + '</span>'
      + '<span style="color:var(--ink3);font-weight:400;font-size:12px;margin-left:6px">as of ' + esc((ins.as_of || '').replace('T', ' ')) + '</span>';
    $('#sysComponents').innerHTML = ((ins.health || {}).components || []).map(c => {
      const col = c.status === 'err' ? '#c0392b' : c.status === 'warn' ? '#c77d0a' : '#1e8449';
      return '<span style="display:inline-flex;align-items:center;gap:6px;background:var(--surface);border:1px solid #e2e6ee;border-radius:20px;padding:4px 12px;font-size:12px">'
        + '<span style="width:8px;height:8px;border-radius:50%;background:' + col + '"></span>'
        + '<b>' + esc(c.name) + '</b><span style="color:var(--ink3)">' + esc(c.detail) + '</span></span>';
    }).join('');

    const u = ins.usage || {}, cs = u.connections || {}, es = u.estate || {};
    $('#sysMetrics').innerHTML =
      '<span><b>' + (u.jobs_7d || 0) + '</b> Jobs this week'
      + (u.success_rate_7d_pct != null ? ' · <span style="color:' + (u.success_rate_7d_pct >= 90 ? '#1e8449' : '#c77d0a') + '">' + u.success_rate_7d_pct + '% success</span>' : '') + '</span>'
      + '<span><b>' + (cs.connected || 0) + '/' + (cs.total || 0) + '</b> Connections healthy</span>'
      + '<span><b>' + (es.built ? Number(es.tables || 0).toLocaleString() : '--') + '</b> Tables in estate'
      + (es.built && es.rows ? ' · ' + Number(es.rows).toLocaleString() + ' rows' : (es.built ? '' : ' · build the Digital Twin')) + '</span>'
      + '<span><b>' + (u.approvals_pending || 0) + '</b> Approvals pending</span>'
      + '<span><b>' + ((ins.workspace || {}).members || 0) + '</b> Workspace members</span>';

    const att = ins.attention || [];
    $('#sysAttention').innerHTML = att.length ? att.map(a => {
      const col = a.severity === 'critical' ? '#c0392b' : a.severity === 'warning' ? '#c77d0a' : 'var(--ink3)';
      return '<div style="display:flex;gap:10px;align-items:flex-start;padding:9px 0;border-bottom:1px solid #f0f2f6">'
        + '<span style="width:8px;height:8px;border-radius:50%;background:' + col + ';margin-top:5px;flex:none"></span>'
        + '<div style="flex:1;min-width:0"><div style="font-weight:600;font-size:13px">' + esc(a.title) + '</div>'
        + '<div style="font-size:12px;color:var(--ink3)">' + esc(a.detail || '') + '</div></div>'
        + (a.page ? '<button class="secondary sys-att-go" data-page="' + esc(a.page) + '" data-sec="' + esc(a.sec || '') + '" style="margin:0;padding:4px 12px;font-size:12px;flex:none">Open</button>' : '')
        + '</div>';
    }).join('')
      : '<div style="display:flex;align-items:center;gap:10px;padding:14px 0;color:#1e8449;font-weight:600;font-size:13.5px">✓ All clear — nothing needs your attention.</div>';
    $('#sysAttention').querySelectorAll('.sys-att-go').forEach(b =>
      b.onclick = () => sysNavigate(b.dataset.page, b.dataset.sec));

    const acts = ins.activity || [];
    $('#sysActivity').innerHTML = acts.length ? acts.map(j => {
      // Lifecycle state, not process state — this list describes the same runs
      // as Overview and the Reports table and must not disagree with them.
      const dur = (j.seconds != null) ? (j.seconds < 60 ? j.seconds + 's' : Math.round(j.seconds / 60) + 'm') : '';
      return '<div class="sys-act-row" data-id="' + esc(j.id) + '" style="display:flex;gap:10px;align-items:center;padding:7px 0;border-bottom:1px solid #f0f2f6;cursor:pointer">'
        + '<span style="font-weight:600;font-size:12.5px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(SYS_KIND_LABELS[j.kind] || j.kind) + '</span>'
        + '<span style="font-size:11.5px">' + runStatusChip(j) + '</span>'
        + (dur ? '<span style="font-size:11.5px;color:var(--muted2)">' + dur + '</span>' : '')
        + '<span style="font-size:11.5px;color:var(--muted2);flex:none">' + esc(fmtAgo(j.created)) + '</span></div>';
    }).join('')
      : '<div style="color:#889;font-size:13px;padding:10px 0">No runs yet in this workspace — start in Modernize, Pipeline Studio or Governance.</div>';
    $('#sysActivity').querySelectorAll('.sys-act-row').forEach(r =>
      r.onclick = () => openJobDetail(r.dataset.id).catch(e => mbAlert(e.message)));
  }

  // 2) platform internals (the OS manifest) — collapsed by default
  let d;
  try { d = await api('/api/system'); }
  catch (e) { err.style.display = 'block'; err.textContent = e.message; return; }
  const os = d.os || {};
  const hs = (d.health || {}).summary || {};
  $('#sysOsMetrics').innerHTML =
    '<span><b>' + (os.engine_count || 0) + '</b> Engines</span>'
    + '<span><b>' + (os.service_count || 0) + '</b> Platform services</span>'
    + '<span><b>' + (os.canonical_model_count || 0) + '</b> Canonical models</span>'
    + '<span><b>v' + esc(os.version || '?') + '</b> OS version</span>'
    + '<span><b>' + (hs.available || 0) + '</b> available'
    + (hs.error ? ' · <span style="color:var(--red)">' + hs.error + ' error</span>' : '')
    + (hs.degraded ? ' · <span style="color:var(--amber)">' + hs.degraded + ' degraded</span>' : '')
    + '</span>';

  // engines grouped by category
  const cats = d.engines_by_category || {};
  $('#sysEngCount').textContent = (d.engines || []).length + ' engines';
  $('#sysEngines').innerHTML = Object.keys(cats).filter(c => (cats[c] || []).length)
    .map(c => '<div style="margin:12px 0 6px"><b style="font-size:13px">' + esc(c)
      + '</b> <span style="color:var(--muted);font-size:12px">(' + cats[c].length + ')</span></div>'
      + '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px">'
      + cats[c].map(e => sysEngineCard(e)).join('') + '</div>').join('');

  // services
  $('#sysServices').innerHTML = (d.services || []).map(s =>
    '<div style="display:flex;align-items:center;gap:10px;padding:5px 0;border-bottom:1px solid #f0f2f6">'
    + '<span style="width:9px;height:9px;border-radius:50%;background:' + sysHc(s.health) + '"></span>'
    + '<span style="font-weight:600;font-size:12.5px;min-width:150px">' + esc(s.name) + '</span>'
    + '<span style="font-size:11.5px;color:' + sysHc(s.health) + '">' + esc(s.health) + '</span>'
    + '<span style="flex:1;font-size:11.5px;color:var(--muted)">' + esc(s.description) + '</span>'
    + '<span style="font-size:10.5px;background:var(--line);border-radius:3px;padding:1px 6px">' + esc(s.layer) + '</span></div>').join('');

  // canonical models with produced_by -> consumed_by
  $('#sysCanonical').innerHTML = (d.canonical_models || []).map(m =>
    '<div style="padding:6px 0;border-bottom:1px solid #f0f2f6">'
    + '<div><span style="width:8px;height:8px;border-radius:50%;display:inline-block;background:'
    + (m.available ? 'var(--green)' : 'var(--red)') + ';margin-right:6px"></span>'
    + '<b style="font-size:12.5px">' + esc(m.name) + '</b> '
    + '<span style="color:var(--muted2);font-size:11px">' + esc(m.ref) + '</span></div>'
    + '<div style="font-size:11px;color:var(--ink3);margin-top:2px">'
    + '<b>' + (m.produced_by || []).length + '</b> producer(s) → <b>'
    + (m.consumed_by || []).length + '</b> consumer(s)</div></div>').join('');

  // feature flags — toggling is a configuration action (settings:manage)
  $('#sysFlags').innerHTML = (d.feature_flags || []).map(f =>
    '<div style="display:flex;align-items:center;gap:10px;padding:5px 0;border-bottom:1px solid #f0f2f6">'
    + '<button type="button" class="sys-flag" data-key="' + esc(f.key) + '" data-on="'
    + (f.enabled ? '1' : '') + '" style="margin:0;padding:2px 10px;font-size:11.5px;'
    + 'background:' + (f.enabled ? 'var(--green-fill)' : 'var(--border)') + ';color:' + (f.enabled ? '#fff' : 'var(--ink3)')
    + '"' + (can('settings:manage') ? '' : ' disabled title="' + esc(permTitle('settings:manage')) + '"')
    + '>' + (f.enabled ? 'ON' : 'OFF') + '</button>'
    + '<span style="font-weight:600;font-size:12px;min-width:170px">' + esc(f.key) + '</span>'
    + '<span style="flex:1;font-size:11.5px;color:var(--muted)">' + esc(f.description || '')
    + (f.rollout_pct != null && f.rollout_pct < 100 ? ' · ' + f.rollout_pct + '% rollout' : '')
    + ((f.roles || []).length ? ' · roles: ' + f.roles.map(esc).join(',') : '') + '</span></div>').join('');

  // versions + notifications
  $('#sysVersions').innerHTML = '<tr><th>Component</th><th>Version</th></tr>'
    + (d.versions || []).map(v => '<tr><td>' + esc(v.component) + '</td><td>' + esc(v.version) + '</td></tr>').join('');
  const nc = d.notifications || {};
  $('#sysNotifCount').textContent = nc.total ? '(' + (nc.unseen || 0) + ' unseen / ' + nc.total + ')' : '(none)';
  try {
    const nd = await api('/api/system/notifications?limit=20');
    $('#sysNotifs').innerHTML = (nd.notifications || []).length
      ? nd.notifications.map(n => {
          const c = n.severity === 'critical' ? 'var(--red)' : n.severity === 'warning'
            ? 'var(--amber)' : n.severity === 'success' ? 'var(--green)' : 'var(--ink3)';
          return '<div style="padding:4px 0;border-bottom:1px solid #f4f5f8">'
            + '<span style="color:' + c + ';font-weight:600">' + esc(n.title) + '</span>'
            + ' <span style="color:var(--muted2);font-size:11px">' + esc(n.topic) + '</span></div>';
        }).join('')
      : '<div style="color:var(--muted)">No notifications.</div>';
  } catch (e) { $('#sysNotifs').innerHTML = ''; }
}

function sysEngineCard(e) {
  const col = sysHc(e.health);
  const io = [(e.consumes || []).length ? 'consumes ' + e.consumes.map(esc).join(', ') : '',
              (e.produces || []).length ? 'produces ' + e.produces.map(esc).join(', ') : '']
    .filter(Boolean).join(' · ');
  return '<div style="border:1px solid var(--border);border-radius:var(--radius);padding:10px 12px;background:var(--surface)">'
    + '<div style="display:flex;align-items:center;gap:8px">'
    + '<span style="width:9px;height:9px;border-radius:50%;background:' + col + '"></span>'
    + '<span style="font-weight:600;font-size:12.5px;flex:1">' + esc(e.name) + '</span>'
    + '<span style="font-size:10.5px;color:' + col + '">' + esc(e.health) + '</span></div>'
    + '<div style="font-size:11.5px;color:var(--ink3);margin:4px 0;min-height:30px">' + esc(e.description) + '</div>'
    + (io ? '<div style="font-size:10.5px;color:var(--muted2)">' + esc(io) + '</div>' : '')
    + (e.api_prefix ? '<div style="font-size:10.5px;color:var(--muted2);margin-top:2px">' + esc(e.api_prefix) + '</div>' : '')
    + '</div>';
}

document.addEventListener('click', async (ev) => {
  const btn = ev.target.closest('.sys-flag');
  if (!btn) return;
  ev.preventDefault();
  const on = btn.dataset.on === '1';
  btn.disabled = true;
  try {
    await api('/api/system/flags', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key: btn.dataset.key, enabled: !on})});
    await loadSystem();
  } catch (e) {
    mbAlert(e.message + (e.message.includes('permission') || e.message.includes('403')
      ? '\n\nThis action needs Settings-manage rights.' : ''), {title: 'Flag update failed'});
    btn.disabled = false;
  }
});

if ($('#sysRefresh')) $('#sysRefresh').onclick = loadSystem;

/* ---------------- Observability ---------------- */
const OBS_MON_LABELS = {
  pipeline_health: 'Pipeline health', migration_progress: 'Migration progress',
  validation_status: 'Validation status', agent_health: 'Agent health',
  connector_health: 'Connector health', performance: 'Performance',
  latency: 'Latency', failures: 'Failures',
  resource_utilization: 'Resource utilization',
  cloud_consumption: 'Cloud consumption',
};
const OBS_STATUS_COLOR = {
  healthy: 'var(--green)', ok: 'var(--green)', pass: 'var(--green)', complete: 'var(--green)',
  in_progress: '#2d6cdf', partial: 'var(--amber)', warn: 'var(--amber)',
  degraded: 'var(--amber)', fail: 'var(--red)', critical: 'var(--red)',
  no_data: 'var(--muted2)',
};
function obsColor(s) { return OBS_STATUS_COLOR[s] || 'var(--ink3)'; }

function obsGaugeSvg(score, band) {
  if (score == null) return '<div style="width:120px;height:120px;display:flex;'
    + 'align-items:center;justify-content:center;color:var(--muted2);font-size:13px;'
    + 'border:2px dashed #dfe3ea;border-radius:50%">no data</div>';
  const col = obsColor(band), circ = 2 * Math.PI * 52,
        off = circ * (1 - score / 100);
  return '<svg width="120" height="120" viewBox="0 0 120 120">'
    + '<circle cx="60" cy="60" r="52" fill="none" stroke="var(--line)" stroke-width="12"/>'
    + '<circle cx="60" cy="60" r="52" fill="none" stroke="' + col + '" stroke-width="12"'
    + ' stroke-linecap="round" stroke-dasharray="' + circ + '" stroke-dashoffset="' + off
    + '" transform="rotate(-90 60 60)"/>'
    + '<text x="60" y="56" text-anchor="middle" font-size="26" font-weight="700" fill="'
    + col + '">' + score + '</text>'
    + '<text x="60" y="76" text-anchor="middle" font-size="11" fill="var(--muted)">/ 100</text></svg>';
}

async function loadObservability() {
  if (!$('#obsMonitors')) return;
  const err = $('#obsErr'); err.style.display = 'none';
  $('#obsExport').href = withKey('/api/observability/export');
  let d;
  try { d = await api('/api/observability'); }
  catch (e) { err.style.display = 'block'; err.textContent = e.message; return; }

  // health, monitors, SLOs and alerts all read from this one payload
  gObs = d;
  wireObsTop();
  paintObsHealth();
  paintObsRibbon();
  paintObsMonitors();
  paintObsSla();
  paintObsAlerts();

  // Trends (sparkline of success rate + ops per day)
  const tr = d.performance_trends || {};
  $('#obsTrendDir').textContent = tr.direction ? '· ' + tr.direction : '';
  gObsTrend = tr.series || [];
  wireObsTrends();
  paintObsTrends();

  // Historical
  gObsHist = d.historical_analytics || {};
  wireObsHist();
  paintObsHist();
}

/* Job wall-clock can span hours, and "5446.3 s" is not a readable number. */
function obsMs(v) {
  if (v == null) return 'n/a';
  if (v < 1000) return Math.round(v) + ' ms';
  if (v < 60000) return (v / 1000).toFixed(1) + ' s';
  if (v < 3600000) return (v / 60000).toFixed(1) + ' min';
  return (v / 3600000).toFixed(1) + ' h';
}

/* Durations arrive rounded to whole milliseconds, so a stored 0 means "faster
   than a millisecond", not "took no time" — say so rather than printing 0 ms. */
function obsDur(v) { return v == null ? '\u2014' : v === 0 ? '<1 ms' : obsMs(v); }

/* ---------------- Historical analytics ----------------
   Same payload as before, but the parts that were dropped on the floor are
   now shown: the status mix (by_status), mean job duration, and the job-kind
   breakdown as a ranked bar list rather than a 20-item comma run-on. */
let gObsHist = {};
let gObsHistSort = 'count';      // 'count' | 'name'
let gObsHistAll = false;         // show every kind, not just the top slice
const HA_TOP = 8;

const HA_STATUS = {
  done: ['#157F3D', 'Succeeded'], ok: ['#157F3D', 'Succeeded'],
  failed: ['#B42318', 'Failed'], error: ['#B42318', 'Failed'],
  running: ['#2d6cdf', 'Running'], pending: ['#B45309', 'Pending'],
  queued: ['#B45309', 'Queued'], cancelled: ['#7A8794', 'Cancelled'],
};
function haStatus(k) { return HA_STATUS[k] || ['var(--muted2)', k]; }
/* A rare kind is "<1%", never "0%" — it did happen. */
function haShare(v, total) {
  if (!total) return '0%';
  const pc = 100 * v / total;
  return v && pc < 0.5 ? '<1%' : Math.round(pc) + '%';
}

function paintObsHist() {
  const host = $('#obsHistorical');
  if (host) host.innerHTML = renderObsHist(gObsHist);
}

function renderObsHist(hi) {
  const jobs = hi.jobs_total || 0, runs = hi.agent_runs_total || 0;
  if (!jobs && !runs) {
    return '<div style="color:var(--ink3);font-size:13px">'
      + 'No run history analyzed yet.</div>';
  }
  const span = hi.date_span;
  let days = null;
  if (span && span.from && span.to) {
    days = Math.round((Date.parse(span.to) - Date.parse(span.from)) / 864e5) + 1;
  }
  const st = hi.by_status || {};
  const stEntries = Object.entries(st).sort((a, b) => b[1] - a[1]);
  const stTotal = stEntries.reduce((a, kv) => a + kv[1], 0);
  const settled = (st.done || 0) + (st.failed || 0);
  const rate = hi.overall_success_rate_pct;

  /* ---- headline numbers (the panel used to show none) ---- */
  const kpi = (label, val, sub) => '<div class="obs-kpi"><dt>' + label
    + '</dt><dd>' + val + (sub ? ' <small>' + sub + '</small>' : '') + '</dd></div>';
  const kpis = '<dl class="obs-kpis">'
    + kpi('Jobs analyzed', jobs.toLocaleString(),
          days ? 'over ' + days + ' day' + (days > 1 ? 's' : '') : '')
    + kpi('Agent runs', runs.toLocaleString(),
          hi.mean_run_ms != null ? esc(obsDur(hi.mean_run_ms)) + ' mean' : '')
    + kpi('Success rate', settled ? rate + '%' : '\u2014',
          settled ? settled.toLocaleString() + ' settled' : 'nothing settled')
    + kpi('Mean job', esc(obsDur(hi.mean_job_ms)), 'end to end')
    + kpi('History window', span ? esc(span.from.slice(5)) + ' \u2192 '
          + esc(span.to.slice(5)) : '\u2014', span ? esc(span.from.slice(0, 4)) : '')
    + '</dl>';

  /* ---- status mix: proportions, so one stacked bar beats five numbers ---- */
  let mix = '';
  if (stTotal) {
    mix = '<div class="ha-sec"><h4>Status mix</h4><div class="ha-mix" role="img" '
      + 'aria-label="' + esc(stEntries.map(kv =>
          kv[1] + ' ' + haStatus(kv[0])[1].toLowerCase()).join(', ')) + '">'
      + stEntries.map(kv => {
          const c = haStatus(kv[0])[0], lbl = haStatus(kv[0])[1];
          return '<span style="width:' + (100 * kv[1] / stTotal).toFixed(2)
            + '%;background:' + c + '" title="' + esc(lbl) + ': ' + kv[1] + ' ('
            + haShare(kv[1], stTotal) + ')"></span>';
        }).join('')
      + '</div><div class="ha-keys">'
      + stEntries.map(kv => {
          const c = haStatus(kv[0])[0], lbl = haStatus(kv[0])[1];
          return '<span class="ha-key"><i style="background:' + c + '"></i>'
            + esc(lbl) + ' <b>' + kv[1].toLocaleString() + '</b><span>('
            + haShare(kv[1], stTotal) + ')</span></span>';
        }).join('')
      + '</div></div>';
  }

  /* ---- job kinds: ranked bars, sortable, collapsed to the top slice ---- */
  let kindSec = '';
  const kinds = Object.entries(hi.by_kind || {});
  if (kinds.length) {
    const sorted = kinds.slice().sort(gObsHistSort === 'name'
      ? (a, b) => a[0].localeCompare(b[0])
      : (a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    const shown = gObsHistAll ? sorted : sorted.slice(0, HA_TOP);
    const total = kinds.reduce((a, kv) => a + kv[1], 0);
    const peak = Math.max.apply(null, kinds.map(kv => kv[1]));
    const hidden = sorted.length - shown.length;
    kindSec = '<div class="ha-sec"><div class="ha-head">'
      + '<h4 style="margin:0">Job kinds <span style="text-transform:none;'
      + 'letter-spacing:0;font-weight:500">\u00b7 ' + kinds.length
      + ' distinct</span></h4>'
      + '<span class="grow"></span>'
      + '<div class="seg" role="tablist" aria-label="Sort job kinds">'
      + [['count', 'By volume'], ['name', 'A\u2013Z']].map(kv =>
          '<button role="tab" data-hasort="' + kv[0] + '" aria-selected="'
          + (gObsHistSort === kv[0]) + '">' + kv[1] + '</button>').join('')
      + '</div></div><div class="ha-rows">'
      + shown.map(kv =>
          '<div class="ha-row"><span class="k" title="' + esc(kv[0]) + '"><code>'
          + esc(kv[0]) + '</code></span>'
          + '<span class="ha-bar"><i style="width:'
          + (100 * kv[1] / peak).toFixed(1) + '%"></i></span>'
          + '<span class="v">' + kv[1].toLocaleString() + '<small>('
          + haShare(kv[1], total) + ')</small></span></div>').join('')
      + '</div>'
      + (hidden > 0 || gObsHistAll
        ? '<button type="button" class="jact" data-hamore="1" style="margin-top:8px" '
          + 'aria-expanded="' + gObsHistAll + '">'
          + (gObsHistAll
            ? 'Show ' + (gObsHistSort === 'name' ? 'first ' : 'top ') + HA_TOP
            : 'Show all ' + sorted.length + ' kinds')
          + '</button>'
        : '')
      + '</div>';
  }

  /* ---- latency: three different measures, each labelled for what it is ---- */
  const durSec = '<div class="ha-sec"><h4>Latency</h4><dl class="ha-dur">'
    + '<div><dt>Mean job</dt><dd>' + esc(obsDur(hi.mean_job_ms))
    + '</dd><p>Whole job, submit to finish.</p></div>'
    + '<div><dt>Mean agent run</dt><dd>' + esc(obsDur(hi.mean_run_ms))
    + '</dd><p>One full agent run.</p></div>'
    + '<div><dt>Median agent action</dt><dd>' + esc(obsDur(hi.median_action_ms))
    + '</dd><p>Single step inside a run.</p></div></dl></div>';

  return kpis + mix + kindSec + durSec;
}

function wireObsHist() {
  const host = $('#obsHistorical');
  if (!host || host.dataset.wired) return;
  host.dataset.wired = '1';
  host.addEventListener('click', e => {
    const so = e.target.closest('[data-hasort]');
    if (so) { gObsHistSort = so.dataset.hasort; paintObsHist(); return; }
    if (e.target.closest('[data-hamore]')) {
      gObsHistAll = !gObsHistAll; paintObsHist();
    }
  });
}

/* ---------------- Observability: verdict / attention / monitors ------------
   Three zones instead of six sibling panels, and a distinct form per kind of
   number rather than one progress bar for everything:

   - the six health signals are independent named metrics, so they get big
     numerals in a matrix (a hexagon/polystat grid is for aggregating many
     related services into composites, and hides its labels behind tooltips);
   - state over time gets a uniform status ribbon, one cell per day, colour
     plus a hatch on bad days — a green/red-only strip is unreadable for the
     ~8% of men with red-green colour deficiency;
   - objectives lead with burn rate, the multiple of sustainable error spend,
     because "how fast is the budget going" is the actionable figure;
   - monitors are grouped rows, worst first, with a shape + word + colour on
     each so no single channel carries the state.

   contributors / top_detractors / sample / value / threshold were all already
   in the payload and none of them were ever rendered. */
let gObs = {};
let gObsMonFilter = 'all';
let gObsAlertSev = '';           // '' = all severities

const OBS_SIG_LABELS = {
  pipeline: 'Pipeline', reliability: 'Reliability', agents: 'Agents',
  validation: 'Validation', connectors: 'Connectors', latency: 'Latency',
};
const OBS_ATTENTION = ['degraded', 'partial', 'fail', 'critical', 'warn',
                       'no_data'];
/* Burn-rate bands from multi-window SRE practice: 1x is spending the budget
   exactly as fast as it is granted, 6x and 14.4x are the usual page-worthy
   multiples for a 30-day window. */
const OBS_BURN = [[14.4, 'critical burn', 'var(--red)'],
                  [6, 'fast burn', 'var(--red)'],
                  [1, 'elevated', 'var(--amber)'],
                  [0, 'sustainable', 'var(--green)']];

/* Geometric status marks. Shape, not just hue: filled disc = healthy,
   half disc = degraded, cross = failed, hollow ring = nothing measured. */
const OBS_MARK = {
  ok: 'disc', pass: 'disc', healthy: 'disc', complete: 'disc',
  in_progress: 'disc', partial: 'half', warn: 'half', degraded: 'half',
  fail: 'cross', critical: 'cross', no_data: 'ring',
};
function obsMarkSvg(status, col) {
  const kind = OBS_MARK[status] || 'ring';
  const open = '<svg class="omr-ic" viewBox="0 0 16 16" aria-hidden="true" '
    + 'fill="none" stroke="' + col + '" stroke-width="2">';
  if (kind === 'disc') {
    return open + '<circle cx="8" cy="8" r="6" fill="' + col
      + '" stroke="none"/></svg>';
  }
  if (kind === 'half') {
    return open + '<circle cx="8" cy="8" r="6"/>'
      + '<path d="M8 2a6 6 0 0 1 0 12Z" fill="' + col + '" stroke="none"/></svg>';
  }
  if (kind === 'cross') {
    return open + '<circle cx="8" cy="8" r="6"/>'
      + '<path d="M5.6 5.6l4.8 4.8M10.4 5.6l-4.8 4.8" stroke-linecap="round"/>'
      + '</svg>';
  }
  return open + '<circle cx="8" cy="8" r="6" stroke-dasharray="2.6 2.4"/></svg>';
}
function obsSevSvg(sev) {
  if (sev === 'critical') {
    return '<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" '
      + 'stroke="currentColor" stroke-width="2" stroke-linecap="round">'
      + '<circle cx="8" cy="8" r="6"/><path d="M5.6 5.6l4.8 4.8M10.4 5.6l-4.8 4.8"/>'
      + '</svg>';
  }
  if (sev === 'warning') {
    return '<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" '
      + 'stroke="currentColor" stroke-width="2" stroke-linecap="round">'
      + '<path d="M8 1.8 14.6 13.4H1.4Z"/><path d="M8 6.4v3.1"/>'
      + '<path d="M8 11.6h.01"/></svg>';
  }
  return '<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" '
    + 'stroke="currentColor" stroke-width="2" stroke-linecap="round">'
    + '<circle cx="8" cy="8" r="6"/><path d="M8 7.4v3.4"/>'
    + '<path d="M8 5.2h.01"/></svg>';
}
function obsTickSvg() {
  return '<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" '
    + 'stroke="currentColor" stroke-width="2.2" stroke-linecap="round" '
    + 'stroke-linejoin="round"><path d="M2.8 8.4l3.4 3.4 7-7.6"/></svg>';
}

function obsScoreColor(v) {
  return v == null ? 'var(--line)'
    : v >= 90 ? 'var(--green)' : v >= 75 ? '#B45309' : v >= 50 ? 'var(--amber)'
    : 'var(--red)';
}
/* Naming the band in words keeps every score from being colour-only. */
function obsScoreBand(v) {
  return v == null ? 'not scored'
    : v >= 90 ? 'strong' : v >= 75 ? 'fair' : v >= 50 ? 'weak' : 'poor';
}

function paintObsHealth() {
  const h = gObs.health_score || {};
  const act = (gObs.operational_dashboard || {}).active || {};
  $('#obsGauge').innerHTML = obsGaugeSvg(h.score, h.band);

  /* Lead with the verdict in plain words, not the engine's sentence. */
  const band = h.band === 'insufficient' ? 'provisional' : (h.band || 'unknown');
  $('#obsHealthHeadline').textContent = h.score == null
    ? 'Not enough history to score operational health'
    : 'Operational health is ' + band;

  const thin = h.sample === 'thin' || h.sample === 'insufficient';
  /* Escape each name on its own — the joining markup must not be escaped. */
  const worst = (h.top_detractors || []).slice(0, 2)
    .map(t => '<b>' + esc((OBS_SIG_LABELS[t.area] || t.area) + ' '
                          + Math.round(t.score)) + '</b>');
  $('#obsExecActive').innerHTML =
    (h.operations != null
      ? '<b>' + h.operations.toLocaleString() + '</b> operation'
        + (h.operations === 1 ? '' : 's') + ' scored'
        + (thin ? ' · <b style="color:var(--amber)">provisional sample</b>' : '')
      : 'No scored operations')
    // This counts action records that were APPROVAL-GATED over all history
    // (42 of 204 actions here), not a live backlog — the live queue had 3.
    // Both used to read "awaiting approval", so the sidebar badge and this
    // line looked like a 14x contradiction about the same number.
    + ' · <b>' + (act.running_jobs || 0) + '</b> running · <b>'
    + (act.pending_approvals || 0) + '</b> action(s) needed approval'
    + (gObs.as_of ? ' · as of ' + esc(gObs.as_of) : '')
    + (worst.length ? '<div class="oh-weak">Held back by '
       + worst.join(' and ') + '</div>' : '');

  /* Severity chips double as the alert filter — counts you can act on. */
  const ac = (gObs.alerting || {}).counts || {};
  $('#obsAlertsBar').innerHTML = ['critical', 'warning', 'info'].map(k => {
    const n = ac[k] || 0;
    const cls = k === 'critical' ? 'crit' : k === 'warning' ? 'warn' : '';
    return '<button type="button" class="oh-chip ' + cls + '" data-obssev="' + k
      + '" aria-pressed="' + (gObsAlertSev === k) + '"' + (n ? '' : ' disabled')
      + (n ? ' title="Show only ' + k + ' alerts"' : '')
      + '>' + n + ' ' + k + '</button>';
  }).join('');

  /* The six signals, worst first, as numerals. */
  const cs = Object.entries(h.contributors || {});
  if (!cs.length) { $('#obsSignals').innerHTML = ''; return; }
  /* Only the two named in "held back by" get emphasised — flagging three of
     six as the worst tells you nothing. */
  const low = new Set((h.top_detractors || []).slice(0, 2).map(t => t.area));
  $('#obsSignals').innerHTML = '<div class="oh-lbl">Signals behind the score</div>'
    + '<div class="oh-grid">'
    + cs.sort((a, b) => a[1] - b[1]).map(([k, v]) => {
        const c = obsScoreColor(v);
        return '<div class="oh-cell' + (low.has(k) ? ' worst' : '')
          + '" style="--sig:' + c + '" title="'
          + esc((OBS_SIG_LABELS[k] || k) + ': ' + Math.round(v) + ' of 100, '
                + obsScoreBand(v)) + '">'
          + '<b>' + Math.round(v) + '</b>'
          + '<span>' + esc(OBS_SIG_LABELS[k] || k) + '</span>'
          + '<em>' + esc(obsScoreBand(v)) + '</em></div>';
      }).join('') + '</div>';
}

/* The daily series is the one real time dimension the payload carries, so the
   ribbon is system-wide — there is no per-monitor history to draw. */
function paintObsRibbon() {
  const host = $('#obsRibbon');
  if (!host) return;
  const series = (gObs.performance_trends || {}).series || [];
  if (!series.length) { host.innerHTML = ''; return; }

  const settled = s => (s.ok || 0) + (s.failed || 0);
  const rate = s => settled(s) ? s.success_rate_pct : null;
  const rated = series.filter(s => settled(s));
  const bad = rated.filter(s => s.success_rate_pct < 90).length;
  const tOk = series.reduce((a, s) => a + (s.ok || 0), 0);
  const tFail = series.reduce((a, s) => a + (s.failed || 0), 0);
  const overall = tOk + tFail
    ? Math.round(1000 * tOk / (tOk + tFail)) / 10 : null;
  const dir = (gObs.performance_trends || {}).direction;

  const cell = s => {
    const r = rate(s);
    const c = r == null ? 'var(--line)'
      : r >= 90 ? 'var(--green)' : r >= 60 ? 'var(--amber)' : 'var(--red)';
    const state = r == null ? 'nothing settled'
      : r >= 90 ? 'healthy' : r >= 60 ? 'degraded' : 'failing';
    /* Bad days also get a hatch, so state survives colour blindness. */
    const hatch = (r != null && r < 60)
      ? 'background-image:repeating-linear-gradient(45deg,'
        + 'rgba(255,255,255,.55) 0 2px,transparent 2px 5px);' : '';
    return '<button type="button" class="orib-cell" data-obsday="' + esc(s.date)
      + '" style="background:' + c + ';' + hatch + '" aria-label="'
      + esc(s.date + ': ' + state + ', ' + (s.operations || 0) + ' operations, '
            + (r == null ? 'no success rate yet' : r + '% succeeded'))
      + '" title="' + esc(s.date + ' — ' + state + ' · ' + (s.operations || 0)
            + ' ops · ' + (r == null ? 'n/a' : r + '% ok')) + '"></button>';
  };

  host.innerHTML = '<div class="orib"><div class="orib-top">'
    + '<span class="oh-lbl" style="margin:0">Daily state · last '
    + series.length + ' active day' + (series.length === 1 ? '' : 's') + '</span>'
    + '<span class="grow"></span><span class="orib-sum">'
    + (overall == null ? 'nothing settled' : '<b>' + overall + '%</b> succeeded')
    + (bad ? ' · <b>' + bad + '</b> day' + (bad === 1 ? '' : 's') + ' below 90%'
           : ' · no bad days')
    + (dir ? ' · trend ' + esc(dir) : '') + '</span></div>'
    + '<div class="orib-strip">' + series.map(cell).join('') + '</div>'
    + '<div class="orib-scale"><span>' + esc(series[0].date) + '</span><span>'
    + esc(series[series.length - 1].date) + '</span></div>'
    + '<div class="orib-key">'
    + '<span><i style="background:var(--green)"></i>90%+ succeeded</span>'
    + '<span><i style="background:var(--amber)"></i>60–89%</span>'
    + '<span><i style="background:var(--red);background-image:'
    + 'repeating-linear-gradient(45deg,rgba(255,255,255,.55) 0 2px,'
    + 'transparent 2px 5px)"></i>below 60% (hatched)</span>'
    + '<span><i style="background:var(--line)"></i>nothing settled</span>'
    + '</div></div>';
}

function paintObsMonitors() {
  const mons = gObs.monitors || {};
  const keys = Object.keys(OBS_MON_LABELS).filter(k => mons[k]);
  const attn = keys.filter(k => OBS_ATTENTION.includes(mons[k].status));
  $('#obsMonCount').textContent = keys.length + ' monitors'
    + (attn.length ? ' · ' + attn.length + ' need attention' : ' · all clear');
  document.querySelectorAll('#obsMonFilter [data-monf]').forEach(b =>
    b.setAttribute('aria-selected', String(b.dataset.monf === gObsMonFilter)));

  const shown = keys.filter(k => gObsMonFilter === 'attn'
    ? OBS_ATTENTION.includes(mons[k].status)
    : gObsMonFilter === 'measured' ? mons[k].basis === 'measured' : true);
  if (!shown.length) {
    $('#obsMonitors').innerHTML = '<div class="om-empty">'
      + (gObsMonFilter === 'attn' ? 'No monitor needs attention right now.'
         : 'No measured monitors yet.') + '</div>';
    return;
  }
  const row = k => {
    const m = mons[k], col = obsColor(m.status);
    return '<div class="omr" role="listitem" id="obsMon-' + esc(k)
      + '" tabindex="-1">'
      + obsMarkSvg(m.status, col)
      + '<div class="omr-nm">' + esc(OBS_MON_LABELS[k])
      + (m.basis === 'modeled'
        ? '<small title="Derived from estate topology, not live metering">'
          + 'modeled, not metered</small>'
        : m.basis === 'no_data'
          ? '<small style="color:var(--ink3)">no history yet</small>' : '')
      + '</div>'
      + '<div class="omr-st" style="color:' + col + '">' + esc(m.status || '')
      + (m.score != null ? '<u>' + Math.round(m.score) + ' / 100 · '
         + esc(obsScoreBand(m.score)) + '</u>' : '') + '</div>'
      + '<div class="omr-hl">' + esc(m.headline || '') + '</div></div>';
  };
  /* Worst first: a degraded monitor should never sit below a healthy one. */
  const needs = shown.filter(k => OBS_ATTENTION.includes(mons[k].status));
  const fine = shown.filter(k => !OBS_ATTENTION.includes(mons[k].status));
  const group = (title, ks) => !ks.length ? ''
    : '<div class="omg"><div class="omg-h">' + title + ' <b>' + ks.length
      + '</b></div><div role="list">' + ks.map(row).join('') + '</div></div>';
  $('#obsMonitors').innerHTML = group('Needs attention', needs)
    + group('Healthy', fine);
}

/* Objective units render through the same formatter as everything else, so a
   60000 ms target reads as "60.0 s" rather than a raw millisecond count. */
function obsSlaVal(v, unit) {
  if (v == null) return '—';
  return unit === 'ms' ? obsMs(v) : v + (unit || '');
}
/* Burn rate = error spend / error allowance. Above 1x the budget for the
   window runs out early; below 1x it lasts. */
function obsBurn(o) {
  const higher = o.met ? o.actual >= o.target : o.actual < o.target;
  if (higher) {
    const allowed = 100 - o.target;
    if (allowed <= 0) return o.actual >= 100 ? 0 : Infinity;
    return Math.max(0, (100 - o.actual) / allowed);
  }
  if (!o.target) return o.actual ? Infinity : 0;
  return Math.max(0, o.actual / o.target);
}
function obsBurnBand(x) {
  for (const [floor, label, col] of OBS_BURN) {
    if (x >= floor) return [label, col];
  }
  return ['sustainable', 'var(--green)'];
}
function obsBurnText(x) {
  if (!isFinite(x)) return '∞';
  return (x >= 100 ? Math.round(x) : x >= 10 ? x.toFixed(1) : x.toFixed(2)) + '×';
}

function paintObsSla() {
  const sla = gObs.sla_dashboard || {};
  const objs = sla.objectives || [];
  $('#obsSlaState').innerHTML = sla.status === 'met'
    ? '<span style="color:var(--green);font-weight:600">all ' + objs.length
      + ' met</span>'
    : sla.status === 'breached'
      ? '<span style="color:var(--red);font-weight:600">' + sla.met + ' of '
        + sla.total + ' met</span>'
      : '<span style="color:var(--ink3)">no data</span>';
  if (!objs.length) {
    $('#obsSla').innerHTML = '<div class="om-empty">No settled operations to '
      + 'measure objectives against yet.</div>';
    return;
  }
  /* Worst burn first — the objective in trouble should not sit third. */
  const sorted = objs.slice().sort((x, y) => obsBurn(y) - obsBurn(x));
  $('#obsSla').innerHTML = '<div class="osc">'
    + sorted.map(o => {
      const x = obsBurn(o), bandPair = obsBurnBand(x);
      const higher = o.met ? o.actual >= o.target : o.actual < o.target;
      const n = o.sample != null ? o.sample : o.samples;
      return '<div class="osc-c' + (o.met ? '' : ' bad') + '" style="--ob:'
        + bandPair[1] + '">'
        + '<div class="osc-nm" title="' + esc(o.name) + '">' + esc(o.name)
        + '</div>'
        + '<div class="osc-cmp"><b style="color:'
        + (o.met ? 'var(--green)' : 'var(--red)') + '">'
        + esc(obsSlaVal(o.actual, o.unit)) + '</b> against '
        + (higher ? '\u2265' : '\u2264') + ' <b>'
        + esc(obsSlaVal(o.target, o.unit)) + '</b></div>'
        + '<div class="osc-burn" role="img" aria-label="'
        + esc('Burn rate ' + obsBurnText(x) + ', ' + bandPair[0]) + '">'
        + '<b>' + obsBurnText(x) + '</b><span>' + esc(bandPair[0]) + '</span></div>'
        + '<div class="osc-ft"><i></i><span><b>'
        + (o.met ? 'met' : 'breached') + '</b>'
        + (o.error_budget_consumed_pct != null
          ? ' \u00b7 ' + o.error_budget_consumed_pct + '% budget used' : '')
        + (n != null ? ' \u00b7 ' + n.toLocaleString() + ' sample'
            + (n === 1 ? '' : 's') : '')
        + (o.low_confidence ? ' \u00b7 low confidence' : '')
        + '</span></div></div>';
    }).join('')
    + '</div><div class="oslo-foot">Burn rate is error spend divided by error '
    + 'allowance: <b>1\u00d7</b> exactly exhausts the budget over the window, '
    + '<b>6\u00d7</b> and <b>14.4\u00d7</b> are the usual page-worthy multiples.'
    + '</div>';
}

/* Which objective, if any, an alert is really about — so a row can say what a
   fired rule costs instead of repeating the threshold twice. */
function obsAlertObjective(a) {
  const objs = (gObs.sla_dashboard || {}).objectives || [];
  const want = a.rule === 'availability' ? 'availability'
    : a.rule === 'failure_rate' ? 'failure rate'
    : a.rule === 'latency_p95' ? 'latency' : null;
  if (!want) return null;
  return objs.filter(o => o.name.toLowerCase().indexOf(want) === 0)[0] || null;
}

function paintObsAlerts() {
  const al = gObs.alerting || {};
  const all = al.alerts || [];
  const filtered = gObsAlertSev
    ? all.filter(a => a.severity === gObsAlertSev) : all;
  /* Severity first, then by what it costs — otherwise the 24x availability
     breach lands third while the objective cards rank it first. */
  const rank = {critical: 0, warning: 1, info: 2};
  const shown = filtered.map((a, i) => [a, i]).sort((x, y) => {
    const bySev = (rank[x[0].severity] ?? 9) - (rank[y[0].severity] ?? 9);
    if (bySev) return bySev;
    const bx = obsAlertObjective(x[0]), by = obsAlertObjective(y[0]);
    const cost = (by ? obsBurn(by) : -1) - (bx ? obsBurn(bx) : -1);
    return cost || x[1] - y[1];
  }).map(pair => pair[0]);
  const ac = al.counts || {};
  $('#obsAlertCount').textContent = all.length
    ? (gObsAlertSev ? '(' + shown.length + ' of ' + all.length + ')'
       : '(' + all.length + (ac.critical ? ', ' + ac.critical + ' critical' : '')
         + ')')
    : '(nothing firing)';
  const clr = $('#obsAlertClear');
  if (clr) clr.style.display = gObsAlertSev ? '' : 'none';

  if (!all.length) {
    $('#obsAlerts').innerHTML = '<div class="oat-none">' + obsTickSvg()
      + 'No alert is firing. Every rule is inside its threshold.</div>';
    return;
  }
  if (!shown.length) {
    $('#obsAlerts').innerHTML = '<div class="om-empty">No ' + esc(gObsAlertSev)
      + ' alerts. Clear the filter to see the other ' + all.length + '.</div>';
    return;
  }
  const chevron = '<svg class="oat-go" viewBox="0 0 16 16" aria-hidden="true" '
    + 'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    + 'stroke-linejoin="round"><path d="M6 3.5 10.5 8 6 12.5"/></svg>';
  $('#obsAlerts').innerHTML = '<div class="oat">' + shown.map(a => {
    /* Some rules fire off the composite score rather than a monitor, so
       there is no tile to open — those rows are not buttons, and their key
       gets humanised instead of leaking as "health_score". */
    const tile = OBS_MON_LABELS[a.monitor];
    const mon = tile || String(a.monitor || '').replace(/_/g, ' ')
      .replace(/^./, c => c.toUpperCase());
    const col = a.severity === 'critical' ? 'var(--red)'
      : a.severity === 'warning' ? 'var(--amber)' : 'var(--ink3)';
    const obj = obsAlertObjective(a);
    /* Say what it costs, not the threshold the message already quotes. */
    const cost = obj
      ? 'spending the ' + obj.name.toLowerCase() + ' error budget <b>'
        + obsBurnText(obsBurn(obj)) + '</b> faster than allowed'
      : (a.value != null && a.threshold != null
        ? '<b>' + a.value + '</b> against a threshold of <b>' + a.threshold + '</b>'
        : '');
    const body = obsMarkSvg(a.severity === 'critical' ? 'fail' : 'warn', col)
        .replace('omr-ic', 'oat-mk')
      + '<span class="oat-b"><span class="oat-sev ' + esc(a.severity) + '">'
      + esc(a.severity) + '</span>'
      + '<span class="oat-msg">' + esc(a.message) + '</span>'
      + '<span class="oat-sub">' + esc(mon)
      + (cost ? ' \u00b7 ' + cost : '') + '</span></span>';
    if (!tile) {
      return '<div class="oat-i oat-static">' + body + '</div>';
    }
    return '<button type="button" class="oat-i" data-obsmon="'
      + esc(a.monitor) + '" aria-label="'
      + esc(a.severity + ': ' + a.message + '. From the ' + mon
            + ' monitor. Opens that monitor.') + '">'
      + body + chevron + '</button>';
  }).join('') + '</div>';
}

/* One delegated wiring pass; the containers outlive every re-render. */
function wireObsTop() {
  const page = $('#page-observability');
  if (!page || page.dataset.obsWired) return;
  page.dataset.obsWired = '1';

  const flash = key => {
    const row = $('#obsMon-' + key);
    if (!row) return;
    row.scrollIntoView({block: 'center',
      behavior: matchMedia('(prefers-reduced-motion:reduce)').matches
        ? 'auto' : 'smooth'});
    row.classList.add('flash');
    row.focus({preventScroll: true});
    setTimeout(() => row.classList.remove('flash'), 1600);
  };

  page.addEventListener('click', e => {
    const f = e.target.closest('#obsMonFilter [data-monf]');
    if (f) { gObsMonFilter = f.dataset.monf; setHash('observability/' + gObsMonFilter); paintObsMonitors(); return; }
    const sev = e.target.closest('[data-obssev]');
    if (sev) {
      gObsAlertSev = gObsAlertSev === sev.dataset.obssev ? '' : sev.dataset.obssev;
      paintObsHealth(); paintObsAlerts();
      return;
    }
    if (e.target.closest('#obsAlertClear')) {
      gObsAlertSev = ''; paintObsHealth(); paintObsAlerts(); return;
    }
    /* A ribbon day scrolls to the trends chart and focuses that same day, so
       the glanceable strip is a way into the detailed view. */
    const day = e.target.closest('[data-obsday]');
    if (day) {
      const i = gObsTrend.findIndex(s => s.date === day.dataset.obsday);
      const plot = $('#obsTrends');
      if (plot) {
        plot.scrollIntoView({block: 'center',
          behavior: matchMedia('(prefers-reduced-motion:reduce)').matches
            ? 'auto' : 'smooth'});
        const hit = plot.querySelectorAll('.ot-hit')[i];
        if (hit) hit.focus({preventScroll: true});
      }
      return;
    }
    const row = e.target.closest('[data-obsmon]');
    if (row && row.dataset.obsmon) {
      /* An alert about a monitor hidden by the current filter is a dead end. */
      const m = (gObs.monitors || {})[row.dataset.obsmon];
      if (m && gObsMonFilter === 'attn' && !OBS_ATTENTION.includes(m.status)) {
        gObsMonFilter = 'all'; setHash('observability/all'); paintObsMonitors();
      }
      flash(row.dataset.obsmon);
    }
  });

  /* Left/right arrows walk the ribbon once a day has focus. */
  page.addEventListener('keydown', e => {
    const cell = e.target.closest('.orib-cell');
    if (!cell || (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight')) return;
    const cells = [...page.querySelectorAll('.orib-cell')];
    const next = cells[cells.indexOf(cell) + (e.key === 'ArrowRight' ? 1 : -1)];
    if (next) { e.preventDefault(); next.focus(); }
  });
}

/* ---------------- Performance trends chart ----------------
   Three views over the same daily series: outcome volume (bars stacked
   ok / failed / in-flight), success rate, and mean duration. Rate is only
   meaningful once a day has settled operations — a day that is entirely
   in-flight reports 0.0% from the API, which must NOT be painted as a
   total failure, so those days are drawn as gaps instead. */
let gObsTrend = [];
let gObsTrendMode = 'ops';
let gObsTrendTable = false;

const OT_COL = {ok: '#157F3D', failed: '#B42318', pend: '#CBD3DC', rate: '#1E62D0'};
const OT_MODES = [['ops', 'Volume & outcome'], ['rate', 'Success rate'],
                  ['dur', 'Mean duration']];
const OT_HEALTHY = 90;   // same threshold the SLA panel treats as healthy

/* Settled = finished either way. Anything else is still in flight, and its
   success rate is unknown rather than zero. */
function otSettled(s) { return (s.ok || 0) + (s.failed || 0); }
function otRate(s) { return otSettled(s) ? s.success_rate_pct : null; }
function otBand(p) {
  return p == null ? OT_COL.pend
    : p >= OT_HEALTHY ? OT_COL.ok : p >= 60 ? '#B45309' : OT_COL.failed;
}
function otNice(v) {
  if (!(v > 0)) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 1.5, 2, 2.5, 4, 5, 7.5]) {
    if (v <= mag * m) return mag * m;
  }
  return mag * 10;
}
function otDay(iso) {
  const p = String(iso).split('-');
  return p.length === 3 ? p[1] + '/' + p[2] : String(iso);
}

function paintObsTrends() {
  const host = $('#obsTrends');
  if (!host) return;
  host.innerHTML = renderObsTrends(gObsTrend);
  /* Bar widths come from the measured container. If the panel had no width yet
     (section revealed in the same frame), re-lay out once it does. */
  if (gObsTrend.length && !host.clientWidth) {
    requestAnimationFrame(() => {
      if (host.clientWidth) host.innerHTML = renderObsTrends(gObsTrend);
    });
  }
}

function renderObsTrends(series) {
  if (!series || !series.length) {
    return '<div style="color:var(--muted);font-size:13px">No history yet.</div>';
  }
  const mode = gObsTrendMode;
  const host = $('#obsTrends');
  const n = series.length;

  /* ---- summary numbers (the panel used to show none) ---- */
  const tOps = series.reduce((a, s) => a + (s.operations || 0), 0);
  const tOk = series.reduce((a, s) => a + (s.ok || 0), 0);
  const tFail = series.reduce((a, s) => a + (s.failed || 0), 0);
  const overall = tOk + tFail ? Math.round(1000 * tOk / (tOk + tFail)) / 10 : null;
  const rated = series.filter(s => otSettled(s));
  const worst = rated.reduce((a, s) =>
    a && a.success_rate_pct <= s.success_rate_pct ? a : s, null);
  /* Median, not mean: one job whose wall-clock spanned a day drags a mean of
     daily means into the hours and buries a typical ~1 s operation. The mean
     is still visible — the Duration view plots the outlier day explicitly. */
  const durs = series.filter(s => s.mean_duration_ms != null)
    .map(s => s.mean_duration_ms).sort((a, b) => a - b);
  const mid = Math.floor(durs.length / 2);
  const medDur = !durs.length ? null
    : durs.length % 2 ? durs[mid] : (durs[mid - 1] + durs[mid]) / 2;
  let delta = null;
  if (rated.length >= 2) {
    const h = Math.floor(rated.length / 2);
    const avg = a => a.reduce((x, s) => x + s.success_rate_pct, 0) / a.length;
    delta = Math.round(10 * (avg(rated.slice(h)) - avg(rated.slice(0, h)))) / 10;
  }
  const kpi = (label, val, sub) =>
    '<div class="obs-kpi"><dt>' + label + '</dt><dd>' + val
    + (sub ? ' <small>' + sub + '</small>' : '') + '</dd></div>';
  const dTone = delta == null || Math.abs(delta) < 5 ? 'var(--ink)'
    : delta > 0 ? 'var(--green)' : 'var(--red)';
  const kpis = '<dl class="obs-kpis">'
    + kpi('Operations', tOps.toLocaleString(), 'over ' + n + ' day' + (n > 1 ? 's' : ''))
    + kpi('Success rate', overall == null ? '—' : overall + '%',
          tFail ? tFail.toLocaleString() + ' failed' : 'no failures')
    + kpi('Rate change', delta == null ? '—'
          : '<span style="color:' + dTone + '">' + (delta > 0 ? '+' : '') + delta + ' pp</span>',
          'first half → second')
    + kpi('Typical duration', medDur == null ? '—' : esc(obsMs(medDur)),
          'median day')
    + kpi('Worst day', worst ? worst.success_rate_pct + '%' : '—',
          worst ? esc(worst.date) : 'nothing settled')
    + '</dl>';

  /* ---- vertical scale first: the axis labels decide the left gutter ---- */
  const vals = series.map(s => mode === 'ops' ? (s.operations || 0)
    : mode === 'rate' ? (otRate(s) || 0) : (s.mean_duration_ms || 0));
  const yMax = mode === 'rate' ? 100 : otNice(Math.max(...vals));
  const fmtY = v => mode === 'ops' ? String(Math.round(v))
    : mode === 'rate' ? v + '%' : obsMs(v);
  /* Fewer ticks on a tiny scale, so 0/1 never prints as "0 0 1 1 1". */
  const ticks = mode === 'rate' ? 4 : Math.max(1, Math.min(4, Math.floor(yMax)));
  const yLabels = [];
  for (let t = 0; t <= ticks; t++) yLabels.push(fmtY(yMax * t / ticks));
  const labW = Math.max(...yLabels.map(s => s.length));

  /* ---- geometry: crisp pixels, scrolls sideways when history is long ---- */
  const avail = Math.max(320, (host && host.clientWidth ? host.clientWidth : 900) - 2);
  const padL = Math.max(30, 12 + labW * 6), padR = mode === 'ops' ? 42 : 14;
  const padT = 14, padB = 42;
  const slot = Math.max(30, Math.min(72, (avail - padL - padR) / n));
  const W = Math.max(avail, Math.round(padL + padR + n * slot));
  const H = 236;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const sw = plotW / n;
  const bw = Math.max(6, Math.min(30, sw * 0.6));
  const x = i => padL + sw * i + sw / 2;
  const base = padT + plotH;
  const y = v => base - plotH * Math.min(1, v / yMax);

  /* gridlines + left axis */
  let g = '';
  for (let t = 0; t <= ticks; t++) {
    const v = yMax * t / ticks, yy = Math.round(y(v)) + 0.5;
    g += '<line class="' + (t ? 'ot-grid' : 'ot-zero') + '" x1="' + padL + '" x2="'
      + (W - padR) + '" y1="' + yy + '" y2="' + yy + '"/>'
      + '<text class="ot-axis" x="' + (padL - 8) + '" y="' + (yy + 3.5)
      + '" text-anchor="end">' + esc(yLabels[t]) + '</text>';
  }
  /* healthy-threshold reference, on whichever axis carries percentages */
  if (mode === 'ops' || mode === 'rate') {
    const ry = Math.round(mode === 'rate' ? y(OT_HEALTHY)
      : base - plotH * OT_HEALTHY / 100) + 0.5;
    g += '<line class="ot-ref" x1="' + padL + '" x2="' + (W - padR)
      + '" y1="' + ry + '" y2="' + ry + '"/>';
  }
  /* right axis: percent scale for the success-rate overlay */
  if (mode === 'ops') {
    for (let t = 0; t <= 4; t++) {
      const yy = Math.round(base - plotH * t / 4) + 3.5;
      g += '<text class="ot-axis" x="' + (W - padR + 8) + '" y="' + yy + '">'
        + (t * 25) + '%</text>';
    }
  }

  /* x labels: thin them out instead of rotating — rotated text was unreadable */
  const step = Math.max(1, Math.ceil(n / Math.floor(plotW / 46)));
  let xl = '';
  series.forEach((s, i) => {
    if (i % step && i !== n - 1) return;
    xl += '<text class="ot-xlab" x="' + x(i).toFixed(1) + '" y="' + (base + 16)
      + '" text-anchor="middle">' + esc(otDay(s.date)) + '</text>';
  });

  /* ---- bars ---- */
  let bars = '';
  series.forEach((s, i) => {
    const cx = x(i), left = cx - bw / 2;
    let seg = '';
    if (mode === 'ops') {
      const parts = [
        [s.ok || 0, OT_COL.ok],
        [s.failed || 0, OT_COL.failed],
        [Math.max(0, (s.operations || 0) - otSettled(s)), OT_COL.pend],
      ];
      let acc = 0;
      parts.forEach(([v, c]) => {
        if (v <= 0) return;
        const h = plotH * v / yMax;
        const top = base - plotH * (acc + v) / yMax;
        acc += v;
        seg += '<rect x="' + left.toFixed(1) + '" y="' + top.toFixed(1) + '" width="'
          + bw.toFixed(1) + '" height="' + Math.max(1, h).toFixed(1)
          + '" fill="' + c + '" rx="1.5"/>';
      });
    } else {
      const v = mode === 'rate' ? otRate(s) : s.mean_duration_ms;
      if (v == null) {
        seg += '<rect x="' + left.toFixed(1) + '" y="' + (base - 3) + '" width="'
          + bw.toFixed(1) + '" height="3" fill="' + OT_COL.pend + '" rx="1.5"/>';
      } else {
        const top = y(v);
        seg += '<rect x="' + left.toFixed(1) + '" y="' + top.toFixed(1) + '" width="'
          + bw.toFixed(1) + '" height="' + Math.max(1, base - top).toFixed(1)
          + '" fill="' + (mode === 'rate' ? otBand(v) : OT_COL.rate) + '" rx="2"/>';
      }
    }
    bars += '<g class="ot-slot">'
      + '<rect class="ot-band" x="' + (cx - sw / 2).toFixed(1) + '" y="' + padT
      + '" width="' + sw.toFixed(1) + '" height="' + plotH + '" rx="3"/>'
      + seg
      + '<rect class="ot-hit" data-i="' + i + '" tabindex="0" role="img" aria-label="'
      + esc(otAria(s)) + '" x="' + (cx - sw / 2).toFixed(1) + '" y="' + padT
      + '" width="' + sw.toFixed(1) + '" height="' + plotH + '"/></g>';
  });

  /* ---- success-rate overlay (volume view only), broken at unsettled days ---- */
  let line = '';
  if (mode === 'ops') {
    let run = [];
    const flush = () => {
      if (run.length > 1) {
        line += '<polyline class="ot-line" points="' + run.join(' ') + '"/>';
      }
      run = [];
    };
    series.forEach((s, i) => {
      const r = otRate(s);
      if (r == null) { flush(); return; }
      const py = base - plotH * r / 100;
      run.push(x(i).toFixed(1) + ',' + py.toFixed(1));
      line += '<circle class="ot-dot" cx="' + x(i).toFixed(1) + '" cy="' + py.toFixed(1)
        + '" r="3"/>';
    });
    flush();
  }

  /* ---- controls ---- */
  const segs = '<div class="seg" role="tablist" aria-label="Trend metric">'
    + OT_MODES.map(([k, lbl]) =>
      '<button role="tab" data-otmode="' + k + '" aria-selected="'
      + (mode === k) + '">' + lbl + '</button>').join('') + '</div>';
  const head = '<div class="ot-head">' + segs + '<span class="grow"></span>'
    + '<button type="button" class="jact" data-ottable="1" aria-expanded="'
    + gObsTrendTable + '">' + (gObsTrendTable ? 'Hide table' : 'Show table') + '</button>'
    + '<button type="button" class="jact" data-otcsv="1">Export CSV</button></div>';

  const legend = '<div class="ot-legend">'
    + (mode === 'ops'
      ? '<span><i style="background:' + OT_COL.ok + '"></i>Succeeded</span>'
        + '<span><i style="background:' + OT_COL.failed + '"></i>Failed</span>'
        + '<span><i style="background:' + OT_COL.pend + '"></i>In flight</span>'
        + '<span><i class="ln" style="background:' + OT_COL.rate + '"></i>Success rate (right axis)</span>'
      : mode === 'rate'
        ? '<span><i style="background:' + OT_COL.ok + '"></i>≥' + OT_HEALTHY + '%</span>'
          + '<span><i style="background:var(--amber)"></i>60–' + (OT_HEALTHY - 1) + '%</span>'
          + '<span><i style="background:' + OT_COL.failed + '"></i>&lt;60%</span>'
          + '<span><i style="background:' + OT_COL.pend + '"></i>Nothing settled</span>'
        : '<span><i style="background:' + OT_COL.rate + '"></i>Mean duration per operation</span>')
    + (mode === 'dur' ? ''
      : '<span><i class="ln" style="background:var(--amber)"></i>' + OT_HEALTHY
        + '% healthy threshold</span>')
    + '</div>';

  const note = '<div class="ot-note">Hover or tab through a day for its full '
    + 'breakdown. Days with nothing settled yet are shown as gaps, not as 0%.</div>';

  return '<div class="ot">' + kpis + head
    + '<div class="ot-plot"><svg width="' + W + '" height="' + H
    + '" viewBox="0 0 ' + W + ' ' + H + '" role="group" aria-label="Daily '
    + esc(OT_MODES.filter(m => m[0] === mode)[0][1]) + '">'
    + g + bars + line + xl + '</svg></div>'
    + legend + note
    + '<div class="ot-tip" id="obsTrendTip" aria-hidden="true"></div>'
    + (gObsTrendTable ? otTable(series) : '') + '</div>';
}

function otAria(s) {
  const r = otRate(s);
  return s.date + ': ' + (s.operations || 0) + ' operations, ' + (s.ok || 0)
    + ' succeeded, ' + (s.failed || 0) + ' failed, '
    + (r == null ? 'success rate not yet measurable' : r + ' percent success')
    + (s.mean_duration_ms != null ? ', mean duration ' + obsMs(s.mean_duration_ms) : '');
}

function otTipHtml(s) {
  const r = otRate(s), inflight = Math.max(0, (s.operations || 0) - otSettled(s));
  const row = (lbl, val, col) => '<div class="r"><span>'
    + (col ? '<i class="sw" style="background:' + col + '"></i>' : '') + lbl
    + '</span><em>' + val + '</em></div>';
  return '<b>' + esc(s.date) + '</b>'
    + row('Operations', (s.operations || 0).toLocaleString())
    + row('Succeeded', (s.ok || 0).toLocaleString(), OT_COL.ok)
    + row('Failed', (s.failed || 0).toLocaleString(), OT_COL.failed)
    + (inflight ? row('In flight', inflight.toLocaleString(), OT_COL.pend) : '')
    + row('Success rate', r == null
        ? '<span style="color:var(--muted)">n/a</span>'
        : '<span style="color:' + otBand(r) + '">' + r + '%</span>')
    + row('Mean duration', esc(obsMs(s.mean_duration_ms)));
}

function otTable(series) {
  return '<div class="ot-table"><table><caption class="ot-note" '
    + 'style="text-align:left;margin:0 0 6px">Daily figures behind the chart'
    + '</caption><thead><tr><th scope="col">Date</th><th scope="col">Operations</th>'
    + '<th scope="col">Succeeded</th><th scope="col">Failed</th>'
    + '<th scope="col">In flight</th><th scope="col">Success rate</th>'
    + '<th scope="col">Mean duration</th></tr></thead><tbody>'
    + series.map(s => {
      const r = otRate(s);
      return '<tr><th scope="row" style="font-weight:500">' + esc(s.date) + '</th>'
        + '<td>' + (s.operations || 0) + '</td><td>' + (s.ok || 0) + '</td>'
        + '<td>' + (s.failed || 0) + '</td>'
        + '<td>' + Math.max(0, (s.operations || 0) - otSettled(s)) + '</td>'
        + '<td style="color:' + otBand(r) + ';font-weight:600">'
        + (r == null ? '—' : r + '%') + '</td>'
        + '<td>' + esc(obsMs(s.mean_duration_ms)) + '</td></tr>';
    }).join('') + '</tbody></table></div>';
}

function otExportCsv() {
  const rows = [['date', 'operations', 'succeeded', 'failed', 'in_flight',
                 'success_rate_pct', 'mean_duration_ms']];
  gObsTrend.forEach(s => rows.push([s.date, s.operations || 0, s.ok || 0,
    s.failed || 0, Math.max(0, (s.operations || 0) - otSettled(s)),
    otRate(s) == null ? '' : s.success_rate_pct,
    s.mean_duration_ms == null ? '' : s.mean_duration_ms]));
  const blob = new Blob([rows.map(r => r.join(',')).join('\n')],
    {type: 'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'performance-trends.csv';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

/* Delegated once on the container, which survives every re-render. */
function wireObsTrends() {
  const host = $('#obsTrends');
  if (!host || host.dataset.wired) return;
  host.dataset.wired = '1';

  const showTip = hit => {
    const tip = $('#obsTrendTip'), s = gObsTrend[+hit.dataset.i];
    if (!tip || !s) return;
    tip.innerHTML = otTipHtml(s);
    tip.classList.add('on');
    const hb = hit.getBoundingClientRect(), pb = host.getBoundingClientRect();
    const left = hb.left - pb.left + hb.width / 2 - tip.offsetWidth / 2;
    tip.style.left = Math.max(0, Math.min(host.clientWidth - tip.offsetWidth, left)) + 'px';
    tip.style.top = Math.max(0, hb.top - pb.top + 8) + 'px';
  };
  const hideTip = () => {
    const tip = $('#obsTrendTip');
    if (tip) tip.classList.remove('on');
  };

  host.addEventListener('mouseover', e => {
    const hit = e.target.closest('.ot-hit');
    if (hit) showTip(hit); else if (!e.target.closest('.ot-tip')) hideTip();
  });
  host.addEventListener('mouseleave', hideTip);
  host.addEventListener('focusin', e => {
    const hit = e.target.closest('.ot-hit');
    if (hit) showTip(hit);
  });
  host.addEventListener('focusout', e => {
    if (e.target.closest('.ot-hit')) hideTip();
  });
  host.addEventListener('click', e => {
    const m = e.target.closest('[data-otmode]');
    if (m) { gObsTrendMode = m.dataset.otmode; paintObsTrends(); return; }
    if (e.target.closest('[data-ottable]')) {
      gObsTrendTable = !gObsTrendTable; paintObsTrends(); return;
    }
    if (e.target.closest('[data-otcsv]')) otExportCsv();
  });
  /* Left/right arrows walk the series once a day has focus. */
  host.addEventListener('keydown', e => {
    const hit = e.target.closest('.ot-hit');
    if (!hit || (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight')) return;
    const hits = [...host.querySelectorAll('.ot-hit')];
    const next = hits[hits.indexOf(hit) + (e.key === 'ArrowRight' ? 1 : -1)];
    if (next) { e.preventDefault(); next.focus(); }
  });
  /* Bar widths are computed from the container, so re-lay out on resize. */
  let rt = null;
  window.addEventListener('resize', () => {
    if (!gObsTrend.length || !host.offsetParent) return;
    clearTimeout(rt);
    rt = setTimeout(paintObsTrends, 150);
  });
}

if ($('#obsRefresh')) $('#obsRefresh').onclick = loadObservability;

/* ---------------- Agentic AI Architecture ---------------- */
let gAgRun = null;
let gAgPollTimer = null;
const AG_POLL_MS = 5000;
const AG_LEVEL_COLOR = {high: 'var(--green)', medium: 'var(--amber)', low: 'var(--red)'};

/* Status must not read by colour alone (WCAG 1.4.1) — every pill and chip
   carries a glyph and a word, so green/red is the third channel, not the
   only one. 16px Lucide-geometry paths, stroked with currentColor. */
const AG_GLYPH = {
  ok:   '<path d="M20 6 9 17l-5-5"/>',
  warn: '<path d="M12 9v4"/><path d="M12 17h.01"/>'
        + '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>',
  wait: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  no:   '<circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6m0-6 6 6"/>',
  fail: '<path d="M12 8v4"/><path d="M12 16h.01"/>'
        + '<path d="M8.7 3h6.6L21 8.7v6.6L15.3 21H8.7L3 15.3V8.7Z"/>',
  skip: '<path d="M5 12h14"/>',
  folder: '<path d="M4 6a2 2 0 0 1 2-2h3l2 2h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2Z"/>',
  db: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/>'
      + '<path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
};
/* decision -> [tone class, glyph, label] */
const AG_TONE = {
  allow:          ['ok',   'ok',   'committed'],
  approved:       ['ok',   'ok',   'approved'],
  flag_review:    ['warn', 'warn', 'flagged'],
  needs_approval: ['warn', 'wait', 'needs approval'],
  rejected:       ['crit', 'no',   'rejected'],
  deny:           ['crit', 'no',   'denied'],
  failed:         ['crit', 'fail', 'failed'],
  skipped:        ['',     'skip', 'skipped'],
};
const AG_ATTN = ['flag_review', 'needs_approval', 'rejected', 'deny', 'failed'];
let gAgPipeFilter = 'all';

function agGlyph(k) {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
    + (AG_GLYPH[k] || AG_GLYPH.skip) + '</svg>';
}
function agTone(decision) {
  return AG_TONE[decision] || ['', 'skip', String(decision || '—').replace(/_/g, ' ')];
}
/* One numeral, ringed — the same verdict form the Observability header
   uses, so "how did this run go" reads identically on both pages. */
function agGaugeSvg(pct, col) {
  const circ = 2 * Math.PI * 44, off = circ * (1 - (pct || 0) / 100);
  return '<svg width="104" height="104" viewBox="0 0 104 104" role="img"'
    + ' aria-label="overall confidence ' + pct + ' percent">'
    + '<circle cx="52" cy="52" r="44" fill="none" stroke="var(--line)" stroke-width="10"/>'
    + '<circle cx="52" cy="52" r="44" fill="none" stroke="' + col + '" stroke-width="10"'
    + ' stroke-linecap="round" stroke-dasharray="' + circ + '" stroke-dashoffset="' + off
    + '" transform="rotate(-90 52 52)"/>'
    + '<text x="52" y="50" text-anchor="middle" font-size="25" font-weight="700" fill="'
    + col + '">' + pct + '<tspan font-size="14">%</tspan></text>'
    + '<text x="52" y="68" text-anchor="middle" font-size="9.5" fill="var(--muted)"'
    + ' letter-spacing=".06em">CONFIDENCE</text></svg>';
}
function agBand(pct) {
  return pct >= 80 ? 'var(--green)' : pct >= 50 ? 'var(--amber)' : 'var(--red)';
}

async function loadAgents() {
  if (!$('#agRoster')) return;
  let d;
  try { d = await api('/api/agents'); } catch (e) { return; }
  const order = {};
  d.plan.forEach((p, i) => { order[p.id] = i + 1; });
  const byId = {};
  d.agents.forEach(a => { byId[a.id] = a; });
  // The roster is reference material: what the twelve agents are and what
  // each is allowed to do. It sits inside the (i) disclosure rather than at
  // the top of the panel, where it used to outrank the run's own result.
  $('#agRoster').innerHTML =
    '<span class="cav" style="margin-bottom:0">' + d.agents.length
    + ' agents · governance floor ' + d.governance.deny_floor
    + ', approval threshold ' + d.governance.approval_threshold + '</span>'
    + '<div class="agz-roster">'
    + d.plan.map(p => {
        const a = byId[p.id] || p;
        const risky = a.action_class === 'generate' || a.action_class === 'mutating';
        return '<div class="agz-rc"><b><i>' + order[p.id] + '</i>'
          + esc(a.id.replace(/_/g, ' ')) + '</b>'
          + '<span class="agz-cls' + (risky ? ' gen' : '') + '">'
          + esc(a.action_class.replace(/_/g, '-')) + '</span>'
          + '<p>' + esc(a.description || '') + '</p>'
          + (a.depends_on && a.depends_on.length
            ? '<span class="agz-dep">after ' + a.depends_on.map(x =>
                esc(x.replace(/_/g, ' '))).join(', ') + '</span>' : '')
          + '</div>';
      }).join('') + '</div>';
}

/* Launcher and history are mutually exclusive drawers under the header. */
function agDrawer(which) {
  [['agRunDrawer', 'agNewBtn'], ['agHistDrawer', 'agHistBtn']].forEach(([d, b]) => {
    const open = d === which && !$('#' + d).classList.contains('on');
    $('#' + d).classList.toggle('on', open);
    $('#' + b).setAttribute('aria-expanded', String(open));
  });
}
$('#agNewBtn') && ($('#agNewBtn').onclick = () => agDrawer('agRunDrawer'));
$('#agHistBtn') && ($('#agHistBtn').onclick = () => agDrawer('agHistDrawer'));
$('#agEmptyBtn') && ($('#agEmptyBtn').onclick = () => {
  $('#agRunDrawer').classList.add('on');
  $('#agNewBtn').setAttribute('aria-expanded', 'true');
  $('#agRunDrawer').scrollIntoView({block: 'nearest'});
});
function agShowEmpty() {
  if (!$('#agEmpty') || gAgRun) return;
  $('#agEmpty').style.display = 'block';
}

/* Saved connections as a run source. Stopped ones are listed but not
   selectable, with the reason inline — hiding them would leave "where is my
   Snowflake?" unanswered, and the server rejects them anyway. */
let gAgConns = [];
async function loadAgentConnections() {
  const host = $('#agConnList');
  if (!host) return;
  let d;
  try { d = await api('/api/v1/connections'); }
  catch (e) {
    host.innerHTML = '<div class="agz-conns-empty">Could not load connections.</div>';
    return;
  }
  gAgConns = d.connections || [];
  const usable = gAgConns.filter(c => c.status === 'active');
  $('#agConnCount').textContent = gAgConns.length
    ? '(' + usable.length + ' of ' + gAgConns.length + ' available)' : '';
  if (!gAgConns.length) {
    host.innerHTML = '<div class="agz-conns-empty">No saved connections yet — '
      + '<a data-go="marketplace" class="agGoConn" style="cursor:pointer;color:var(--accent);'
      + 'font-weight:600">connect a system</a> to run agents against it live.</div>';
    return;
  }
  host.innerHTML = '<div class="agz-conns">' + gAgConns.map(c => {
    const off = c.status !== 'active';
    return '<label class="agz-conn' + (off ? ' off' : '') + '">'
      + '<input type="checkbox" class="agConnCb" value="' + esc(c.id) + '"'
      + (off ? ' disabled' : '') + '>'
      + '<b>' + esc(c.name || c.connector) + '</b>'
      + '<span class="agz-cn-tech">' + esc(c.connector || '') + '</span>'
      + '<span class="grow"></span>'
      + (off ? '<span class="agz-cn-why">stopped — start it in Integrations</span>'
             : '<span class="agz-cn-why">' + esc(
                 (c.last_analysis && c.last_analysis.tables != null)
                   ? c.last_analysis.tables + ' tables analyzed'
                   : 'not analyzed yet — will be read on run') + '</span>')
      + '</label>';
  }).join('') + '</div>';
}
function agSelectedConns() {
  return [...document.querySelectorAll('.agConnCb:checked')].map(c => c.value);
}
/* Provenance on the verdict: an uploaded folder and a live warehouse read are
   not the same evidence, so the run says which it had. Names resolve from the
   connection list; a deleted connection falls back to its short id rather
   than rendering blank. */
function agSourceTags(rep) {
  const P = rep.params || {};
  const cids = P.connection_ids || [];
  const tag = (glyph, text) => '<span class="agz-srctag">' + agGlyph(glyph)
    + esc(text) + '</span>';
  const tags = [];
  if (P.has_upload) tags.push(tag('folder', 'uploaded project folder'));
  cids.forEach(id => {
    const c = gAgConns.find(x => x.id === id);
    tags.push(tag('db', 'live · ' + (c ? (c.name || c.connector)
                                       : String(id).slice(0, 8))));
  });
  const notes = P.connection_notes || [];
  return (tags.length ? '<div class="agz-srcs">' + tags.join('') + '</div>' : '')
    + (notes.length
        ? '<div class="agz-note"><b>Partial read:</b> ' + notes.map(esc).join(' · ')
          + '</div>' : '');
}
document.addEventListener('click', (ev) => {
  if (!ev.target.closest('.agGoConn')) return;
  ev.preventDefault();
  const a = [...document.querySelectorAll('nav a[data-page]')]
    .find(x => x.dataset.page === 'marketplace');
  if (a) a.click();
});

$('#agRun') && ($('#agRun').onclick = async () => {
  const err = $('#agErr'); err.style.display = 'none';
  $('#agStatus').textContent = 'Running agent swarm…';
  try {
    const conns = agSelectedConns();
    const files = await Promise.all(pdropFiles('ag').map(f =>
      new Promise((res, rej) => {
        const r = new FileReader();
        r.onload = () => res({name: pdropPath(f), content: r.result});
        r.onerror = rej;
        r.readAsText(f);
      })));
    // Either source is enough, and both together is the fullest run.
    if (!files.length && !conns.length) {
      throw new Error('Choose a project folder, a connected system, or both.');
    }
    const picked = conns.length
      ? gAgConns.find(c => c.id === conns[0]) : null;
    const project = files.length ? (files[0].name.split('/')[0] || 'estate')
      : ((picked && (picked.name || picked.connector)) || 'estate');
    $('#agStatus').textContent = conns.length
      ? 'Reading ' + conns.length + ' connected system(s), then running the swarm…'
      : 'Running agent swarm…';
    const rep = await api('/api/agents/run', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({files, connection_ids: conns, project,
                            target_region: $('#agRegion').value})});
    $('#agStatus').textContent = '';
    renderAgentRun(rep);
    // The launcher has done its job — collapse it so the result it produced
    // is the first thing on screen rather than the form that produced it.
    $('#agRunDrawer').classList.remove('on');
    $('#agNewBtn').setAttribute('aria-expanded', 'false');
    paintRecentAgentRuns();
    // JS smooth scrolling ignores prefers-reduced-motion unless asked to.
    $('#agVerdict').scrollIntoView({block: 'nearest',
      behavior: matchMedia('(prefers-reduced-motion:reduce)').matches ? 'auto' : 'smooth'});
  } catch (e) {
    $('#agStatus').textContent = '';
    err.style.display = 'block'; err.textContent = e.message;
  }
});

function renderAgentRun(rep) {
  gAgRun = rep;
  localStorage.setItem('mb_agent_run_id', rep.run_id);
  $('#agResult').style.display = 'block';
  if ($('#agEmpty')) $('#agEmpty').style.display = 'none';
  const s = rep.summary || {};

  // ---- Zone 1: the verdict ------------------------------------------------
  // The recommendation is what a reader came for, so it is the headline, and
  // overall confidence is a numeral rather than a phrase in a sentence. The
  // outcome chips list only counts that actually happened: three of the six
  // were permanently "0 denied / 0 skipped / 0 failed" and occupied the most
  // valuable strip on the page saying nothing.
  const execRow = (rep.results || []).find(r => r.agent_id === 'executive_reporting');
  const er = (execRow && execRow.outputs && execRow.outputs.executive_report) || null;
  const results = rep.results || [];
  const conf = er && er.overall_confidence != null
    ? Math.round(er.overall_confidence)
    : results.length
      ? Math.round(results.reduce((t, r) => t + (r.confidence || 0), 0) / results.length * 100)
      : 0;
  const chips = [
    ['committed', s.committed, 'ok', 'ok'],
    ['flagged for review', s.flagged, 'warn', 'warn'],
    ['need approval', s.needs_approval, 'warn', 'wait'],
    ['denied', s.denied, 'crit', 'no'],
    ['failed', s.failed, 'crit', 'fail'],
    ['skipped', s.skipped, '', 'skip'],
  ].filter(c => c[1]).map(c =>
    '<span class="agz-chip ' + c[2] + '">' + agGlyph(c[3])
    + '<b>' + c[1] + '</b> ' + c[0] + '</span>').join('');

  const cell = (val, lbl, col) =>
    '<div class="agz-cell" style="--sig:' + col + '"><b>' + val + '</b><span>'
    + lbl + '</span></div>';
  const sig = er
    ? cell(er.conversion_confidence != null ? er.conversion_confidence + '%' : '—',
           'conversion', agBand(er.conversion_confidence || 0))
      + cell(er.security_score != null ? er.security_score : '—',
             'security score', agBand(er.security_score || 0))
      + cell(er.policy_violations || 0, 'policy violations',
             (er.policy_violations || 0) ? 'var(--red)' : 'var(--green)')
      + cell(er.modeled_three_year_roi_pct != null
               ? er.modeled_three_year_roi_pct + '%' : '—',
             'modeled 3-yr ROI', 'var(--accent)')
    : '';

  $('#agVerdict').innerHTML = '<div class="agz-v"><div class="agz-vl">'
    + agGaugeSvg(conf, agBand(conf))
    + '<div style="min-width:0">'
    + '<div class="agz-rec">' + esc(er && er.recommendation
        ? er.recommendation.charAt(0).toUpperCase() + er.recommendation.slice(1)
        : 'Run complete') + '</div>'
    + '<div class="agz-meta">' + esc(rep.project || 'estate')
      + ' · <code>' + esc((rep.run_id || '').slice(0, 8)) + '</code>'
      + (rep.created_at ? ' · ' + fmtRunTime(rep.created_at) : '')
      + (rep.requested_by ? ' · by ' + esc(rep.requested_by) : '') + '</div>'
    + (chips ? '<div class="agz-chips">' + chips + '</div>' : '')
    + (er && er.blockers && er.blockers.length
        ? '<div class="agz-blk"><b>' + er.blockers.length + ' blocker'
          + (er.blockers.length > 1 ? 's' : '') + ':</b> '
          + er.blockers.map(esc).join('; ') + '</div>' : '')
    + agSourceTags(rep)
    + '</div></div>'
    + (sig ? '<div class="agz-sig"><p class="oh-lbl">Executive signals</p>'
             + '<div class="agz-sgrid">' + sig + '</div></div>' : '')
    + '</div>';

  // live approval outcome per agent — run-time `results[].decision` is
  // frozen at the moment the swarm ran and never rewritten, so a
  // needs_approval decision would otherwise show as unresolved forever
  // even after someone approves/rejects it. Overlay the CURRENT decided
  // status from the (live-refreshed) approvals list instead of the
  // frozen one wherever a decision has since been made.
  const decidedByAgent = {};
  (rep.approvals || []).forEach(a => {
    if (a.status === 'approved' || a.status === 'rejected') {
      decidedByAgent[a.agent_id] = a.status;
    }
  });

  gAgDecided = decidedByAgent;
  paintAgPipeline();

  // ---- Zone 2: what needs a person ---------------------------------------
  const pend = (rep.approvals || []).filter(a => a.status === 'pending');
  $('#agApprovalsWrap').style.display = (rep.approvals || []).length ? 'block' : 'none';
  // "Needs approval" over three already-decided rows is the same kind of
  // stale label as the frozen agent summaries — the heading follows the state.
  $('#agApprovalTitle').textContent = pend.length ? 'Needs approval' : 'Approvals';
  $('#agApprovalCount').textContent = pend.length
    ? pend.length + ' pending of ' + (rep.approvals || []).length
    : 'all ' + (rep.approvals || []).length + ' decided';
  $('#agApprovals').innerHTML =
    (pend.some(a => a.no_eligible_approver) ? approvalEscalationBanner() : '')
    // Pending first: a decided row is a receipt, a pending one is work.
    + (rep.approvals || []).slice()
        .sort((a, b) => (a.status === 'pending' ? 0 : 1) - (b.status === 'pending' ? 0 : 1))
        .map(a => approvalRow(a)).join('');

  // audit trail
  const av = rep.audit && rep.audit.verification;
  $('#agAuditState').innerHTML = av
    ? (av.intact
        ? '<span style="color:var(--green)">✓ chain intact · ' + (rep.audit.events || []).length + ' events</span>'
        : '<span style="color:var(--red)">⚠ chain broken at event ' + av.broken_at + '</span>')
    : '';
  // The ledger itself is append-only by design (that's what makes it
  // tamper-evident) — rows are never rewritten. But a needs_approval row
  // left as-is reads as "still stuck" even long after it was resolved by
  // a later event further down the same log. Find, for each such row,
  // the LATEST later event for the same agent and note its outcome
  // inline — the original row's own cells are never changed.
  const events = rep.audit.events || [];
  const resolutionFor = (seq, agentId) => {
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i];
      if (e.seq > seq && e.agent_id === agentId
          && (e.status === 'decided') && e.decision !== 'needs_approval') {
        return e;
      }
    }
    return null;
  };
  $('#agAudit').innerHTML = '<table class="agz-audit"><thead><tr><th>#</th>'
    + '<th>Agent</th><th>Decision</th><th>Conf</th><th>Status</th></tr></thead><tbody>'
    + events.map(e => {
        const resolved = e.status === 'needs_approval'
          ? resolutionFor(e.seq, e.agent_id) : null;
        return '<tr><td>' + e.seq + '</td>'
          + '<td style="text-transform:capitalize">' + esc(e.agent_id.replace(/_/g, ' ')) + '</td>'
          + '<td>' + esc((e.decision || '').replace(/_/g, ' ')) + '</td>'
          + '<td>' + Math.round((e.confidence || 0) * 100) + '%</td>'
          + '<td' + (resolved ? ' class="agz-ares"' : '') + '>'
          + esc((e.status || '').replace(/_/g, ' '))
          + (resolved
              ? ' &rarr; <b>' + esc(resolved.decision) + '</b> at #' + resolved.seq
              : '')
          + '</td></tr>';
      }).join('')
    + '</tbody></table>';

  if (pend.length) startAgentRunPolling(); else stopAgentRunPolling();
}

/* ---- Zone 3: the pipeline -------------------------------------------------
   Kept in dependency order, not sorted worst-first: the order IS information
   here (discovery feeds parse feeds semantic), and resorting it would break
   the "after: parse" contract shown in the roster. Rows that need attention
   are tinted and reachable in one click through the filter instead. */
let gAgDecided = {};
function paintAgPipeline() {
  if (!gAgRun || !$('#agPipeline')) return;
  const results = gAgRun.results || [];
  const live = r => gAgDecided[r.agent_id] || r.decision;
  const attn = results.filter(r => AG_ATTN.includes(live(r)));
  $('#agPipeCount').textContent = results.length + ' agents'
    + (attn.length ? ' · ' + attn.length + ' need attention' : ' · all clear');
  document.querySelectorAll('#agPipeFilter [data-agf]').forEach(b =>
    b.setAttribute('aria-selected', String(b.dataset.agf === gAgPipeFilter)));

  const shown = results.filter(r => gAgPipeFilter === 'attn'
    ? AG_ATTN.includes(live(r))
    : gAgPipeFilter === 'ok' ? !AG_ATTN.includes(live(r)) : true);
  if (!shown.length) {
    $('#agPipeline').innerHTML = '<div class="agz-empty" style="padding:20px"><b>'
      + (gAgPipeFilter === 'attn' ? 'No agent needs attention in this run.'
                                  : 'No agent committed in this run.') + '</b></div>';
    return;
  }
  $('#agPipeline').innerHTML = shown.map(r => {
    const d = live(r), [tone, glyph, label] = agTone(d);
    const lc = AG_LEVEL_COLOR[r.confidence_level] || 'var(--ink3)';
    const pct = Math.round((r.confidence || 0) * 100);
    // A frozen "— awaiting approval to publish" summary next to a live
    // "rejected" pill read as a contradiction. Once the request is decided,
    // that clause is replaced by the decision it actually got.
    let sum = r.summary || '';
    if (gAgDecided[r.agent_id]) {
      sum = sum.replace(/\s*[—–-]\s*awaiting approval[^.;]*/i, '')
             + ' — approval ' + gAgDecided[r.agent_id];
    }
    return '<div class="agz-row' + (AG_ATTN.includes(d) ? ' attn' : '')
      + '" style="--sig:' + lc + '">'
      + agGlyph(glyph)
      + '<span class="agz-nm">' + esc(r.agent_id.replace(/_/g, ' ')) + '</span>'
      + '<div class="agz-bar" role="img" aria-label="confidence ' + pct + ' percent, '
      + esc(r.confidence_level || 'unrated') + '" title="confidence ' + pct + '% ('
      + esc(r.confidence_level || 'unrated') + ')"><i style="width:' + pct + '%"></i></div>'
      + '<span class="agz-pct">' + pct + '%</span>'
      + '<span class="agz-pill ' + tone + '" title="'
      + esc((r.decision_reasons || []).join(' · ')) + '">' + agGlyph(glyph)
      + esc(label) + '</span>'
      + (sum ? '<div class="agz-sum">' + esc(sum) + '</div>' : '')
      + agDocPanel(r, d)
      + '</div>';
  }).join('');
}

/* The approved documentation set, downloadable from the run that approved it.
   Gated on `approved`: the point of the GENERATE class is that a second
   person signs off before artifacts exist, so the action does not appear on a
   pending, rejected or flagged row. */
function agDocPanel(r, decision) {
  if (r.agent_id !== 'documentation' || decision !== 'approved') return '';
  const docs = ((r.outputs || {}).documents || {}).documents || [];
  if (!docs.length) return '';
  return '<div class="agz-doc"><div class="agz-doc-top">'
    + '<b>' + docs.length + ' document(s) approved for publication</b>'
    + '<span class="grow"></span>'
    + DOC_FORMATS.map(f =>
        '<label class="agz-doc-fmt"><input type="checkbox" class="agDocFmt" value="'
        + f + '"' + (f === 'pdf' || f === 'docx' ? ' checked' : '') + '> '
        + DOC_FMT_LABEL[f] + '</label>').join('')
    + '<button type="button" class="agDocGen">Generate &amp; download</button>'
    + '</div><div class="agz-doc-out" id="agDocOut"></div></div>';
}

/* Which job holds this run's uploaded project. Recorded by _finish_job as
   `run_id` on the agents job, so the source tree is reachable without asking
   the user to upload the same folder into a second tab. */
async function agRunJobId() {
  if (!gAgRun) return '';
  try {
    const {jobs} = await api('/api/jobs?kind=agents&limit=1000');
    const j = (jobs || []).find(x => x.run_id === gAgRun.run_id);
    return j ? j.id : '';
  } catch (e) { return ''; }
}

document.addEventListener('click', async (ev) => {
  const btn = ev.target.closest('.agDocGen');
  if (!btn) return;
  ev.preventDefault();
  const out = $('#agDocOut');
  const formats = [...document.querySelectorAll('.agDocFmt:checked')]
    .map(c => c.value);
  if (!formats.length) {
    out.innerHTML = '<div class="agz-doc-note">Pick at least one format.</div>';
    return;
  }
  const row = (gAgRun.results || []).find(x => x.agent_id === 'documentation');
  const slugs = (((row || {}).outputs || {}).documents || {}).documents || [];
  btn.disabled = true;
  out.innerHTML = '<div class="agz-doc-note">Generating '
    + slugs.length + ' document(s)…</div>';
  try {
    const jobId = await agRunJobId();
    if (!jobId) {
      throw new Error("This run's source project is no longer on disk — "
        + 'generate from the Documentation tab instead.');
    }
    const d = await api('/api/docs', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({from_job: jobId, formats,
                            documents: slugs.map(s => s.slug)})});
    out.innerHTML = '<table class="agz-doc-tbl"><thead><tr><th>Document</th>'
      + '<th>Download</th></tr></thead><tbody>'
      + d.documents.map(doc => '<tr><td>' + esc(doc.title) + '</td><td>'
          + Object.keys(doc.files).map(f => '<a href="'
              + withKey('/api/docs/' + d.docs_id + '/download?doc='
                + encodeURIComponent(doc.slug) + '&format=' + f) + '">'
              + DOC_FMT_LABEL[f] + '</a>').join(' · ')
          + '</td></tr>').join('')
      + '</tbody></table>';
  } catch (e) {
    out.innerHTML = '<div class="agz-doc-note" style="color:var(--red)">'
      + esc(e.message) + '</div>';
  }
  btn.disabled = false;
});
document.addEventListener('click', (ev) => {
  const f = ev.target.closest('#agPipeFilter [data-agf]');
  if (!f) return;
  gAgPipeFilter = f.dataset.agf;
  paintAgPipeline();
});

async function refreshAgentRun() {
  if (!gAgRun) return;
  try {
    const rep = await api('/api/agents/runs/' + encodeURIComponent(gAgRun.run_id));
    renderAgentRun(rep);
  } catch (e) { /* ignore */ }
}

function startAgentRunPolling() {
  stopAgentRunPolling();
  gAgPollTimer = setInterval(() => {
    if (document.visibilityState === 'visible') refreshAgentRun();
  }, AG_POLL_MS);
}

function stopAgentRunPolling() {
  if (gAgPollTimer) { clearInterval(gAgPollTimer); gAgPollTimer = null; }
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') {
    stopAgentRunPolling();
  } else if (gAgRun && (gAgRun.approvals || []).some(a => a.status === 'pending')) {
    startAgentRunPolling();
    refreshAgentRun();
  }
});

async function openAgentRun(runId) {
  try {
    const rep = await api('/api/agents/runs/' + encodeURIComponent(runId));
    renderAgentRun(rep);
    // Picking a run is the end of the errand: close both drawers so the result
    // lands at the top of the panel instead of below the picker.
    [['agHistDrawer', 'agHistBtn'], ['agRunDrawer', 'agNewBtn']].forEach(([d, b]) => {
      if (!$('#' + d)) return;
      $('#' + d).classList.remove('on');
      $('#' + b).setAttribute('aria-expanded', 'false');
    });
    paintRecentAgentRuns();
  } catch (e) {
    mbAlert(e.message, {title: 'Could not open run'});
  }
}

async function restoreAgentRun() {
  const rid = localStorage.getItem('mb_agent_run_id');
  if (!rid || gAgRun) { agShowEmpty(); return; }
  try {
    const rep = await api('/api/agents/runs/' + encodeURIComponent(rid));
    renderAgentRun(rep);
    // Repaint history so the restored run carries the "current" marker —
    // paintRecentAgentRuns races this restore on first load.
    paintRecentAgentRuns();
  } catch (e) {
    localStorage.removeItem('mb_agent_run_id');
    agShowEmpty();
  }
}

function fmtRunTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return esc(iso);
  const date = d.toLocaleDateString('en-US', {month: 'short', day: '2-digit', year: 'numeric'});
  const time = d.toLocaleTimeString('en-US', {hour: '2-digit', minute: '2-digit', hour12: true});
  return date + ' | ' + time;
}

async function paintRecentAgentRuns() {
  const host = $('#agRecentRuns');
  if (!host) return;
  let runsResp, apprResp;
  try {
    [runsResp, apprResp] = await Promise.all([
      api('/api/agents/runs'),
      api('/api/agents/approvals?scope=pending'),
    ]);
  } catch (e) { return; }
  const runs = (runsResp.runs || []).slice().reverse().slice(0, 8);
  if ($('#agHistCount')) $('#agHistCount').textContent = runs.length ? '(' + runs.length + ')' : '';
  if (!runs.length) {
    host.innerHTML = '<div style="font-size:12px;color:var(--ink3)">No runs yet.</div>';
    return;
  }
  const pendByRun = {};
  (apprResp.approvals || []).forEach(a => {
    if (a.run_id) pendByRun[a.run_id] = (pendByRun[a.run_id] || 0) + 1;
  });
  // Eight runs of the same project with the same counts, stacked as two-line
  // blocks, were indistinguishable. Aligned columns make the one column that
  // differs the only thing your eye has to compare.
  host.innerHTML = '<table class="agz-runs"><thead><tr>'
    + '<th>Project</th><th class="agz-hide">Run</th><th>When</th>'
    + '<th class="agz-hide">Agents</th><th>Outcome</th><th></th></tr></thead><tbody>'
    + runs.map(r => {
        const n = pendByRun[r.run_id] || 0;
        const s = r.summary || {};
        const out = [
          [s.committed, 'committed', 'var(--green)'],
          [s.flagged, 'flagged', 'var(--amber)'],
          [s.denied, 'denied', 'var(--red)'],
          [s.failed, 'failed', 'var(--red)'],
        ].filter(c => c[0]).map(c =>
          '<span style="color:' + c[2] + '"><u>' + c[0] + '</u> ' + c[1] + '</span>'
        ).join('<span style="color:var(--border)"> · </span>');
        const open = gAgRun && gAgRun.run_id === r.run_id;
        return '<tr class="agrunlink' + (open ? ' on' : '') + '" tabindex="0" role="link"'
          + (open ? ' aria-current="true"' : '')
          + ' data-run="' + esc(r.run_id) + '" title="Open this run">'
          + '<td style="font-weight:600">' + esc(r.project || '—') + '</td>'
          + '<td class="agz-rid agz-hide">' + esc((r.run_id || '').slice(0, 8)) + '</td>'
          + '<td class="agz-when">' + fmtRunTime(r.created_at) + '</td>'
          + '<td class="agz-hide" style="color:var(--ink3)">' + (r.agents || 0) + '</td>'
          + '<td class="agz-out">' + (out || '—') + '</td>'
          + '<td style="text-align:right">' + (n
              ? '<span class="agz-chip warn" style="height:22px;font-size:11px">'
                + agGlyph('wait') + n + ' pending</span>' : '') + '</td>'
          + '</tr>';
      }).join('')
    + '</tbody></table>';
}
document.addEventListener('click', (ev) => {
  const rl = ev.target.closest('.agrunlink');
  if (!rl) return;
  ev.preventDefault();
  openAgentRun(rl.dataset.run);
});
// The history rows are table rows, so they need the keyboard activation an
// <a> would have given them for free.
document.addEventListener('keydown', (ev) => {
  if (ev.key !== 'Enter' && ev.key !== ' ') return;
  const rl = ev.target.closest && ev.target.closest('.agrunlink');
  if (!rl) return;
  ev.preventDefault();
  openAgentRun(rl.dataset.run);
});

// One approval row used by both the run view and Governance → Approval
// queue. Buttons render ONLY for actions the signed-in user may take
// (server-computed can_approve / can_reject / can_claim), so segregation of
// duties is visible up front instead of failing on click.
function approvalRow(a, withRun) {
  const done = a.status !== 'pending';
  let act;
  if (done) {
    const [tone, glyph] = agTone(a.status);
    // "by X" twice in one row (requester, then approver) read as a duplicate.
    // Each side says which role it is.
    act = '<span class="agz-pill ' + tone + '">' + agGlyph(glyph) + esc(a.status) + '</span>'
      + (a.approver ? '<span class="agz-ap-mt agz-who" title="' + esc(a.approver)
                      + '">decided by ' + esc(a.approver) + '</span>' : '');
  } else {
    const bits = [];
    if (a.claimed_by) bits.push('<span class="tag" title="An approver marked this as theirs to review">reviewing: ' + esc(a.claimed_by) + '</span>');
    if (a.can_claim && !a.claimed_by)
      bits.push('<button type="button" class="secondary ag-claim" data-id="' + esc(a.id)
        + '" style="margin:0;padding:2px 12px;font-size:12px" title="Mark this request as yours to review">Claim</button>');
    if (a.can_approve)
      bits.push('<button type="button" class="ag-approve" data-id="' + esc(a.id)
        + '" style="margin:0;padding:2px 12px;font-size:12px">Approve</button>');
    if (a.can_reject)
      bits.push('<button type="button" class="secondary ag-reject" data-id="' + esc(a.id)
        + '" style="margin:0;padding:2px 12px;font-size:12px">'
        + (a.is_requester && !a.can_approve ? 'Withdraw' : 'Reject') + '</button>');
    // Say WHY approval is unavailable whenever it is unavailable — not only
    // when there is no button at all. The requester always gets a Withdraw
    // button, so gating this on `!bits.length` meant the one person who most
    // needs the explanation (they are looking at their own blocked request)
    // was the only person who never saw it.
    if (!a.can_approve) {
      const why = a.is_requester
        ? 'You requested this run — a different approver must decide it. '
          + 'You can withdraw it instead.'
        : myUser ? permTitle('agents:approve')
                 : 'An owner or admin must approve this action';
      bits.unshift('<span style="font-size:11.5px;color:var(--muted)" title="'
        + esc(why) + '">'
        + (a.is_requester ? 'another approver must decide this'
                          : 'awaiting an approver') + '</span>');
    }
    act = bits.join('');
  }
  // The reason on every generate proposal is the same sentence — "generate
  // action (produces artifacts / changes systems) requires human approval" —
  // and it was ~40% of the row on all of them, restating the section title.
  // It moves to the row tooltip; anything non-boilerplate still shows.
  const reason = (a.reasons || [])[0] || '';
  const boiler = /requires human approval/i.test(reason);
  return '<div class="agz-ap' + (done ? ' done' : '') + '" title="'
    + esc((a.reasons || []).join(' · ')) + '">'
    + '<span class="agz-ap-nm">' + esc(a.agent_id.replace(/_/g, ' ')) + '</span>'
    + '<span class="agz-ap-mt">' + esc(a.action_class) + ' · '
    + Math.round((a.confidence || 0) * 100) + '% confidence</span>'
    + (withRun ? '<span class="agz-rid agz-ap-mt">' + esc((a.run_id || '').slice(0, 8))
                 + '</span>' : '')
    + (a.requested_by ? '<span class="agz-ap-mt agz-who" title="' + esc(a.requested_by)
                        + '">requested by ' + esc(a.requested_by) + '</span>' : '')
    + (boiler || !reason ? '' : '<span class="agz-ap-mt" style="color:var(--muted)">'
        + esc(reason) + '</span>')
    + '<span class="grow"></span>'
    + act + '</div>';
}
function approvalEscalationBanner() {
  return '<div style="background:var(--red-bg);border:1px solid #eec5c0;border-radius:8px;padding:9px 12px;'
    + 'margin-bottom:8px;font-size:12.5px;color:var(--red)"><b>No eligible approver.</b> '
    + 'The requester may never approve their own run, and no other member holds approval rights. '
    + 'Ask an owner to promote another admin in Settings → Members'
    + (canManageUsers() ? ' — <a data-go="settings" data-sec="members" class="gotoMembers" style="cursor:pointer;font-weight:600;color:var(--red);text-decoration:underline">open Members</a>' : '')
    + '. The requester can still withdraw the request.</div>';
}
function gotoAgentRun(runId) {
  // reportTool set BEFORE navigating: showPage('reports') reads it (both for
  // the hash it writes and for loadReports' synchronous applyReportTool()
  // call), so setting it first means landing on Agentic AI directly instead
  // of painting the previous tool tab and then correcting it a beat later.
  reportTool = 'ag';
  showPage('reports');
  openAgentRun(runId);
}
document.addEventListener('click', (ev) => {
  const rl = ev.target.closest('.aqrunlink');
  if (!rl) return;
  ev.preventDefault();
  gotoAgentRun(rl.dataset.run);
});
document.addEventListener('click', async (ev) => {
  const gm = ev.target.closest('.gotoMembers');
  if (gm) { document.querySelector('nav a[data-page="settings"]').click(); showSettings('members'); return; }
  const ap = ev.target.closest('.ag-approve');
  const rj = ev.target.closest('.ag-reject');
  const cl = ev.target.closest('.ag-claim');
  if (!ap && !rj && !cl) return;
  ev.preventDefault();
  const btn = ap || rj || cl;
  const url = ap ? '/api/agents/approvals/approve'
    : rj ? '/api/agents/approvals/reject' : '/api/agents/approvals/claim';
  btn.disabled = true;
  try {
    await api(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({approval_id: btn.dataset.id})});
    await refreshAgentRun();
    if (typeof loadApprovalQueue === 'function') loadApprovalQueue();
  } catch (e) {
    mbAlert(e.message, {title: 'Approval error'});
    btn.disabled = false;
  }
});

/* ---------------- Reports tool picker ----------------------------------
   Seven upload-and-run tools plus the run history, switched instead of
   stacked. Same shape as the Overview view toggle (`applyDashView`): only the
   selected panel is in the DOM flow, and the choice persists per browser.
   Default is the history — the common visit is "find the report I already
   generated", which should not open behind seven forms. Lives in the hash
   (#reports/ag), not localStorage — see dashView above for why. */
const REPORT_TOOL_PANELS = {
  hist: 'histPanel', assess: 'assessPanel', air: 'airPanel', debt: 'debtPanel',
  fin: 'finPanel', sec: 'secPanel', docs: 'docsPanel', ag: 'agPanel',
};
let reportTool = 'hist';

function applyReportTool() {
  // A persisted key from an older build (or a removed tool) falls back to the
  // history rather than hiding every panel and leaving the page blank.
  if (!REPORT_TOOL_PANELS[reportTool]) reportTool = 'hist';
  Object.keys(REPORT_TOOL_PANELS).forEach(k => {
    const el = $('#' + REPORT_TOOL_PANELS[k]);
    if (el) el.style.display = (k === reportTool) ? '' : 'none';
  });
  document.querySelectorAll('#reportToolSeg button').forEach(b =>
    b.setAttribute('aria-selected', String(b.dataset.tool === reportTool)));
}

function bindReportToolSeg() {
  document.querySelectorAll('#reportToolSeg button').forEach(b => {
    b.onclick = () => {
      if (reportTool === b.dataset.tool) return;
      reportTool = b.dataset.tool;
      setHash('reports/' + reportTool);
      applyReportTool();
    };
  });
}

async function loadReports() {
  try { await loadReportsBody(); }
  catch (e) { loadErr('reportsErr', e, loadReports); }
}
async function loadReportsBody() {
  // Before the await: switching tabs must not wait on /api/jobs.
  bindReportToolSeg();
  applyReportTool();
  paintExtLists();
  loadDocsCatalog();
  loadAgents();
  // Connections first: the verdict resolves connection ids to names from this
  // list, so a restored run would otherwise show bare ids on first paint.
  loadAgentConnections().then(restoreAgentRun);
  paintRecentAgentRuns();
  const {jobs} = await api('/api/jobs');
  gJobsCache = jobs;
  const rows = jobs.filter(j => j.kind !== 'upload').map(j =>
    '<tr><td><b>' + esc(reportKindLabel(j.kind)) + '</b></td>'
    + '<td>' + esc(j.project || '—') + '</td>'
    + '<td><a class="joblink" data-id="' + esc(j.id) + '" role="link" tabindex="0" '
      + 'title="Open job detail" style="color:var(--accent);cursor:pointer;font-family:monospace;font-size:12px">'
      + esc(j.id) + '</a></td>'
    + '<td>' + esc((j.created || '').replace('T', ' ')) + '</td>'
    // Was hardcoded done -> GENERATED, so a run that actually PASSED validation
    // still read "Generated" here while the modernization table read "Passed".
    + '<td>' + runStatusChip(j) + '</td>'
    + '<td>' + (j.status === 'done'
      ? '<a class="jobopen" data-id="' + esc(j.id) + '" style="color:var(--accent);cursor:pointer">View</a> · '
        + '<a href="' + withKey('/api/jobs/' + j.id + '/download') + '" style="color:var(--accent)">Export</a>' : '') + '</td></tr>');
  const reportsHeader = '<tr><th>Report</th><th>Project</th><th>Job ID</th><th>Generated</th><th>Status</th><th></th></tr>';
  const reportsEmpty = '<tr><td colspan=6><div class="empty" style="border:none"><b>No reports yet</b>Reports appear here after analysis, modernization, scaffold and governance runs.</div></td></tr>';
  // Job IDs and "View" both open the job-detail overlay (list state preserved)
  const renderReportsTable = () => {
    renderPage('reportsTable', rows, reportsHeader, reportsEmpty, 'reports', () => {
      $('#reportsTable').querySelectorAll('.joblink, .jobopen').forEach(a => {
        a.onclick = () => openJobDetail(a.dataset.id);
        a.onkeydown = ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); openJobDetail(a.dataset.id); } };
      });
    });
  };
  renderReportsTable();
  bindPager('reportsTable', renderReportsTable);
}

/* ---------------- Governance extras ---------------- */
// The agent approval queue — visible to every signed-in member so an
// eligible approver (not the requester) can find and decide pending
// requests; the sidebar badge advertises pending work without opening it.
// A rejection recorded against the requester is a WITHDRAWAL: the server never
// lets anyone approve their own request, so that is the only decision a runner
// can make on their own run (web/app.py agents_reject). Calling both of them
// "rejected" made a routine self-cancel look like a governance veto.
function aqWithdrawn(a) {
  const by = (a.approver || '').trim().toLowerCase();
  const req = (a.requested_by || '').trim().toLowerCase();
  return a.status === 'withdrawn'
      || (a.status === 'rejected' && !!by && by === req);
}
function aqOutcome(a) {
  if (a.status === 'approved') return {key: 'approved', label: 'Approved', tone: 'ok'};
  if (aqWithdrawn(a)) return {key: 'withdrawn', label: 'Withdrawn', tone: ''};
  if (a.status === 'rejected') return {key: 'rejected', label: 'Rejected', tone: 'bad'};
  // badge() writes its text as HTML, and this branch carries a server value
  return {key: a.status, label: esc(a.status || 'unknown'), tone: 'warn'};
}
function aqConfidence(a) {
  const pct = Math.round((a.confidence || 0) * 100);
  return '<span class="cbar' + (pct < 70 ? ' low' : '') + '">'
    + '<span class="t"><span class="f" style="width:' + Math.max(0, Math.min(100, pct)) + '%"></span></span>'
    + '<span class="v">' + pct + '%</span></span>';
}
// One block per run: header line (id, who ran it, how it came out) + its rows.
function aqRunBlock(runId, rows) {
  const ok = rows.filter(a => aqOutcome(a).key === 'approved').length;
  const wd = rows.filter(a => aqOutcome(a).key === 'withdrawn').length;
  const rj = rows.filter(a => aqOutcome(a).key === 'rejected').length;
  const chip = rj ? badge(rj + ' rejected', 'var(--red)')
    : wd && !ok ? badge(wd + ' withdrawn by runner', 'var(--amber)')
    : wd ? badge(ok + ' approved · ' + wd + ' withdrawn', 'var(--amber)')
    : badge(ok + ' of ' + rows.length + ' approved', 'var(--green)');
  const who = (rows.find(a => a.requested_by) || {}).requested_by || '';
  return '<div class="aqrun">' + (runId
      ? '<a class="rid aqrunlink" data-run="' + esc(runId) + '" style="cursor:pointer" title="Open this run">' + esc(runId) + '</a>'
      : '<span class="rid">—</span>')
    + (who ? '<span class="who">run by ' + esc(who) + '</span>' : '')
    + chip + '</div>'
    + '<table class="aqtbl"><colgroup><col style="width:26%"><col style="width:18%">'
    + '<col style="width:34%"><col style="width:22%"></colgroup>'
    + rows.map(a => {
        const o = aqOutcome(a);
        return '<tr><td class="act" title="' + esc(a.agent_id || '') + '">'
          + esc((a.agent_id || '').replace(/_/g, ' ')) + '</td>'
          + '<td>' + aqConfidence(a) + '</td>'
          + '<td class="mail" title="' + esc(a.approver || '') + '">' + esc(a.approver || '—') + '</td>'
          + '<td>' + badge(o.label, o.tone === 'ok' ? 'var(--green)'
                                  : o.tone === 'bad' ? 'var(--red)'
                                  : o.tone === 'warn' ? 'var(--amber)' : '') + '</td></tr>';
      }).join('')
    + '</table>';
}
async function loadApprovalQueue() {
  if (!$('#aqList')) return;
  try {
    const d = await api('/api/agents/approvals?scope=all');
    const all = d.approvals || [];
    const pend = all.filter(a => a.status === 'pending');
    const done = all.filter(a => a.status !== 'pending');
    // counts run over EVERY decision, not just the page of recent ones shown
    const n = k => done.filter(a => aqOutcome(a).key === k).length;
    $('#aqStats').innerHTML = card(done.length, 'Decisions')
      + card(n('approved'), 'Approved', n('approved') ? 'var(--green)' : 'var(--muted)')
      + card(n('withdrawn'), 'Withdrawn')
      + card(n('rejected'), 'Rejected', n('rejected') ? 'var(--red)' : 'var(--muted)');
    $('#aqCount').textContent = pend.length ? pend.length + ' waiting' : '';
    $('#aqEscalation').innerHTML = pend.some(a => a.no_eligible_approver) ? approvalEscalationBanner() : '';
    $('#aqList').innerHTML = pend.map(a => approvalRow(a, true)).join('')
      || '<div class="aqempty">&#10003; Nothing waiting. Consequential actions appear here when a swarm runs.</div>';
    // newest run first; rows keep the order the queue reports them in
    const recent = done.slice(-12).reverse();
    const order = [], byRun = {};
    recent.forEach(a => {
      const k = a.run_id || '';
      if (!byRun[k]) { byRun[k] = []; order.push(k); }
      byRun[k].push(a);
    });
    $('#aqDecided').innerHTML = order.length
      ? order.map(k => aqRunBlock(k, byRun[k].slice().reverse())).join('')
      : '<div class="aqempty">No decisions recorded yet.</div>';
    // Name the action class rather than asserting one: a mixed set of classes
    // would make the single-sentence claim false.
    const classes = [...new Set(recent.map(a => a.action_class).filter(Boolean))];
    $('#aqFoot').textContent = recent.length && classes.length === 1
      ? 'All ' + recent.length + ' are ' + classes[0] + ' actions — they produce artifacts or '
        + 'change systems, so approval is always required.'
      : recent.length ? 'Action classes in view: ' + classes.join(', ') + '.' : '';
    updateGovBadge(d.pending_count || 0);
  } catch (e) { /* queue stays empty; server enforces access */ }
}
// The audit trail is rendered per run on Reports -> Agentic AI; there is no
// separate page for it, so this goes to where it actually lives. Prefer the
// most recent run with a pending approval (the thing an admin most likely
// wants to inspect); fall back to whatever run was last viewed.
$('#aqAudit').onclick = async () => {
  let runId = null;
  try {
    const d = await api('/api/agents/approvals?scope=pending');
    const pend = d.approvals || [];
    if (pend.length) runId = pend[pend.length - 1].run_id;
  } catch (e) { /* fall through to last-viewed run */ }
  if (runId) { gotoAgentRun(runId); return; }
  reportTool = 'ag';
  showPage('reports');
  restoreAgentRun();
};
function updateGovBadge(n) {
  const nav = document.querySelector('nav a[data-page="governance"]');
  if (!nav) return;
  let b = nav.querySelector('.navbadge');
  if (!b) { b = document.createElement('span'); b.className = 'navbadge'; nav.appendChild(b); }
  b.textContent = n > 0 ? String(n) : '';
  b.style.display = n > 0 ? 'inline-block' : 'none';
  b.title = n > 0 ? n + ' agent action(s) awaiting approval' : '';
}
async function loadGovernanceExtras() {
  loadApprovalQueue();
  try {
    // Server-side filter, so the kind-blind 100-job cap cannot hide runs.
    const converts = (await api('/api/jobs?kind=convert&status=done')).jobs;
    // Each row's status used to be a HARDCODED statChip('MANUAL_REVIEW'), so
    // every completed run was labelled as needing review — 14 of 19 rows were
    // wrong on a real workspace, including a FAIL disguised as "manual review".
    // Read the run's own verdict instead; jobReport() memoizes and mapLimit()
    // throttles, so this is the same machinery the Overview already uses.
    const reports = await mapLimit(converts, 8, j => jobReport(j.id));
    const runs = converts.map((job, i) => {
      const r = reports[i] || {};
      const verdict = (r.migration_validation || {}).verdict || '';
      const manual = ((r.summary || {}).workload || {}).manual_queue || 0;
      // "Needs review" = the engine asked for a human, or the run failed, or
      // there are outstanding manual items. A PASS_WITH_WARNINGS run with an
      // empty queue has nothing to approve.
      const needs = verdict === 'MANUAL_REVIEW' || verdict === 'FAIL' || manual > 0;
      return {job, verdict, manual, needs, known: !!reports[i]};
    });
    const govHeader = '<tr><th>Project</th><th>Generated</th><th>Status</th><th>Manual items</th><th></th></tr>';
    const govRow = ({job, verdict, manual, known}) =>
      '<tr><td><b>' + esc(jobLabel(job)) + '</b></td>'
      + '<td>' + esc((job.finished || job.created || '').replace('T', ' ')) + '</td>'
      + '<td>' + (known ? statChip(verdictToStatus(verdict))
                        : '<span style="color:var(--muted)">—</span>') + '</td>'
      + '<td>' + (manual || (known ? '0' : '--')) + '</td>'
      + '<td><a class="gq" data-id="' + job.id + '" style="color:var(--accent);cursor:pointer">Review proposals</a></td></tr>';
    const renderGovQueue = () => {
      const scope = ($('#govScope') || {}).value || 'needs';
      const shown = scope === 'all' ? runs : runs.filter(x => x.needs);
      const hint = $('#govScopeHint');
      if (hint) hint.textContent = scope === 'needs'
        ? shown.length + ' of ' + runs.length + ' run(s) need a person'
        : '';
      const empty = scope === 'needs'
        ? '<tr><td colspan=5 style="color:var(--muted)">Nothing needs approval — every completed run '
          + 'either passed or has an empty manual queue. '
          + '<a id="govShowAll" style="color:var(--accent);cursor:pointer">Show all completed runs</a></td></tr>'
        : '<tr><td colspan=5 style="color:var(--muted)">No modernization runs yet — proposals appear after a run.</td></tr>';
      renderPage('govQueue', shown.map(govRow), govHeader, empty,
                 scope === 'needs' ? 'runs needing review' : 'runs', () => {
        $('#govQueue').querySelectorAll('.gq').forEach(a =>
          a.onclick = () => openJobFindings(a.dataset.id).catch(e => mbAlert(e.message)));
        const sa = $('#govShowAll');
        if (sa) sa.onclick = () => { $('#govScope').value = 'all';
                                     pagerState('govQueue').page = 1; renderGovQueue(); };
      });
    };
    const scopeSel = $('#govScope');
    if (scopeSel) scopeSel.onchange = () => {
      pagerState('govQueue').page = 1;   // page 2 of "needs" is not page 2 of "all"
      renderGovQueue();
    };
    renderGovQueue();
    bindPager('govQueue', renderGovQueue);
    const latest = converts[0];
    if (latest) {
      const r = await jobReport(latest.id);
      if (r) {
        const pool = (r.mappings || []).flatMap(m => m.issues || []).filter(i => i.severity === 'MANUAL' || i.severity === 'ERROR');
        const byCode = {};
        pool.forEach(i => byCode[i.code] = (byCode[i.code] || 0) + 1);
        $('#govRisksPanel').style.display = 'block';
        $('#govRisks').innerHTML = '<div style="font-size:12.5px;color:var(--muted);margin-bottom:8px">Latest run: ' + esc(latest.project || latest.id) + '</div>'
          + (Object.entries(byCode).sort((a, b) => b[1] - a[1]).slice(0, 10)
            .map(([k, n]) => '<span class="tag" style="margin:0 6px 6px 0;display:inline-block">' + esc(k) + ' × ' + n + '</span>').join('')
            || '<span style="color:var(--muted);font-size:13px">No manual-review risks in the latest run.</span>');
      }
    }
  } catch (e) {}
}

/* ---------------- Settings > System ---------------- */
let sysDiag = null;
async function loadSystemInfo() {
  try {
    const info = await api('/api/v1/info');
    const mode = gFirstRun ? 'Open mode' : 'Workspace accounts';
    const modeHint = gFirstRun
      ? 'No accounts or API key are configured, so this instance is open on this host. '
        + 'Creating the first account enables sign-in and role-based access.'
      : 'Sign-in is required; role-based permissions are enforced by the server.';
    gBuildId = info.console_build || '';
    const rows = [
      ['Deployment', 'Self-hosted', ''],
      ['Version', 'v' + String(info.version), ''],
      ['Console build', String(info.console_build || 'unknown'),
       'Content stamp of the page this browser loaded. If it differs from the '
       + 'server after a rebuild, you are on a cached page — hard-reload.'],
      ['System mode', mode, modeHint],
      ['API automation', info.auth_required
          ? 'API key required for programmatic access'
          : 'No API key configured', ''],
      ['MetaBridge AI', info.llm_available ? 'Enabled' : 'Not configured', ''],
      ['Supported formats', String((info.formats || []).length) + ' source / target formats', ''],
    ];
    $('#sysList').innerHTML = rows.map(r =>
      '<div class="fld"><label>' + r[0] + '</label><div class="ro">' + esc(r[1]) + '</div>'
      + (r[2] ? '<div class="fhint">' + esc(r[2]) + '</div>' : '') + '</div>').join('');
    sysDiag = {product: info.product, version: info.version, deployment: 'self-hosted',
               system_mode: mode, api_key_required: !!info.auth_required,
               ai_runtime: !!info.llm_available,
               formats: (info.formats || []).length,
               generated_at: new Date().toISOString()};
  } catch (e) { $('#sysList').textContent = '—'; }
}
$('#sysCopy').onclick = async () => {
  if (!sysDiag) await loadSystemInfo();
  try {
    await navigator.clipboard.writeText(JSON.stringify(sysDiag, null, 2));
    $('#sysCopied').hidden = false;
    setTimeout(() => { $('#sysCopied').hidden = true; }, 2000);
  } catch (e) { /* clipboard unavailable */ }
};

/* ---------------- Ask MetaBridge AI ---------------- */
const AI_SUGGESTIONS = [
  'Why does this asset require manual review?',
  'Explain the original business logic.',
  'What changed in the target implementation?',
  'What validation should I run?',
  'Show downstream impact.',
];
function openAiPanel(context) {
  $('#modalBody').innerHTML = '<h2 style="font-size:18px">Ask MetaBridge AI</h2>'
    + '<div class="v" style="color:var(--ink3);font-size:13px;margin-bottom:8px">Semantic reasoning over your migration context'
    + (context && context.project ? ' — <b>' + esc(context.project) + '</b>' : '') + '. Answers are advisory, never applied automatically.</div>'
    + '<div style="margin:8px 0">' + AI_SUGGESTIONS.map(q =>
        '<button type="button" class="secondary" style="margin:0 6px 6px 0;padding:6px 10px;font-size:12.5px" data-q="' + q + '">' + q + '</button>').join('') + '</div>'
    + '<textarea id="aiQ" rows="3" style="width:100%;border:1px solid #ccd3de;border-radius:8px;padding:10px;font-size:14px" placeholder="Ask about this migration…"></textarea>'
    + '<button id="aiAsk">Ask</button> <button class="secondary" id="closeModal">Close</button>'
    + '<div id="aiOut"></div>';
  $('#modalBody').className = 'modal'; $('#modalBg').style.display = 'flex';
  $('#closeModal').onclick = () => $('#modalBg').style.display = 'none';
  $('#modalBody').querySelectorAll('[data-q]').forEach(b => b.onclick = () => { $('#aiQ').value = b.dataset.q; });
  $('#aiAsk').onclick = async () => {
    const q = $('#aiQ').value.trim();
    if (!q) return;
    $('#aiAsk').disabled = true;
    $('#aiOut').innerHTML = '<div style="color:var(--muted);font-size:13px;padding:8px 0">Thinking…</div>';
    try {
      const d = await api('/api/v1/ai/ask', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({question: q, migration_id: context && context.migration_id || ''})});
      $('#aiOut').innerHTML = '<pre style="white-space:pre-wrap">' + esc(d.answer) + '</pre>'
        + '<div style="font-size:11.5px;color:var(--muted);margin-top:4px">MetaBridge AI · ' + esc(d.model || '') + ' · advisory only</div>';
    } catch (e) { $('#aiOut').innerHTML = '<div class="err" style="display:block">' + esc(e.message) + '</div>'; }
    finally { $('#aiAsk').disabled = false; }
  };
}
['askAiMod', 'askAiVal', 'askAiGov'].forEach(id => {
  const el = $('#' + id);
  if (el) el.onclick = () => openAiPanel(window._valCtx || (planJob ? {migration_id: planJob.id, project: planJob.project} : null));
});

// loadUser() first: renderers that gate controls by permission (marketplace
// install/uninstall, approval queue) must run AFTER myPerms is known, or a
// fast /api response could paint enabled controls for an unprivileged role.
loadUser().then(() => { loadApprovalQueue(); loadMarketplace(); });
// Restore the section named in the URL (#estate, #settings/members) so a
// refresh or a pasted link reopens where the user was. routeFromHash() runs
// that section's own loader, so loadDashboard() is only needed when there is
// no usable hash — otherwise it would fetch dashboard data nobody is looking at.
if (!routeFromHash()) loadDashboard();
loadSavedConnections(); fillScaffoldSelects(); fillModConnList();
