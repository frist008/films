/* films/browser/toloka_collect.js — toloka.to collector for the owner's Chrome (R206; update-db step 6а).
 *
 * toloka.to («Гуртом», source priority 6 after rutor, rutracker, nnmclub, kinozal, rustorka) shows nothing to guests — every forum, search and topic
 * redirects to login.php — and accounts / passwords are the owner's alone, so the pages are read in HIS logged-in tab by
 * this script, run by HIM from the DevTools console (no «Allow» windows; the same way as rutracker_collect.js):
 * `python -X utf8 -m films.sources.toloka queries` writes data/cache/toloka/run.js = this file + `__tlCollect({queriesUrl})`;
 * the owner opens it in Notepad, copies it and pastes it into the console of a toloka.to tab (the first time Chrome asks
 * him to type `allow pasting`, and once to allow local-network access to 127.0.0.1). The call returns at once; the loop
 * keeps running in the tab:
 *
 *   index page                -> the forum list {id, name} (first payload: Python learns toloka's forums);
 *   for every query (original, latin, Ukrainian names are SEPARATE queries — owner's rule R151):
 *     tracker.php?nm=<utf-8>  -> all result pages (start=50, 100, … up to maxPages);
 *     rows -> {topic id, forum id + name, title, size bytes, seeders, leechers, added};
 *     selected rows (video forum, year window ±1, title shows a needed feature: 4K / HDR-DV — a Ukrainian dub cannot be
 *     seen in a title, so «dub» / «ukdub» needs take every video row; not known yet)
 *     -> /t<id>  -> first post HTML (description + MediaInfo) + size, seeds, btih, registration date;
 *   one POST per query to the local bridge http://127.0.0.1:8765/ingest/toloka (`python -X utf8 -m films bridge`);
 *   whatever the bridge did not take stays in window.__tlUnsent and localStorage «tlc_unsent» (__tlChunk(i) reads it out).
 *
 * toloka pages are UTF-8 (checked on the public index page, 2026-10-26). Polite: one request at a time, delayMs (default
 * 1500) + jitter, backoff on errors, stops on a login page / Cloudflare challenge. Reading only: nothing is clicked or posted
 * to toloka. Announce keys (magnet «tr=…», «?h=…» passkeys) are cut from everything sent.
 *
 * The logged-in markup was never seen by the author (no account): the parsers below are written from toloka's public pages
 * and the usual phpBB / TorrentPier markup, and report what they could not read (errors 'row_parse_failed' with a snippet of
 * the result table, 'no_post_body' with the start of the topic page) — the first live run must be checked
 * (`python -X utf8 -m films.sources.toloka ingest` prints the error samples).
 *
 * Helpers: __tlStatus() — progress; __tlStop() — stop after the current request; __tlFlush() — resend unsent;
 * __tlChunk(i) — {i, n, data} slice of the unsent payloads. Pure parsers are exported for node tests (tests/test_toloka.py).
 */
(function (root) {
  'use strict';

  const BASE = 'https://toloka.to';
  const BRIDGE = 'http://127.0.0.1:8765/ingest/toloka';
  const PER_PAGE = 50;

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
  function text(html) { return unesc(String(html || '').replace(/<[^>]+>/g, ' ')).replace(/\s+/g, ' ').trim(); }
  function int(s) { const n = parseInt(String(s == null ? '' : s).replace(/[^\d-]/g, ''), 10); return Number.isFinite(n) ? n : null; }
  const UNITS = { B: 0, 'Б': 0, K: 1, 'К': 1, M: 2, 'М': 2, G: 3, 'Г': 3, T: 4, 'Т': 4 };
  function sizeBytes(s) {
    const m = /(\d+(?:[.,]\d+)?)\s*([KMGTBКМГТБ])i?[BБ]?/i.exec(unesc(String(s || '')).replace(/\u00a0/g, ' '));
    if (!m) return null;
    return Math.round(parseFloat(m[1].replace(',', '.')) * Math.pow(1024, UNITS[m[2].toUpperCase()]));
  }
  // announce keys are personal: never sent anywhere (magnet trackers, ?h= / passkey= / uk= parameters)
  function scrub(s) {
    return String(s || '')
      .replace(/(magnet:\?xt=urn:btih:[0-9A-Za-z]+)[^"'\s<>]*/g, '$1')
      .replace(/([?&;](?:h|uk|passkey|pk|key|sid|hash)=)[^&"'\s<>]+/gi, '$1REDACTED');
  }

  // ---------------------------------------------------------------------------------------------- forums
  // keep in sync with films/parse/ua_trackers.py category_for_forum (Python decides the category; this only selects)
  const NOT_VIDEO = /музик|аудіо|книг|літератур|ігр|гра\b|програм|софт|документ|спорт|футбол|формул|бокс|концерт|кліп|шоу|аніме|anime|обговор|архів|пошук|запит|правил|новин|інструкц|кодуванн|субтитр|мобільн|телефон|трейлер|незабаром|неоформлен|фотограф|шпалер|гумор|караоке|театр|опер|балет|навчальн|флуд|техпідтримк|music|game|soft/i;
  const VIDEO = /кіно|фільм|серіал|мульт|відео|video|\bhd\b|uhd|4k/i;
  function videoForum(name) { const f = String(name || ''); return !!f && !NOT_VIDEO.test(f) && VIDEO.test(f); }

  function parseIndex(html) {
    const out = [], seen = new Set();
    const re = /<a\b[^>]*href="(?:https?:\/\/toloka\.to)?\/?(?:f|viewforum\.php\?f=)(\d+)"[^>]*>([\s\S]*?)<\/a>/gi;
    let m;
    while ((m = re.exec(String(html || '')))) {
      if (seen.has(m[1])) continue;
      seen.add(m[1]);
      const name = text(m[2]);
      if (name) out.push({ f: m[1], name, video: videoForum(name) });
    }
    return out;
  }

  // ---------------------------------------------------------------------------------------------- search page
  function cellsOf(tr) {
    return tr.split(/<td\b/i).slice(1).map(c => {
      const gt = c.indexOf('>');
      return { tag: '<td' + c.slice(0, gt + 1), html: c.slice(gt + 1).replace(/<\/td>[\s\S]*$/i, '') };
    });
  }
  function parseSearch(html) {
    html = String(html || '');
    const rows = [], seen = new Set();
    const reRow = /<tr\b([^>]*)>([\s\S]*?)<\/tr>/gi;
    let m;
    while ((m = reRow.exec(html))) {
      const body = m[2];
      const tl = /<a\b[^>]*href="(?:https?:\/\/toloka\.to)?\/?t(\d+)"[^>]*>([\s\S]*?)<\/a>/i.exec(body) ||
                 /<a\b[^>]*href="[^"]*viewtopic\.php\?t=(\d+)"[^>]*>([\s\S]*?)<\/a>/i.exec(body);
      const fm = /<a\b[^>]*href="(?:https?:\/\/toloka\.to)?\/?(?:f|viewforum\.php\?f=|tracker\.php\?f=)(\d+)"[^>]*>([\s\S]*?)<\/a>/i.exec(body);
      if (!tl || !fm) continue;                       // not a result row (header, pagination …)
      const t = tl[1];
      if (seen.has(t)) continue;
      seen.add(t);
      const cells = cellsOf(body);
      let size = null, seeds = null, leech = null, added = null;
      for (const c of cells) {
        const tx = text(c.html), cls = (/class="([^"]*)"/i.exec(c.tag) || [])[1] || '';
        if (size == null && /^\d+(?:[.,]\d+)?\s*(?:[KMGT]i?B|[КМГТ]Б)$/i.test(tx)) size = sizeBytes(tx);
        if (seeds == null && (/seed/i.test(cls) || /class="[^"]*seed/i.test(c.html))) seeds = int(tx);
        if (leech == null && (/leech/i.test(cls) || /class="[^"]*leech/i.test(c.html))) leech = int(tx);
        const dm = /((?:19|20)\d{2}-\d{1,2}-\d{1,2}(?:\s+\d{1,2}:\d{2})?)/.exec(tx);
        if (dm) added = dm[1];
      }
      if (seeds == null) {   // Jackett's toloka layout: seeders in a <b> of the 10th cell
        const c = cells[9];
        if (c && /<b\b/i.test(c.html)) seeds = int(text(c.html));
      }
      rows.push({ t, fid: fm[1], forum: text(fm[2]), title: text(tl[2]), size, seeds: seeds != null && seeds < 0 ? 0 : seeds,
                  leech, added });
    }
    let maxStart = 0;
    for (const s of html.matchAll(/[?&;]start=(\d+)/g)) maxStart = Math.max(maxStart, parseInt(s[1], 10));
    return { rows, maxStart };
  }

  function rowYears(title) {
    const t = String(title || '');
    const m = /\(\s*((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)?\d{2}))?\s*\)/.exec(t) || /\[\s*((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)?\d{2}))?/.exec(t);
    if (!m) return null;
    const a = parseInt(m[1], 10);
    const b = m[2] ? parseInt(m[2].length === 2 ? m[1].slice(0, 2) + m[2] : m[2], 10) : a;
    return [Math.min(a, b), Math.max(a, b)];
  }

  const NEED_RE = {
    '4k': /(^|[^\w])(2160p|4K|UHD)([^\w]|$)/i,
    hdr: /(^|[^\w])(HDR10\+?|HDR|Dolby\s*Vision|DV|DoVi|HLG)([^\w]|$)/i,
  };
  function needsMatch(title, need) {
    if (!need || !need.length || need.includes('any') || need.includes('dub') || need.includes('ukdub')) return true;
    return need.some(n => NEED_RE[n] && NEED_RE[n].test(title || ''));
  }
  // owner, 2026-10-26: only what is newer than the last successful sync of this query (Python sends `since` = that date
  // minus 2 days, 'YYYY-MM-DD'); a row without a readable date counts as new
  function isOld(row, since) { return !!(since && row.added && String(row.added).slice(0, 10) < since); }
  function olderThan(rows, since) { return !!since && rows.length > 0 && rows.every(r => isOld(r, since)); }
  function select(row, q) {
    if (!videoForum(row.forum)) return false;
    const y = rowYears(row.title);
    if (!y) return false;
    const lo = q.lo == null ? 0 : q.lo, hi = q.hi == null ? 9999 : q.hi;
    if (y[1] < lo - 1 || y[0] > hi + 1) return false;
    return needsMatch(row.title, q.need);
  }

  // ---------------------------------------------------------------------------------------------- topic page
  function tagBlock(html, start, tag) {
    const i = html.indexOf('>', start) + 1;
    const re = new RegExp('<(/?)' + tag + '\\b[^>]*?(/?)>', 'gi');
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
    let m = /<a\b[^>]*class="[^"]*maintitle[^"]*"[^>]*>([\s\S]*?)<\/a>/i.exec(html) || /<h1\b[^>]*>([\s\S]*?)<\/h1>/i.exec(html);
    if (m) out.title = text(m[1]);
    if (!out.title && (m = /<title>([\s\S]*?)<\/title>/i.exec(html))) out.title = text(m[1]).split(/\s+(?:::|—|-|\|)\s+(?:Гуртом|Толока|toloka)/i)[0];
    const pb = /<(div|span|td)\b[^>]*class="(?:[^"]*\s)?(?:post_body|postbody|post-body)(?:\s[^"]*)?"/i.exec(html);
    const head = pb ? html.slice(0, pb.index) : html;
    const fr = [...head.matchAll(/href="(?:https?:\/\/toloka\.to)?\/?(?:f|viewforum\.php\?f=)(\d+)"[^>]*>([^<]+)<\/a>/g)];
    if (fr.length) { out.forum_id = fr[fr.length - 1][1]; out.forum = text(fr[fr.length - 1][2]); }
    m = /Розмір\s*:?\s*(?:&nbsp;|\s|<[^>]+>)*(\d+(?:[.,]\d+)?(?:&nbsp;|\s)*[KMGTКМГТ]?i?[BБ])/i.exec(html);
    if (m) out.size_bytes = sizeBytes(m[1]);
    m = /class="seed[^"]*"[^>]*>[^<]*<b>(\d+)<\/b>/i.exec(html) || /(?:Сідів|Сіди|Сідери|Роздають)\s*:?\s*(?:&nbsp;|\s|<[^>]+>)*(\d+)/.exec(html);
    if (m) out.seeders = int(m[1]);
    m = /class="leech[^"]*"[^>]*>[^<]*<b>(\d+)<\/b>/i.exec(html) || /(?:Лічерів|Лічери|Качають)\s*:?\s*(?:&nbsp;|\s|<[^>]+>)*(\d+)/.exec(html);
    if (m) out.leechers = int(m[1]);
    m = /magnet:\?xt=urn:btih:([0-9A-Fa-f]{40})/.exec(html) || /(?:info[\s_-]*hash|хеш)[^0-9A-Fa-f]{0,80}([0-9A-Fa-f]{40})(?![0-9A-Fa-f])/i.exec(html);
    if (m) out.btih = m[1].toLowerCase();
    m = /(?:Зареєстрован\w*|Додано|Зареєстр\.)\s*:?\s*(?:&nbsp;|\s|<[^>]+>)*((?:19|20)\d{2}-\d{1,2}-\d{1,2}(?:\s+\d{1,2}:\d{2})?|\d{1,2}[\s-][А-Яа-яІіЇїЄє]{3}[А-Яа-яІіЇїЄє]*[\s\-,]+\d{2,4}(?:,?\s+\d{1,2}:\d{2})?|(?:Сьогодні|Вчора)\s+\d{1,2}:\d{2})/.exec(html);
    if (m) out.reged = m[1];
    if (pb) out.post_html = scrub(tagBlock(html, pb.index, pb[1].toLowerCase()));
    return out;
  }
  function pageProblem(html) {
    const loggedIn = /login\.php\?logout|logout=true|mode=logout/i.test(html);
    if (/cf-chl|challenge-platform|Just a moment|Один момент/i.test(html) && !loggedIn) return 'cloudflare';
    // maintenance / outage page (as rutracker_collect.js): never a result
    if (!loggedIn && /техн\S*\s+(робіт|роботи|перерв|работ)|тимчасово\s+недоступ|временно\s+недоступ|temporarily\s+unavailable|maintenance/i.test(html)) return 'maintenance';
    if (/name="password"/i.test(html) && /name="username"/i.test(html) && !loggedIn) return 'not_logged_in';
    return null;
  }

  // ---------------------------------------------------------------------------------------------- browser loop
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  function lsGet(k, d) { try { const v = root.localStorage && root.localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch (e) { return d; } }
  function lsSet(k, v) { try { root.localStorage.setItem(k, JSON.stringify(v)); return true; } catch (e) { return false; } }

  function makeRunner(cfg) {
    const S = root.__tl = {
      state: 'running', run: cfg.run || new Date().toISOString().slice(0, 19), started: new Date().toISOString(),
      qDone: 0, qTotal: cfg.queries.length, requests: 0, topics: 0, posted: 0, errors: 0, current: null, stop: false,
      lastError: null, lsFull: false,
    };
    root.__tlUnsent = root.__tlUnsent && root.__tlUnsent.length ? root.__tlUnsent : lsGet('tlc_unsent', []);
    let last = 0;
    const delay = cfg.delayMs || 1500;

    // Outages do not lose queries (as rutracker_collect.js, owner 2026-10-26): a maintenance page, Cloudflare, a login
    // page (logged out) or the site being down (5xx / network) pauses the loop on the SAME request — state «waiting: …»
    // (also posted to the bridge as kind toloka_status), a retry every waitPollMs (2 min) — and it goes on by itself once
    // the site is back / the owner logged in again; only after waitHours (12 h) of waiting it stops as fatal.
    // cfg.waitOnProblem === false: stop at once instead (tests).
    async function pause(reason) {
      const since = S.waitingSince || (S.waitingSince = Date.now());
      if (Date.now() - since > (cfg.waitHours || 12) * 3600e3) {
        const e = new Error(reason + ' (ждали ' + Math.round((Date.now() - since) / 60000) + ' мин)'); e.fatal = true; throw e;
      }
      S.state = 'waiting: ' + reason;
      try {
        await fetch((cfg.bridge || BRIDGE).replace(/\/ingest\/toloka$/, '/ingest/toloka_status'), { method: 'POST',
          headers: { 'Content-Type': 'text/plain' },
          body: JSON.stringify({ v: 1, kind: 'toloka_status', run: S.run, state: S.state, since: new Date(since).toISOString(),
                                 at: new Date().toISOString(), qDone: S.qDone, qTotal: S.qTotal }) });
      } catch (e) { /* bridge down: the state is still in __tlStatus() */ }
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
            const html = await r.text();
            const p = /\/login\.php/.test(r.url || '') ? 'not_logged_in' : pageProblem(html);
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
      root.__tlUnsent.push(body);
      if (!lsSet('tlc_unsent', root.__tlUnsent)) S.lsFull = true;
      return false;
    }

    // o=1&s=2: newest registered first (TorrentPier / Jackett toloka parameters — check on the first live run)
    function searchUrl(q, start) {
      return `${BASE}/tracker.php?nm=${encodeURIComponent(q)}&o=1&s=2` + (start ? `&start=${start}` : '');
    }

    async function search(q, payload, since) {
      const first = await get(searchUrl(q, 0));
      const p = parseSearch(first);
      const res = { q, pages: 1, rows: p.rows, capped: false };
      if (!p.rows.length && /href="(?:https?:\/\/toloka\.to)?\/?t\d+"/.test(first)) {
        const i = Math.max(0, first.search(/forumline|tablesorter|prow/));
        payload.errors.push({ q, err: 'row_parse_failed', snippet: scrub(first.slice(i, i + 3000)) });
        return res;
      }
      const maxPages = cfg.maxPages || 10;
      let rows = p.rows;
      for (let start = PER_PAGE; start <= p.maxStart && res.pages < maxPages && !S.stop; start += PER_PAGE) {
        if (since && olderThan(rows, since)) { res.sinceStop = true; break; }   // the last successful sync is reached
        const pp = parseSearch(await get(searchUrl(q, start)));
        res.pages++;
        if (!pp.rows.length) break;
        rows = pp.rows;
        res.rows.push(...pp.rows);
      }
      res.capped = p.maxStart >= PER_PAGE * maxPages;
      return res;
    }

    async function loop() {
      const known = new Set((cfg.known || []).map(String));
      const seen = new Set();
      const prog = lsGet('tlc_progress', {});
      const startAt = cfg.startAt != null ? cfg.startAt : (cfg.resume && prog.run === S.run ? prog.next || 0 : 0);
      S.qDone = startAt;
      let fatal = null;
      try {   // the forum list, once per run (Python learns toloka's forums and their names)
        const idx = await get(`${BASE}/`);
        await send({ v: 1, kind: 'toloka', run: S.run, forums: parseIndex(idx), at: new Date().toISOString() });
      } catch (e) {
        S.lastError = String(e && e.message || e);
        if (e.fatal) fatal = S.lastError;
      }
      for (let i = startAt; i < cfg.queries.length && !S.stop && !fatal; i++) {
        const q = cfg.queries[i];
        S.current = q.q;
        const payload = { v: 1, kind: 'toloka', run: S.run, seq: i, query: q, searches: [], topics: [], errors: [],
                          at: new Date().toISOString() };
        try {
          const s = await search(q.q, payload, q.since);
          payload.searches.push(s);
          const cand = new Map();
          for (const r of s.rows) {
            r.sel = select(r, q) && !isOld(r, q.since) ? 1 : 0;
            if (r.sel && !known.has(r.t) && !seen.has(r.t)) cand.set(r.t, r);
          }
          const picked = [...cand.values()].sort((a, b) => (b.seeds || 0) - (a.seeds || 0)).slice(0, cfg.maxTopics || 12);
          for (const r of picked) {
            if (S.stop) break;
            seen.add(r.t);
            try {
              const html = await get(`${BASE}/t${r.t}`);
              const tp = parseTopic(html);
              tp.topic_id = r.t;
              tp.fetched_at = new Date().toISOString();
              if (!tp.post_html) payload.errors.push({ t: r.t, err: 'no_post_body', snippet: scrub(html.slice(0, 2000)) });
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
        if (!fatal) { S.qDone = i + 1; lsSet('tlc_progress', { run: S.run, next: i + 1 }); }
      }
      await flush();
      await send({ v: 1, kind: 'toloka', run: S.run, final: true, fatal, stats: Object.assign({}, S, { stop: undefined }) });
      S.state = fatal ? 'stopped: ' + fatal : S.stop ? 'stopped by __tlStop' : 'done';
      S.current = null;
    }

    async function flush() {
      const left = [];
      for (const body of root.__tlUnsent.splice(0)) {
        try {
          const r = await fetch(cfg.bridge || BRIDGE, { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body });
          if (r.ok) { S.posted++; continue; }
        } catch (e) { /* still down */ }
        left.push(body);
      }
      root.__tlUnsent.push(...left);
      lsSet('tlc_unsent', root.__tlUnsent);
      return left.length;
    }

    return { S, loop, flush };
  }

  function start(cfg) {
    cfg = cfg || {};
    if (!Array.isArray(cfg.queries) && !cfg.queriesUrl) {
      return 'usage: __tlCollect({queriesUrl}) or __tlCollect({queries: [{q, lo, hi, need}], known: [], run})';
    }
    if (root.__tl && /^(running|loading)/.test(root.__tl.state)) return 'already running: ' + JSON.stringify(status());
    const go = c => {
      const r = makeRunner(c);
      root.__tlRunner = r;
      r.loop().catch(e => { r.S.state = 'crashed: ' + (e && e.message || e); });
      return r;
    };
    if (Array.isArray(cfg.queries)) {
      const r = go(cfg);
      return `started run ${r.S.run}: ${cfg.queries.length} queries, ${(cfg.known || []).length} known topics; ` +
             `results -> ${cfg.bridge || BRIDGE}`;
    }
    root.__tl = { state: 'loading queries from ' + cfg.queriesUrl, qDone: 0, qTotal: 0 };
    fetch(cfg.queriesUrl, { cache: 'no-store' })
      .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(loaded => go(Object.assign({}, loaded, cfg, { queries: loaded.queries, known: loaded.known || [] })))
      .catch(e => { root.__tl = { state: 'no queries: ' + (e && e.message || e) + ' — bridge down or blocked; use run_inline*.js', qDone: 0, qTotal: 0 }; });
    return 'loading queries from ' + cfg.queriesUrl + ' — check with __tlStatus() or watch the bridge output';
  }
  function status() {
    const S = root.__tl;
    if (!S) return 'not started';
    return { state: S.state, run: S.run, queries: `${S.qDone}/${S.qTotal}`, current: S.current, requests: S.requests,
             topics: S.topics, posted: S.posted, unsent: (root.__tlUnsent || []).length, errors: S.errors,
             lastError: S.lastError, lsFull: S.lsFull };
  }
  function chunk(i, size) {
    const all = JSON.stringify(root.__tlUnsent && root.__tlUnsent.length ? root.__tlUnsent : lsGet('tlc_unsent', []));
    size = size || 400000;
    return { i, n: Math.ceil(all.length / size), data: all.slice(i * size, (i + 1) * size) };
  }

  const api = { parseSearch, parseTopic, parseIndex, rowYears, videoForum, needsMatch, select, isOld, olderThan, unesc, tagBlock,
                pageProblem, sizeBytes, scrub };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof root.document !== 'undefined') {
    root.__tlCollect = start;
    root.__tlStatus = status;
    root.__tlStop = () => { if (root.__tl) root.__tl.stop = true; return status(); };
    root.__tlFlush = () => (root.__tlRunner ? root.__tlRunner.flush() : Promise.resolve('no runner'));
    root.__tlChunk = chunk;
    root.__tlApi = api;
  }
})(typeof window !== 'undefined' ? window : globalThis);
