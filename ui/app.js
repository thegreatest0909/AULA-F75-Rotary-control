'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const state = {
  config: { bindings: {} },
  configHash: '',
  ui: { dim: 42, blur: 0, accent: 'violet', wallpaper: null },
  service: { running: false, external: false, error: null },
  apps: [],
  master: null,
  target: { app: '', step: 2, isVolume: true, action: null },
  wallpaperUrl: null,
  bindingsDirty: false,
  savingBindings: false,
  logCursor: 0,
  draggingUntil: 0,
  masterDraggingUntil: 0,
  audioError: null,
};

const BINDINGS = [
  { key: 'rotate_left', title: 'Вращение влево', hint: 'Против часовой стрелки' },
  { key: 'rotate_right', title: 'Вращение вправо', hint: 'По часовой стрелке' },
  { key: 'press', title: 'Короткое нажатие', hint: 'Отпускание раньше порога удержания' },
  { key: 'hold', title: 'Удержание', hint: 'Дольше порога hold_ms из конфига' },
  { key: 'held_rotate_left', title: 'Влево + второй слой', hint: 'Второй слой действий' },
  { key: 'held_rotate_right', title: 'Вправо + второй слой', hint: 'Второй слой действий' },
];

// Второй слой (held_rotate_*) включается вращением с зажатым модификатором:
// на AULA F75 драйвер присылает нажатие ручки мгновенным тапом, поэтому
// «зажатая ручка» до софта не доходит.
function secondLayerModifier() {
  const knob = (state.config && state.config.knob) || {};
  const raw = knob.second_layer_modifier == null ? 'shift' : knob.second_layer_modifier;
  const name = String(raw).trim().toLowerCase();
  return name === 'none' || name === '' ? '' : name;
}

function bindingMetaList() {
  const mod = secondLayerModifier();
  const modTitle = mod ? mod.charAt(0).toUpperCase() + mod.slice(1) : '';
  return BINDINGS.map((meta) => {
    if (meta.key !== 'held_rotate_left' && meta.key !== 'held_rotate_right') return meta;
    const dir = meta.key === 'held_rotate_left' ? 'Влево' : 'Вправо';
    return {
      ...meta,
      title: mod ? `${dir} + ${modTitle}` : `${dir} при зажатой ручке`,
      hint: mod ? `Второй слой: вращение с зажатым ${modTitle}` : 'Второй слой действий',
    };
  });
}

const TYPES = [
  ['volume', 'Громкость'],
  ['mute', 'Mute'],
  ['media', 'Медиа'],
  ['keys', 'Горячие клавиши'],
  ['text', 'Текст'],
  ['command', 'Команда'],
  ['open', 'Открыть файл или папку'],
  ['url', 'Ссылка'],
  ['wheel', 'Прокрутка'],
  ['focus', 'Фокус окна'],
  ['sleep', 'Пауза'],
  ['none', 'Ничего'],
];

const MEDIA = [
  ['play_pause', 'Play / pause'],
  ['next', 'Следующий трек'],
  ['prev', 'Предыдущий трек'],
  ['stop', 'Стоп'],
  ['mute', 'Mute системы'],
];

const PAGES = {
  home: ['Обзор', 'Одна ручка — и весь звук под контролем.'],
  volume: ['Громкость', 'Ручка управляет только выбранным приложением.'],
  bindings: ['Привязки', 'Любое действие на вращение, нажатие и удержание.'],
  appearance: ['Оформление', 'Свои обои и акцентный цвет меню.'],
  log: ['Журнал', 'События ручки и сервиса.'],
};

const esc = (value) =>
  String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));

const num = (value, fallback) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

let toastTimer = 0;

function toast(message, kind = 'ok') {
  const element = $('#toast');
  element.textContent = message;
  element.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    element.className = 'toast';
  }, kind === 'error' ? 5200 : 2800);
}

async function api(path, options = {}) {
  const opts = { method: options.method || 'GET', headers: {} };
  if (options.body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(options.body);
  }
  if (options.raw) {
    opts.body = options.raw;
  }
  const response = await fetch(path, opts);
  let data;
  try {
    data = await response.json();
  } catch (error) {
    throw new Error(`Сервер вернул некорректный ответ (HTTP ${response.status})`);
  }
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `Ошибка HTTP ${response.status}`);
  }
  return data;
}

function paintRange(element) {
  const min = Number(element.min || 0);
  const max = Number(element.max || 100);
  const value = Number(element.value);
  const percent = max === min ? 0 : ((value - min) / (max - min)) * 100;
  element.style.setProperty('--val', String(percent));
}

function actionObject(value) {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return { ...value };
  }
  if (Array.isArray(value)) {
    return { type: 'command', cmd: '', _list: value };
  }
  if (typeof value === 'string') {
    const index = value.indexOf(':');
    if (index < 0) {
      return { type: 'command', cmd: value };
    }
    const head = value.slice(0, index).trim().toLowerCase();
    const tail = value.slice(index + 1);
    const map = {
      keys: { type: 'keys', keys: tail.trim() },
      key: { type: 'keys', keys: tail.trim() },
      text: { type: 'text', text: tail },
      run: { type: 'command', cmd: tail },
      cmd: { type: 'command', cmd: tail },
      command: { type: 'command', cmd: tail },
      open: { type: 'open', target: tail.trim() },
      url: { type: 'url', url: tail.trim() },
      media: { type: 'media', action: tail.trim() },
      volume: { type: 'volume', delta: Number(tail) || 0 },
      wheel: { type: 'wheel', delta: Number(tail) || 0 },
      focus: { type: 'focus', title: tail },
      sleep: { type: 'sleep', ms: Number(tail) || 0 },
    };
    return map[head] || { type: 'command', cmd: value };
  }
  return { type: 'none' };
}

function typeLabel(type) {
  const found = TYPES.find(([value]) => value === type);
  return found ? found[1] : type;
}

function typeOptions(current) {
  return TYPES.map(
    ([value, label]) =>
      `<option value="${value}"${value === current ? ' selected' : ''}>${label}</option>`
  ).join('');
}

function mediaOptions(current) {
  return MEDIA.map(
    ([value, label]) =>
      `<option value="${value}"${value === current ? ' selected' : ''}>${label}</option>`
  ).join('');
}

function stateName(value) {
  if (value === true) return 'on';
  if (value === false) return 'off';
  const name = String(value ?? 'toggle').toLowerCase();
  return ['on', 'off'].includes(name) ? name : 'toggle';
}

function describeAction(value) {
  const action = actionObject(value);
  switch (action.type) {
    case 'volume': {
      const app = action.app ? ` · ${action.app}` : '';
      if (action.level !== undefined) return `Громкость${app} = ${action.level}%`;
      const delta = num(action.delta, 1);
      return `Громкость${app} · ${delta > 0 ? '+' : ''}${delta} × ${num(action.step, 2)}%`;
    }
    case 'mute':
      return `Mute ${action.app || 'системы'}`;
    case 'media': {
      const item = MEDIA.find(([key]) => key === action.action);
      return `Медиа · ${item ? item[1] : action.action || '—'}`;
    }
    case 'keys': {
      const keys = Array.isArray(action.keys) ? action.keys.join(', ') : action.keys;
      return `Клавиши · ${keys || '—'}`;
    }
    case 'text':
      return `Текст · ${(action.text || '').slice(0, 36) || '—'}`;
    case 'command':
      return `Команда · ${(action.cmd || '').slice(0, 42) || '—'}`;
    case 'open':
      return `Открыть · ${action.target || '—'}`;
    case 'url':
      return `Ссылка · ${action.url || '—'}`;
    case 'wheel':
      return `Прокрутка · ${action.delta ?? 0}${action.horizontal ? ' ↔' : ''}`;
    case 'focus':
      return `Фокус окна · ${action.title || '—'}`;
    case 'sleep':
      return `Пауза · ${action.ms ?? 0} мс`;
    default:
      return 'Ничего';
  }
}

function defaultsFor(type, bindingKey) {
  const left = bindingKey.includes('left');
  switch (type) {
    case 'volume':
      return { type, delta: left ? -1 : 1, step: 2 };
    case 'mute':
      return { type, state: 'toggle' };
    case 'media':
      return { type, action: 'play_pause' };
    case 'keys':
      return { type, keys: 'ctrl+alt+t' };
    case 'text':
      return { type, text: '' };
    case 'command':
      return { type, cmd: '', hidden: true };
    case 'open':
      return { type, target: '' };
    case 'url':
      return { type, url: 'https://' };
    case 'wheel':
      return { type, delta: left ? 1 : -1 };
    case 'focus':
      return { type, title: '' };
    case 'sleep':
      return { type, ms: 200 };
    default:
      return { type: 'none' };
  }
}

/* -------------------------------------------------------------------- tabs */
function switchTab(name) {
  if (!PAGES[name]) name = 'home';
  $$('.nav-item').forEach((button) => {
    button.classList.toggle('active', button.dataset.tab === name);
  });
  $$('.tab').forEach((section) => {
    section.classList.toggle('active', section.dataset.tab === name);
  });
  const [title, subtitle] = PAGES[name] || PAGES.home;
  $('#page-title').textContent = title;
  $('#page-subtitle').textContent = subtitle;
  if (location.hash !== `#${name}`) {
    history.replaceState(null, '', `#${name}`);
  }
  const main = $('.main');
  if (main) main.scrollTop = 0;
}

/* ------------------------------------------------------------------- theme */
function applyUi() {
  const ui = state.ui || {};
  document.documentElement.dataset.accent = ui.accent || 'violet';
  document.documentElement.style.setProperty('--dim', String((num(ui.dim, 42) / 100).toFixed(3)));
  document.documentElement.style.setProperty('--blur', `${num(ui.blur, 0)}px`);
  const background = $('#bg');
  if (state.wallpaperUrl) {
    background.style.backgroundImage = `url("${state.wallpaperUrl}")`;
  } else {
    background.style.backgroundImage = '';
  }
  renderUiControls();
}

function renderUiControls() {
  const ui = state.ui || {};
  const dim = $('#dim');
  const blur = $('#blur');
  if (dim && document.activeElement !== dim) {
    dim.value = String(num(ui.dim, 42));
    $('#dim-value').textContent = `${dim.value}%`;
  }
  if (blur && document.activeElement !== blur) {
    blur.value = String(num(ui.blur, 0));
    $('#blur-value').textContent = `${blur.value} px`;
  }
  if (dim) paintRange(dim);
  if (blur) paintRange(blur);
  $$('.accent').forEach((button) => {
    button.classList.toggle('active', button.dataset.accent === (ui.accent || 'violet'));
  });
}

let uiTimer = 0;

function queueUiSave() {
  clearTimeout(uiTimer);
  uiTimer = setTimeout(async () => {
    try {
      await api('/api/ui', {
        method: 'POST',
        body: { dim: state.ui.dim, blur: state.ui.blur, accent: state.ui.accent },
      });
    } catch (error) {
      toast(error.message, 'error');
    }
  }, 180);
}

/* ----------------------------------------------------------------- service */
function renderService() {
  const service = state.service || {};
  const dot = $('#service-dot');
  const text = $('#service-text');
  const button = $('#service-toggle');
  const note = $('#service-note');
  dot.className = 'dot';
  note.className = 'service-note';
  note.textContent = '';

  if (service.running) {
    dot.classList.add('on');
    text.textContent = 'Сервис работает';
    button.textContent = 'Остановить';
  } else if (service.external) {
    dot.classList.add('warn');
    text.textContent = 'Работает отдельно';
    button.textContent = 'Остановить';
    note.classList.add('show');
    note.textContent = 'Ручку обслуживает процесс, запущенный вне меню.';
  } else {
    text.textContent = 'Сервис остановлен';
    button.textContent = 'Запустить';
    if (service.error) {
      note.classList.add('show');
      note.textContent = service.error;
    }
  }
}

/* ------------------------------------------------------------------ target */
function deriveTarget() {
  const bindings = (state.config && state.config.bindings) || {};
  const right = actionObject(bindings.rotate_right);
  if (right.type === 'volume') {
    const press = actionObject(bindings.press);
    const targetApp = right.app ? String(right.app).toLowerCase() : '';
    const pressApp = press.type === 'mute' && press.app ? String(press.app).toLowerCase() : '';
    return {
      app: right.app ? String(right.app) : '',
      step: num(right.step, 2),
      isVolume: true,
      action: right,
      muteOnPress: press.type === 'mute' && pressApp === targetApp,
    };
  }
  return { app: '', step: 2, isVolume: false, action: right, muteOnPress: false };
}

function renderTarget() {
  state.target = deriveTarget();
  const target = state.target;
  const appName = target.app || 'Система';
  $('#top-target').textContent = appName;
  $('#home-target').textContent = target.app || 'Системная громкость';
  $('#home-type').textContent = target.app ? 'Приложение' : 'Система';
  $('#hero-caption').textContent = target.app || 'Системная громкость';
  $('#target-badge').textContent = target.app || 'Система';
  $('#target-mute').checked = Boolean(target.muteOnPress);

  if (target.isVolume) {
    $('#home-step').textContent = `Шаг ${target.step}% за щелчок`;
    $('#target-app').value = target.app;
    $('#target-step').value = String(Math.min(10, Math.max(1, target.step)));
  } else {
    $('#home-step').textContent = describeAction(target.action);
    $('#target-app').value = '';
  }
  $('#target-step-value').textContent = `${$('#target-step').value}%`;
  paintRange($('#target-step'));
}

async function applyTarget(appOverride) {
  const app = appOverride !== undefined ? String(appOverride).trim() : $('#target-app').value.trim();
  const step = Math.max(1, Math.min(50, Number($('#target-step').value) || 2));
  const muteOnPress = $('#target-mute').checked;
  if (appOverride !== undefined) {
    $('#target-app').value = app;
  }
  const button = $('#target-apply');
  button.disabled = true;
  try {
    const data = await api('/api/target', {
      method: 'POST',
      body: { app, step, mute_on_press: muteOnPress },
    });
    state.config = data.config;
    state.configHash = JSON.stringify(data.config);
    renderTarget();
    renderApps();
    if (state.bindingsDirty) {
      updateBindingCard('rotate_left', actionObject(data.config.bindings.rotate_left));
      updateBindingCard('rotate_right', actionObject(data.config.bindings.rotate_right));
      if (muteOnPress) {
        updateBindingCard('press', actionObject(data.config.bindings.press));
      }
    } else {
      renderBindings();
    }
    toast(app ? `Ручка управляет громкостью: ${app}` : 'Ручка управляет системной громкостью');
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    button.disabled = false;
  }
}

/* ------------------------------------------------------------------- audio */
function renderApps() {
  if (Date.now() < state.draggingUntil) return;
  const container = $('#app-rows');
  if (!state.apps.length) {
    const hint = state.audioError
      ? `Не удалось получить аудиосессии: ${esc(state.audioError)}`
      : 'Пока нет активных аудиосессий. Запустите музыку или видео в приложении.';
    container.innerHTML = `<div class="empty">${hint}</div>`;
    return;
  }
  const targetApp = (state.target.app || '').toLowerCase();
  container.innerHTML = state.apps
    .map((app) => {
      const initials = (app.title || app.app || '?').replace(/[^a-zA-Zа-яА-Я0-9]/g, '').slice(0, 2);
      const current = targetApp && app.app.toLowerCase() === targetApp;
      return `
        <div class="app-row${current ? ' current' : ''}" data-app="${esc(app.app)}">
          <div class="app-info">
            <div class="app-avatar">${esc(initials || '??')}</div>
            <div class="app-name">
              <strong>${esc(app.title || app.app)}</strong>
              <small>${esc(app.app)}${app.sessions > 1 ? ` · ${app.sessions} сессии` : ''}${current ? ' · цель ручки' : ''}</small>
            </div>
          </div>
          <input type="range" min="0" max="100" value="${app.volume}" data-action="volume" aria-label="Громкость ${esc(app.app)}">
          <span class="app-percent">${app.volume}%</span>
          <div class="app-actions">
            <button class="mini${app.mute ? ' on' : ''}" data-action="mute">${app.mute ? 'Unmute' : 'Mute'}</button>
            <button class="mini accent" data-action="target">Ручка</button>
          </div>
        </div>`;
    })
    .join('');
  $$('.app-row input[type=range]').forEach(paintRange);
  renderAppDatalist();
}

function renderAppDatalist() {
  $('#app-list').innerHTML = state.apps
    .map((app) => `<option value="${esc(app.app)}">${esc(app.title || app.app)}</option>`)
    .join('');
}

function renderMaster() {
  const master = state.master;
  if (!master) return;
  if (Date.now() >= state.masterDraggingUntil) {
    $('#master-slider').value = String(master.volume);
  }
  $('#master-value').textContent = `${master.volume}%`;
  const button = $('#master-mute');
  button.textContent = master.mute ? 'Unmute' : 'Mute';
  button.classList.toggle('primary', false);
  paintRange($('#master-slider'));
}

const volumeTimers = new Map();

function queueAppVolume(app, level) {
  clearTimeout(volumeTimers.get(app));
  volumeTimers.set(
    app,
    setTimeout(async () => {
      try {
        await api('/api/app-volume', { method: 'POST', body: { app, level } });
        const item = state.apps.find((entry) => entry.app === app);
        if (item) item.volume = level;
      } catch (error) {
        toast(error.message, 'error');
      }
    }, 70)
  );
}

let masterTimer = 0;

function queueMasterVolume(level) {
  clearTimeout(masterTimer);
  masterTimer = setTimeout(async () => {
    try {
      await api('/api/master-volume', { method: 'POST', body: { level } });
      if (state.master) state.master.volume = level;
    } catch (error) {
      toast(error.message, 'error');
    }
  }, 70);
}

async function refreshAudio() {
  try {
    const data = await api('/api/audio');
    state.apps = data.apps || [];
    state.master = data.master;
    state.audioError = data.audio_error || null;
    renderApps();
    renderMaster();
  } catch (error) {
    state.audioError = error.message;
    renderApps();
  }
}

/* ---------------------------------------------------------------- bindings */
function fieldHtml(type, action) {
  switch (type) {
    case 'volume':
      return `
        <label class="field wide"><span>Приложение <small>пусто = система</small></span>
          <input data-field="app" list="app-list" value="${esc(action.app || '')}" placeholder="chrome.exe" spellcheck="false"></label>
        <label class="field"><span>Шагов за щелчок</span>
          <input data-field="delta" type="number" step="1" value="${num(action.delta ?? 1, 1)}"></label>
        <label class="field"><span>Шаг, %</span>
          <input data-field="step" type="number" min="1" max="50" step="1" value="${num(action.step, 2)}"></label>`;
    case 'mute':
      return `
        <label class="field wide"><span>Приложение <small>пусто = система</small></span>
          <input data-field="app" list="app-list" value="${esc(action.app || '')}" placeholder="chrome.exe" spellcheck="false"></label>
        <label class="field"><span>Состояние</span>
          <select data-field="state">
            <option value="toggle"${stateName(action.state) === 'toggle' ? ' selected' : ''}>Переключать</option>
            <option value="on"${stateName(action.state) === 'on' ? ' selected' : ''}>Включить mute</option>
            <option value="off"${stateName(action.state) === 'off' ? ' selected' : ''}>Выключить mute</option>
          </select></label>`;
    case 'media':
      return `<label class="field wide"><span>Медиа-действие</span>
        <select data-field="action">${mediaOptions(action.action || 'play_pause')}</select></label>`;
    case 'keys': {
      const keys = Array.isArray(action.keys) ? action.keys.join(', ') : action.keys || '';
      return `
        <label class="field wide"><span>Сочетания <small>через запятую</small></span>
          <input data-field="keys" value="${esc(keys)}" placeholder="ctrl+shift+m, alt+tab" spellcheck="false"></label>
        <label class="field"><span>Повторов</span>
          <input data-field="repeat" type="number" min="1" max="50" value="${num(action.repeat, 1)}"></label>`;
    }
    case 'text':
      return `<label class="field wide"><span>Текст</span>
        <textarea data-field="text" placeholder="Текст, который нужно напечатать">${esc(action.text || '')}</textarea></label>`;
    case 'command':
      return `
        <label class="field wide"><span>Команда</span>
          <textarea data-field="cmd" placeholder="notepad.exe">${esc(action.cmd || '')}</textarea></label>
        <label class="check"><input type="checkbox" data-field="hidden"${action.hidden === false ? '' : ' checked'}><span class="check-box"></span>Скрыть окно консоли</label>
        <label class="check"><input type="checkbox" data-field="wait"${action.wait ? ' checked' : ''}><span class="check-box"></span>Ждать завершения</label>`;
    case 'open':
      return `<label class="field wide"><span>Файл, папка или URI</span>
        <input data-field="target" value="${esc(action.target || '')}" placeholder="C:\\Users\\… или discord://" spellcheck="false"></label>`;
    case 'url':
      return `<label class="field wide"><span>Ссылка</span>
        <input data-field="url" value="${esc(action.url || '')}" placeholder="https://example.com" spellcheck="false"></label>`;
    case 'wheel':
      return `
        <label class="field"><span>Щелчков</span>
          <input data-field="delta" type="number" step="1" value="${num(action.delta, 1)}"></label>
        <label class="check"><input type="checkbox" data-field="horizontal"${action.horizontal ? ' checked' : ''}><span class="check-box"></span>Горизонтально</label>`;
    case 'focus':
      return `<label class="field wide"><span>Заголовок окна <small>подстрока</small></span>
        <input data-field="title" value="${esc(action.title || '')}" placeholder="Discord" spellcheck="false"></label>`;
    case 'sleep':
      return `<label class="field"><span>Пауза, мс</span>
        <input data-field="ms" type="number" min="0" step="10" value="${num(action.ms, 200)}"></label>`;
    default:
      return '<div class="empty" style="padding:14px">Действие отключено</div>';
  }
}

function renderFields(card, action) {
  const container = card.querySelector('[data-role="fields"]');
  container.innerHTML = fieldHtml(action.type, action);
  const chip = card.querySelector('[data-role="chip"]');
  if (chip) chip.textContent = typeLabel(action.type);
  const description = card.querySelector('[data-role="desc"]');
  if (description) description.textContent = describeAction(action);
}

function collectAction(card) {
  const select = card.querySelector('[data-role="type"]');
  const type = select.value;
  const original = card._action || {};
  const action = type === original.type ? { ...original, type } : { type };

  const fields = $$('[data-field]', card);
  for (const element of fields) {
    const key = element.dataset.field;
    if (element.type === 'checkbox') {
      action[key] = element.checked;
      continue;
    }
    const raw = element.value;
    if (raw === '') {
      delete action[key];
      continue;
    }
    if (element.type === 'number') {
      const value = Number(raw);
      if (Number.isFinite(value)) action[key] = value;
      else delete action[key];
    } else if (key === 'keys') {
      const combos = raw.split(',').map((part) => part.trim()).filter(Boolean);
      action.keys = combos.length > 1 ? combos : combos[0] || '';
    } else {
      action[key] = raw;
    }
  }
  return action;
}

function renderBindings() {
  const container = $('#binding-list');
  const bindings = (state.config && state.config.bindings) || {};
  container.innerHTML = '';
  for (const meta of bindingMetaList()) {
    const action = actionObject(bindings[meta.key]);
    const card = document.createElement('div');
    card.className = 'binding';
    card.dataset.binding = meta.key;
    card._action = action;
    card.innerHTML = `
      <div class="binding-head">
        <div><h3>${meta.title}</h3><p>${meta.hint}</p></div>
        <span class="type-chip" data-role="chip">${typeLabel(action.type)}</span>
      </div>
      <div class="binding-body">
        <label class="field"><span>Действие</span>
          <select data-role="type">${typeOptions(action.type)}</select></label>
        <div class="fields" data-role="fields"></div>
      </div>
      <div class="binding-foot">
        <span class="binding-desc" data-role="desc">${esc(describeAction(action))}</span>
        <button class="button ghost tiny" data-role="test">Проверить</button>
      </div>`;
    const select = card.querySelector('[data-role="type"]');
    select.addEventListener('change', () => {
      const next = defaultsFor(select.value, meta.key);
      card._action = next;
      renderFields(card, next);
      markBindingsDirty();
    });
    card.querySelector('[data-role="test"]').addEventListener('click', () => testBinding(meta.key));
    card.addEventListener('input', () => {
      markBindingsDirty();
      const current = collectAction(card);
      card.querySelector('[data-role="chip"]').textContent = typeLabel(current.type);
      card.querySelector('[data-role="desc"]').textContent = describeAction(current);
    });
    renderFields(card, action);
    container.appendChild(card);
  }
}

function updateBindingCard(key, action) {
  const card = document.querySelector(`.binding[data-binding="${key}"]`);
  if (!card) return;
  card._action = action;
  card.querySelector('[data-role="type"]').value = action.type;
  renderFields(card, action);
}

function markBindingsDirty() {
  state.bindingsDirty = true;
  const label = $('#bindings-state');
  label.textContent = 'не сохранено';
  label.classList.add('show');
}

function collectBindings() {
  const result = {};
  $$('.binding').forEach((card) => {
    result[card.dataset.binding] = collectAction(card);
  });
  return result;
}

async function saveBindings() {
  if (state.savingBindings) return;
  state.savingBindings = true;
  const saveButton = $('#bindings-save');
  saveButton.disabled = true;
  $('#bindings-error').hidden = true;
  try {
    const bindings = collectBindings();
    const data = await api('/api/config', {
      method: 'POST',
      body: { ...state.config, bindings },
    });
    state.config = data.config;
    state.configHash = JSON.stringify(data.config);
    state.bindingsDirty = false;
    $('#bindings-state').classList.remove('show');
    renderBindings();
    renderTarget();
    toast('Привязки сохранены');
  } catch (error) {
    const box = $('#bindings-error');
    box.hidden = false;
    box.textContent = error.message;
    toast('Проверьте привязки', 'error');
  } finally {
    state.savingBindings = false;
    saveButton.disabled = false;
  }
}

async function reloadBindings() {
  try {
    const data = await api('/api/state');
    state.config = data.config;
    state.configHash = JSON.stringify(data.config);
    state.bindingsDirty = false;
    $('#bindings-state').classList.remove('show');
    $('#bindings-error').hidden = true;
    renderBindings();
    renderTarget();
    toast('Изменения отменены');
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function testBinding(name) {
  try {
    await api('/api/test', { method: 'POST', body: { binding: name } });
    toast('Тестовое действие отправлено');
    setTimeout(pollLog, 350);
  } catch (error) {
    toast(error.message, 'error');
  }
}

/* --------------------------------------------------------------------- log */
function appendLog(text) {
  const log = $('#log');
  if (log.dataset.placeholder === '1') {
    log.textContent = '';
    log.dataset.placeholder = '0';
  }
  const nearBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
  const combined = `${log.textContent}${log.textContent ? '\n' : ''}${text}`;
  log.textContent = combined.length > 220000 ? combined.slice(-200000) : combined;
  if (nearBottom) log.scrollTop = log.scrollHeight;
}

async function pollLog() {
  try {
    const data = await api(`/api/log?since=${state.logCursor}`);
    state.logCursor = data.cursor;
    if (data.lines.length) {
      appendLog(data.lines.map((line) => line.text).join('\n'));
    }
  } catch (error) {
    /* журнал не критичен */
  }
}

/* ------------------------------------------------------------------ status */
async function pollStatus() {
  try {
    const data = await api('/api/state');
    state.service = data.service;
    renderService();
    const hash = JSON.stringify(data.config);
    if (hash !== state.configHash) {
      state.configHash = hash;
      if (!state.bindingsDirty) {
        state.config = data.config;
        renderTarget();
        renderBindings();
      }
    }
  } catch (error) {
    /* временная недоступность сервера */
  }
}

/* ---------------------------------------------------------------- wallpaper */
async function uploadWallpaper(file) {
  if (!file) return;
  if (file.size > 30 * 1024 * 1024) {
    toast('Файл больше 30 МБ', 'error');
    return;
  }
  if (!/^image\//.test(file.type) && !/\.(png|jpe?g|webp|gif|bmp)$/i.test(file.name)) {
    toast('Это не похоже на изображение', 'error');
    return;
  }
  const hint = $('#wallpaper-hint');
  hint.textContent = 'Загружаю…';
  try {
    const response = await fetch('/api/wallpaper', {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: file,
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || 'Не удалось загрузить обои');
    }
    state.ui = data.ui;
    state.wallpaperUrl = `${data.url}&t=${Date.now()}`;
    applyUi();
    hint.textContent = 'Обои обновлены. Настройте затемнение и размытие ниже.';
    toast('Обои применены');
  } catch (error) {
    hint.textContent = 'До 30 МБ · PNG, JPG, WEBP, GIF или BMP';
    toast(error.message, 'error');
  }
}

async function clearWallpaper() {
  try {
    const data = await api('/api/wallpaper/clear', { method: 'POST', body: {} });
    state.ui = data.ui;
    state.wallpaperUrl = null;
    applyUi();
    $('#wallpaper-hint').textContent = 'Обои убраны. Можно выбрать новую картинку.';
    toast('Обои убраны');
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function setDesktopWallpaper() {
  try {
    await api('/api/wallpaper/desktop', { method: 'POST', body: {} });
    toast('Обои поставлены на рабочий стол');
  } catch (error) {
    toast(error.message, 'error');
  }
}

/* ------------------------------------------------------------------- wiring */
function wire() {
  $$('.nav-item').forEach((button) => {
    button.addEventListener('click', () => switchTab(button.dataset.tab));
  });
  $$('[data-goto]').forEach((button) => {
    button.addEventListener('click', () => switchTab(button.dataset.goto));
  });
  window.addEventListener('hashchange', () => {
    switchTab((location.hash || '').replace('#', ''));
  });

  $('#service-toggle').addEventListener('click', async () => {
    const button = $('#service-toggle');
    const action = state.service && (state.service.running || state.service.external) ? 'stop' : 'start';
    button.disabled = true;
    try {
      const data = await api('/api/service', { method: 'POST', body: { action } });
      state.service = data.service;
      renderService();
      toast(action === 'stop' ? 'Сервис остановлен' : 'Сервис запущен');
      setTimeout(pollStatus, 800);
    } catch (error) {
      toast(error.message, 'error');
      pollStatus();
    } finally {
      button.disabled = false;
    }
  });

  $('#target-apply').addEventListener('click', () => applyTarget());
  $('#target-system').addEventListener('click', () => applyTarget(''));
  $('#target-step').addEventListener('input', (event) => {
    $('#target-step-value').textContent = `${event.target.value}%`;
    paintRange(event.target);
  });

  $('#master-slider').addEventListener('input', (event) => {
    const level = Number(event.target.value);
    state.masterDraggingUntil = Date.now() + 1200;
    $('#master-value').textContent = `${level}%`;
    paintRange(event.target);
    queueMasterVolume(level);
  });

  $('#master-mute').addEventListener('click', async () => {
    const current = state.master ? state.master.mute : false;
    try {
      const data = await api('/api/master-mute', {
        method: 'POST',
        body: { state: !current },
      });
      if (state.master) state.master.mute = data.mute;
      renderMaster();
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  $('#apps-refresh').addEventListener('click', refreshAudio);

  const appRows = $('#app-rows');

  appRows.addEventListener('input', (event) => {
    const slider = event.target.closest('input[type=range][data-action="volume"]');
    if (!slider) return;
    const row = slider.closest('.app-row');
    const level = Number(slider.value);
    row.querySelector('.app-percent').textContent = `${level}%`;
    paintRange(slider);
    state.draggingUntil = Date.now() + 1200;
    queueAppVolume(row.dataset.app, level);
  });

  appRows.addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-action]');
    if (!button) return;
    const row = button.closest('.app-row');
    const appName = row.dataset.app;
    const app = state.apps.find((entry) => entry.app === appName);
    if (button.dataset.action === 'mute') {
      try {
        const data = await api('/api/app-mute', {
          method: 'POST',
          body: { app: appName, state: !(app && app.mute) },
        });
        if (app) app.mute = data.mute;
        renderApps();
      } catch (error) {
        toast(error.message, 'error');
      }
    }
    if (button.dataset.action === 'target') {
      $('#target-app').value = appName;
      $('#target-step').value = String(Math.min(10, Math.max(1, state.target.step || 2)));
      $('#target-step-value').textContent = `${$('#target-step').value}%`;
      paintRange($('#target-step'));
      await applyTarget(appName);
      switchTab('volume');
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  });

  $('#bindings-save').addEventListener('click', saveBindings);
  $('#bindings-reload').addEventListener('click', reloadBindings);

  $('#log-clear').addEventListener('click', () => {
    const log = $('#log');
    log.textContent = 'Журнал очищен. Новые события появятся здесь.';
    log.dataset.placeholder = '1';
  });

  const dropzone = $('#dropzone');
  const fileInput = $('#wallpaper-file');
  dropzone.addEventListener('click', () => fileInput.click());
  dropzone.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      fileInput.click();
    }
  });
  fileInput.addEventListener('change', () => {
    uploadWallpaper(fileInput.files && fileInput.files[0]);
    fileInput.value = '';
  });
  ['dragenter', 'dragover'].forEach((name) => {
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.add('drag');
    });
  });
  ['dragleave', 'drop'].forEach((name) => {
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.remove('drag');
    });
  });
  dropzone.addEventListener('drop', (event) => {
    const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
    uploadWallpaper(file);
  });

  $('#wallpaper-clear').addEventListener('click', clearWallpaper);
  $('#wallpaper-desktop').addEventListener('click', setDesktopWallpaper);

  $('#dim').addEventListener('input', (event) => {
    state.ui.dim = Number(event.target.value);
    $('#dim-value').textContent = `${state.ui.dim}%`;
    document.documentElement.style.setProperty('--dim', String(state.ui.dim / 100));
    paintRange(event.target);
    queueUiSave();
  });

  $('#blur').addEventListener('input', (event) => {
    state.ui.blur = Number(event.target.value);
    $('#blur-value').textContent = `${state.ui.blur} px`;
    document.documentElement.style.setProperty('--blur', `${state.ui.blur}px`);
    paintRange(event.target);
    queueUiSave();
  });

  $$('.accent').forEach((button) => {
    button.addEventListener('click', () => {
      state.ui.accent = button.dataset.accent;
      applyUi();
      queueUiSave();
    });
  });

  $('#quit').addEventListener('click', async () => {
    if (!window.confirm('Закрыть меню и остановить сервис ручки?')) return;
    try {
      await api('/api/quit', { method: 'POST', body: {} });
    } catch (error) {
      /* процесс всё равно останавливается */
    }
    document.body.innerHTML = `
      <div style="position:fixed;inset:0;display:grid;place-items:center;background:#07080d;color:#f3f4fa;font-family:Segoe UI,system-ui,sans-serif;text-align:center;padding:30px">
        <div><div style="font-size:40px;margin-bottom:12px">◉</div>
        <div style="font-size:20px;font-weight:600">Меню остановлено</div>
        <div style="color:#9aa2b6;font-size:13px;margin-top:8px">Сервис ручки выключен. Окно можно закрыть.</div></div>
      </div>`;
  });

  window.addEventListener('beforeunload', () => {
    /* ничего не делаем: сервис живёт в процессе меню */
  });
}

async function init() {
  wire();
  $$('input[type=range]').forEach(paintRange);
  try {
    const data = await api('/api/state');
    state.config = data.config && typeof data.config === 'object' ? data.config : { bindings: {} };
    state.configHash = JSON.stringify(state.config);
    state.ui = data.ui || state.ui;
    state.wallpaperUrl = data.wallpaper_url || null;
    state.service = data.service || state.service;
    applyUi();
    renderService();
    renderTarget();
    renderBindings();
    const initial = (location.hash || '').replace('#', '');
    switchTab(initial);
  } catch (error) {
    toast(`Не удалось загрузить меню: ${error.message}`, 'error');
  }
  await refreshAudio();
  await pollLog();
  setInterval(pollStatus, 2200);
  setInterval(refreshAudio, 3000);
  setInterval(pollLog, 1400);
}

init();
