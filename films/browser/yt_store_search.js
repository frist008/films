// YouTube store search for watched films (R94 / store-sync step 3, saved 2026-10-25): read only, nothing is bought.
// Load into a youtube.com tab (owner's account), then: __ytStore.run(items) once — items = [[id, orig, latin, ru, uk, year,
// runtime], …] (data/cache/stores/store_todo_<date>.json). Progress in window.__ytRun and localStorage 'ytStoreRun'
// (after a reload: __ytStore.run() resumes). Output (read out in chunks with __ytStore.chunk(n)):
//   lines: `id|vid,S,langs,buy,buy_old,rent,yt_year,acquired,e|y,minutes;…` → python -X utf8 -m films.store_watch lines <f> <todo ids>
//   buy:   `vid langs uhd:price[:old] hd:… sd:…`                         → python -X utf8 -m films.store_watch buy <f>
// Search: youtubei/v1/search with the «Movies» filter (EgIQBA==), orig + latin with hl=en, ru with hl=ru, uk with hl=uk
// (the listing language follows hl). Candidate: exact title (normalised like store_watch._norm) and year ±1 or a later
// year (re-release, checked by runtime in ingest), or same year only + runtime ±5 min. Watch page: playability
// (OK = playable: owned if «Приобретено», else free; «платное» = for sale), «Языки», «Приобретено»; ypc/get_offers prices.
window.__ytStore = (() => {
  // Chrome throttles a hidden tab's timers to one wake-up a minute, and youtube.com's CSP blocks Blob Workers (the trick of
  // gtv_loop.js fails here, 2026-10-25) → in a hidden tab the pause is a tiny same-origin request instead of a timer
  const sleep = ms => document.hidden ? fetch('/generate_204', {credentials: 'omit'}).then(() => {}, () => {}) : new Promise(r => setTimeout(r, ms));
  const auth = async () => { const sap = document.cookie.match(/(?:^|; )SAPISID=([^;]+)/)[1];
    const ts = Math.floor(Date.now() / 1000); const s = `${ts} ${sap} https://www.youtube.com`;
    const h = [...new Uint8Array(await crypto.subtle.digest('SHA-1', new TextEncoder().encode(s)))].map(b => b.toString(16).padStart(2, '0')).join('');
    return `SAPISIDHASH ${ts}_${h} SAPISID1PHASH ${ts}_${h} SAPISID3PHASH ${ts}_${h}`; };
  const yti = async (path, body, hl) => { const cfg = ytcfg.data_;
    const ctx = structuredClone(cfg.INNERTUBE_CONTEXT); if (hl) ctx.client.hl = hl;
    const r = await fetch(`/youtubei/v1/${path}?prettyPrint=false`, {method: 'POST', credentials: 'include',
      headers: {'content-type': 'application/json', authorization: await auth(), 'x-origin': 'https://www.youtube.com',
        'x-goog-authuser': String(cfg.SESSION_INDEX || 0), 'x-youtube-client-name': String(cfg.INNERTUBE_CONTEXT_CLIENT_NAME),
        'x-youtube-client-version': cfg.INNERTUBE_CLIENT_VERSION},
      body: JSON.stringify({context: ctx, ...body})});
    if (!r.ok) throw new Error('http ' + r.status + ' ' + path);
    return r.json(); };
  const norm = s => (s || '').toLowerCase().replace(/ё/g, 'е').replace(/^007:\s*/, '')
    .replace(/[«»"'’`:;,.!?()\[\]\-–—·½⅓]/g, ' ').replace(/\s+/g, ' ').trim();
  const find = (o, key, out = []) => { if (o && typeof o === 'object') { for (const k in o) { if (k === key) out.push(o[k]); else find(o[k], key, out); } } return out; };
  const txt = x => !x ? '' : x.simpleText || (x.runs || []).map(r => r.text).join('');
  const mins = s => { const p = (s || '').split(':').map(Number); return p.length === 3 ? p[0] * 60 + p[1] : p.length === 2 ? p[0] : 0; };
  const LANG = {'русский': 'ru', 'украинский': 'uk', 'английский': 'en'};
  const price = s => { const m = (s || '').match(/[\d\s ]+,\d\d/g) || []; return m.map(x => Math.round(parseFloat(x.replace(/[\s ]/g, '').replace(',', '.')))); };

  async function search(q, hl) {
    const s = await yti('search', {query: q, params: 'EgIQBA=='}, hl);
    return find(s, 'movieRenderer').map(r => ({vid: r.videoId, t: txt(r.title), y: +(((r.topMetadataItems || []).map(txt).join(' ').match(/\b(19|20)\d\d\b/) || [])[0] || 0),
      m: mins(txt(r.lengthText))}));
  }
  async function watch(vid) {
    const h = await (await fetch('/watch?v=' + vid + '&hl=ru', {credentials: 'include'})).text();
    const pm = h.match(/"playabilityStatus":\{"status":"([A-Z_]+)"(?:,"reason":"([^"]*)")?/);
    const st = pm ? pm[1] : '?', reason = pm && pm[2] || '';
    const rows = {};
    for (const m of h.matchAll(/"metadataRowRenderer":\{"title":\{(?:"simpleText":"([^"]+)"|"runs":\[\{"text":"([^"]+)"\}\])\},"contents":\[(.*?)\],"trackingParams"/g)) {
      const k = m[1] || m[2]; const v = [...m[3].matchAll(/"(?:simpleText|text)":"([^"]*)"/g)].map(x => x[1]).join(', ');
      rows[k] = v;
    }
    const pp = h.match(/"ypcOffersEndpoint":\{"params":"([^"]+)"/);
    return {st, reason, langs: rows['Языки'] || '', got: rows['Приобретено'] || '', date: rows['Дата выхода'] || '', itemParams: pp ? JSON.parse('"' + pp[1] + '"') : null};
  }
  async function offers(itemParams) {
    const o = await yti('ypc/get_offers', {itemParams});
    const out = {buy: {}, rent: {}};
    for (const sec of find(o, 'offerSectionRenderer')) {
      const kind = /Купить|Buy/i.test(txt(sec.title)) ? 'buy' : 'rent';
      for (const c of find(sec, 'offerContentRenderer')) {
        const q = txt(c.title); const qq = /Ультра|Ultra/i.test(q) ? 'uhd' : /Высок|High/i.test(q) ? 'hd' : /Станд|Standard/i.test(q) ? 'sd' : 'x';
        const p = price(find(c, 'buttonRenderer').map(b => txt(b.text)).join(' '));
        if (p.length) out[kind][qq] = p.length > 1 && p[0] > p[p.length - 1] ? {p: p[p.length - 1], old: p[0]} : {p: p[0]};
      }
    }
    return out;
  }
  const similar = (a, b) => { if (!a || !b) return false; if (a.includes(b) || b.includes(a)) return true;
    const A = new Set(a.split(' ')), B = new Set(b.split(' ')); let n = 0; A.forEach(x => B.has(x) && n++); return n / Math.max(A.size, B.size) >= 0.5; };
  const best = d => d.uhd || d.hd || d.sd || d.x || null;
  const langCodes = s => [...new Set((s || '').split(/,\s*/).filter(Boolean).map(x => LANG[x.split(' (')[0].trim().toLowerCase()] || 'x'))].sort().join('+');

  async function one(item) {
    const [id, orig, latin, ru, uk, year, rt] = item;
    const names = new Set([orig, latin, ru, uk].filter(Boolean).map(norm));
    const qs = [[orig, 'en'], [latin, 'en'], [ru, 'ru'], [uk, 'uk']].filter(x => x[0]);
    const cands = new Map();
    for (const [q, hl] of qs) {
      let res = [];
      try { res = await search(q, hl); } catch (e) { __ytRun.err = String(e); await sleep(5000); try { res = await search(q, hl); } catch (e2) { throw e2; } }
      for (const r of res.slice(0, 12)) {
        const e = names.has(norm(r.t));
        const dy = r.y && year ? Math.abs(r.y - year) : null, dd = r.m && rt ? Math.abs(r.m - rt) : null;
        // year-only candidates must share words with a name (2026-10-25: «Spy Unrated» 2015 ≈ «Bridge of Spies» 2015 by runtime)
        const near = [...names].some(n => similar(n, norm(r.t)));
        const ok = e ? (dy !== null && (dy <= 1 || r.y > year)) : (near && dy === 0 && (dd === null || dd <= 5));
        if (ok && !cands.has(r.vid)) cands.set(r.vid, {...r, how: e ? 'e' : 'y'});
      }
      await sleep(700);
    }
    const parts = [], buys = [];
    for (const c of cands.values()) {
      let w; try { w = await watch(c.vid); } catch (e) { continue; }
      await sleep(500);
      const S = w.st === 'OK' ? 'O' : /платное|paid/i.test(w.reason) ? 'P' : 'N';
      let of = {buy: {}, rent: {}};
      if (S === 'P' && w.itemParams) { try { of = await offers(w.itemParams); } catch (e) {} await sleep(500); }
      const b = best(of.buy), r = best(of.rent), lc = langCodes(w.langs);
      parts.push([c.vid, S, lc, b ? b.p : '', b && b.old ? b.old : '', r ? r.p : '', c.y || '', w.got ? 1 : 0, c.how, c.m || ''].join(','));
      if (Object.keys(of.buy).length) buys.push([c.vid, lc || '-', ...['uhd', 'hd', 'sd'].filter(k => of.buy[k]).map(k => k + ':' + of.buy[k].p + (of.buy[k].old ? ':' + of.buy[k].old : ''))].join(' '));
    }
    return {line: parts.length ? id + '|' + parts.join(';') : '', buys, n: cands.size};
  }

  function save() { try { localStorage.setItem('ytStoreRun', JSON.stringify({res: __ytRun.res, i: __ytRun.i})); } catch (e) {} }
  function run(items) {
    if (!items) { try { items = JSON.parse(localStorage.getItem('ytStoreItems') || 'null'); } catch (e) {} }
    if (!items) return 'no items';
    try { localStorage.setItem('ytStoreItems', JSON.stringify(items)); } catch (e) {}
    if (window.__ytRun && !window.__ytRun.done) return 'refused: running';
    let res = {}; try { res = JSON.parse(localStorage.getItem('ytStoreRun') || '{}').res || {}; } catch (e) {}
    window.__ytRun = {started: new Date().toISOString(), total: items.length, i: 0, res, errs: 0};
    (async () => {
      for (let i = 0; i < items.length; i++) {
        __ytRun.i = i; const id = items[i][0];
        if (__ytRun.res[id]) continue;
        try { __ytRun.res[id] = await one(items[i]); } catch (e) { __ytRun.errs++; __ytRun.lastErr = String(e); await new Promise(r => setTimeout(r, 60000)); i--; if (__ytRun.errs > 30) { __ytRun.stopped = __ytRun.lastErr; save(); return; } continue; }
        save();
      }
      __ytRun.done = new Date().toISOString(); save();
    })();
    return 'started ' + items.length;
  }
  const status = () => { const r = window.__ytRun || {}; const v = Object.values(r.res || {}); return {i: r.i, total: r.total, done: r.done, stopped: r.stopped, errs: r.errs, lastErr: r.lastErr, processed: v.length, withCand: v.filter(x => x.line).length}; };
  const text = () => { const v = Object.values((window.__ytRun || {}).res || {}); return v.map(x => x.line).filter(Boolean).join('\n') + '\n#BUY\n' + v.flatMap(x => x.buys).join('\n'); };
  const chunk = (n, size = 900) => text().slice(n * size, (n + 1) * size);
  return {run, status, text, chunk, one, version: 2};
})();
'yt_store_search loaded';
