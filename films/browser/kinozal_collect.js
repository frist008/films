/* films/browser/kinozal_collect.js — kinozal collector for the owner's Chrome (source priority 4; owner, 2026-10-26:
 * «и вот ещё источники после ру трекера которые должны идти https://nnmclub.to и https://kinozal.guru»).
 *
 * kinozal answers curl with a Cloudflare challenge, so the pages are read in the owner's browser: he opens
 * data/cache/kinozal/run.js (written by `python -X utf8 -m films.sources.kinozal queries`) in Notepad, copies it and pastes
 * it into the DevTools console of a kinozal.guru tab where Cloudflare is passed (logged in or not). `__kzCollect({queriesUrl})`
 * loads the job from the bridge (GET /queries/kinozal) and the loop keeps running in the tab:
 *
 *   0. browse.php                     → the site's category list (<select name="c">), posted once (type 'categories');
 *   1. listing: browse.php?c=<group>&page=N (newest uploads first) for every group of the job, from startPage, until a
 *      page whose rows are all older than `since` (the last successful sync minus 2 days — owner: «смотреть обновления
 *      только с последней даты … об успешной синхронизации»), the last page or maxPages; rows posted every 5 pages
 *      (type 'listing', `done` on the last post of a finished group);
 *   2. searches: browse.php?s=<cp1251>&g=0&c=0&v=0&d=0&w=0&t=0&f=0&page=N for every query (original and Russian names
 *      are SEPARATE queries — R151), up to searchPages pages; one post per query (type 'search');
 *   3. topics: details.php?id=… for the job's `topics`, the selected search rows and the listing rows of a collected
 *      category from yearFrom on (not `known`), newest first, up to maxTopics: title, side panel («Раздают», «Вес», «Залит»),
 *      the description blocks, the «Тех. данные» tab (get_srv_details.php?id=…&pagesd=K, as the page's showtab() links
 *      say) and the info hash (…&action=2) when the page lets it; posted 10 at a time (type 'topics');
 *   a final post (type 'final').
 *
 * The first three topic pages also carry a `probe` (which blocks / tabs were found, a short scrubbed snippet of the content
 * area) — the markup was not seen live when this was written. Pages are windows-1251. Polite: one request at a time,
 * delayMs (1500) + jitter, backoff. A Cloudflare challenge, a maintenance page or the site being down pauses the loop on the
 * SAME request (state «waiting: …», posted to /ingest/kinozal_status, a retry every waitPollMs = 2 min) and it goes on by
 * itself once the site is back; after waitHours (12) it stops. Nothing is clicked or posted to kinozal — reading only.
 * Personal keys are cut before anything is posted (scrub: passkey-like parameters, magnet trackers).
 *
 * Helpers: __kzStatus(), __kzStop(), __kzFlush() (resend unsent), __kzChunk(i). The tail of a stopped run:
 * `python -X utf8 -m films.sources.kinozal resume` writes a new run.js. Pure parsers are exported for node tests.
 */
(function (root) {
  'use strict';

  const BRIDGE = 'http://127.0.0.1:8765/ingest/kinozal';
  const PER_PAGE = 50;
  function base(cfg) {
    if (cfg && cfg.base) return cfg.base;
    try { if (root.location && /kinozal/i.test(root.location.host)) return root.location.origin; } catch (e) { /* node */ }
    return 'https://kinozal.guru';
  }

  // ---------------------------------------------------------------------------------------------- text helpers
  const ENT = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', laquo: '«', raquo: '»', ndash: '–',
                mdash: '—', hellip: '…', times: '×' };
  function unesc(s) {
    return String(s || '').replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e) => {
      if (e[0] === '#') {
        const n = e[1] === 'x' || e[1] === 'X' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
        return Number.isFinite(n) ? String.fromCodePoint(n) : m;
      }
      return Object.prototype.hasOwnProperty.call(ENT, e.toLowerCase()) ? ENT[e.toLowerCase()] : m;
    });
  }
  function text(html) { return unesc(String(html || '').replace(/<[^>]+>/g, ' ')).replace(/\s+/g, ' ').trim(); }
  function int(s) { const n = parseInt(String(s == null ? '' : s).replace(/[^\d-]/g, ''), 10); return Number.isFinite(n) ? n : null; }
  function scrub(s) {
    return String(s || '')
      .replace(/(magnet:\?xt=urn:btih:[0-9A-Za-z]+)[^"'\s<>]*/g, '$1')
      .replace(/([?&;](?:uk|passkey|pk|key|sid|hash4u|hash)=)[^&"'\s<>]+/gi, '$1REDACTED');
  }

  let CP = null;
  function enc1251(s) {
    if (!CP) {
      CP = new Map();
      const dec = new TextDecoder('windows-1251');
      for (let b = 0x80; b < 0x100; b++) { const ch = dec.decode(new Uint8Array([b])); if (ch !== '�' && !CP.has(ch)) CP.set(ch, b); }
    }
    let out = '';
    for (const ch of String(s || '')) {
      const c = ch.codePointAt(0);
      if (c < 0x80) out += /[A-Za-z0-9\-_.~]/.test(ch) ? ch : ch === ' ' ? '+' : '%' + c.toString(16).toUpperCase().padStart(2, '0');
      else if (CP.has(ch)) out += '%' + CP.get(ch).toString(16).toUpperCase();
      else out += '+';
    }
    return out;
  }

  // ---------------------------------------------------------------------------------------------- dates / sizes
  const MONTHS = [['январ', 1], ['феврал', 2], ['март', 3], ['апрел', 4], ['ма', 5], ['июн', 6], ['июл', 7], ['август', 8],
                  ['сентябр', 9], ['октябр', 10], ['ноябр', 11], ['декабр', 12]];
  function pad(n) { return String(n).padStart(2, '0'); }
  function iso(d, hh, mm) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(hh)}:${pad(mm)}:00`; }
  function parseDate(s, now) {
    s = text(s).toLowerCase();
    if (!s) return null;
    now = now || new Date();
    const hm = /(\d{1,2}):(\d{2})/.exec(s);
    const hh = hm ? +hm[1] : 0, mm = hm ? +hm[2] : 0;
    if (s.startsWith('сейчас')) return iso(now, now.getHours(), now.getMinutes());
    if (s.startsWith('сегодня')) return iso(now, hh, mm);
    if (s.startsWith('вчера')) return iso(new Date(now.getTime() - 864e5), hh, mm);
    let m = /(\d{1,2})\.(\d{1,2})\.(\d{4})/.exec(s);
    if (m) return `${m[3]}-${pad(+m[2])}-${pad(+m[1])}T${pad(hh)}:${pad(mm)}:00`;
    m = /(\d{1,2})\s+([а-яё]+)\s+(\d{4})/.exec(s);
    if (m) {
      const mo = MONTHS.slice().sort((a, b) => b[0].length - a[0].length).find(([k]) => m[2].startsWith(k));
      if (mo) return `${m[3]}-${pad(mo[1])}-${pad(+m[1])}T${pad(hh)}:${pad(mm)}:00`;
    }
    return null;
  }
  const UNITS = { 'б': 0, 'кб': 1, 'мб': 2, 'гб': 3, 'тб': 4, b: 0, kb: 1, mb: 2, gb: 3, tb: 4 };
  function parseSize(s) {
    const t = text(s);
    let m = /\(([\d\s,.]{4,})\)/.exec(t);          // «3.9 ГБ (4,192,456,351)» (live), «(4 851 231 744)»
    if (m) return int(m[1]);
    m = /([\d.,]+)\s*([КМГТ]?Б|[KMGT]?B)(?![\wа-я])/i.exec(t);
    return m ? Math.round(parseFloat(m[1].replace(',', '.')) * Math.pow(1024, UNITS[m[2].toLowerCase()])) : null;
  }
  function rowYears(title) {
    const parts = String(title || '').split(' / ').map(x => x.trim());
    for (const p of parts) {
      const m = /^((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)?\d{2}))?(?:\s*,\s*(?:19|20)\d{2})*$/.exec(p);
      if (m) {
        const ys = (p.match(/(?:19|20)\d{2}/g) || []).map(Number);
        if (m[2] && m[2].length === 2) ys.push(+(m[1].slice(0, 2) + m[2]));
        return [Math.min(...ys), Math.max(...ys)];
      }
    }
    return null;
  }

  // «Колокол надежды / 2025 / РУ / WEB-DL (1080p)»: a Russian original (kinozal keeps Russian films in its genre
  // categories) — not collected; same rule as films/parse/kinozal.py russian_original
  function ruOriginal(title) {
    return String(title || '').split(' / ').some(p => {
      const toks = p.split(',').map(x => x.trim()).filter(Boolean);
      return toks.includes('РУ') && toks.every(t => /^(РУ|СТ|УКР|ENG|АНГ)$/.test(t));
    });
  }

  // ---------------------------------------------------------------------------------------------- pages
  function parseRows(html, now) {
    const rows = [], seen = new Set();
    for (const tr of String(html || '').split(/<tr\b/i).slice(1)) {
      if (!/class=["']bt["']/.test(tr)) continue;
      const tm = /<a[^>]*href="\/?details\.php\?id=(\d+)"[^>]*>([\s\S]*?)<\/a>/i.exec(tr);
      if (!tm || seen.has(tm[1])) continue;
      seen.add(tm[1]);
      const cm = /cat\((\d+)\)|browse\.php\?c=(\d+)|\/pic\/cat\/(\d+)\./.exec(tr);
      const cells = tr.split(/<td\b/i).slice(1).map(c => c.slice(c.indexOf('>') + 1).replace(/<\/td>[\s\S]*$/i, ''));
      const sm = /class=['"]sl_s['"][^>]*>([^<]*)</.exec(tr), lm = /class=['"]sl_p['"][^>]*>([^<]*)</.exec(tr);
      rows.push({
        t: tm[1], title: text(tm[2]), cat: cm ? int(cm[1] || cm[2] || cm[3]) : null,
        size: cells[3] != null ? parseSize(cells[3]) : null,
        seeds: sm ? int(sm[1]) : (cells[4] != null ? int(text(cells[4])) : null),
        leech: lm ? int(lm[1]) : (cells[5] != null ? int(text(cells[5])) : null),
        added: cells[6] != null ? parseDate(cells[6], now) : null,
      });
    }
    return rows;
  }
  function parseTotal(html) {
    const m = /Найден[оаы]?\s*(\d[\d\s]*)\s*раздач/i.exec(text(html));
    return m ? int(m[1]) : null;
  }
  function parseSelect(html, name) {
    const m = new RegExp('<select[^>]*name=["\']' + name + '["\'][^>]*>([\\s\\S]*?)<\\/select>', 'i').exec(String(html || ''));
    if (!m) return null;
    return [...m[1].matchAll(/<option[^>]*value=["']?([^"'\s>]+)["']?[^>]*>([^<]*)/gi)].map(o => ({ id: /^\d+$/.test(o[1]) ? +o[1] : o[1], name: text(o[2]) }));
  }
  function parseCategories(html) { return (parseSelect(html, 'c') || []).filter(o => typeof o.id === 'number'); }
  function divBlock(html, start) {
    const i = html.indexOf('>', start) + 1;
    const re = /<(\/?)div\b[^>]*?(\/?)>/gi;
    re.lastIndex = i;
    let depth = 1, m;
    while ((m = re.exec(html))) {
      if (m[2]) continue;
      depth += m[1] ? -1 : 1;
      if (depth === 0) return html.slice(i, m.index);
    }
    return html.slice(i);
  }
  function parseDetails(html) {
    html = String(html || '');
    const out = { title: null, cat: null, size_bytes: null, seeders: null, leechers: null, reged: null, btih: null,
                  post_html: '', tabIds: [], side: [] };
    const h1 = /<h1\b[^>]*>([\s\S]*?)<\/h1>/i.exec(html);
    if (h1) out.title = text(h1[1]);
    const after = h1 ? html.slice(h1.index) : html;
    // the side panel (category icon, «Раздают», «Вес», «Залит») sits before or after the title: the whole page, known labels only
    const cm = /<li\b[^>]*class=["']img["'][^>]*>\s*<a[^>]*browse\.php\?c=(\d+)/i.exec(html) || /\/pic\/cat\/(\d+)\./.exec(html) ||
               /browse\.php\?c=(\d+)/.exec(after);
    if (cm) out.cat = int(cm[1]);
    // live page (2026-10-26): <li><span class="bulet"></span><a onclick="get_torm(…)">Раздают<span class="floatright">5</span></a></li>
    for (const li of html.matchAll(/<li\b[^>]*>\s*(?:<span[^>]*><\/span>)?\s*(?:<a\b[^>]*>)?\s*(Раздают|Скачивают|Вес|Размер|Залит|Обновлен|Скачали)[^<]{0,20}<span\b[^>]*>([\s\S]*?)<\/span>/gi)) {
      const k = text(li[1]), v = text(li[2]);
      out.side.push([k, v]);
      if (/^Раздают/i.test(k)) out.seeders = int(v);
      else if (/^Скачивают/i.test(k)) out.leechers = int(v);
      else if (/^(Вес|Размер)/i.test(k)) out.size_bytes = parseSize(v);
      else if (/^Залит/i.test(k)) out.reged = parseDate(v);
    }
    const blocks = [];
    const seenAt = new Set();
    for (const m of after.matchAll(/<div\b[^>]*class=["'][^"']*\b(?:bx1|justify|mn2|content)\b[^"']*["'][^>]*>/gi)) {
      const b = divBlock(after, m.index);
      if (seenAt.has(b)) continue;
      if (/Перевод|Качество|Аудио|Видео|Субтитры|Режисс|Год выпуска|Оригинальное название|О фильме|Описание|Жанр/i.test(b)) {
        if (blocks.some(x => x.includes(b))) continue;
        for (let j = blocks.length - 1; j >= 0; j--) if (b.includes(blocks[j])) blocks.splice(j, 1);
        blocks.push(b);
        seenAt.add(b);
      }
    }
    // the IMDb link sits in the side menu («Голосование»), not in the description: kept for matching (Kinopoisk never)
    const imdb = [...new Set([...html.matchAll(/https?:\/\/(?:www\.)?imdb\.com\/title\/(tt\d{7,9})/g)].map(x => x[1]))];
    out.post_html = scrub(blocks.join('\n<hr>\n')) + imdb.map(t => `\n<a href="https://www.imdb.com/title/${t}/">IMDb</a>`).join('');
    const mh = /magnet:\?xt=urn:btih:([0-9A-Fa-f]{40})/.exec(html) || /Инфо\s*хеш[^0-9A-Fa-f]{0,40}([0-9A-Fa-f]{40})/i.exec(html);
    if (mh) out.btih = mh[1].toLowerCase();
    out.tabIds = [...new Set([...html.matchAll(/showtab\(\s*\d+\s*,\s*(\d+)\s*\)/g)].map(x => +x[1]))];
    return out;
  }
  function pageProblem(html) {
    const h = String(html || '');
    // real kinozal markup (a list row, the topic side panel, the category select) — the Cloudflare challenge page
    // itself links details.php?id=… (seen 2026-10-26 10:44), so links alone do not count
    const content = /class=["']bt["']|class=["']mn1_menu["']|<select[^>]*name=["']c["']/.test(h);
    if (/cf-chl|challenge-platform|Just a moment|Один момент|cf_chl_opt/i.test(h) && !content) return 'cloudflare';
    if (!content && /техническ\S*\s+(работ|перерыв)|временно\s+недоступ|temporarily\s+unavailable|maintenance/i.test(h)) return 'maintenance';
    return null;
  }
  const NEED_RE = {
    '4k': /(^|[^\w])(2160p|4K|UHD)([^\w]|$)/i,
    hdr: /(^|[^\w])(HDR10\+?|HDR|Dolby\s*Vision|DV|DoVi|HLG)([^\w]|$)/i,
    dub: /(^|[^\wа-я])ДБ([^\wа-я]|$)/i,
    flarrow: /Flarrow/i,
  };
  function needsMatch(title, need) {
    if (!need || !need.length || need.includes('any')) return true;
    return need.some(n => NEED_RE[n] && NEED_RE[n].test(title || ''));
  }
  function select(row, q, cats) {
    if (!cats.has(row.cat)) return false;
    const y = rowYears(row.title);
    if (!y) return false;
    const lo = q.lo == null ? 0 : q.lo, hi = q.hi == null ? 9999 : q.hi;
    if (y[1] < lo - 1 || y[0] > hi + 1) return false;
    return needsMatch(row.title, q.need);
  }

  // ---------------------------------------------------------------------------------------------- browser loop
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  function lsGet(k, d) { try { const v = root.localStorage && root.localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch (e) { return d; } }
  function lsSet(k, v) { try { root.localStorage.setItem(k, JSON.stringify(v)); return true; } catch (e) { return false; } }

  function makeRunner(cfg) {
    const B = base(cfg);
    const S = root.__kz = {
      state: 'running', run: cfg.run || new Date().toISOString().slice(0, 19), started: new Date().toISOString(),
      phase: 'start', listPages: 0, qDone: 0, qTotal: (cfg.queries || []).length, topics: 0, tTotal: 0, requests: 0,
      posted: 0, errors: 0, current: null, stop: false, lastError: null, lsFull: false,
    };
    root.__kzUnsent = root.__kzUnsent && root.__kzUnsent.length ? root.__kzUnsent : lsGet('kzc_unsent', []);
    const delay = cfg.delayMs || 1500;
    const bridge = cfg.bridge || BRIDGE;
    let last = 0;

    async function pause(reason) {
      const since = S.waitingSince || (S.waitingSince = Date.now());
      if (Date.now() - since > (cfg.waitHours || 12) * 3600e3) {
        const e = new Error(reason + ' (ждали ' + Math.round((Date.now() - since) / 60000) + ' мин)'); e.fatal = true; throw e;
      }
      S.state = 'waiting: ' + reason;
      try {
        await fetch(bridge.replace(/\/ingest\/kinozal$/, '/ingest/kinozal_status'), { method: 'POST',
          headers: { 'Content-Type': 'text/plain' },
          body: JSON.stringify({ v: 1, kind: 'kinozal_status', run: S.run, state: S.state, phase: S.phase,
                                 since: new Date(since).toISOString(), at: new Date().toISOString() }) });
      } catch (e) { /* bridge down */ }
      await sleep(cfg.waitPollMs || 120000);
    }
    async function get(url, allowEmpty) {
      let err = null;
      for (let a = 0; ; a++) {
        const wait = last + delay + Math.random() * 700 - Date.now();
        if (wait > 0) await sleep(wait);
        last = Date.now();
        S.requests++;
        try {
          const r = await fetch(url, { credentials: 'include' });
          if (r.ok) {
            // pages are windows-1251, the AJAX tabs (get_srv_details.php) UTF-8 (seen 2026-10-26): the header decides
            const ct = (r.headers && r.headers.get && r.headers.get('content-type')) || '';
            const html = new TextDecoder(/utf-?8/i.test(ct) ? 'utf-8' : 'windows-1251').decode(await r.arrayBuffer());
            const p = allowEmpty ? null : pageProblem(html);
            if (!p) { if (S.waitingSince) { S.waitingSince = null; S.state = 'running'; } return html; }
            if (cfg.waitOnProblem === false) { const e = new Error(p); e.fatal = true; throw e; }
            await pause(p);
            a = -1;
            continue;
          }
          err = new Error('HTTP ' + r.status);
          if (r.status === 404) break;
          if (r.status === 403 && !allowEmpty) {           // Cloudflare answers 403 with the challenge page
            const body = new TextDecoder('windows-1251').decode(await r.arrayBuffer());
            if (pageProblem(body) === 'cloudflare') { if (cfg.waitOnProblem === false) { const e = new Error('cloudflare'); e.fatal = true; throw e; } await pause('cloudflare'); a = -1; continue; }
          }
          if (allowEmpty) break;
        } catch (e) {
          if (e.fatal) throw e;
          err = e;
        }
        if (a >= 3) {
          if (cfg.waitOnProblem === false || allowEmpty) break;
          await pause('down: ' + (err && err.message || 'fetch failed'));
          a = -1;
          continue;
        }
        await sleep(5000 * (a + 1));
      }
      throw err || new Error('fetch failed');
    }
    async function send(obj) {
      const body = JSON.stringify(Object.assign({ v: 1, kind: 'kinozal', run: S.run, at: new Date().toISOString() }, obj));
      try {
        const r = await fetch(bridge, { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body });
        if (r.ok) { S.posted++; return true; }
      } catch (e) { /* keep */ }
      root.__kzUnsent.push(body);
      if (!lsSet('kzc_unsent', root.__kzUnsent)) S.lsFull = true;
      return false;
    }
    async function flush() {
      const left = [];
      for (const body of root.__kzUnsent.splice(0)) {
        try { const r = await fetch(bridge, { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body }); if (r.ok) { S.posted++; continue; } } catch (e) { /* down */ }
        left.push(body);
      }
      root.__kzUnsent.push(...left);
      lsSet('kzc_unsent', root.__kzUnsent);
      return left.length;
    }

    async function topic(id, probe) {
      const html = await get(`${B}/details.php?id=${id}`);
      const d = parseDetails(html);
      const tp = { topic_id: String(id), title: d.title, cat: d.cat, size_bytes: d.size_bytes, seeders: d.seeders,
                   leechers: d.leechers, reged: d.reged, btih: d.btih, post_html: d.post_html, tabs: [],
                   fetched_at: new Date().toISOString() };
      const errs = [];
      // tabs: live (2026-10-26) showtab(id, 100 / 0 / 1 / 2) — 0 holds the release's «Перевод: … | студия» and
      // «Субтитры: … forced» lines, 1 the awards, 100 / 2 were empty. cfg.tabs 'needed' (fewer requests): only tab 0, and
      // only when the description lacks the video / audio lines or says subtitles without saying which are forced
      let tabIds = d.tabIds.slice(0, 5);
      if (cfg.tabs === 'needed') {
        const desc = text(d.post_html);
        const lacks = !/Видео\s*:/i.test(desc) || !/Аудио\s*:/i.test(desc) ||
                      (/Субтитры\s*:/i.test(desc) && !/forced|форсир|надпис/i.test(desc));
        tabIds = lacks ? d.tabIds.filter(k => k === 0).slice(0, 1) : [];
      }
      if (cfg.tabs !== false) {
        for (const k of tabIds) {
          const url = `${B}/get_srv_details.php?id=${id}&pagesd=${k}`;
          try {
            const t = await get(url, true);
            const tt = text(t);
            if (tt.length > 30 && !pageProblem(t)) tp.tabs.push({ k, name: /MediaInfo|Видео:|Аудио:|Format/i.test(tt) ? 'MediaInfo' : 'Тех. данные', html: scrub(t) });
            else tp.tabs.push({ k, len: tt.length });
          } catch (e) { errs.push({ t: String(id), err: 'tab_' + k + ': ' + String(e && e.message || e) }); }
        }
      }
      if (cfg.hash !== false && !tp.btih) {
        try {
          const h = await get(`${B}/get_srv_details.php?id=${id}&action=2`, true);
          const m = /([0-9A-Fa-f]{40})/.exec(h || '');
          if (m) tp.btih = m[1].toLowerCase();
        } catch (e) { /* not available for guests: fine */ }
      }
      if (!tp.post_html) errs.push({ t: String(id), err: 'no_post_body', snippet: scrub(text(html).slice(0, 600)) });
      if (probe) {
        const i = html.search(/<h1\b/i);
        tp.probe = { h1: !!d.title, side: d.side, blocks: (d.post_html.match(/<hr>/g) || []).length + (d.post_html ? 1 : 0),
                     tabIds: d.tabIds, tabs: tp.tabs.map(t => ({ k: t.k, name: t.name, len: t.html ? t.html.length : t.len })),
                     post_len: d.post_html.length, snippet: scrub(html.slice(Math.max(0, i), Math.max(0, i) + 2500)) };
      }
      return { tp, errs };
    }

    async function loop() {
      const cats = new Set((cfg.cats || []).map(Number));
      const known = new Set((cfg.known || []).map(String));
      const queued = new Map();             // topic id -> priority [group, sortKey]
      const yearFrom = cfg.yearFrom || 2024;
      let fatal = null;
      try {
        // 0. category list
        S.phase = 'categories';
        let years = null;
        const noYear = new Set();
        if (cfg.categories !== false || (cfg.listing || []).some(g => g.d != null)) {
          const h = await get(`${B}/browse.php`);
          years = parseSelect(h, 'd');
          await send({ type: 'categories', categories: parseCategories(h), years, rows_on_page: parseRows(h).length });
        }
        // 0b. the first job topics right away, with a probe (the details markup is checked before the long listing)
        if (!S.stop && (cfg.topics || []).length && cfg.probeFirst !== 0) {
          S.phase = 'probe';
          const batch = { type: 'topics', topics: [], errors: [], probe_first: true };
          for (const t of cfg.topics.slice(0, cfg.probeFirst || 3)) {
            try { const { tp, errs } = await topic(t, true); batch.topics.push(tp); batch.errors.push(...errs); S.topics++; known.add(String(t)); }
            catch (e) { batch.errors.push({ t: String(t), err: String(e && e.message || e) }); if (e.fatal) throw e; }
          }
          await send(batch);
        }
        // 1. listing — one slice per category (and release year `d` when the job gives one). kinozal shows at most 100
        //    pages (5000 rows) of a list: page 100+ repeats page 99 (seen 2026-10-26), so a repeated page ends the slice
        for (const g of cfg.listing || []) {
          if (S.stop) break;
          S.phase = 'listing c=' + g.c + (g.d != null ? ' d=' + g.d : '');
          const slice = { type: 'listing', c: g.c, d: g.d == null ? null : g.d, since: g.since };
          // the year filter <select name="d">: the option whose value or text is the year; none → the category alone, once
          let dv = g.d;
          if (g.d != null && years && years.length) {
            const o = years.find(x => String(x.id) === String(g.d)) || years.find(x => x.name === String(g.d));
            dv = o ? o.id : null;
            if (!o) {      // the site has no such year yet (e.g. next year): nothing to list
              await send(Object.assign({}, slice, { pages: [], done: false, reason: 'd_option_missing', next: 0 }));
              continue;
            }
          } else if (g.d != null && !years) {   // no year select at all: the category alone, once
            if (noYear.has(g.c)) continue;
            noYear.add(g.c);
            slice.errors = [{ err: 'no_year_select', d: g.d }];
          }
          let page = g.startPage || 0, buf = [], newest = null, done = false, reason = null, prevKey = null, next = page;
          const maxPages = g.maxPages || 100;
          const dq = dv != null ? `&d=${encodeURIComponent(dv)}` : '';
          for (let n = 0; n < maxPages && !S.stop; n++, page++) {
            next = page + 1;
            const html = await get(`${B}/browse.php?c=${g.c}${dq}&t=0&f=0&page=${page}`);
            const rows = parseRows(html);
            S.listPages++;
            if (!rows.length) {
              if (page === (g.startPage || 0)) buf.push({ page, err: 'row_parse_failed', snippet: scrub(text(html).slice(0, 800)) });
              done = true; reason = 'last page';
              break;
            }
            const key = rows.map(r => r.t).join(',');
            if (key === prevKey) { done = true; reason = 'page cap'; break; }   // the site's 100-page limit
            prevKey = key;
            buf.push({ page, rows });
            for (const r of rows) {
              if (r.added && (!newest || r.added > newest)) newest = r.added;
              const y = rowYears(r.title);
              if (cats.has(r.cat) && y && y[1] >= yearFrom && !ruOriginal(r.title) && !known.has(r.t) && !queued.has(r.t)) queued.set(r.t, [1, r.added || '']);
            }
            const dated = rows.filter(r => r.added);
            if (g.since && dated.length && dated.every(r => r.added < g.since)) { done = true; reason = 'sync point reached'; break; }
            if (buf.length >= 5) { await send(Object.assign({}, slice, { pages: buf, done: false, newest })); buf = []; }
          }
          if (!done) reason = S.stop ? 'stopped' : 'maxPages';
          await send(Object.assign({}, slice, { pages: buf, done, reason, newest, next: done ? 0 : next }));
        }
        // 2. searches
        const qs = cfg.queries || [];
        for (let i = 0; i < qs.length && !S.stop; i++) {
          const q = qs[i];
          S.phase = 'search'; S.current = q.q;
          const payload = { type: 'search', seq: i, query: q, searches: [], errors: [] };
          try {
            const s = { q: q.q, rows: [], pages: 0, total: null };
            for (let p = 0; p < (cfg.searchPages || 4) && !S.stop; p++) {
              const html = await get(`${B}/browse.php?s=${enc1251(q.q)}&g=0&c=0&v=0&d=0&w=0&t=0&f=0&page=${p}`);
              const rows = parseRows(html);
              s.pages++;
              if (p === 0) s.total = parseTotal(html);
              if (p === 0 && !rows.length && s.total) payload.errors.push({ q: q.q, err: 'row_parse_failed', snippet: scrub(text(html).slice(0, 800)) });
              s.rows.push(...rows);
              if (rows.length < PER_PAGE) break;
            }
            s.capped = s.rows.length >= (cfg.searchPages || 4) * PER_PAGE;
            for (const r of s.rows) {
              r.sel = select(r, q, cats) ? 1 : 0;
              if (r.sel && !known.has(r.t) && !queued.has(r.t)) queued.set(r.t, [2, r.added || '']);
            }
            payload.searches.push(s);
          } catch (e) {
            payload.errors.push({ q: q.q, err: String(e && e.message || e) });
            S.lastError = String(e && e.message || e);
            if (e.fatal) fatal = S.lastError;
          }
          S.errors += payload.errors.length;
          await send(payload);
          if (fatal) break;
          S.qDone = i + 1;
          lsSet('kzc_progress', { run: S.run, phase: 'search', next: i + 1 });
        }
        // 3. topics: the job's own first, then search hits, then listing rows (newest first)
        if (!fatal && !S.stop) {
          for (const t of cfg.topics || []) if (!known.has(String(t))) queued.set(String(t), [3, '']);
          const order = [...queued.entries()].sort((a, b) => b[1][0] - a[1][0] || (b[1][1] > a[1][1] ? 1 : b[1][1] < a[1][1] ? -1 : 0))
            .map(x => x[0]).slice(0, cfg.maxTopics || 1500);
          S.phase = 'topics'; S.tTotal = order.length;
          let batch = { type: 'topics', topics: [], errors: [] };
          for (let i = 0; i < order.length && !S.stop; i++) {
            S.current = order[i];
            try {
              const { tp, errs } = await topic(order[i], i < 3);
              batch.topics.push(tp); batch.errors.push(...errs); S.topics++;
            } catch (e) {
              batch.errors.push({ t: order[i], err: String(e && e.message || e) });
              if (e.fatal) { fatal = String(e.message || e); break; }
            }
            if (batch.topics.length >= 10) {
              S.errors += batch.errors.length;
              await send(Object.assign(batch, { requests: S.requests, topicsDone: S.topics }));   // requests per topic, measurable
              batch = { type: 'topics', topics: [], errors: [] };
            }
          }
          if (batch.topics.length || batch.errors.length) { S.errors += batch.errors.length; await send(batch); }
        }
      } catch (e) {
        fatal = String(e && e.message || e);
        S.lastError = fatal;
      }
      await flush();
      await send({ type: 'final', final: true, fatal, stats: Object.assign({}, S, { stop: undefined }) });
      S.state = fatal ? 'stopped: ' + fatal : S.stop ? 'stopped by __kzStop' : 'done';
      S.current = null;
    }
    return { S, loop, flush };
  }

  function start(cfg) {
    cfg = cfg || {};
    if (!cfg.queriesUrl && !Array.isArray(cfg.queries) && !Array.isArray(cfg.listing)) {
      return 'usage: __kzCollect({queriesUrl}) or __kzCollect({listing: [{c, since}], queries: [{q, lo, hi, need}], cats, known, run})';
    }
    if (root.__kz && /^(running|loading|waiting)/.test(root.__kz.state)) return 'already running: ' + JSON.stringify(status());
    const go = c => { const r = makeRunner(c); root.__kzRunner = r; r.loop().catch(e => { r.S.state = 'crashed: ' + (e && e.message || e); }); return r; };
    if (!cfg.queriesUrl) { const r = go(cfg); return `started run ${r.S.run}: ${(cfg.listing || []).length} listing groups, ${(cfg.queries || []).length} queries`; }
    root.__kz = { state: 'loading job from ' + cfg.queriesUrl };
    fetch(cfg.queriesUrl, { cache: 'no-store' })
      .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(loaded => go(Object.assign({}, loaded, cfg)))
      .catch(e => { root.__kz = { state: 'no job: ' + (e && e.message || e) + ' — is `python -X utf8 -m films bridge` running?' }; });
    return 'loading job from ' + cfg.queriesUrl + ' — check with __kzStatus()';
  }
  function status() {
    const S = root.__kz;
    if (!S) return 'not started';
    return { state: S.state, run: S.run, phase: S.phase, listPages: S.listPages, queries: `${S.qDone}/${S.qTotal}`,
             topics: `${S.topics}/${S.tTotal}`, current: S.current, requests: S.requests, posted: S.posted,
             unsent: (root.__kzUnsent || []).length, errors: S.errors, lastError: S.lastError, lsFull: S.lsFull };
  }
  function chunk(i, size) {
    const all = JSON.stringify(root.__kzUnsent && root.__kzUnsent.length ? root.__kzUnsent : lsGet('kzc_unsent', []));
    size = size || 400000;
    return { i, n: Math.ceil(all.length / size), data: all.slice(i * size, (i + 1) * size) };
  }

  const api = { ruOriginal, parseRows, parseTotal, parseCategories, parseSelect, parseDetails, parseDate, parseSize, rowYears, needsMatch, select,
                pageProblem, enc1251, scrub, unesc, divBlock };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof root.document !== 'undefined') {
    root.__kzCollect = start;
    root.__kzStatus = status;
    root.__kzStop = () => { if (root.__kz) root.__kz.stop = true; return status(); };
    root.__kzFlush = () => (root.__kzRunner ? root.__kzRunner.flush() : Promise.resolve('no runner'));
    root.__kzChunk = chunk;
    root.__kzApi = api;
  }
})(typeof window !== 'undefined' ? window : globalThis);
