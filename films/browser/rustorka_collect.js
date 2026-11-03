/* films/browser/rustorka_collect.js — rustorka.com history collector for a logged-in Chrome tab (R208, R232; update-db 6а).
 *
 * Guests see no film / series / cartoon forum on rustorka (only the RSS feed of the last ~100 topics, read over HTTP by
 * `films.sources.rustorka crawl`); the owner is logged in in Chrome (owner, 2026-10-26 05:30: «везде в хроме вошёл! во
 * всех источниках»). Search (tracker.php) answers a captcha to guests and is not used: the video forums are walked
 * instead, newest activity first, which also fits the owner's incremental rule (R212: only what is newer than the last
 * successful sync point). ONE javascript_tool call in a rustorka tab runs everything: data/cache/rustorka/run.js =
 * this file + `__rkCollect({queriesUrl})` (written by `python -X utf8 -m films.sources.rustorka queries`); the job — run
 * id, sync point per forum, known topics, extra topics — comes from the bridge's GET /queries/rustorka. The call returns
 * at once; the loop keeps running in the tab:
 *
 *   index.php                      -> forum tree (category / forum / subforum); the video forums are picked
 *                                     (videoForum: films, series, cartoons, doramas; not anime, games, music, Russian
 *                                     films and series, porn, documentaries …) and posted once ({type: 'forums'});
 *   viewforum.php?f=F&start=50k    -> topic rows {t, title, seeds, leech, size, last (last post «YYYY-MM-DD HH:MM»),
 *                                     sticky}, newest activity first; a forum stops at the first page with no row newer
 *                                     than its `since` (sync point − 2 days, or the first-run horizon), at its last
 *                                     page, or after maxPages;
 *   viewtopic.php?t=T              -> for rows of year ≥ yearFrom (or in `extra`) whose title is not known yet (a new
 *                                     topic, or more episodes in the title): first post HTML (description + MediaInfo),
 *                                     first post id, breadcrumb, size (magnet xl=), btih, registration date;
 *   one POST per listing page to the bridge http://127.0.0.1:8765/ingest/rustorka (`python -X utf8 -m films bridge`),
 *   the page that ends a forum carries forumDone {stopped, newest}; at the end {final: true, fatal, stats}.
 *
 * Outages do not lose work (same as rutracker_collect.js): a maintenance page, a captcha, Cloudflare, a logged-out page
 * or the site being down pauses the loop on the SAME request — state «waiting: …», also POSTed to
 * /ingest/rustorka_status — and retries every waitPollMs (2 min) until waitHours (12 h). Nothing is clicked, no form is
 * sent, no captcha is touched: reading only. Pages are windows-1251 (TextDecoder). Polite: one request at a time,
 * delayMs (1500) + jitter. Magnet links are cut to the btih (the announce URL of a logged-in user carries a passkey).
 *
 * Helpers: __rkStatus(), __rkStop(), __rkFlush(), __rkChunk(i). Pure parsers are exported for node tests
 * (tests/test_rustorka.py).
 */
(function (root) {
  'use strict';

  const BASE = 'https://rustorka.com/forum';
  const BRIDGE = 'http://127.0.0.1:8765/ingest/rustorka';
  const PER_PAGE = 50;

  // ---------------------------------------------------------------------------------------------- text helpers
  const ENT = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', laquo: '«', raquo: '»', ndash: '–',
                mdash: '—', hellip: '…', radic: '√', times: '×', middot: '·' };
  function unesc(s) {
    return String(s || '').replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e) => {
      if (e[0] === '#') {
        const n = e[1] === 'x' || e[1] === 'X' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
        return Number.isFinite(n) ? String.fromCodePoint(n) : m;
      }
      return Object.prototype.hasOwnProperty.call(ENT, e.toLowerCase()) ? ENT[e.toLowerCase()] : m;
    });
  }
  function text(html) { return unesc(String(html || '').replace(/<wbr\s*\/?>/gi, '').replace(/<[^>]+>/g, '')).replace(/\s+/g, ' ').trim(); }
  function int(s) { const n = parseInt(String(s == null ? '' : s).replace(/[^\d-]/g, ''), 10); return Number.isFinite(n) ? n : null; }
  function sizeBytes(s) {
    const m = /([\d.,]+)\s*([KMGT]?)i?B/i.exec(unesc(String(s || '')).replace(/\s+/g, ' '));
    if (!m) return null;
    return Math.round(parseFloat(m[1].replace(',', '.')) * Math.pow(1024, ' KMGT'.indexOf((m[2] || ' ').toUpperCase())));
  }
  function titleYears(title) {
    const t = String(title || '');
    const m = /\[\s*((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)?\d{2}))?/.exec(t) || /\(((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)?\d{2}))?\)/.exec(t);
    if (!m) return null;
    const a = parseInt(m[1], 10);
    const b = m[2] ? parseInt(m[2].length === 2 ? m[1].slice(0, 2) + m[2] : m[2], 10) : a;
    return [Math.min(a, b), Math.max(a, b)];
  }

  // ---------------------------------------------------------------------------------------------- index: forum tree
  // keep in sync with films/parse/rustorka.py classify (Python decides the category; this only picks forums to walk)
  const NOT_VIDEO = /аниме|anime|игр|музык|аудио|книг|журнал|саундтрек|soundtrack|клип|концерт|трейлер|документал|научно|познават|спорт|футбол|хоккей|обучающ|видеокурс|обои|wallpaper|софт|программ|юмор|караоке|театр|мюзикл|балет|новост|обсужден|флуд|общени|шаблон|правила|магазин|заказ|pron|порно|эроти|xxx|\b3d\b|стерео|мобильн|техническ|\bpsp\b|android|iphone|ios\b/i;
  const RUSSIAN = /наш|русск|росси|отечеств|ссср|советск/i;
  const VIDEO = /кино|фильм|сериал|мульт|дорам|видео|\bhd\b|\bdvd|blu-?ray|remux|web-?dl|hevc|uhd|\b4k\b|2160|rip\b|telesync|camrip|\bts\b/i;
  function videoForum(f) {
    const path = [f.category, f.parent, f.name].filter(Boolean).join(' / ');
    if (!path || NOT_VIDEO.test(path)) return false;
    if (RUSSIAN.test(path) && !/мульт/i.test(path)) return false;   // cartoons of every country are collected
    return VIDEO.test(path);
  }
  function parseIndex(html) {
    html = String(html || '');
    const out = [];
    const seen = new Set();
    let category = null, parent = null;
    const re = /<a\b[^>]*href="(?:\.\/)?index\.php\?c=(\d+)"[^>]*>([\s\S]*?)<\/a>|<h4\b[^>]*class="forumlink"[^>]*>\s*<a\b[^>]*href="(?:\.\/)?viewforum\.php\?f=(\d+)"[^>]*>([\s\S]*?)<\/a>|<span\b[^>]*class="sf_title"[^>]*>[\s\S]*?<a\b[^>]*href="(?:\.\/)?viewforum\.php\?f=(\d+)"[^>]*>([\s\S]*?)<\/a>/gi;
    let m;
    while ((m = re.exec(html))) {
      if (m[1]) { category = text(m[2]); parent = null; continue; }
      if (m[3]) {
        parent = text(m[4]);
        if (!seen.has(m[3])) { seen.add(m[3]); out.push({ f: m[3], name: parent, parent: null, category }); }
        continue;
      }
      if (m[5] && !seen.has(m[5])) { seen.add(m[5]); out.push({ f: m[5], name: text(m[6]), parent, category }); }
    }
    return out;
  }

  // ---------------------------------------------------------------------------------------------- forum listing
  function parseListing(html) {
    html = String(html || '');
    const rows = [];
    const re = /<tr\b[^>]*\bid="tr-(\d+)"[^>]*>([\s\S]*?)<\/tr>/gi;
    let m;
    while ((m = re.exec(html))) {
      const t = m[1], body = m[2];
      const tl = /<a\b[^>]*href="(?:\.\/)?viewtopic\.php\?t=\d+"[^>]*class="(?:torTopic|topictitle)[^"]*"[^>]*>([\s\S]*?)<\/a>/i.exec(body);
      const sd = /class="seedmed"[^>]*>\s*<b>\s*(-?\d+)/i.exec(body);
      const lc = /class="leechmed"[^>]*>\s*<b>\s*(-?\d+)/i.exec(body);
      const sz = /<a\b[^>]*href="(?:\.\/)?download\.php\?id=\d+"[^>]*>([\s\S]*?)<\/a>/i.exec(body);
      const last = /<p>\s*((?:19|20)\d{2}-\d{2}-\d{2} \d{1,2}:\d{2})\s*<\/p>/.exec(body);
      rows.push({
        t, title: tl ? text(tl[1]) : null,
        seeds: sd ? Math.max(0, int(sd[1])) : null, leech: lc ? Math.max(0, int(lc[1])) : null,
        size: sz ? sizeBytes(sz[1]) : null, last: last ? last[1].replace(/ (\d):/, ' 0$1:') : null,
        sticky: /folder_(?:sticky|announce)/.test(body) ? 1 : 0,
      });
    }
    // topics per page: the page's own «К странице…» script (pg_start = (page_num - 1) * 50) — a user setting may differ
    const pp = /pg_start\s*=\s*\(page_num\s*-\s*1\)\s*\*\s*(\d+)/.exec(html);
    return { rows, perPage: pp ? parseInt(pp[1], 10) : PER_PAGE, empty: /В этом форуме пока нет сообщений/.test(html),
             missing: /Форума, который вы выбрали, не существует/.test(html) };
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
  function scrub(s) {   // magnet: btih only (a logged-in announce URL carries the user's passkey); key-like params cut
    return String(s || '').replace(/(magnet:\?xt=urn:btih:[0-9A-Za-z]+)[^"'\s<>]*/g, '$1')
      .replace(/([?&;](?:uk|passkey|pk|key|sid|hash)=)[^&"'\s<>]+/gi, '$1REDACTED');
  }
  function parseTopic(html) {
    html = String(html || '');
    const out = { title: null, forum: null, forum_id: null, forum_path: null, post_id: null, size_bytes: null,
                  btih: null, reged: null, post_html: '', missing: /Темы, которую вы запросили, не существует/.test(html) };
    let m = /<h1\b[^>]*class="bigtitle"[^>]*>([\s\S]*?)<\/h1>/i.exec(html);
    if (m) out.title = text(m[1]).replace(/^Скачать торрент\s+/, '');
    const main = html.search(/id="topic_main"/);
    const head = main >= 0 ? html.slice(0, main) : html;
    const crumbs = [...head.matchAll(/href="(?:\.\/)?viewforum\.php\?f=(\d+)"[^>]*>([^<]+)<\/a>/g)]
      .filter(c => !/^(?:1035|1135|1290|1975)$/.test(c[1]));   // header menu: templates, «Новинки», orders, FAQ
    if (crumbs.length) {
      out.forum_id = crumbs[crumbs.length - 1][1];
      out.forum = text(crumbs[crumbs.length - 1][2]);
      out.forum_path = [...new Set(crumbs.map(c => text(c[2])))].join(' / ');
    }
    m = /<tbody\b[^>]*\bid="post_(\d+)"/i.exec(html);
    if (m) out.post_id = m[1];
    const magnet = /magnet:\?xt=urn:btih:([0-9A-Fa-f]{40})([^"'\s<>]*)/.exec(html);
    if (magnet) {
      out.btih = magnet[1].toLowerCase();
      const xl = /[?&]xl=(\d+)/.exec(magnet[2].replace(/&amp;/g, '&'));
      if (xl) out.size_bytes = parseInt(xl[1], 10);
    }
    if (out.size_bytes == null) {
      m = /Размер:\s*<\/td>\s*<td[^>]*>([\s\S]*?)<\/td>/i.exec(html);
      if (m) out.size_bytes = sizeBytes(m[1]);
    }
    m = /Зарегистрирован:\s*<\/td>\s*<td[^>]*>\s*([^<]+?)\s*<\/td>/i.exec(html);
    if (m) out.reged = text(m[1]);
    const first = /<tbody\b[^>]*\bid="post_\d+"/i.exec(html);
    const pbRe = /<div\b[^>]*class="post_body"/gi;
    pbRe.lastIndex = first ? first.index : 0;
    const pb = pbRe.exec(html);
    if (pb) out.post_html = scrub(divBlock(html, pb.index));
    return out;
  }
  function pageProblem(html, status) {
    html = String(html || '');
    const loggedOut = /id="register_link"/.test(html) && !/logout/i.test(html);
    if (/cf-chl|challenge-platform|Just a moment|Один момент/i.test(html) && html.length < 60000) return 'cloudflare';
    if ((status === 403 || html.length < 20000) && /captcha|капч/i.test(html)) return 'captcha';
    if (loggedOut && /техническ\S*\s+(работ|перерыв|причин)|временно\s+недоступ|temporarily\s+unavailable|maintenance/i.test(html) &&
        !/viewtopic\.php\?t=/.test(html)) return 'maintenance';
    if (loggedOut) return 'not_logged_in';
    return null;
  }

  // ---------------------------------------------------------------------------------------------- selection
  function wantTopic(row, cfg, known, extra) {
    if (!row.t || !row.title) return false;
    if (known[row.t] === row.title) return false;                // page stored with this very title
    if (extra.has(row.t)) return true;
    const y = titleYears(row.title);
    return !!y && y[1] >= (cfg.yearFrom || 2024);
  }
  function newerThan(last, since) { return !since || !last || last >= since; }   // «YYYY-MM-DD HH:MM» strings

  // ---------------------------------------------------------------------------------------------- browser loop
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  function lsGet(k, d) { try { const v = root.localStorage && root.localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch (e) { return d; } }
  function lsSet(k, v) { try { root.localStorage.setItem(k, JSON.stringify(v)); return true; } catch (e) { return false; } }

  function makeRunner(cfg) {
    const S = root.__rk = {
      state: 'running', run: cfg.run || new Date().toISOString().slice(0, 19), started: new Date().toISOString(),
      fDone: 0, fTotal: 0, pages: 0, requests: 0, topics: 0, posted: 0, errors: 0, current: null, stop: false,
      lastError: null, lsFull: false,
    };
    root.__rkUnsent = root.__rkUnsent && root.__rkUnsent.length ? root.__rkUnsent : lsGet('rkc_unsent', []);
    let last = 0;
    const delay = cfg.delayMs || 1500;
    const bridge = cfg.bridge || BRIDGE;

    async function pause(reason) {
      const since = S.waitingSince || (S.waitingSince = Date.now());
      if (Date.now() - since > (cfg.waitHours || 12) * 3600e3) {
        const e = new Error(reason + ' (ждали ' + Math.round((Date.now() - since) / 60000) + ' мин)'); e.fatal = true; throw e;
      }
      S.state = 'waiting: ' + reason;
      try {
        await fetch(bridge.replace(/\/ingest\/rustorka$/, '/ingest/rustorka_status'), { method: 'POST',
          headers: { 'Content-Type': 'text/plain' },
          body: JSON.stringify({ v: 1, kind: 'rustorka_status', run: S.run, state: S.state, since: new Date(since).toISOString(),
                                 at: new Date().toISOString(), fDone: S.fDone, fTotal: S.fTotal, current: S.current }) });
      } catch (e) { /* bridge down: the state is still in __rkStatus() */ }
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
          const html = new TextDecoder('windows-1251').decode(await r.arrayBuffer());
          const p = pageProblem(html, r.status);
          if (p) {
            if (cfg.waitOnProblem === false) { const e = new Error(p); e.fatal = true; throw e; }
            await pause(p);
            a = -1;
            continue;
          }
          if (r.ok || r.status === 404) {             // 404 = «… не существует» page: the caller decides
            if (S.waitingSince) { S.waitingSince = null; S.state = 'running'; }
            return html;
          }
          err = new Error('HTTP ' + r.status);
        } catch (e) {
          if (e.fatal) throw e;
          err = e;
        }
        if (a >= 3) {                                  // 4 tries failed: the site is down → wait for it
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
        const r = await fetch(bridge, { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body });
        if (r.ok) { S.posted++; return true; }
      } catch (e) { /* bridge down: keep */ }
      root.__rkUnsent.push(body);
      if (!lsSet('rkc_unsent', root.__rkUnsent)) S.lsFull = true;
      return false;
    }

    async function topic(t, payload) {
      try {
        const html = await get(`${BASE}/viewtopic.php?t=${t}`);
        const tp = parseTopic(html);
        tp.topic_id = t;
        tp.fetched_at = new Date().toISOString();
        if (tp.missing) { payload.errors.push({ t, err: 'topic_missing' }); return; }
        if (!tp.post_html) payload.errors.push({ t, err: 'no_post_body', snippet: html.slice(0, 1500) });
        payload.topics.push(tp);
        S.topics++;
      } catch (e) {
        if (e.fatal) throw e;
        payload.errors.push({ t, err: String(e && e.message || e) });
      }
    }

    async function loop() {
      const known = cfg.known || {};
      const extra = new Set((cfg.extra || []).map(String));
      const opened = new Set();
      let fatal = null, seq = 0;
      const prog = lsGet('rkc_progress', {});
      try {
        // 1. forum tree
        let forums = null;
        for (let tries = 0; !forums; tries++) {
          const all = parseIndex(await get(`${BASE}/index.php`));
          const video = all.filter(videoForum);
          if (video.length) {
            forums = video;
            await send({ v: 1, kind: 'rustorka', run: S.run, type: 'forums', seq: seq++, forums: all, video: video.map(f => f.f),
                         at: new Date().toISOString() });
          } else {
            if (cfg.waitOnProblem === false || tries > 1000) throw Object.assign(new Error('no video forums on index.php'), { fatal: true });
            await pause('not_logged_in (no video forums on index.php)');
          }
        }
        if (Array.isArray(cfg.onlyForums) && cfg.onlyForums.length) {
          const only = new Set(cfg.onlyForums.map(String));
          forums = forums.filter(f => only.has(String(f.f)));
        }
        const startAt = Object.assign({}, cfg.startAt || {}, cfg.resume && prog.run === S.run ? prog.startAt || {} : {});
        const doneLs = cfg.resume && prog.run === S.run ? new Set(prog.done || []) : new Set();
        S.fTotal = forums.length;
        // 2. listings, newest activity first
        for (const f of forums) {
          if (S.stop) break;
          if (doneLs.has(String(f.f))) { S.fDone++; continue; }
          const since = (cfg.since || {})[f.f] || cfg.defaultSince || null;
          let start = startAt[f.f] || 0, pages = 0, newest = null, stopped = null;
          S.current = `${f.f} ${f.name}`;
          while (!S.stop && !stopped) {
            const html = await get(`${BASE}/viewforum.php?f=${f.f}${start ? '&start=' + start : ''}`);
            const p = parseListing(html);
            pages++; S.pages++;
            const payload = { v: 1, kind: 'rustorka', run: S.run, type: 'page', seq: seq++, forum: f, start, since,
                              rows: p.rows, topics: [], errors: [], at: new Date().toISOString() };
            if (p.missing) payload.errors.push({ f: f.f, err: 'forum_missing' });
            const fresh = p.rows.filter(r => !r.sticky && newerThan(r.last, since));
            for (const r of p.rows) {
              if (r.last && !r.sticky && (!newest || r.last > newest)) newest = r.last;
              if (S.stop || opened.has(r.t) || !newerThan(r.last, since) && !extra.has(r.t)) continue;
              if (!wantTopic(r, cfg, known, extra)) continue;
              opened.add(r.t);
              await topic(r.t, payload);
            }
            const plain = p.rows.filter(r => !r.sticky);
            if (p.missing || p.empty || p.rows.length < p.perPage) stopped = 'last page';
            else if (plain.length && !fresh.length) stopped = 'since';
            else if (pages >= (cfg.maxPages || 400)) stopped = 'max pages';
            if (stopped) payload.forumDone = { f: f.f, stopped, pages, newest, since };
            S.errors += payload.errors.length;
            start += p.perPage;
            payload.next = start;                          // where a resume continues this forum
            await send(payload);
            const pr = lsGet('rkc_progress', {});
            const st = pr.run === S.run ? pr : { run: S.run, startAt: {}, done: [] };
            if (stopped) st.done = [...new Set([...(st.done || []), String(f.f)])]; else st.startAt[f.f] = start;
            lsSet('rkc_progress', st);
          }
          if (stopped) S.fDone++;
        }
        // 3. extra topics the listings did not reach (older titles of the DB, asked by Python)
        const rest = [...extra].filter(t => !opened.has(t));
        for (let i = 0; i < rest.length && !S.stop; i += 20) {
          const payload = { v: 1, kind: 'rustorka', run: S.run, type: 'topics', seq: seq++, rows: [], topics: [], errors: [],
                            at: new Date().toISOString() };
          for (const t of rest.slice(i, i + 20)) { if (S.stop) break; opened.add(t); await topic(t, payload); }
          S.errors += payload.errors.length;
          await send(payload);
        }
      } catch (e) {
        S.lastError = String(e && e.message || e);
        if (e.fatal) fatal = S.lastError; else fatal = 'crashed: ' + S.lastError;
      }
      await flush();
      await send({ v: 1, kind: 'rustorka', run: S.run, final: true, fatal, stats: Object.assign({}, S, { stop: undefined }) });
      S.state = fatal ? 'stopped: ' + fatal : S.stop ? 'stopped by __rkStop' : 'done';
      S.current = null;
    }

    async function flush() {
      const left = [];
      for (const body of root.__rkUnsent.splice(0)) {
        try {
          const r = await fetch(bridge, { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body });
          if (r.ok) { S.posted++; continue; }
        } catch (e) { /* still down */ }
        left.push(body);
      }
      root.__rkUnsent.push(...left);
      lsSet('rkc_unsent', root.__rkUnsent);
      return left.length;
    }

    return { S, loop, flush };
  }

  function start(cfg) {
    cfg = cfg || {};
    if (root.__rk && /^(running|loading|waiting)/.test(root.__rk.state)) return 'already running: ' + JSON.stringify(status());
    const go = c => {
      const r = makeRunner(c);
      root.__rkRunner = r;
      r.loop().catch(e => { r.S.state = 'crashed: ' + (e && e.message || e); });
      return r;
    };
    if (!cfg.queriesUrl) {
      const r = go(cfg);
      return `started run ${r.S.run}; results -> ${cfg.bridge || BRIDGE}`;
    }
    root.__rk = { state: 'loading job from ' + cfg.queriesUrl, fDone: 0, fTotal: 0 };
    fetch(cfg.queriesUrl, { cache: 'no-store' })
      .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(loaded => go(Object.assign({}, loaded, cfg)))
      .catch(e => { root.__rk = { state: 'no job: ' + (e && e.message || e) + ' — bridge down or blocked', fDone: 0, fTotal: 0 }; });
    return 'loading job from ' + cfg.queriesUrl + ' — check with __rkStatus() or watch data/cache/ingest/rustorka-*.txt';
  }
  function status() {
    const S = root.__rk;
    if (!S) return 'not started';
    return { state: S.state, run: S.run, forums: `${S.fDone}/${S.fTotal}`, current: S.current, pages: S.pages,
             requests: S.requests, topics: S.topics, posted: S.posted, unsent: (root.__rkUnsent || []).length,
             errors: S.errors, lastError: S.lastError, lsFull: S.lsFull };
  }
  function chunk(i, size) {
    const all = JSON.stringify(root.__rkUnsent && root.__rkUnsent.length ? root.__rkUnsent : lsGet('rkc_unsent', []));
    size = size || 400000;
    return { i, n: Math.ceil(all.length / size), data: all.slice(i * size, (i + 1) * size) };
  }

  const api = { parseIndex, parseListing, parseTopic, pageProblem, videoForum, wantTopic, titleYears, sizeBytes, scrub,
                divBlock, unesc };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof root.document !== 'undefined') {
    root.__rkCollect = start;
    root.__rkStatus = status;
    root.__rkStop = () => { if (root.__rk) root.__rk.stop = true; return status(); };
    root.__rkFlush = () => (root.__rkRunner ? root.__rkRunner.flush() : Promise.resolve('no runner'));
    root.__rkChunk = chunk;
    root.__rkApi = api;
  }
})(typeof window !== 'undefined' ? window : globalThis);
