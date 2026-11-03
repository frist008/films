/* films/browser/rutracker_collect.js — rutracker collector for the owner's Chrome (R9, R44, R48; update-db step 6).
 *
 * rutracker is closed to curl (Cloudflare), and every javascript_tool call there needs the owner's «Allow» click, so the
 * WHOLE batch runs from ONE call: this file + `__rtCollect({...})` (python -X utf8 -m films.sources.rutracker queries
 * writes the ready call to data/cache/rutracker/run.js: `__rtCollect({queriesUrl})` loads the job list — queries, known
 * topic ids, run id — from the bridge's GET /queries/rutracker; run_inline_N.js carry the queries inline for a page that
 * cannot reach the bridge). The call returns at once; the loop keeps running in the tab:
 *
 *   for every query (original and Russian titles are SEPARATE queries — owner's rule R151):
 *     tracker.php?nm=<cp1251>  -> all pages up to the 500-result cap (50 per page; search_id paging when the page has it),
 *                                 capped queries are split once (by year, or by the needed feature words);
 *     rows -> {topic id, forum, size bytes, seeders, leechers, added, title};
 *     selected rows (video forum, year window ±1, title shows a needed feature: 4K / HDR-DV / Dub, not known yet)
 *     -> viewtopic.php?t=…  -> first post HTML (description + MediaInfo) + size, seeds, btih, registration date;
 *   one POST per query to the local bridge http://127.0.0.1:8765/ingest/rutracker (`python -X utf8 -m films bridge`);
 *   whatever the bridge did not take stays in window.__rtUnsent and localStorage «rtc_unsent» (read out with
 *   __rtChunk(i) — one Allow per call, so only when the bridge was down).
 *
 * Pages are windows-1251: queries are percent-encoded in cp1251, responses decoded with TextDecoder('windows-1251').
 * Polite: one request at a time, delayMs (default 1500) + jitter between requests, backoff on errors, stops on a login
 * page / Cloudflare challenge. Nothing is clicked, nothing is posted to rutracker — reading only.
 *
 * Helpers: __rtStatus() — progress; __rtStop() — stop after the current request; __rtFlush() — resend unsent;
 * __rtChunk(i) — {i, n, data} slice of the unsent payloads. Pure parsers are exported for node tests
 * (tests/test_rutracker.py runs them through node).
 */
(function (root) {
  'use strict';

  const BASE = 'https://rutracker.org/forum';
  const BRIDGE = 'http://127.0.0.1:8765/ingest/rutracker';
  const PER_PAGE = 50;
  const CAP = 500;

  // ---------------------------------------------------------------------------------------------- text helpers
  const ENT = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', laquo: '«', raquo: '»', ndash: '–',
                mdash: '—', hellip: '…', radic: '√', times: '×', darr: '↓', uarr: '↑' };
  function unesc(s) {
    return String(s || '').replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e) => {
      if (e[0] === '#') {
        const n = e[1] === 'x' || e[1] === 'X' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
        return Number.isFinite(n) ? String.fromCodePoint(n) : m;
      }
      return Object.prototype.hasOwnProperty.call(ENT, e.toLowerCase()) ? ENT[e.toLowerCase()] : m;
    });
  }
  function text(html) { return unesc(String(html || '').replace(/<[^>]+>/g, '')).replace(/\s+/g, ' ').trim(); }
  function attr(tag, name) {
    const m = new RegExp('\\b' + name + '\\s*=\\s*("([^"]*)"|\'([^\']*)\'|([^\\s>]+))', 'i').exec(tag || '');
    return m ? unesc(m[2] !== undefined ? m[2] : m[3] !== undefined ? m[3] : m[4]) : null;
  }
  function int(s) { const n = parseInt(String(s == null ? '' : s).replace(/[^\d-]/g, ''), 10); return Number.isFinite(n) ? n : null; }

  // ---------------------------------------------------------------------------------------------- cp1251 query
  let CP = null;
  function cp1251() {
    if (CP) return CP;
    CP = new Map();
    const dec = new TextDecoder('windows-1251');
    for (let b = 0x80; b < 0x100; b++) {
      const ch = dec.decode(new Uint8Array([b]));
      if (ch !== '\ufffd' && !CP.has(ch)) CP.set(ch, b);
    }
    return CP;
  }
  function enc1251(s) {
    const m = cp1251();
    let out = '';
    for (const ch of String(s || '')) {
      const c = ch.codePointAt(0);
      if (c < 0x80) out += /[A-Za-z0-9\-_.~]/.test(ch) ? ch : ch === ' ' ? '+' : '%' + c.toString(16).toUpperCase().padStart(2, '0');
      else if (m.has(ch)) out += '%' + m.get(ch).toString(16).toUpperCase();
      else out += '+';               // not in cp1251 (e.g. CJK): a word separator, rutracker cannot search it anyway
    }
    return out;
  }

  // ---------------------------------------------------------------------------------------------- search page
  function parseSearch(html) {
    html = String(html || '');
    const tm = /Результатов поиска:\s*(\d+)/.exec(html);
    const sid = /tracker\.php\?search_id=([\w-]+)/.exec(html);
    const rows = [];
    const seen = new Set();
    const reRow = /<tr\b([^>]*\bid="trs-tr-(\d+)"[^>]*)>([\s\S]*?)<\/tr>/g;
    let m;
    while ((m = reRow.exec(html))) {
      const t = m[2], body = m[3];
      if (seen.has(t)) continue;
      seen.add(t);
      const cells = body.split(/<td\b/i).slice(1).map(c => {
        const gt = c.indexOf('>');
        return { tag: '<td' + c.slice(0, gt + 1), html: c.slice(gt + 1).replace(/<\/td>\s*$/i, '') };
      });
      const fm = /<a\b[^>]*href="tracker\.php\?f=(\d+)"[^>]*>([\s\S]*?)<\/a>/i.exec(body);
      const tl = /<a\b[^>]*href="viewtopic\.php\?t=(\d+)"[^>]*>([\s\S]*?)<\/a>/i.exec(body);
      const si = cells.findIndex(c => /\btor-size\b/.test(attr(c.tag, 'class') || ''));
      let seeds = null, leech = null;
      const sm = /<b\b[^>]*class="[^"]*seedmed[^"]*"[^>]*>\s*(-?\d+)/i.exec(body);
      if (sm) seeds = int(sm[1]);
      else if (si >= 0 && cells[si + 1]) seeds = int(attr(cells[si + 1].tag, 'data-ts_text') ?? text(cells[si + 1].html));
      const lc = cells.find(c => /\bleechmed\b/.test(attr(c.tag, 'class') || '') || /\bleechmed\b/.test(c.html));
      if (lc) leech = int(text(lc.html));
      const last = cells.length ? cells[cells.length - 1] : null;
      rows.push({
        t,
        fid: fm ? fm[1] : null,
        forum: fm ? text(fm[2]) : null,
        title: tl ? text(tl[2]) : null,
        size: si >= 0 ? int(attr(cells[si].tag, 'data-ts_text')) : null,
        seeds: seeds != null && seeds < 0 ? 0 : seeds,
        leech,
        added: last ? int(attr(last.tag, 'data-ts_text')) : null,
      });
    }
    return { total: tm ? parseInt(tm[1], 10) : rows.length, searchId: sid ? sid[1] : null, rows };
  }

  function rowYears(title) {
    const t = String(title || '');
    let m = /\[\s*((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)?\d{2}))?/.exec(t) || /\(((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)?\d{2}))?\)/.exec(t);
    if (!m) return null;
    const a = parseInt(m[1], 10);
    let b = m[2] ? parseInt(m[2].length === 2 ? m[1].slice(0, 2) + m[2] : m[2], 10) : a;
    return [Math.min(a, b), Math.max(a, b)];
  }

  // keep in sync with films/parse/rutracker.py category_for_forum (Python decides the category; this only selects)
  const NOT_VIDEO = /игр|музык|аудио|книг|саундтрек|soundtrack|\bost\b|клип|концерт|трейлер|документал|спорт|обучающ|обои|журнал|комикс|софт|программ|юмор|караоке|опер[аы]\b|балет|театр|новост|обсужден|архив|флуд|wallpaper|game|music/i;
  const VIDEO = /кино|фильм|сериал|мульт|аниме|видео|video|uhd|\bhd\b|\bdvd\b|3d|стерео/i;
  function videoForum(name) { const f = String(name || ''); return !!f && !NOT_VIDEO.test(f) && VIDEO.test(f); }

  const NEED_RE = {
    '4k': /(^|[^\w])(2160p|4K|UHD)([^\w]|$)/i,
    hdr: /(^|[^\w])(HDR10\+?|HDR|Dolby\s*Vision|DV|DoVi|HLG)([^\w]|$)/i,
    dub: /(^|[^\w])(Dub|DUB)([^\w]|$)|Дубл/,
    flarrow: /Flarrow/i,   // R168: Marvel / DC series — FlarrowFilms voice (owner, 2026-10-25)
  };
  function needsMatch(title, need) {
    if (!need || !need.length || need.includes('any')) return true;
    return need.some(n => NEED_RE[n] && NEED_RE[n].test(title || ''));
  }
  function select(row, q) {
    if (!videoForum(row.forum)) return false;
    const y = rowYears(row.title);
    if (!y) return false;
    const lo = q.lo == null ? 0 : q.lo, hi = q.hi == null ? 9999 : q.hi;
    if (y[1] < lo - 1 || y[0] > hi + 1) return false;
    return needsMatch(row.title, q.need);
  }
  function splitQueries(q, thisYear) {
    const now = thisYear || new Date().getFullYear();
    if (q.lo != null && q.hi != null && Math.min(q.hi + 1, now + 1) - (q.lo - 1) <= 8) {
      const out = [];   // the same ±1 year window select() uses
      for (let y = q.lo - 1; y <= Math.min(q.hi + 1, now + 1); y++) out.push(q.q + ' ' + y);
      return out;
    }
    const words = { '4k': '2160p', hdr: 'HDR', dub: 'Dub', flarrow: 'Flarrow' };
    return (q.need || []).filter(n => words[n]).map(n => q.q + ' ' + words[n]);
  }

  // ---------------------------------------------------------------------------------------------- topic page
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
  function parseTopic(html) {
    html = String(html || '');
    const out = { title: null, forum: null, forum_id: null, size_bytes: null, seeders: null, leechers: null,
                  btih: null, reged: null, post_html: '' };
    let m = /<a\b[^>]*id="topic-title"[^>]*>([\s\S]*?)<\/a>/i.exec(html) || /<h1\b[^>]*class="maintitle"[^>]*>([\s\S]*?)<\/h1>/i.exec(html);
    if (m) out.title = text(m[1]);
    const pb = /<div\b[^>]*class="post_body"/i.exec(html);
    const head = pb ? html.slice(0, pb.index) : html;
    const fr = [...head.matchAll(/href="viewforum\.php\?f=(\d+)"[^>]*>([^<]+)<\/a>/g)];
    if (fr.length) { out.forum_id = fr[fr.length - 1][1]; out.forum = text(fr[fr.length - 1][2]); }
    m = /<[^>]*id="tor-size-humn"[^>]*>/i.exec(html);
    if (m) out.size_bytes = int(attr(m[0], 'title'));
    m = /class="seed"[^>]*>[^<]*<b>(\d+)<\/b>/i.exec(html);
    if (m) out.seeders = int(m[1]);
    m = /class="leech"[^>]*>[^<]*<b>(\d+)<\/b>/i.exec(html);
    if (m) out.leechers = int(m[1]);
    m = /magnet:\?xt=urn:btih:([0-9A-Fa-f]{40})/.exec(html) || /id="tor-hash"[^>]*>\s*([0-9A-Fa-f]{40})/.exec(html);
    if (m) out.btih = m[1].toLowerCase();
    m = /Зарегистрирован[\s\S]{0,300}?(\d{1,2}-[А-Яа-яЁё]{3}-\d{2}(?:\s+\d{1,2}:\d{2})?)/.exec(html);
    if (m) out.reged = m[1];
    if (pb) out.post_html = divBlock(html, pb.index);
    return out;
  }
  function pageProblem(html) {
    const logged = /id="logged-in-username"/.test(html);
    if (/cf-chl|challenge-platform|Just a moment|Один момент/i.test(html) && !logged) return 'cloudflare';
    // maintenance / outage page (owner, 2026-10-26: «у ру трекера вроде есть время технических работ»): never a result
    if (!logged && /техническ\S*\s+(работ|перерыв|причин)|временно\s+недоступ|temporarily\s+unavailable|maintenance/i.test(html)) return 'maintenance';
    if (/name="login_username"/.test(html) && !logged) return 'not_logged_in';
    return null;
  }

  // ---------------------------------------------------------------------------------------------- browser loop
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  function lsGet(k, d) { try { const v = root.localStorage && root.localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch (e) { return d; } }
  function lsSet(k, v) { try { root.localStorage.setItem(k, JSON.stringify(v)); return true; } catch (e) { return false; } }

  function makeRunner(cfg) {
    const S = root.__rt = {
      state: 'running', run: cfg.run || new Date().toISOString().slice(0, 19), started: new Date().toISOString(),
      qDone: 0, qTotal: cfg.queries.length, requests: 0, topics: 0, posted: 0, errors: 0, current: null, stop: false,
      lastError: null, lsFull: false,
    };
    root.__rtUnsent = root.__rtUnsent && root.__rtUnsent.length ? root.__rtUnsent : lsGet('rtc_unsent', []);
    let last = 0;
    const delay = cfg.delayMs || 1500;

    // Outages do not lose queries (owner, 2026-10-26: «у ру трекера вроде есть время технических работ», «там ещё и
    // релогин нужен будет после тех работ»): a maintenance page, Cloudflare, a login page or the site being down (5xx /
    // network) pauses the loop on the SAME request — state «waiting: …» (also posted to the bridge as rutracker_status,
    // the watchdog shows it), a retry every waitPollMs (2 min) — and it goes on by itself once the site is back / the
    // owner logged in again; only after waitHours (12 h) of waiting it stops as fatal.
    async function pause(reason) {
      const since = S.waitingSince || (S.waitingSince = Date.now());
      if (Date.now() - since > (cfg.waitHours || 12) * 3600e3) {
        const e = new Error(reason + ' (ждали ' + Math.round((Date.now() - since) / 60000) + ' мин)'); e.fatal = true; throw e;
      }
      S.state = 'waiting: ' + reason;
      try {
        await fetch((cfg.bridge || BRIDGE).replace(/\/ingest\/rutracker$/, '/ingest/rutracker_status'), { method: 'POST',
          headers: { 'Content-Type': 'text/plain' },
          body: JSON.stringify({ v: 1, kind: 'rutracker_status', run: S.run, state: S.state, since: new Date(since).toISOString(),
                                 at: new Date().toISOString(), qDone: S.qDone, qTotal: S.qTotal }) });
      } catch (e) { /* bridge down: the state is still in __rtStatus() */ }
      await sleep(cfg.waitPollMs || 120000);
    }

    async function get(url) {
      let err = null;
      for (let a = 0; ; a++) {
        const wait = last + delay + Math.random() * 700 - Date.now();
        if (wait > 0) await sleep(wait);
        last = Date.now();
        S.requests++;
        try {
          const r = await fetch(url, { credentials: 'include' });
          if (r.ok) {
            const html = new TextDecoder('windows-1251').decode(await r.arrayBuffer());
            const p = pageProblem(html);
            if (!p) {
              if (S.waitingSince) { S.waitingSince = null; S.state = 'running'; }
              return html;
            }
            if (cfg.waitOnProblem === false) { const e = new Error(p); e.fatal = true; throw e; }
            await pause(p);
            a = -1;
            continue;
          }
          err = new Error('HTTP ' + r.status);
          if (r.status === 404) break;
        } catch (e) {
          if (e.fatal) throw e;
          err = e;
        }
        if (a >= 3) {                       // 4 tries failed: the site is down → wait for it instead of skipping the query
          if (cfg.waitOnProblem === false) break;
          await pause('down: ' + (err && err.message || 'fetch failed'));
          a = -1;
          continue;
        }
        await sleep(5000 * (a + 1));
      }
      throw err || new Error('fetch failed');
    }

    async function send(obj) {
      const body = JSON.stringify(obj);
      try {
        const r = await fetch(cfg.bridge || BRIDGE, { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body });
        if (r.ok) { S.posted++; return true; }
      } catch (e) { /* bridge down: keep */ }
      root.__rtUnsent.push(body);
      if (!lsSet('rtc_unsent', root.__rtUnsent)) S.lsFull = true;
      return false;
    }

    // Only what is new since the last successful sync of this query (R207 / R212, owner 2026-10-26: «поставим фильтр на
    // сканер только на новые от даты последнего сканирования»): with `since` (unix seconds, set by Python) the search is
    // sorted by registration date, newest first (o=1&s=2), and paging stops at the first page whose rows are all older.
    async function search(q, payload, since) {
      const enc = enc1251(q) + (since ? '&o=1&s=2' : '');
      const first = await get(`${BASE}/tracker.php?nm=${enc}`);
      const p = parseSearch(first);
      const res = { q, total: p.total, capped: p.total >= CAP, pages: 1, rows: p.rows };
      if (p.total > 0 && !p.rows.length) {
        payload.errors.push({ q, err: 'row_parse_failed', snippet: first.slice(first.indexOf('tor-tbl'), first.indexOf('tor-tbl') + 3000) });
        return res;
      }
      const n = Math.min(p.total, CAP);
      for (let start = PER_PAGE; start < n && !S.stop; start += PER_PAGE) {
        const url = p.searchId ? `${BASE}/tracker.php?search_id=${p.searchId}&start=${start}` : `${BASE}/tracker.php?nm=${enc}&start=${start}`;
        if (since && res.rows.length && res.rows.slice(-PER_PAGE).every(r => r.added && r.added < since)) { res.sinceStop = true; break; }
        const pp = parseSearch(await get(url));
        res.pages++;
        if (!pp.rows.length) break;
        res.rows.push(...pp.rows);
      }
      return res;
    }

    async function loop() {
      const known = new Set((cfg.known || []).map(String));
      const seen = new Set();
      const prog = lsGet('rtc_progress', {});
      const startAt = cfg.startAt != null ? cfg.startAt : (cfg.resume && prog.run === S.run ? prog.next || 0 : 0);
      S.qDone = startAt;
      let fatal = null;
      for (let i = startAt; i < cfg.queries.length && !S.stop && !fatal; i++) {
        const q = cfg.queries[i];
        S.current = q.q;
        const payload = { v: 1, kind: 'rutracker', run: S.run, seq: i, query: q, searches: [], topics: [], errors: [],
                          at: new Date().toISOString() };
        try {
          const s = await search(q.q, payload, q.since);
          payload.searches.push(s);
          if (s.capped && cfg.split !== false) {
            for (const sub of splitQueries(q)) { if (S.stop) break; payload.searches.push(await search(sub, payload, q.since)); }
          }
          const cand = new Map();
          for (const sr of payload.searches) {
            for (const r of sr.rows) {
              r.sel = select(r, q) && !(q.since && r.added && r.added < q.since) ? 1 : 0;   // older than the sync point: known
              if (r.sel && !known.has(r.t) && !seen.has(r.t)) cand.set(r.t, r);
            }
          }
          const picked = [...cand.values()].sort((a, b) => (b.seeds || 0) - (a.seeds || 0)).slice(0, cfg.maxTopics || 40);
          for (const r of picked) {
            if (S.stop) break;
            seen.add(r.t);
            try {
              const html = await get(`${BASE}/viewtopic.php?t=${r.t}`);
              const tp = parseTopic(html);
              tp.topic_id = r.t;
              tp.fetched_at = new Date().toISOString();
              if (!tp.post_html) payload.errors.push({ t: r.t, err: 'no_post_body', snippet: html.slice(0, 2000) });
              payload.topics.push(tp);
              S.topics++;
            } catch (e) {
              if (e.fatal) throw e;
              payload.errors.push({ t: r.t, err: String(e && e.message || e) });
            }
          }
        } catch (e) {
          payload.errors.push({ q: q.q, err: String(e && e.message || e) });
          S.lastError = String(e && e.message || e);
          if (e.fatal) fatal = S.lastError;
        }
        S.errors += payload.errors.length;
        await send(payload);
        if (!fatal) { S.qDone = i + 1; lsSet('rtc_progress', { run: S.run, next: i + 1 }); }
      }
      await flush();
      await send({ v: 1, kind: 'rutracker', run: S.run, final: true, fatal, stats: Object.assign({}, S, { stop: undefined }) });
      S.state = fatal ? 'stopped: ' + fatal : S.stop ? 'stopped by __rtStop' : 'done';
      S.current = null;
    }

    async function flush() {
      const left = [];
      for (const body of root.__rtUnsent.splice(0)) {
        try {
          const r = await fetch(cfg.bridge || BRIDGE, { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body });
          if (r.ok) { S.posted++; continue; }
        } catch (e) { /* still down */ }
        left.push(body);
      }
      root.__rtUnsent.push(...left);
      lsSet('rtc_unsent', root.__rtUnsent);
      return left.length;
    }

    return { S, loop, flush };
  }

  function start(cfg) {
    cfg = cfg || {};
    if (!Array.isArray(cfg.queries) && !cfg.queriesUrl) {
      return 'usage: __rtCollect({queriesUrl}) or __rtCollect({queries: [{q, lo, hi, need}], known: [], run})';
    }
    if (root.__rt && /^(running|loading)/.test(root.__rt.state)) return 'already running: ' + JSON.stringify(status());
    const go = c => {
      const r = makeRunner(c);
      root.__rtRunner = r;
      r.loop().catch(e => { r.S.state = 'crashed: ' + (e && e.message || e); });
      return r;
    };
    if (Array.isArray(cfg.queries)) {
      const r = go(cfg);
      return `started run ${r.S.run}: ${cfg.queries.length} queries, ${(cfg.known || []).length} known topics; ` +
             `results -> ${cfg.bridge || BRIDGE}`;
    }
    // the query list (with known topic ids and the run id) comes from the bridge: GET /queries/rutracker
    root.__rt = { state: 'loading queries from ' + cfg.queriesUrl, qDone: 0, qTotal: 0 };
    fetch(cfg.queriesUrl, { cache: 'no-store' })
      .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(loaded => go(Object.assign({}, loaded, cfg, { queries: loaded.queries, known: loaded.known || [] })))
      .catch(e => { root.__rt = { state: 'no queries: ' + (e && e.message || e) + ' — bridge down or blocked; use run_inline*.js', qDone: 0, qTotal: 0 }; });
    return 'loading queries from ' + cfg.queriesUrl + ' — check with __rtStatus() or watch the bridge output';
  }
  function status() {
    const S = root.__rt;
    if (!S) return 'not started';
    return { state: S.state, run: S.run, queries: `${S.qDone}/${S.qTotal}`, current: S.current, requests: S.requests,
             topics: S.topics, posted: S.posted, unsent: (root.__rtUnsent || []).length, errors: S.errors,
             lastError: S.lastError, lsFull: S.lsFull };
  }
  function chunk(i, size) {
    const all = JSON.stringify(root.__rtUnsent && root.__rtUnsent.length ? root.__rtUnsent : lsGet('rtc_unsent', []));
    size = size || 400000;
    return { i, n: Math.ceil(all.length / size), data: all.slice(i * size, (i + 1) * size) };
  }

  const api = { parseSearch, parseTopic, rowYears, videoForum, needsMatch, select, splitQueries, enc1251, unesc, divBlock,
                pageProblem };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof root.document !== 'undefined') {
    root.__rtCollect = start;
    root.__rtStatus = status;
    root.__rtStop = () => { if (root.__rt) root.__rt.stop = true; return status(); };
    root.__rtFlush = () => (root.__rtRunner ? root.__rtRunner.flush() : Promise.resolve('no runner'));
    root.__rtChunk = chunk;
    root.__rtApi = api;
  }
})(typeof window !== 'undefined' ? window : globalThis);
