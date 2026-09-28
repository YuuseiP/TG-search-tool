(() => {
  'use strict';

  const REQUEST_TIMEOUT = 7000;
  const TELEGRAM_AUTH_TIMEOUT = 45000;
  const MODEL_TIMEOUT = 20000;
  const READ_RETRIES = 2;
  const STORAGE_KEY = 'telescope.ui-state';
  const RAIL_STORAGE_KEY = 'telescope.rail-collapsed';
  const THEME_STORAGE_KEY = 'telescope.theme';
  const STORAGE_VERSION = 2;
  // Keep one append small enough that parsing rich text and inserting cards never monopolizes a frame.
  const PAGE_SIZE = 30;
  const TAG_DICTIONARY_PAGE_SIZE = 80;
  const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);
  const MEDIA_LABELS = { text: '文本', image: '图片', video: '视频', document: '文件', audio: '音频', link: '链接' };
  const VIEW_META = {
    search: { label: 'ARCHIVE', title: 'Search', subtitle: '从完整本地档案中检索、筛选并回到原消息。' },
    saved: { label: 'SAVED', title: 'Saved', subtitle: '收藏的消息与原消息链接。' },
    groups: { label: 'SEARCH GROUPS', title: 'Search Groups', subtitle: '维护固定频道范围，并将它们直接用于搜索。' },
    sources: { label: 'SOURCES / SYNC', title: 'Sources / Sync', subtitle: '扫描来源，选择同步，观察本地进度。' },
    settings: { label: 'SETTINGS', title: 'Settings', subtitle: 'Telegram、Clash 与自然语言连接。' },
  };
  const SOURCE_LABELS = { all: '全部', channel: '频道', group: '群组', synced: '已同步', unsynced: '未同步' };
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const isRecord = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
  const state = {
    activeView: 'search', channels: [], searchGroups: [], activeSearchGroupId: '', groupEditorId: '', groupEditorChannels: new Set(), dialogs: [], selectedChannels: new Set(),
    selectedDialogs: new Set(), dialogKind: 'all', mediaTypes: new Set(Object.keys(MEDIA_LABELS)), showHiddenAds: false,
    searchMode: 'full', searchSequence: 0, searchController: null, searchBusy: false,
    page: { offset: 0, total: 0 }, itemsById: new Map(),
    tags: { dictionary: [], filter: '', dictionaryLimit: TAG_DICTIONARY_PAGE_SIZE, ranked: [], effective: [], threshold: 12, initialCount: null, finalCount: null, trace: [], status: 'idle' },
    sync: { jobId: null, job: null, jobs: [], timer: null, inFlight: false, starting: false, retry: 0, recovered: false, resumeAfterRecovery: false },
    models: { local: { controller: null, sequence: 0 }, cloud: { controller: null, sequence: 0 } },
    auth: { codeSent: false }, testController: null, toastTimer: null, lastFocused: null,
  };

  class RequestError extends Error {
    constructor(message, kind = 'unknown', status = 0, cause = null) {
      super(message);
      this.name = 'RequestError'; this.kind = kind; this.status = status; this.cause = cause;
    }
  }
  const sharedReads = new Map();
  const fail = (message, kind, status = 0, cause = null) => new RequestError(message, kind, status, cause);
  const monotonicNow = () => typeof performance !== 'undefined' && typeof performance.now === 'function' ? performance.now() : Date.now();
  function reportNetworkEvent(path, options, kind, startedAt) {
    if (path === '/api/client-events') return;
    const payload = JSON.stringify({ path, method: options.method || 'GET', kind, duration_ms: Math.round(monotonicNow() - startedAt) });
    try {
      if (navigator.sendBeacon && typeof Blob !== 'undefined') navigator.sendBeacon('/api/client-events', new Blob([payload], { type: 'application/json' }));
    } catch (_) { /* Logging must never interfere with the user's request. */ }
  }
  function bodyMessage(body, fallback = '请求失败') {
    if (typeof body === 'string' && body.trim()) return body.trim();
    if (isRecord(body)) {
      if (typeof body.detail === 'string') return body.detail;
      if (isRecord(body.detail) && typeof body.detail.message === 'string') return body.detail.message;
      if (typeof body.message === 'string') return body.message;
    }
    return fallback;
  }
  const offlineNow = () => typeof navigator !== 'undefined' && navigator.onLine === false;
  function delay(ms, signal) {
    return new Promise((resolve, reject) => {
      let settled = false;
      const finish = (callback) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        signal?.removeEventListener('abort', abort);
        callback();
      };
      const timer = setTimeout(() => finish(resolve), ms);
      const abort = () => finish(() => reject(fail('请求已取消。', 'cancelled')));
      if (signal) {
        if (signal.aborted) abort();
        else signal.addEventListener('abort', abort, { once: true });
      }
    });
  }
  async function fetchOnce(path, options, timeout) {
    if (offlineNow()) throw fail('当前处于离线状态，请恢复网络后重试。', 'offline');
    const controller = new AbortController();
    const externalSignal = options.controller?.signal || options.signal;
    let timedOut = false;
    const startedAt = monotonicNow();
    let forwardAbort;
    if (externalSignal) {
      if (externalSignal.aborted) throw fail('请求已取消。', 'cancelled');
      forwardAbort = () => controller.abort();
      externalSignal.addEventListener('abort', forwardAbort, { once: true });
    }
    const timeoutId = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
    try {
      const headers = { Accept: 'application/json', ...(options.headers || {}) };
      if (options.body !== undefined && options.body !== null && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
      const response = await fetch(path, { method: options.method, headers, body: options.body, credentials: options.credentials || 'same-origin', signal: controller.signal });
      const raw = await response.text();
      let body = null;
      if (raw.trim()) {
        try { body = JSON.parse(raw); } catch (error) { throw fail('服务返回了无法读取的数据。', 'validation', response.status, error); }
      }
      if (!response.ok) throw fail(bodyMessage(body, `请求失败（${response.status}）`), 'http', response.status);
      if (typeof options.validate === 'function' && !options.validate(body)) throw fail('服务返回的数据格式不符合预期。', 'validation', response.status);
      return body;
    } catch (error) {
      if (error instanceof RequestError) throw error;
      if (externalSignal?.aborted) throw fail('请求已取消。', 'cancelled', 0, error);
      if (timedOut) { reportNetworkEvent(path, options, 'timeout', startedAt); throw fail('请求超时，请检查服务状态后重试。', 'timeout', 408, error); }
      reportNetworkEvent(path, options, offlineNow() ? 'offline' : 'network', startedAt);
      throw fail(offlineNow() ? '当前处于离线状态，请恢复网络后重试。' : '网络连接暂时不可用，请稍后重试。', offlineNow() ? 'offline' : 'network', 0, error);
    } finally {
      clearTimeout(timeoutId);
      if (externalSignal && forwardAbort) externalSignal.removeEventListener('abort', forwardAbort);
    }
  }
  function shouldRetry(error, attempt, safeRead) {
    if (!safeRead || attempt >= READ_RETRIES) return false;
    if (['cancelled', 'validation', 'offline'].includes(error.kind)) return false;
    return error.kind === 'network' || error.kind === 'timeout' || (error.kind === 'http' && error.status >= 500);
  }
  async function executeRequest(path, options, safeRead) {
    for (let attempt = 0; attempt <= (safeRead ? READ_RETRIES : 0); attempt += 1) {
      try { return await fetchOnce(path, options, options.timeout || REQUEST_TIMEOUT); }
      catch (error) { if (!shouldRetry(error, attempt, safeRead)) throw error; await delay(250 * (attempt + 1), options.controller?.signal || options.signal); }
    }
    throw fail('请求失败，请稍后重试。', 'network');
  }
  function request(path, options = {}) {
    const method = (options.method || 'GET').toUpperCase();
    const safeRead = options.safeRead ?? SAFE_METHODS.has(method);
    const key = safeRead && options.merge !== false ? `${method}:${path}:${typeof options.body === 'string' ? options.body : ''}` : null;
    const shared = key ? sharedReads.get(key) : null;
    if (shared) {
      if (!shared.signal?.aborted) return shared.promise;
      sharedReads.delete(key);
    }
    const promise = executeRequest(path, { ...options, method }, safeRead);
    if (!key) return promise;
    const entry = { promise, signal: options.controller?.signal || options.signal };
    const cleanup = () => {
      if (sharedReads.get(key) === entry) sharedReads.delete(key);
      entry.signal?.removeEventListener('abort', cleanup);
    };
    sharedReads.set(key, entry);
    if (entry.signal) {
      if (entry.signal.aborted) cleanup();
      else entry.signal.addEventListener('abort', cleanup, { once: true });
    }
    promise.then(cleanup, cleanup);
    return promise;
  }

  function escapeHtml(value = '') {
    return String(value).replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
  }
  function normalizeMessageMarkdown(value = '') {
    return String(value)
      .replace(/\u00a0/g, ' ')
      .replace(/\*{4,}/g, '**')
      .replace(/_{4,}/g, '__')
      .replace(/~{4,}/g, '~~')
      .replace(/(\*\*|__)\s+\1(?=\s*[#\w\u4e00-\u9fff])/g, '$1')
      .replace(/[ \t]+\n/g, '\n')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  }
  function renderInlineMarkdown(value = '') {
    return escapeHtml(value)
      .replace(/`([^`\n]+)`/g, '<code>$1</code>')
      .replace(/(\*\*|__)([^\n]+?)\1/g, '<strong>$2</strong>')
      .replace(/~~([^\n]+?)~~/g, '<s>$1</s>')
      .replace(/\n/g, '<br>');
  }
  function renderMessageText(value = '') {
    const source = normalizeMessageMarkdown(value);
    const links = /\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g;
    let cursor = 0;
    let match;
    let rendered = '';
    while ((match = links.exec(source))) {
      rendered += renderInlineMarkdown(source.slice(cursor, match.index));
      const href = safeUrl(match[2]);
      const label = renderInlineMarkdown(match[1]);
      rendered += href
        ? '<a class="message-inline-link" href="' + escapeAttr(href) + '" target="_blank" rel="noopener noreferrer">' + label + '</a>'
        : label;
      cursor = match.index + match[0].length;
    }
    return rendered + renderInlineMarkdown(source.slice(cursor));
  }
  const escapeAttr = (value = '') => escapeHtml(value).replace(new RegExp(String.fromCharCode(96), 'g'), '&#96;');
  function safeUrl(value) {
    if (!value) return '';
    try {
      const url = new URL(value, window.location.origin);
      return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
    } catch (_) { return ''; }
  }
  function numberOrNull(value) {
    return value === null || value === undefined || value === '' || Number.isNaN(Number(value)) ? null : Number(value);
  }
  function numberText(value, fallback = '—') {
    const number = numberOrNull(value);
    return number === null ? fallback : number.toLocaleString('zh-CN');
  }
  function formatDate(value) {
    if (!value) return '时间未知';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? escapeHtml(value) : escapeHtml(date.toLocaleString('zh-CN', { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }));
  }
  function tagName(value) {
    let tag = String(value ?? '').trim();
    if (!tag) return '';
    if (!tag.startsWith('#')) tag = '#' + tag;
    return tag.toLowerCase();
  }
  function normalizeTagList(value) {
    if (Array.isArray(value)) return [...new Set(value.map((item) => (isRecord(item) ? item.tag || item.name : item)).map(tagName).filter(Boolean))];
    if (typeof value === 'string') {
      try { const parsed = JSON.parse(value); if (Array.isArray(parsed)) return normalizeTagList(parsed); }
      catch (_) { return value.split(/\s+/).map(tagName).filter(Boolean); }
    }
    return [];
  }
  function normalizeDictionary(payload) {
    const raw = Array.isArray(payload) ? payload : payload?.tag_dictionary || payload?.dictionary || payload?.tags || payload?.items || [];
    if (!Array.isArray(raw)) return [];
    const counts = new Map(Object.entries(isRecord(payload?.counts) ? payload.counts : {})
      .map(([tag, count]) => [tagName(tag), numberOrNull(count)])
      .filter(([tag, count]) => tag && count !== null));
    const map = new Map();
    raw.forEach((entry) => {
      const tag = tagName(isRecord(entry) ? entry.tag || entry.name : entry);
      if (!tag) return;
      const count = counts.get(tag) ?? numberOrNull(isRecord(entry) ? entry.message_count ?? entry.count ?? entry.total : null) ?? 0;
      map.set(tag, { tag, count: Math.max(count, map.get(tag)?.count || 0) });
    });
    return [...map.values()].sort((a, b) => a.tag.localeCompare(b.tag, 'zh-CN'));
  }
  const firstDefined = (...values) => values.find((value) => value !== undefined && value !== null);

  function readStoredState() {
    try {
      const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
      if (!isRecord(parsed) || parsed.version !== STORAGE_VERSION) { localStorage.removeItem(STORAGE_KEY); return {}; }
      const views = new Set(Object.keys(VIEW_META));
      const media = Array.isArray(parsed.mediaTypes) ? parsed.mediaTypes.filter((item) => Object.hasOwn(MEDIA_LABELS, item)) : [];
      return {
        activeView: views.has(parsed.activeView) ? parsed.activeView : 'search',
        searchMode: parsed.searchMode === 'natural' ? 'natural' : 'full',
        query: typeof parsed.query === 'string' ? parsed.query.slice(0, 500) : '',
        mediaTypes: media.length ? media : Object.keys(MEDIA_LABELS),
        channelIds: Array.isArray(parsed.channelIds) ? parsed.channelIds.map(String).filter(Boolean).slice(0, 500) : [],
        activeSearchGroupId: typeof parsed.activeSearchGroupId === 'string' ? parsed.activeSearchGroupId.slice(0, 64) : '',
        fromDate: typeof parsed.fromDate === 'string' ? parsed.fromDate.slice(0, 10) : '',
        untilDate: typeof parsed.untilDate === 'string' ? parsed.untilDate.slice(0, 10) : '',
        threshold: Math.min(200, Math.max(1, Number(parsed.threshold) || 12)),
        syncJobId: typeof parsed.syncJobId === 'string' ? parsed.syncJobId.slice(0, 120) : null,
      };
    } catch (_) {
      try { localStorage.removeItem(STORAGE_KEY); } catch (__) { /* storage is optional */ }
      return {};
    }
  }
  function persistState() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        version: STORAGE_VERSION, activeView: state.activeView, searchMode: state.searchMode,
        query: $('#query')?.value.slice(0, 500) || '', mediaTypes: [...state.mediaTypes],
        channelIds: [...state.selectedChannels], activeSearchGroupId: state.activeSearchGroupId, fromDate: $('#from-date')?.value || '',
        untilDate: $('#until-date')?.value || '', sortOrder: $('#sort-order')?.value || 'desc', threshold: state.tags.threshold, syncJobId: state.sync.jobId,
      }));
    } catch (_) { /* storage is optional */ }
  }
  function restoreUi() {
    const stored = readStoredState();
    state.activeView = stored.activeView || 'search'; state.searchMode = stored.searchMode || 'full';
    state.selectedChannels = new Set(stored.channelIds || []); state.activeSearchGroupId = stored.activeSearchGroupId || ''; state.mediaTypes = new Set(stored.mediaTypes || Object.keys(MEDIA_LABELS));
    state.tags.threshold = stored.threshold || 12; state.sync.jobId = stored.syncJobId || null;
    $('#query').value = stored.query || ''; $('#from-date').value = stored.fromDate || ''; $('#until-date').value = stored.untilDate || ''; $('#sort-order').value = stored.sortOrder === 'asc' ? 'asc' : 'desc';
    $('#minimum-results').value = String(state.tags.threshold);
    $$('input[name="search-mode"]').forEach((input) => { input.checked = input.value === state.searchMode; });
    $$('#media-types .filter-chip').forEach((chip) => { const selected = state.mediaTypes.has(chip.dataset.type); chip.classList.toggle('selected', selected); chip.setAttribute('aria-pressed', String(selected)); });
  }
  function setGlobalStatus(message, tone = 'neutral') {
    $('#global-status-text').textContent = message; $('#global-status').dataset.tone = tone;
  }
  function showToast(message, tone = 'neutral') {
    const node = $('#toast'); node.textContent = message; node.dataset.tone = tone; node.classList.remove('hidden');
    clearTimeout(state.toastTimer); state.toastTimer = setTimeout(() => node.classList.add('hidden'), 3600);
  }
  function markSearchDirty(message = '筛选已变更 · 提交搜索') {
    $('#search-state').textContent = message; $('#search-state').dataset.tone = 'attention'; persistState();
  }
  function setSearchBusy(busy, mode = state.searchMode) {
    state.searchBusy = busy; $('#results').setAttribute('aria-busy', String(busy)); $('#search-button').disabled = busy;
    $('#cancel-search').classList.toggle('hidden', !busy);
    $('#search-button span').textContent = busy ? (mode === 'natural' ? '理解并搜索…' : '搜索中…') : '提交搜索';
    if (busy) $('#search-state').textContent = mode === 'natural' ? '自然语言请求进行中' : '全文请求进行中';
  }

  function searchPayload(offset = 0, natural = false) {
    const payload = {
      query: $('#query').value.trim(),
      channel_ids: [...state.selectedChannels].map(Number).filter((value) => Number.isFinite(value)),
      media_types: [...state.mediaTypes],
      from_date: $('#from-date').value || null,
      until_date: $('#until-date').value || null,
      sort_order: $('#sort-order').value,
      include_hidden_ads: state.showHiddenAds,
      limit: PAGE_SIZE,
      offset,
    };
    if (natural) {
      payload.use_ai = true;
      payload.selected_tags = [...state.tags.effective];
      payload.minimum_results = state.tags.threshold;
    }
    return payload;
  }
  let mediaObserver = null;
  function loadDeferredMedia(image) {
    const src = image.dataset.mediaSrc;
    if (!src) return;
    delete image.dataset.mediaSrc;
    image.src = src;
  }
  function currentTheme() {
    return document.documentElement?.dataset?.theme === 'dark' ? 'dark' : 'light';
  }
  function applyTheme(theme, persist = true) {
    const next = theme === 'dark' ? 'dark' : 'light';
    if (document.documentElement?.dataset) document.documentElement.dataset.theme = next;
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', next === 'dark' ? '#121a1d' : '#172225');
    const button = $('#theme-toggle');
    const dark = next === 'dark';
    button.setAttribute('aria-pressed', String(dark));
    button.setAttribute('aria-label', dark ? '切换亮色主题' : '切换暗色主题');
    button.title = dark ? '切换亮色主题' : '切换暗色主题';
    $('#theme-toggle-icon').setAttribute('href', dark ? '#icon-sun' : '#icon-moon');
    if (persist) {
      try { localStorage.setItem(THEME_STORAGE_KEY, next); } catch (_) { /* storage is optional */ }
    }
  }
  function getMediaObserver() {
    if (mediaObserver || !('IntersectionObserver' in window)) return mediaObserver;
    mediaObserver = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        mediaObserver.unobserve(entry.target);
        loadDeferredMedia(entry.target);
      });
    }, { rootMargin: '420px 0px' });
    return mediaObserver;
  }
  function releaseDeferredMedia(target) {
    if (!mediaObserver) return;
    $$('img[data-media-src]', target).forEach((image) => mediaObserver.unobserve(image));
  }
  function prepareDeferredMedia(target) {
    const observer = getMediaObserver();
    $$('img[data-media-src]', target).forEach((image) => {
      image.addEventListener('error', () => image.closest('.message-preview')?.remove(), { once: true });
      if (observer) observer.observe(image);
      else loadDeferredMedia(image);
    });
  }
  function messageCard(item, variant = 'result') {
    const id = String(item.id ?? '');
    const tags = normalizeTagList(item.tags).map((tag) => '<span class="tag-chip readonly">' + escapeHtml(tag) + '</span>').join('');
    const reasons = Array.isArray(item.search_reasons) ? item.search_reasons.filter(Boolean).map((reason) => '<span class="match-note">' + escapeHtml(reason) + '</span>').join('') : '';
    const link = safeUrl(item.permalink);
    const text = renderMessageText(item.text || item.album_text || '无文字说明');
    const channel = item.channel_title || item.channel_name || '未命名来源';
    const media = MEDIA_LABELS[item.media_type] || item.media_type || '消息';
    const bookmark = Boolean(item.bookmarked);
    const detailAction = variant === 'detail' ? '' : '<button class="text-button" type="button" data-action="detail">查看详情</button>';
    const preview = item.media_type === 'image' && id ? '<div class="message-preview"><img loading="lazy" decoding="async" fetchpriority="low" data-media-src="/api/messages/' + encodeURIComponent(id) + '/preview" alt="Telegram 图片预览"></div>' : '';
    const videoPreview = item.media_type === 'video' && id && link ? '<a class="message-preview video-preview" href="' + escapeAttr(link) + '" target="_blank" rel="noopener noreferrer" title="在 Telegram 中播放视频"><img loading="lazy" decoding="async" fetchpriority="low" data-media-src="/api/messages/' + encodeURIComponent(id) + '/thumbnail" alt="Telegram 视频缩略图"><span class="video-play">播放视频</span></a>' : '';
    return '<article class="message-card" data-message-id="' + escapeAttr(id) + '"><div class="message-card-top"><div class="message-source"><strong>' + escapeHtml(channel) + '</strong><span class="media-label">' + escapeHtml(media) + '</span></div><time>' + formatDate(item.sent_at) + '</time></div>' + preview + videoPreview + '<p class="message-body">' + text + '</p><div class="message-card-bottom"><div class="tag-row">' + (tags || '<span class="muted-text">无本地标签</span>') + reasons + '</div><div class="message-actions">' + (link ? '<a class="action-link" href="' + escapeAttr(link) + '" target="_blank" rel="noopener noreferrer" title="在 Telegram 中打开"><svg class="icon" aria-hidden="true"><use href="#icon-external"></use></svg><span>原消息</span></a>' : '') + '<button class="icon-button bookmark-button ' + (bookmark ? 'is-saved' : '') + '" type="button" data-action="bookmark" aria-label="' + (bookmark ? '取消收藏' : '收藏消息') + '" title="' + (bookmark ? '取消收藏' : '收藏消息') + '" aria-pressed="' + bookmark + '"><svg class="icon" aria-hidden="true"><use href="#icon-bookmark"></use></svg></button>' + detailAction + '</div></div></article>';
  }
  function renderMessages(target, items, append = false) {
    const list = Array.isArray(items) ? items : [];
    list.forEach((item) => state.itemsById.set(String(item.id), item));
    if (append && !list.length) return;
    const content = list.length ? list.map((item) => messageCard(item)).join('') : '<div class="empty-state"><strong>没有符合当前条件的消息</strong><span>调整关键词或筛选后重新提交。</span></div>';
    if (append) {
      const firstNewIndex = target.children.length;
      const marker = document.createElement('div');
      marker.className = 'render-marker'; marker.hidden = true;
      target.append(marker);
      marker.insertAdjacentHTML('beforebegin', content);
      const inserted = [...target.children].slice(firstNewIndex, -1);
      marker.remove();
      inserted.forEach((element) => prepareDeferredMedia(element));
    } else {
      releaseDeferredMedia(target);
      target.innerHTML = content;
      prepareDeferredMedia(target);
    }
  }
  function updateBookmarkButtons(id, bookmarked) {
    $$('.bookmark-button').forEach((button) => {
      if (button.closest('[data-message-id]')?.dataset.messageId !== String(id)) return;
      button.classList.toggle('is-saved', bookmarked);
      button.setAttribute('aria-pressed', String(bookmarked));
      button.setAttribute('aria-label', bookmarked ? '取消收藏' : '收藏消息');
      button.title = bookmarked ? '取消收藏' : '收藏消息';
    });
  }
  async function toggleBookmark(id) {
    const item = state.itemsById.get(String(id));
    if (!item) return;
    const previous = Boolean(item.bookmarked);
    item.bookmarked = !previous;
    updateBookmarkButtons(id, item.bookmarked);
    try {
      const result = await request('/api/messages/' + encodeURIComponent(id) + '/bookmark', { method: 'POST', body: JSON.stringify({ bookmarked: item.bookmarked }) });
      item.bookmarked = Boolean(result?.bookmarked ?? item.bookmarked);
      updateBookmarkButtons(id, item.bookmarked);
      if (state.activeView === 'saved' && !item.bookmarked) await loadSaved();
    } catch (error) {
      item.bookmarked = previous; updateBookmarkButtons(id, previous);
      showToast(error.message, error.kind === 'offline' ? 'offline' : 'error');
    }
  }
  function bindResultActions(target) {
    target.addEventListener('click', (event) => {
      const action = event.target.closest('[data-action]');
      const card = action?.closest('[data-message-id]');
      if (!action || !card || !target.contains(action)) return;
      if (action.dataset.action === 'bookmark') void toggleBookmark(card.dataset.messageId);
      if (action.dataset.action === 'detail') openDetail(card.dataset.messageId);
    });
  }
  function openDetail(id) {
    const item = state.itemsById.get(String(id));
    if (!item) { showToast('详情暂未载入，请先重新提交搜索。', 'error'); return; }
    state.lastFocused = document.activeElement;
    const link = safeUrl(item.permalink);
    $('#message-detail').innerHTML = messageCard(item, 'detail') + '<div class="detail-note">' + (link ? '原消息链接保持可用。' : '这条消息没有可用的 Telegram 跳转链接。') + '</div>';
    const dialog = $('#message-dialog');
    if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
    $('#close-dialog').focus();
  }
  function closeDetail() {
    const dialog = $('#message-dialog');
    if (dialog.open && typeof dialog.close === 'function') dialog.close(); else dialog.removeAttribute('open');
    state.lastFocused?.focus?.();
  }

  function normalizeTrace(raw, ranked, effective, initial, final) {
    const source = Array.isArray(raw) ? raw : (Array.isArray(raw?.steps) ? raw.steps : []);
    const trace = source.map((entry, index) => {
      if (typeof entry === 'string') return { label: '步骤 ' + (index + 1), removed: '', tags: normalizeTagList(entry), count: null };
      return {
        label: entry?.label || entry?.step_label || '步骤 ' + (index + 1),
        removed: tagName(firstDefined(entry?.removed_tag, entry?.removed, entry?.dropped_tag, entry?.relaxed_tag)),
        tags: normalizeTagList(firstDefined(entry?.tags, entry?.effective_tags, entry?.remaining_tags, entry?.selected_tags)),
        count: numberOrNull(firstDefined(entry?.count, entry?.result_count, entry?.total, entry?.final_count)),
      };
    });
    if (trace.length) return trace;
    if (!ranked.length && !effective.length && initial === null && final === null) return [];
    const generated = [{ label: '初始', removed: '', tags: ranked, count: initial }];
    if (effective.join('|') !== ranked.join('|') || final !== initial) generated.push({ label: '最终', removed: '', tags: effective, count: final });
    return generated;
  }
  function renderTagChips(target, tags, editable = false, counts = new Map()) {
    if (!tags.length) { target.innerHTML = '<span class="muted-text">尚未产生标签</span>'; return; }
    target.innerHTML = tags.map((tag) => editable
      ? '<button class="tag-chip editable-chip" type="button" data-tag-action="remove" data-tag="' + escapeAttr(tag) + '" aria-label="移除 ' + escapeAttr(tag) + '" title="移除 ' + escapeAttr(tag) + '"><span>' + escapeHtml(tag) + '</span><b aria-hidden="true">×</b></button>'
      : '<button class="tag-chip" type="button" data-tag-action="add" data-tag="' + escapeAttr(tag) + '" title="加入有效标签"><span>' + escapeHtml(tag) + '</span>' + (counts.has(tag) ? '<small>' + numberText(counts.get(tag), '0') + '</small>' : '') + '</button>').join('');
  }
  function renderTagDictionary() {
    const target = $('#tag-dictionary');
    const needle = state.tags.filter.toLowerCase();
    const dictionary = state.tags.dictionary.filter((entry) => !needle || entry.tag.includes(needle));
    const visible = dictionary.slice(0, state.tags.dictionaryLimit);
    $('#tag-dictionary-count').textContent = String(state.tags.dictionary.length);
    if (!dictionary.length) { target.innerHTML = '<div class="empty-mini">没有匹配的本地标签。</div>'; return; }
    const active = new Set(state.tags.effective);
    target.innerHTML = visible.map((entry) => '<button class="dictionary-row ' + (active.has(entry.tag) ? 'is-active' : '') + '" type="button" data-tag-action="add" data-tag="' + escapeAttr(entry.tag) + '" aria-pressed="' + active.has(entry.tag) + '"><span>' + escapeHtml(entry.tag) + '</span><small>' + numberText(entry.count, '0') + '</small></button>').join('')
      + (visible.length < dictionary.length ? '<button class="dictionary-more" type="button" data-tag-dictionary-more="true">显示更多标签（' + numberText(dictionary.length - visible.length, '0') + '）</button>' : '');
  }
  function renderTagAnalysis() {
    $('#ranked-count').textContent = String(state.tags.ranked.length);
    $('#effective-count').textContent = String(state.tags.effective.length);
    $('#minimum-results').value = String(state.tags.threshold);
    $('#initial-count').textContent = numberText(state.tags.initialCount);
    $('#final-count').textContent = numberText(state.tags.finalCount);
    const counts = new Map(state.tags.dictionary.map((entry) => [entry.tag, entry.count]));
    renderTagChips($('#ranked-tags'), state.tags.ranked, false, counts);
    renderTagChips($('#effective-tags'), state.tags.effective, true);
    const trace = $('#tag-trace');
    trace.innerHTML = state.tags.trace.length ? state.tags.trace.map((entry, index) => '<li><span class="trace-index">' + (index + 1) + '</span><div><strong>' + escapeHtml(entry.label) + '</strong>' + (entry.removed ? '<span class="trace-removed">移除 ' + escapeHtml(entry.removed) + '</span>' : '') + '<small>' + (entry.tags.length ? escapeHtml(entry.tags.join(' ')) : '无有效标签') + (entry.count !== null && entry.count !== undefined ? ' · ' + numberText(entry.count, '0') + ' 条' : '') + '</small></div></li>').join('') : '<li class="trace-empty">提交自然语言搜索后显示放宽轨迹。</li>';
    $('#tag-analysis-state').textContent = state.tags.status === 'natural' ? '自然语言已提交' : state.tags.status === 'full' ? '普通全文' : '尚未提交';
    $('#apply-tags').disabled = !state.tags.effective.length;
    renderTagDictionary();
  }
  function updateTagAnalysis(data, natural) {
    const info = data?.interpretation || data?.tag_analysis || data?.tag_analysis_result || {};
    if (!natural) {
      Object.assign(state.tags, { ranked: [], effective: [], initialCount: null, finalCount: null, trace: [], status: 'full' });
      renderTagAnalysis(); return;
    }
    const ranked = normalizeTagList(firstDefined(info.ranked_tags, data?.ranked_tags, info.matched_tags, data?.matched_tags));
    const effective = normalizeTagList(firstDefined(info.effective_tags, data?.effective_tags, info.selected_tags, data?.selected_tags, ranked));
    const relaxation = firstDefined(data?.relaxation, info?.relaxation, data?.relaxation_trace, info?.relaxation_trace);
    const threshold = numberOrNull(firstDefined(relaxation?.threshold, info.minimum_results, info.min_results, data?.minimum_results, data?.min_results));
    const initial = numberOrNull(firstDefined(relaxation?.initial_count, info.initial_count, info.initial_results, data?.initial_count, data?.initial_results, data?.total));
    const final = numberOrNull(firstDefined(relaxation?.final_count, info.final_count, info.final_results, data?.final_count, data?.final_results, data?.total));
    state.tags.ranked = ranked; state.tags.effective = effective;
    state.tags.threshold = threshold === null ? state.tags.threshold : Math.min(200, Math.max(1, threshold));
    state.tags.initialCount = initial; state.tags.finalCount = final;
    state.tags.trace = normalizeTrace(relaxation, ranked, effective, initial, final);
    state.tags.status = 'natural'; renderTagAnalysis();
  }
  function addEffectiveTag(value) {
    const tag = tagName(value);
    if (!tag) return;
    const known = new Set([...state.tags.dictionary.map((entry) => entry.tag), ...state.tags.ranked]);
    if (!known.has(tag)) { $('#tag-dictionary-status').textContent = '只可选择本地字典中的标签。'; return; }
    if (!state.tags.effective.includes(tag)) state.tags.effective.push(tag);
    renderTagAnalysis();
  }
  function removeEffectiveTag(value) {
    const tag = tagName(value);
    state.tags.effective = state.tags.effective.filter((entry) => entry !== tag); renderTagAnalysis();
  }
  function handleTagAction(event) {
    const trigger = event.target.closest('[data-tag-action]');
    if (!trigger) return;
    if (trigger.dataset.tagAction === 'remove') removeEffectiveTag(trigger.dataset.tag);
    else addEffectiveTag(trigger.dataset.tag);
  }
  async function loadTagDictionary() {
    $('#tag-dictionary-status').textContent = '正在读取本地标签…';
    try {
      const data = await request('/api/tags', { validate: (body) => Array.isArray(body) || isRecord(body) });
      state.tags.dictionary = normalizeDictionary(data);
      $('#tag-dictionary-status').textContent = state.tags.dictionary.length ? '完整字典已载入。' : '当前档案尚无 #tag。';
      renderTagAnalysis();
    } catch (error) {
      $('#tag-dictionary-status').textContent = error.status === 404 ? '标签字典将在首次同步后出现。' : error.message;
    }
  }

  function renderChannelMenu() {
    const menu = $('#channel-menu');
    const allSelected = state.selectedChannels.size === 0;
    $('#channel-count').textContent = allSelected ? String(state.channels.length) : String(state.selectedChannels.size);
    if (!state.channels.length) { menu.innerHTML = '<div class="empty-mini">尚无已索引来源。</div>'; return; }
    const reset = '<button class="popover-reset" type="button" data-clear-channels="true">全部已索引频道 <span>' + state.channels.length + '</span></button>';
    const rows = state.channels.map((channel) => {
      const id = String(channel.id);
      const checked = allSelected || state.selectedChannels.has(id);
      return '<label class="popover-row"><input type="checkbox" value="' + escapeAttr(id) + '" ' + (checked ? 'checked' : '') + '><span>' + escapeHtml(channel.title || channel.name || id) + '</span><small>' + numberText(channel.message_count, '0') + '</small></label>';
    }).join('');
    menu.innerHTML = reset + rows;
  }
  function setRailCollapsed(collapsed) {
    document.body.classList.toggle('rail-collapsed', collapsed);
    const button = $('#rail-collapse');
    button.setAttribute('aria-expanded', String(!collapsed));
    button.setAttribute('aria-label', collapsed ? '展开导航' : '收起导航');
    button.title = collapsed ? '展开导航' : '收起导航';
    try { localStorage.setItem(RAIL_STORAGE_KEY, collapsed ? '1' : '0'); } catch (_) { /* storage is optional */ }
  }
  function renderSearchGroups() {
    const picker = $('#search-group-picker');
    const active = state.searchGroups.find((group) => group.id === state.activeSearchGroupId);
    picker.innerHTML = '<option value="">全部已索引频道</option>' + state.searchGroups.map((group) => '<option value="' + escapeAttr(group.id) + '">' + escapeHtml(group.name) + ' · ' + numberText(group.channel_ids.length, '0') + ' 个频道</option>').join('');
    picker.value = active ? active.id : '';
    renderSearchGroupWorkspace();
  }
  function renderSearchGroupWorkspace() {
    const list = $('#search-group-list'); const editorChannels = $('#group-editor-channels');
    if (!list || !editorChannels) return;
    const active = state.searchGroups.find((group) => group.id === state.groupEditorId);
    $('#search-group-count').textContent = numberText(state.searchGroups.length, '0');
    list.innerHTML = state.searchGroups.length ? state.searchGroups.map((group) => '<div class="group-list-item ' + (group.id === state.groupEditorId ? 'active' : '') + '"><button class="text-button" type="button" data-edit-search-group="' + escapeAttr(group.id) + '"><strong>' + escapeHtml(group.name) + '</strong><small>' + numberText(group.channel_ids.length, '0') + ' 个频道</small></button><button class="icon-button" type="button" data-use-search-group="' + escapeAttr(group.id) + '" aria-label="使用 ' + escapeAttr(group.name) + ' 搜索" title="用于搜索"><svg class="icon" aria-hidden="true"><use href="#icon-search"></use></svg></button></div>').join('') : '<div class="empty-mini">尚无搜索分组。</div>';
    $('#group-editor-heading').textContent = active ? '编辑分组' : '新建分组';
    $('#search-group-name').value = active ? active.name : '';
    $('#save-search-group-label').textContent = active ? '保存修改' : '创建分组';
    $('#delete-editor-group').disabled = !active;
    $('#use-group-for-search').disabled = !active;
    $('#group-editor-selection').textContent = numberText(state.groupEditorChannels.size, '0') + ' 已选';
    editorChannels.innerHTML = state.channels.length ? state.channels.map((channel) => {
      const id = String(channel.id); const selected = state.groupEditorChannels.has(id);
      return '<label class="group-channel-option"><input type="checkbox" value="' + escapeAttr(id) + '" ' + (selected ? 'checked' : '') + '><span>' + escapeHtml(channel.title || channel.name || id) + '</span><small>' + numberText(channel.message_count, '0') + ' 条</small></label>';
    }).join('') : '<div class="empty-mini">请先同步至少一个频道。</div>';
  }
  function editSearchGroup(groupId) {
    const group = state.searchGroups.find((item) => item.id === groupId);
    state.groupEditorId = group ? group.id : '';
    state.groupEditorChannels = new Set(group ? group.channel_ids.map(String) : []);
    renderSearchGroups();
  }
  function startNewSearchGroup() {
    state.groupEditorId = '';
    state.groupEditorChannels = new Set(state.activeSearchGroupId ? [] : state.selectedChannels);
    renderSearchGroups(); $('#search-group-name').focus();
  }
  async function loadSearchGroups() {
    try {
      const data = await request('/api/search-groups', { validate: Array.isArray });
      state.searchGroups = data.filter((group) => isRecord(group) && typeof group.id === 'string' && Array.isArray(group.channel_ids));
      const active = state.searchGroups.find((group) => group.id === state.activeSearchGroupId);
      if (active) state.selectedChannels = new Set(active.channel_ids.map(String));
      else state.activeSearchGroupId = '';
      if (state.groupEditorId && !state.searchGroups.some((group) => group.id === state.groupEditorId)) state.groupEditorId = '';
      if (state.groupEditorId) editSearchGroup(state.groupEditorId); else renderSearchGroups();
      renderChannelMenu(); persistState();
    } catch (error) { showToast(error.message, 'error'); }
  }
  function applySearchGroup(groupId) {
    const group = state.searchGroups.find((item) => item.id === groupId);
    state.activeSearchGroupId = group ? group.id : '';
    state.selectedChannels = new Set(group ? group.channel_ids.map(String) : []);
    renderSearchGroups(); renderChannelMenu(); markSearchDirty(group ? '已切换至搜索组“' + group.name + '”' : '已切换至全部已索引频道');
  }
  async function saveSearchGroupEditor(event) {
    event.preventDefault();
    const name = String(new FormData(event.currentTarget).get('name') || '').trim();
    const channelIds = [...state.groupEditorChannels].map(Number).filter(Number.isFinite);
    if (!name) return;
    if (!channelIds.length) { showToast('请先选择至少一个频道，再保存搜索组。', 'error'); return; }
    try {
      const editing = state.groupEditorId;
      const group = await request(editing ? '/api/search-groups/' + encodeURIComponent(editing) : '/api/search-groups', { method: editing ? 'PUT' : 'POST', body: JSON.stringify({ name, channel_ids: channelIds }), validate: isRecord });
      state.searchGroups = editing ? state.searchGroups.map((item) => item.id === group.id ? group : item) : [...state.searchGroups, group];
      state.groupEditorId = group.id; state.groupEditorChannels = new Set(group.channel_ids.map(String));
      renderSearchGroups(); markSearchDirty('已保存搜索组“' + group.name + '”');
    } catch (error) { showToast(error.message, 'error'); }
  }
  async function deleteEditorSearchGroup() {
    const group = state.searchGroups.find((item) => item.id === state.groupEditorId);
    if (!group || !window.confirm('删除搜索组“' + group.name + '”？')) return;
    try {
      await request('/api/search-groups/' + encodeURIComponent(group.id), { method: 'DELETE' });
      state.searchGroups = state.searchGroups.filter((item) => item.id !== group.id); if (state.activeSearchGroupId === group.id) state.activeSearchGroupId = ''; state.groupEditorId = ''; state.groupEditorChannels.clear();
      renderSearchGroups(); markSearchDirty('已删除搜索组“' + group.name + '”');
    } catch (error) { showToast(error.message, 'error'); }
  }
  function renderIndexedChannels() {
    const target = $('#indexed-channels');
    const completedTarget = $('#completed-channels');
    const completed = state.channels.filter((channel) => Boolean(channel.full_history_synced));
    const partial = state.channels.filter((channel) => !channel.full_history_synced);
    $('#indexed-count').textContent = partial.length.toLocaleString('zh-CN') + ' 个来源';
    $('#completed-count').textContent = completed.length.toLocaleString('zh-CN') + ' 个来源';
    if (!state.channels.length) {
      target.innerHTML = '<div class="empty-state"><strong>尚无本地档案来源</strong><span>完成同步后，来源会出现在这里。</span></div>';
      completedTarget.innerHTML = '<div class="empty-mini">完成全部历史同步的频道会自动归入这里。</div>';
      return;
    }
    const busy = syncIsActive();
    const row = (channel, complete) => '<div class="source-row indexed-row"><div><strong>' + escapeHtml(channel.title || channel.name || '未命名来源') + '</strong><span>' + numberText(channel.message_count, '0') + ' 条本地消息' + (complete ? ' · <b class="auto-update-label">自动增量更新</b>' : '') + '</span></div><span class="source-row-actions"><button class="button button-ghost button-compact" type="button" data-verify-channel="' + escapeAttr(channel.id) + '" ' + (busy ? 'disabled' : '') + '>校验补全</button><button class="icon-button danger-icon" type="button" data-delete-channel="' + escapeAttr(channel.id) + '" ' + (busy ? 'disabled' : '') + ' aria-label="删除 ' + escapeAttr(channel.title || '来源') + ' 的本地档案" title="删除本地档案"><svg class="icon" aria-hidden="true"><use href="#icon-trash"></use></svg></button></span></div>';
    target.innerHTML = partial.length ? partial.map((channel) => row(channel, false)).join('') : '<div class="empty-mini">当前所有本地来源均已完成全部历史同步。</div>';
    completedTarget.innerHTML = completed.length ? completed.map((channel) => row(channel, true)).join('') : '<div class="empty-mini">尚无完成全部历史同步的频道。</div>';
  }
  async function loadChannels() {
    try {
      const data = await request('/api/channels', { validate: (body) => Array.isArray(body) || isRecord(body) });
      state.channels = Array.isArray(data) ? data : (Array.isArray(data?.items) ? data.items : []);
      const available = new Set(state.channels.map((channel) => String(channel.id)));
      state.selectedChannels = new Set([...state.selectedChannels].filter((id) => available.has(String(id))));
      renderChannelMenu(); renderIndexedChannels(); renderSearchGroupWorkspace(); renderDialogs();
    } catch (error) {
      $('#channel-menu').innerHTML = '<div class="empty-mini">' + escapeHtml(error.message) + '</div>';
      $('#indexed-channels').innerHTML = '<div class="empty-state error-state"><strong>来源读取失败</strong><span>' + escapeHtml(error.message) + '</span></div>';
    }
  }
  function updateChannelSelection(id, checked) {
    const allIds = state.channels.map((channel) => String(channel.id));
    if (state.selectedChannels.size === 0) state.selectedChannels = new Set(allIds);
    if (checked) state.selectedChannels.add(String(id)); else state.selectedChannels.delete(String(id));
    if (state.selectedChannels.size === allIds.length) state.selectedChannels.clear();
    state.activeSearchGroupId = ''; renderSearchGroups(); renderChannelMenu(); markSearchDirty();
  }
  function renderDialogFilters() {
    $('#dialog-kind-filter').innerHTML = Object.entries(SOURCE_LABELS).map(([kind, label]) => '<button class="filter-chip ' + (state.dialogKind === kind ? 'selected' : '') + '" type="button" role="tab" data-kind="' + kind + '" aria-selected="' + (state.dialogKind === kind) + '">' + label + '</button>').join('');
  }
  function sourceKind(item) {
    const kind = String(item.kind || '').toLowerCase();
    return kind.includes('group') || kind.includes('chat') ? 'group' : 'channel';
  }
  function renderDialogs() {
    renderDialogFilters();
    const target = $('#dialogs');
    if (!state.dialogs.length) { target.innerHTML = '<div class="empty-state"><strong>尚未扫描来源</strong><span>扫描后选择需要同步的频道或群组。</span></div>'; updateSyncButton(); return; }
    const archivedById = new Map(state.channels.map((item) => [String(item.id), item]));
    const visible = state.dialogs.filter((item) => {
      const archived = archivedById.has(String(item.id));
      if (state.dialogKind === 'synced') return archived;
      if (state.dialogKind === 'unsynced') return !archived;
      return state.dialogKind === 'all' || sourceKind(item) === state.dialogKind;
    });
    if (!visible.length) { target.innerHTML = '<div class="empty-state"><strong>该分类暂无来源</strong><span>扫描来源后可在此选择同步。</span></div>'; updateSyncButton(); return; }
    target.innerHTML = visible.map((item) => {
      const id = String(item.id); const kind = sourceKind(item);
      const archived = archivedById.get(id);
      const archiveState = !archived ? ' · 未同步' : archived.full_history_synced ? ' · <b class="source-archive-label">已完成全部历史 · 自动更新</b>' : ' · <b class="source-archive-label">已同步 ' + numberText(archived.message_count, '0') + ' 条</b>';
      return '<label class="source-row selectable-row"><input type="checkbox" value="' + escapeAttr(id) + '" ' + (state.selectedDialogs.has(id) ? 'checked' : '') + '><span class="source-row-copy"><strong>' + escapeHtml(item.title || item.name || id) + '</strong><small>' + (kind === 'group' ? '群组' : '频道') + (item.username ? ' · @' + escapeHtml(item.username) : '') + archiveState + '</small></span></label>';
    }).join('');
    updateSyncButton();
  }
  function updateSyncButton() {
    const queued = syncIsActive();
    $('#sync-button').disabled = !state.selectedDialogs.size || state.sync.starting;
    $('#sync-button span').textContent = state.sync.starting ? '正在创建…' : queued ? '加入队列' : '开始同步';
    $('#incremental-sync-button').disabled = !state.channels.some((channel) => Boolean(channel.full_history_synced)) || state.sync.starting;
    $('#incremental-sync-button span').textContent = state.sync.starting ? '正在创建…' : queued ? '加入队列' : '更新已同步频道';
    renderIndexedChannels();
  }
  async function loadDialogs() {
    try {
      const data = await request('/api/dialogs', { validate: (body) => Array.isArray(body) || isRecord(body) });
      state.dialogs = Array.isArray(data) ? data : (Array.isArray(data?.items) ? data.items : []);
      state.selectedDialogs = new Set([...state.selectedDialogs].filter((id) => state.dialogs.some((item) => String(item.id) === String(id))));
      renderDialogs();
    } catch (error) {
      $('#dialogs').innerHTML = '<div class="empty-state error-state"><strong>来源读取失败</strong><span>' + escapeHtml(error.message) + '</span></div>';
    }
  }
  async function scanDialogs() {
    const button = $('#scan-button'); button.disabled = true; button.querySelector('span').textContent = '扫描中…'; $('#scan-status').textContent = '正在读取 Telegram 来源…';
    try {
      const data = await request('/api/channels/scan', { method: 'POST', body: JSON.stringify({}) });
      state.dialogs = Array.isArray(data) ? data : (Array.isArray(data?.items) ? data.items : []);
      state.selectedDialogs.clear(); renderDialogs();
      $('#scan-status').textContent = '已扫描 ' + numberText(data?.count ?? state.dialogs.length, '0') + ' 个来源，请选择后同步。';
      setGlobalStatus('来源扫描完成。', 'ok');
    } catch (error) {
      $('#scan-status').textContent = error.message; setGlobalStatus(error.message, error.kind === 'offline' ? 'offline' : 'error');
    } finally { button.disabled = false; button.querySelector('span').textContent = '扫描来源'; }
  }

  const syncIsActive = () => Boolean(state.sync.jobId);
  const ACTIVE_SYNC_STATES = new Set(['queued', 'running']);
  function syncJobIdentifier(job) {
    const value = firstDefined(job?.job_id, job?.id);
    return value === undefined || value === null || value === '' ? null : String(value).slice(0, 120);
  }
  function handleDictionaryAction(event) {
    if (event.target.closest('[data-tag-dictionary-more]')) {
      state.tags.dictionaryLimit += TAG_DICTIONARY_PAGE_SIZE;
      renderTagDictionary();
      return;
    }
    handleTagAction(event);
  }
  function findActiveSyncJob(payload) {
    const candidates = [];
    if (isRecord(payload?.active)) candidates.push(payload.active);
    if (Array.isArray(payload?.items)) candidates.push(...payload.items);
    if (Array.isArray(payload?.jobs)) candidates.push(...payload.jobs);
    return candidates.find((job) => isRecord(job) && ACTIVE_SYNC_STATES.has(job.state) && syncJobIdentifier(job)) || null;
  }
  function syncJobsFrom(payload) {
    const raw = Array.isArray(payload?.items) ? payload.items : Array.isArray(payload?.jobs) ? payload.jobs : [];
    return raw.filter((job) => isRecord(job) && syncJobIdentifier(job));
  }
  function mergeSyncJob(job) {
    const id = syncJobIdentifier(job);
    if (!id) return;
    const remaining = state.sync.jobs.filter((item) => syncJobIdentifier(item) !== id);
    state.sync.jobs = [job, ...remaining];
  }
  function renderSyncQueue() {
    const target = $('#sync-queue');
    if (!target) return;
    const jobs = state.sync.jobs;
    target.innerHTML = jobs.length ? jobs.map((job) => {
      const id = syncJobIdentifier(job);
      const label = job.state === 'running' ? '同步中' : job.state === 'queued' ? '排队' : job.state === 'completed' ? '已完成' : '失败';
      const mode = job.mode === 'verify' ? '校验补全' : job.mode === 'incremental' ? '增量更新' : '历史同步';
      const detail = Number(job.messages_processed || 0).toLocaleString('zh-CN') + ' 条 · ' + Number(job.channels_completed || 0) + ' / ' + Number(job.channels_total || 0) + ' 个来源';
      return '<div class="sync-queue-row" data-sync-job="' + escapeAttr(id) + '"><div><strong>' + escapeHtml(mode + ' · ' + label) + '</strong><small>' + escapeHtml(detail) + '</small></div><button class="icon-button danger-icon" type="button" data-delete-sync-job="' + escapeAttr(id) + '" aria-label="删除同步任务" title="删除此任务"><svg class="icon" aria-hidden="true"><use href="#icon-trash"></use></svg></button></div>';
    }).join('') : '<div class="empty-mini">当前没有同步任务。</div>';
  }
  function applyRecoveredSyncJob(job) {
    const jobId = syncJobIdentifier(job);
    if (!jobId) return false;
    clearSyncTimer();
    state.sync.jobId = jobId; state.sync.job = job; state.sync.retry = 0;
    persistState(); renderSyncProgress(job); updateSyncButton();
    return true;
  }
  function clearRecoveredSyncJob() {
    clearSyncTimer();
    state.sync.jobId = null; state.sync.job = null; state.sync.retry = 0;
    persistState(); renderSyncProgress(null); updateSyncButton();
  }
  async function recoverSync() {
    try {
      const data = await request('/api/sync', { validate: isRecord });
      state.sync.jobs = syncJobsFrom(data); renderSyncQueue();
      const active = findActiveSyncJob(data);
      if (active) applyRecoveredSyncJob(active);
      else clearRecoveredSyncJob();
    } catch (error) {
      if (!state.sync.jobId) setGlobalStatus(error.message, error.kind === 'offline' ? 'offline' : 'error');
    } finally {
      state.sync.recovered = true;
    }
  }
  function clearSyncTimer() { if (state.sync.timer) clearTimeout(state.sync.timer); state.sync.timer = null; }
  function syncDelay() {
    if (offlineNow()) return 30000;
    if (document.visibilityState === 'hidden') return 15000;
    return state.sync.retry ? Math.min(12000, 1000 * (2 ** Math.min(state.sync.retry, 3))) : 850;
  }
  function scheduleSyncPoll(delayMs = syncDelay()) {
    clearSyncTimer();
    if (!state.sync.jobId) return;
    state.sync.timer = setTimeout(() => { state.sync.timer = null; void pollSync(); }, delayMs);
  }
  function renderSyncProgress(job, transientError = '') {
    const panel = $('#sync-progress');
    if (!job && !transientError && !state.sync.jobs.length) { panel.classList.add('hidden'); return; }
    panel.classList.remove('hidden');
    const current = job?.current || null;
    const results = Array.isArray(job?.results) ? job.results : [];
    const totalChannels = Number(job?.channels_total ?? job?.total_channels ?? state.selectedDialogs.size ?? 0);
    const completedChannels = Number(job?.channels_completed ?? job?.completed_channels ?? 0);
    const processed = Number(job?.messages_processed ?? job?.processed ?? 0);
    const percent = job?.state === 'completed' ? 100 : totalChannels ? Math.min(99, Math.round(completedChannels / totalChannels * 100)) : 0;
    $('#sync-overall').value = percent;
    $('#sync-summary').textContent = percent + '% · ' + completedChannels + ' / ' + totalChannels + ' 个来源 · ' + processed.toLocaleString('zh-CN') + ' 条';
    $('#sync-current').textContent = transientError || (current ? (current.title || '当前来源') + '：' + Number(current.processed || 0).toLocaleString('zh-CN') + ' 条' : job?.error || (job?.state === 'completed' ? '同步已完成。' : '正在准备同步任务…'));
    $('#sync-results').innerHTML = results.length ? results.map((item) => {
      const itemProcessed = Number(item.processed || 0);
      const target = Number(item.target ?? item.limit ?? (current?.channel_id === item.channel_id ? current.target : 0));
      const itemDone = item.state === 'completed';
      const itemPercent = itemDone ? 100 : target ? Math.min(99, Math.round(itemProcessed / target * 100)) : 0;
      const stateText = item.state === 'failed' ? '失败' : itemDone ? '完成' : item.state === 'running' ? '同步中' : '排队';
      const integrity = item.integrity;
      const check = integrity?.verified ? ' · ID 顺序已校验（' + Number(integrity.message_count || 0).toLocaleString('zh-CN') + ' 条）' : integrity ? ' · ID 顺序校验异常' : '';
      const verification = item.verification;
      const reconciliation = verification?.verified ? ' · 远端核对：补回 ' + Number(verification.restored_count || 0).toLocaleString('zh-CN') + ' 条，保留仅本地 ' + Number(verification.local_only_count || 0).toLocaleString('zh-CN') + ' 条' : '';
      const detail = (item.error || itemProcessed.toLocaleString('zh-CN') + ' 条 · 本地 ' + Number(item.stored_total || 0).toLocaleString('zh-CN') + ' 条') + check + reconciliation;
      return '<div class="sync-row"><div><strong>' + escapeHtml(item.title || item.channel_title || '未命名来源') + '</strong><span class="sync-state">' + stateText + '</span></div><progress value="' + itemPercent + '" max="100">' + itemPercent + '%</progress><small>' + escapeHtml(detail) + '</small></div>';
    }).join('') : '<div class="empty-mini">等待来源进度…</div>';
    renderSyncQueue();
  }
  async function pollSync() {
    clearSyncTimer();
    if (!state.sync.jobId || state.sync.inFlight) return;
    if (document.visibilityState === 'hidden') { renderSyncProgress(state.sync.job, '页面隐藏，已延后检查同步状态。'); scheduleSyncPoll(15000); return; }
    state.sync.inFlight = true;
    const jobId = state.sync.jobId;
    try {
      const job = await request('/api/sync/' + encodeURIComponent(jobId), { validate: isRecord });
      if (jobId !== state.sync.jobId) return;
      state.sync.job = job; state.sync.retry = 0; mergeSyncJob(job); renderSyncProgress(job); updateSyncButton();
      if (['queued', 'running'].includes(job.state)) scheduleSyncPoll();
      else {
        state.sync.jobId = null; persistState(); updateSyncButton(); await loadChannels();
        if (job.state === 'completed') {
          setGlobalStatus('同步完成，档案已更新。', 'ok');
          showToast('同步完成，共读取 ' + Number(job.messages_processed || 0).toLocaleString('zh-CN') + ' 条消息。', 'ok');
        } else {
          setGlobalStatus(job.error || '同步结束但存在失败来源。', 'error'); showToast(job.error || '同步结束但存在失败来源。', 'error');
        }
      }
    } catch (error) {
      if (error.status === 404 && jobId === state.sync.jobId) {
        state.sync.jobId = null; state.sync.job = null; state.sync.retry = 0; persistState();
        renderSyncProgress(null); updateSyncButton();
        await recoverSync();
        if (state.sync.jobId) state.sync.resumeAfterRecovery = true;
      } else if (jobId === state.sync.jobId) {
        state.sync.retry += 1; renderSyncProgress(state.sync.job, error.message + ' · ' + (offlineNow() ? '离线退避中' : '稍后重试'));
        setGlobalStatus(error.message, error.kind === 'offline' ? 'offline' : 'error'); scheduleSyncPoll();
      }
    } finally {
      state.sync.inFlight = false;
      if (state.sync.resumeAfterRecovery) {
        state.sync.resumeAfterRecovery = false;
        void pollSync();
      }
    }
  }
  async function startSync() {
    const ids = [...state.selectedDialogs].map(Number).filter((id) => Number.isFinite(id));
    if (!ids.length || state.sync.starting) return;
    const allHistory = $('#sync-all-history').checked;
    const limit = Math.min(10000, Math.max(1, Number($('#sync-limit').value) || 2000));
    state.sync.starting = true; updateSyncButton(); setGlobalStatus('正在创建同步任务…', 'loading');
    try {
      const result = await request('/api/sync', { method: 'POST', body: JSON.stringify({ channel_ids: ids, limit_per_channel: allHistory ? null : limit }) });
      state.sync.jobId = result?.job_id || result?.id || null;
      state.sync.job = { state: result?.state || 'queued', channels_total: ids.length, channels_completed: 0, messages_processed: 0, results: [] };
      mergeSyncJob(result);
      const submitted = new Set(ids.map(String));
      state.selectedDialogs = new Set([...state.selectedDialogs].filter((id) => !submitted.has(String(id))));
      state.sync.retry = 0; persistState(); renderDialogs(); renderSyncProgress(state.sync.job); updateSyncButton();
      setGlobalStatus(result?.state === 'queued' ? '同步任务已加入队列。' : '同步任务已创建。', 'loading'); await pollSync();
    } catch (error) { setGlobalStatus(error.message, error.kind === 'offline' ? 'offline' : 'error'); showToast(error.message, error.kind === 'offline' ? 'offline' : 'error'); }
    finally { state.sync.starting = false; updateSyncButton(); }
  }
  async function deleteChannel(channelId) {
    const channel = state.channels.find((item) => String(item.id) === String(channelId));
    if (!channel || !window.confirm('删除“' + (channel.title || '该来源') + '”的本地档案与收藏？')) return;
    try {
      const result = await request('/api/channels/' + encodeURIComponent(channelId), { method: 'DELETE' });
      state.selectedChannels.delete(String(channelId)); await loadChannels();
      await runSearch({ modeOverride: 'full', explicit: false });
      showToast('已删除 ' + numberText(result?.messages_deleted, '0') + ' 条本地消息。', 'ok');
    } catch (error) { showToast(error.message, 'error'); }
  }
  async function loadSaved() {
    const target = $('#saved-results'); target.innerHTML = '<div class="loading-state">正在读取收藏…</div>';
    try {
      const data = await request('/api/search', { method: 'POST', safeRead: true, body: JSON.stringify({ bookmarked_only: true, media_types: [], limit: 200, offset: 0 }), validate: isRecord });
      const items = Array.isArray(data.items) ? data.items : [];
      items.forEach((item) => state.itemsById.set(String(item.id), item)); renderMessages(target, items);
      $('#saved-count').textContent = numberText(data.total ?? items.length, '0') + ' 条收藏';
    } catch (error) {
      target.innerHTML = '<div class="empty-state error-state"><strong>收藏读取失败</strong><span>' + escapeHtml(error.message) + '</span></div>'; $('#saved-count').textContent = '读取失败';
    }
  }

  const formValues = (form) => Object.fromEntries(new FormData(form).entries());
  function setCodeFormReady(ready, message = '') {
    const form = $('#code-form');
    form.classList.toggle('is-ready', ready);
    $$('input, button', form).forEach((element) => { element.disabled = !ready; });
    $('#code-form-state').textContent = message || (ready ? '验证码已发送' : '请先发送验证码');
  }
  async function startIncrementalSync() {
    if (state.sync.starting) return;
    state.sync.starting = true; updateSyncButton(); setGlobalStatus('正在读取已同步频道的新消息…', 'loading');
    try {
      const result = await request('/api/sync/incremental', { method: 'POST', body: JSON.stringify({}) });
      if (!result?.job_id && !result?.id) {
        setGlobalStatus('没有需要更新的已同步频道。', 'ok');
        return;
      }
      state.sync.jobId = result.job_id || result.id;
      state.sync.job = result; state.sync.retry = 0; mergeSyncJob(result); persistState(); renderSyncProgress(result); updateSyncButton();
      await pollSync();
    } catch (error) { setGlobalStatus(error.message, error.kind === 'offline' ? 'offline' : 'error'); showToast(error.message, error.kind === 'offline' ? 'offline' : 'error'); }
    finally { state.sync.starting = false; updateSyncButton(); }
  }
  async function verifyChannel(channelId) {
    const channel = state.channels.find((item) => String(item.id) === String(channelId));
    if (!channel || state.sync.starting) return;
    if (!window.confirm('校验“' + (channel.title || '该来源') + '”的完整远端历史并补全缺失消息？校验完成后会启用自动增量更新。')) return;
    state.sync.starting = true; updateSyncButton(); setGlobalStatus('正在校验并补全“' + (channel.title || '该来源') + '”…', 'loading');
    try {
      const result = await request('/api/channels/' + encodeURIComponent(channelId) + '/verify', { method: 'POST', body: JSON.stringify({}) });
      state.sync.jobId = result.job_id || result.id; state.sync.job = result; state.sync.retry = 0;
      mergeSyncJob(result); persistState(); renderSyncProgress(result); updateSyncButton(); await pollSync();
    } catch (error) {
      const message = error.status === 404 ? '本地服务尚未重启，校验补全需要重启应用后使用。' : error.message;
      setGlobalStatus(message, error.kind === 'offline' ? 'offline' : 'error'); showToast(message, error.kind === 'offline' ? 'offline' : 'error');
    }
    finally { state.sync.starting = false; updateSyncButton(); }
  }
  async function deleteSyncTask(jobId = state.sync.jobId) {
    if (!jobId || !window.confirm('删除这个同步任务？已经保存的本地消息会保留。')) return;
    const button = $('[data-delete-sync-job="' + String(jobId).replace(/"/g, '') + '"]');
    if (button) button.disabled = true;
    const previousJob = state.sync.job;
    const isCurrent = jobId === state.sync.jobId;
    if (isCurrent) { clearSyncTimer(); state.sync.jobId = null; persistState(); updateSyncButton(); }
    try {
      await request('/api/sync/' + encodeURIComponent(jobId), { method: 'DELETE' });
      state.sync.jobs = state.sync.jobs.filter((item) => syncJobIdentifier(item) !== jobId);
      if (isCurrent) clearRecoveredSyncJob();
      else renderSyncProgress(state.sync.job);
      await loadChannels();
      setGlobalStatus('同步任务已删除，本地档案已保留。', 'ok'); showToast('同步任务已删除。', 'ok');
    } catch (error) {
      if (isCurrent) { state.sync.jobId = jobId; state.sync.job = previousJob; persistState(); renderSyncProgress(previousJob); updateSyncButton(); }
      showToast(error.message, 'error'); if (button) button.disabled = false;
    }
  }
  async function refreshStatus({ restore = false } = {}) {
    try {
      if (restore) $('#auth-status').textContent = '正在自动恢复本机 Telegram 登录状态…';
      const status = await request('/api/status' + (restore ? '?restore=true' : ''), { timeout: restore ? TELEGRAM_AUTH_TIMEOUT : REQUEST_TIMEOUT, validate: isRecord });
      const authorized = Boolean(status.authorized); const configured = Boolean(status.configured); const sessionSaved = Boolean(status.session_saved); const sessionReady = authorized || sessionSaved;
      $('#connection-dot').classList.toggle('muted', !authorized); $('#auth-status-dot').classList.toggle('muted', !authorized);
        $('#connection-text').textContent = authorized ? ('已登录 ' + (status.phone || '')).trim() : sessionSaved ? '已登录（本地会话）' : configured ? '等待验证' : '未配置';
        $('#rail-status-dot').classList.toggle('muted', !(authorized || sessionSaved)); $('#rail-status-text').textContent = authorized ? 'Telegram 已连接' : sessionSaved ? 'Telegram 已登录' : configured ? '等待登录验证' : '尚未连接';
      if (authorized) { $('#code-form').classList.add('hidden'); $('#auth-status').textContent = '已复用本地登录状态。'; }
        else if (sessionReady) { $('#code-form').classList.add('hidden'); $('#auth-status').textContent = '已检测到本地登录状态，正在连接 Telegram。'; }
      else { $('#code-form').classList.remove('hidden'); setCodeFormReady(state.auth.codeSent); }
    } catch (error) {
      $('#connection-text').textContent = error.kind === 'offline' ? '离线' : '状态未知';
      $('#rail-status-text').textContent = error.kind === 'offline' ? '当前离线' : '状态读取失败';
    }
  }
  async function loadTelegramSettings() {
    try {
      const data = await request('/api/settings', { validate: isRecord }); const form = $('#settings-form');
      ['api_id', 'phone', 'proxy_type', 'proxy_host', 'proxy_port', 'proxy_username'].forEach((key) => {
        const input = form.elements.namedItem(key); if (input && data[key] !== undefined && data[key] !== '') input.value = data[key];
      });
      form.elements.namedItem('api_hash').placeholder = data.api_hash_saved ? '已保存，留空不变' : '留空保留已保存值';
      form.elements.namedItem('proxy_password').placeholder = data.proxy_password_saved ? '已保存，留空不变' : '留空保留已保存值';
    } catch (error) { $('#auth-status').textContent = error.message; }
  }
  function currentModelSettings() {
    const values = formValues($('#model-settings-form')); values.mode = values.mode || 'disabled'; return values;
  }
  function updateModelFields() {
    const mode = currentModelSettings().mode;
    $('#local-fields').classList.toggle('hidden', !['local', 'hybrid'].includes(mode));
    $('#cloud-fields').classList.toggle('hidden', !['cloud', 'hybrid'].includes(mode));
  }
  async function loadModelSettings() {
    try {
      const data = await request('/api/ai/settings', { validate: isRecord }); const form = $('#model-settings-form');
      $$('input[name="mode"]', form).forEach((input) => { input.checked = input.value === (data.mode || 'disabled'); });
      ['local_base_url', 'cloud_base_url'].forEach((key) => { const input = form.elements.namedItem(key); if (input && data[key] !== undefined) input.value = data[key] || ''; });
      ['local', 'cloud'].forEach((provider) => setModelOptions(provider, [], data[provider + '_model'] || ''));
      form.elements.namedItem('cloud_api_key').placeholder = data.cloud_api_key ? '已保存，留空不变' : '留空保留已保存密钥';
      updateModelFields(); $('#model-status').textContent = data.cloud_api_key ? '云端密钥已保存。' : '未保存云端密钥。';
    } catch (error) { $('#model-status').textContent = error.message; }
  }
  async function loadAdFilters() {
    try {
      const data = await request('/api/ad-filters', { validate: isRecord });
      $('#ad-filter-keywords').value = Array.isArray(data.keywords) ? data.keywords.join('\n') : '';
    } catch (error) { $('#ad-filter-status').textContent = error.message; }
  }
  function setModelOptions(provider, models, selected = null) {
    const select = $('#model-settings-form').elements.namedItem(provider + '_model');
    if (!select) return;
    const current = selected === null ? String(select.value || '').trim() : String(selected || '').trim();
    const values = [...new Set(models.filter(Boolean).map(String))];
    if (current && !values.includes(current)) values.unshift(current);
    select.innerHTML = '<option value="">选择模型</option>' + values.map((model) => '<option value="' + escapeAttr(model) + '">' + escapeHtml(model) + '</option>').join('');
    select.value = current || values[0] || '';
  }
  async function fetchModels(provider) {
    const record = state.models[provider]; const button = $('#get-' + provider + '-models');
    if (record.controller) {
      record.controller.abort(); record.controller = null; button.textContent = '获取模型'; $('#model-status').textContent = '模型读取已取消。'; return;
    }
    const settings = currentModelSettings(); const baseUrl = String(settings[provider + '_base_url'] || '').trim();
    if (!baseUrl) { $('#model-status').textContent = '请先填写服务地址。'; return; }
    const controller = new AbortController(); record.controller = controller; const sequence = ++record.sequence;
    button.textContent = '取消读取'; $('#model-status').textContent = '正在读取' + (provider === 'local' ? '本地' : '云端') + '模型…';
    try {
      const data = await request('/api/ai/models', { method: 'POST', body: JSON.stringify({ provider, base_url: baseUrl, api_key: provider === 'cloud' ? settings.cloud_api_key || '' : '' }), controller, timeout: MODEL_TIMEOUT, validate: (body) => isRecord(body) && Array.isArray(body.models) });
      if (sequence !== record.sequence) return;
      setModelOptions(provider, data.models);
      const modelInput = $('#model-settings-form').elements.namedItem(provider + '_model');
      if (modelInput && !String(modelInput.value || '').trim() && data.models[0]) modelInput.value = data.models[0];
      $('#model-status').textContent = '已读取 ' + data.models.length.toLocaleString('zh-CN') + ' 个' + (provider === 'local' ? '本地' : '云端') + '模型，可直接测试当前选择。';
    } catch (error) { if (sequence === record.sequence && error.kind !== 'cancelled') $('#model-status').textContent = error.message; }
    finally { if (sequence === record.sequence) { record.controller = null; button.textContent = '获取模型'; } }
  }
  async function testModel() {
    const button = $('#test-model');
    if (state.testController) { state.testController.abort(); state.testController = null; button.textContent = '测试连接'; $('#model-status').textContent = '连接测试已取消。'; return; }
    const controller = new AbortController(); state.testController = controller; button.textContent = '取消测试'; $('#model-status').textContent = '正在测试当前填写的配置…';
    try {
      const settings = currentModelSettings(); const provider = settings.mode === 'local' ? 'local' : 'cloud';
      const result = await request('/api/ai/test', { method: 'POST', controller, timeout: MODEL_TIMEOUT, body: JSON.stringify({ provider, base_url: settings[provider + '_base_url'] || '', model: settings[provider + '_model'] || '', api_key: provider === 'cloud' ? settings.cloud_api_key || '' : '' }), validate: isRecord });
      $('#model-status').textContent = (result.message || '连接正常') + ' · ' + (result.provider || '') + ' / ' + (result.model || '');
    } catch (error) { if (error.kind !== 'cancelled') $('#model-status').textContent = error.message; }
    finally { if (state.testController === controller) state.testController = null; button.textContent = '测试连接'; }
  }

  async function saveTelegramSettings(event) {
    event.preventDefault();
    const form = event.currentTarget; const submit = $('.form-submit', form);
    submit.disabled = true; $('#auth-status').textContent = '正在保存连接设置…';
    const values = formValues(form); values.api_id = Number(values.api_id); values.proxy_port = Number(values.proxy_port);
    try {
      const result = await request('/api/settings', { method: 'POST', body: JSON.stringify(values), validate: isRecord });
      const auth = await request('/api/auth/send-code', { method: 'POST', body: JSON.stringify({}), timeout: TELEGRAM_AUTH_TIMEOUT, validate: isRecord });
      if (auth.authorized || result.authorized) { state.auth.codeSent = false; $('#code-form').classList.add('hidden'); $('#auth-status').textContent = '已复用本地登录状态。'; }
      else { state.auth.codeSent = true; $('#code-form').classList.remove('hidden'); setCodeFormReady(true); $('#auth-status').textContent = '验证码已发送，请完成登录。'; $('#code-form input[name="code"]').focus(); }
      await refreshStatus();
    } catch (error) { state.auth.codeSent = false; $('#code-form').classList.remove('hidden'); setCodeFormReady(false, '验证码尚未确认发送'); $('#auth-status').textContent = error.kind === 'timeout' ? '发送验证码超过 45 秒，请确认 Clash 代理可用后重试。' : error.message; }
    finally { submit.disabled = false; }
  }
  async function verifyTelegram(event) {
    event.preventDefault();
    const form = event.currentTarget; const submit = $('.form-submit', form);
    submit.disabled = true; $('#auth-status').textContent = '正在验证…';
    try {
      await request('/api/auth/verify', { method: 'POST', body: JSON.stringify(formValues(form)), timeout: TELEGRAM_AUTH_TIMEOUT, validate: isRecord });
      state.auth.codeSent = false; form.classList.add('hidden'); $('#auth-status').textContent = '登录成功，状态已保存在本机。'; await refreshStatus();
    } catch (error) { $('#auth-status').textContent = error.message; }
    finally { submit.disabled = false; }
  }
  async function saveModelSettings(event) {
    event.preventDefault();
    const submit = $('button[type="submit"]', event.currentTarget);
    submit.disabled = true; $('#model-status').textContent = '正在保存模型设置…';
    try {
      const data = await request('/api/ai/settings', { method: 'PUT', body: JSON.stringify(currentModelSettings()), validate: isRecord });
      $('#model-status').textContent = data.cloud_api_key ? '模型设置已保存，云端密钥保持本机。' : '模型设置已保存。';
    } catch (error) { $('#model-status').textContent = error.message; }
    finally { submit.disabled = false; }
  }
  async function saveAdFilters(event) {
    event.preventDefault(); const button = $('button[type="submit"]', event.currentTarget);
    button.disabled = true; $('#ad-filter-status').textContent = '正在保存广告规则…';
    try {
      const keywords = $('#ad-filter-keywords').value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
      const data = await request('/api/ad-filters', { method: 'PUT', body: JSON.stringify({ keywords }), validate: isRecord });
      $('#ad-filter-keywords').value = (data.keywords || []).join('\n'); $('#ad-filter-status').textContent = '广告规则已保存。';
      void runSearch({ reset: true, explicit: false });
    } catch (error) { $('#ad-filter-status').textContent = error.message; }
    finally { button.disabled = false; }
  }
  async function runSearch({ reset = true, modeOverride = null, explicit = true } = {}) {
    const query = $('#query').value.trim(); const mode = modeOverride || state.searchMode;
    const hasEffectiveTags = state.tags.effective.length > 0;
    const natural = mode === 'natural' && explicit && (Boolean(query) || hasEffectiveTags);
    if (mode === 'natural' && !query && !hasEffectiveTags && explicit) { $('#search-state').textContent = '自然语言搜索需要输入内容或有效标签。'; $('#query').focus(); return; }
    if (reset) state.page.offset = 0;
    state.searchController?.abort();
    const sequence = ++state.searchSequence; const controller = new AbortController(); state.searchController = controller;
    setSearchBusy(true, natural ? 'natural' : 'full');
    if (reset) $('#results').innerHTML = '<div class="loading-state">正在读取本地档案…</div>';
    try {
      const path = natural ? '/api/ai/search' : '/api/search';
      const data = await request(path, { method: 'POST', safeRead: !natural, body: JSON.stringify(searchPayload(state.page.offset, natural)), controller, validate: (body) => isRecord(body) && Array.isArray(body.items) });
      if (sequence !== state.searchSequence) return;
      const items = data.items; const previousOffset = state.page.offset;
      renderMessages($('#results'), items, !reset);
      state.page.total = numberOrNull(data.total) ?? items.length; state.page.offset = previousOffset + items.length;
      $('#result-count').textContent = numberText(state.page.total, '0') + ' 条消息';
      const hiddenAds = Number(data.hidden_ad_count || 0);
      $('#result-context').textContent = (natural ? ' · 自然语言标签' : ' · 普通全文') + (!state.showHiddenAds && hiddenAds ? ' · 已隐藏 ' + hiddenAds + ' 条广告/重复' : '');
      $('#result-page').textContent = state.page.total > state.page.offset ? '已显示 ' + state.page.offset : '已显示全部';
      $('#load-more').classList.toggle('hidden', state.page.offset >= state.page.total || !items.length);
      updateTagAnalysis(data, natural); setGlobalStatus(natural ? '自然语言搜索完成。' : '全文搜索完成。', 'ok');
      $('#search-state').textContent = '结果已更新'; $('#search-state').dataset.tone = 'ok';
    } catch (error) {
      if (sequence !== state.searchSequence || error.kind === 'cancelled') return;
      if (reset) $('#results').innerHTML = '<div class="empty-state error-state"><strong>搜索失败</strong><span>' + escapeHtml(error.message) + '</span></div>';
      $('#search-state').textContent = error.message; $('#search-state').dataset.tone = error.kind === 'offline' ? 'offline' : 'error';
      setGlobalStatus(error.message, error.kind === 'offline' ? 'offline' : 'error');
    } finally {
      if (sequence === state.searchSequence) { state.searchController = null; setSearchBusy(false, natural ? 'natural' : 'full'); }
    }
  }
  function cancelSearch() {
    if (!state.searchBusy) return;
    state.searchController?.abort(); state.searchSequence += 1; state.searchController = null; setSearchBusy(false);
    $('#search-state').textContent = '搜索已取消'; $('#search-state').dataset.tone = 'attention';
  }
  function switchView(view) {
    if (!VIEW_META[view]) return;
    state.activeView = view; const meta = VIEW_META[view];
    $('#section-label').textContent = meta.label; $('#page-title').textContent = meta.title; $('#page-subtitle').textContent = meta.subtitle;
    $$('.view').forEach((section) => section.classList.toggle('active', section.id === view + '-view'));
    $$('.nav-item').forEach((button) => { const active = button.dataset.view === view; button.classList.toggle('active', active); if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current'); });
    document.body.classList.remove('rail-open'); persistState();
    if (view === 'saved') void loadSaved();
    if (view === 'groups') { void loadChannels(); void loadSearchGroups(); }
    if (view === 'sources') { void loadDialogs(); void loadChannels(); if (state.sync.recovered && state.sync.jobId) void pollSync(); }
  }

  function bindEvents() {
    $$('.nav-item').forEach((button) => button.addEventListener('click', () => switchView(button.dataset.view)));
    $('#rail-open').addEventListener('click', () => document.body.classList.add('rail-open'));
    $('#rail-close').addEventListener('click', () => document.body.classList.remove('rail-open'));
    $('#rail-collapse').addEventListener('click', () => setRailCollapsed(!document.body.classList.contains('rail-collapsed')));
    $('#theme-toggle').addEventListener('click', () => applyTheme(currentTheme() === 'dark' ? 'light' : 'dark'));
    $('#search-form').addEventListener('submit', (event) => { event.preventDefault(); persistState(); void runSearch({ reset: true, explicit: true }); });
    $('#cancel-search').addEventListener('click', cancelSearch);
    $('#query').addEventListener('input', () => {
      // Typing must never rebuild the tag console. It can contain thousands of
      // rows, so redraw it only when the dictionary or selected tags change.
      state.tags.status = 'idle';
      $('#tag-analysis-state').textContent = '输入已变更';
      $('#apply-tags').disabled = !state.tags.effective.length;
      markSearchDirty('输入已变更 · 提交搜索');
    });
    $('#query').addEventListener('keydown', (event) => { if (event.key === 'Escape') cancelSearch(); });
    $$('input[name="search-mode"]').forEach((input) => input.addEventListener('change', () => { state.searchMode = input.value; markSearchDirty((input.value === 'natural' ? '自然语言' : '普通全文') + ' · 提交搜索'); }));
    $$('#media-types .filter-chip').forEach((chip) => chip.addEventListener('click', () => {
      const selected = !chip.classList.contains('selected'); chip.classList.toggle('selected', selected); chip.setAttribute('aria-pressed', String(selected));
      if (selected) state.mediaTypes.add(chip.dataset.type); else state.mediaTypes.delete(chip.dataset.type); markSearchDirty();
    }));
    $('#from-date').addEventListener('change', () => markSearchDirty());
    $('#until-date').addEventListener('change', () => markSearchDirty());
    $('#sort-order').addEventListener('change', () => { persistState(); void runSearch({ reset: true, explicit: false }); });
    $('#minimum-results').addEventListener('input', () => { state.tags.threshold = Math.min(200, Math.max(1, Number($('#minimum-results').value) || 1)); persistState(); });
    $('#tag-filter').addEventListener('input', () => { state.tags.filter = $('#tag-filter').value.trim().toLowerCase(); state.tags.dictionaryLimit = TAG_DICTIONARY_PAGE_SIZE; renderTagDictionary(); });
    $('#tag-dictionary').addEventListener('click', handleDictionaryAction); $('#ranked-tags').addEventListener('click', handleTagAction); $('#effective-tags').addEventListener('click', handleTagAction);
    $('#clear-effective-tags').addEventListener('click', () => { state.tags.effective = []; renderTagAnalysis(); });
    $('#apply-tags').addEventListener('click', () => {
      if (!state.tags.effective.length) return;
      state.searchMode = 'natural'; $$('input[name="search-mode"]').forEach((input) => { input.checked = input.value === 'natural'; });
      persistState(); void runSearch({ reset: true, explicit: true });
    });
    $('#clear-filters').addEventListener('click', () => {
      $('#query').value = ''; $('#from-date').value = ''; $('#until-date').value = ''; $('#sort-order').value = 'desc'; state.selectedChannels.clear(); state.mediaTypes = new Set(Object.keys(MEDIA_LABELS));
      $$('#media-types .filter-chip').forEach((chip) => { chip.classList.add('selected'); chip.setAttribute('aria-pressed', 'true'); });
      renderChannelMenu(); markSearchDirty('筛选已清除 · 提交搜索');
    });
    $('#load-more').addEventListener('click', () => { if (!state.searchBusy && state.page.offset < state.page.total) void runSearch({ reset: false, explicit: true }); });
    $('#refresh-saved').addEventListener('click', () => void loadSaved());
    $('#channel-picker').addEventListener('click', (event) => {
      event.stopPropagation(); const menu = $('#channel-menu'); const closed = menu.classList.toggle('hidden'); $('#channel-picker').setAttribute('aria-expanded', String(!closed));
    });
    $('#channel-menu').addEventListener('change', (event) => { if (event.target.matches('input[type="checkbox"]')) updateChannelSelection(event.target.value, event.target.checked); });
    $('#channel-menu').addEventListener('click', (event) => { if (event.target.closest('[data-clear-channels]')) { state.selectedChannels.clear(); state.activeSearchGroupId = ''; renderSearchGroups(); renderChannelMenu(); markSearchDirty(); } });
    $('#search-group-picker').addEventListener('change', () => applySearchGroup($('#search-group-picker').value));
    $('#new-search-group').addEventListener('click', () => startNewSearchGroup());
    $('#search-group-editor-form').addEventListener('submit', (event) => void saveSearchGroupEditor(event));
    $('#group-editor-channels').addEventListener('change', (event) => { if (!event.target.matches('input[type="checkbox"]')) return; if (event.target.checked) state.groupEditorChannels.add(String(event.target.value)); else state.groupEditorChannels.delete(String(event.target.value)); renderSearchGroupWorkspace(); });
    $('#search-group-list').addEventListener('click', (event) => { const edit = event.target.closest('[data-edit-search-group]'); if (edit) { editSearchGroup(edit.dataset.editSearchGroup); return; } const use = event.target.closest('[data-use-search-group]'); if (use) { applySearchGroup(use.dataset.useSearchGroup); switchView('search'); void runSearch({ reset: true, explicit: true }); } });
    $('#use-group-for-search').addEventListener('click', () => { if (!state.groupEditorId) return; applySearchGroup(state.groupEditorId); switchView('search'); void runSearch({ reset: true, explicit: true }); });
    $('#delete-editor-group').addEventListener('click', () => void deleteEditorSearchGroup());
    document.addEventListener('click', (event) => { if (!event.target.closest('.channel-filter')) { $('#channel-menu').classList.add('hidden'); $('#channel-picker').setAttribute('aria-expanded', 'false'); } });
    $('#dialog-kind-filter').addEventListener('click', (event) => { const button = event.target.closest('[data-kind]'); if (button) { state.dialogKind = button.dataset.kind; renderDialogs(); } });
    $('#dialogs').addEventListener('change', (event) => { if (!event.target.matches('input[type="checkbox"]')) return; const id = String(event.target.value); if (event.target.checked) state.selectedDialogs.add(id); else state.selectedDialogs.delete(id); updateSyncButton(); });
    $('#scan-button').addEventListener('click', () => void scanDialogs()); $('#sync-button').addEventListener('click', () => void startSync()); $('#incremental-sync-button').addEventListener('click', () => void startIncrementalSync()); $('#sync-queue').addEventListener('click', (event) => { const button = event.target.closest('[data-delete-sync-job]'); if (button) void deleteSyncTask(button.dataset.deleteSyncJob); });
    ['#indexed-channels', '#completed-channels'].forEach((selector) => { const target = $(selector); target.addEventListener('click', (event) => { const verify = event.target.closest('[data-verify-channel]'); if (verify) { void verifyChannel(verify.dataset.verifyChannel); return; } const button = event.target.closest('[data-delete-channel]'); if (button) void deleteChannel(button.dataset.deleteChannel); }); });
    $('#sync-all-history').addEventListener('change', () => { $('#sync-limit').disabled = $('#sync-all-history').checked; });
    $('#show-hidden-ads').addEventListener('change', () => { state.showHiddenAds = $('#show-hidden-ads').checked; void runSearch({ reset: true, explicit: false }); });
    $('#settings-form').addEventListener('submit', (event) => void saveTelegramSettings(event)); $('#code-form').addEventListener('submit', (event) => void verifyTelegram(event));
    $('#model-settings-form').addEventListener('submit', (event) => void saveModelSettings(event));
    $('#ad-filter-form').addEventListener('submit', (event) => void saveAdFilters(event));
    $$('input[name="mode"]').forEach((input) => input.addEventListener('change', updateModelFields));
    $('#get-local-models').addEventListener('click', () => void fetchModels('local')); $('#get-cloud-models').addEventListener('click', () => void fetchModels('cloud')); $('#test-model').addEventListener('click', () => void testModel());
    $('#close-dialog').addEventListener('click', closeDetail);
    $('#message-dialog').addEventListener('click', (event) => { const action = event.target.closest('[data-action="bookmark"]'); const card = action?.closest('[data-message-id]'); if (card) void toggleBookmark(card.dataset.messageId); });
    $('#message-dialog').addEventListener('cancel', (event) => { event.preventDefault(); closeDetail(); });
  }
  function renderInitialUi() {
    renderChannelMenu(); renderSearchGroups(); renderIndexedChannels(); renderDialogFilters(); renderDialogs(); renderTagAnalysis(); updateModelFields(); updateSyncButton();
  }
  function handleOnlineState(online) {
    if (online) { setGlobalStatus('网络已恢复。', 'ok'); if (state.sync.recovered && state.sync.jobId) void pollSync(); }
    else { setGlobalStatus('当前离线，读取请求将退避。', 'offline'); if (state.sync.recovered && state.sync.jobId) scheduleSyncPoll(30000); }
  }
  function boot() {
    applyTheme(currentTheme(), false);
    restoreUi();
    try { setRailCollapsed(localStorage.getItem(RAIL_STORAGE_KEY) === '1'); } catch (_) { setRailCollapsed(false); }
    renderInitialUi(); bindResultActions($('#results')); bindResultActions($('#saved-results')); bindEvents();
    const syncRecovery = recoverSync();
    switchView(state.activeView);
    window.addEventListener('online', () => { handleOnlineState(true); void refreshStatus({ restore: true }); }); window.addEventListener('offline', () => handleOnlineState(false));
    document.addEventListener('visibilitychange', () => { if (state.sync.recovered && state.sync.jobId) { if (document.visibilityState === 'visible') void pollSync(); else scheduleSyncPoll(15000); } });
    Promise.allSettled([syncRecovery, loadChannels(), loadSearchGroups(), loadTagDictionary(), refreshStatus({ restore: true }), loadTelegramSettings(), loadModelSettings(), loadAdFilters()]).then(() => {
      void runSearch({ reset: true, modeOverride: 'full', explicit: false }); if (state.sync.jobId) void pollSync();
    });
  }
  boot();
})();
