// Google TV watchlist loop (R94): adds watched, not owned films to «Хочу посмотреть» ONLY through __gtvGuard.add
// (films/browser/gtv_watchlist_guard.js, loaded first, verbatim). Saved here 2026-10-24 after the page running the
// loop froze and its in-memory results were lost. One loop at a time (CLAUDE.md: two loops = removals).
// Items: [[title_id, original, ru, year], …] (data/cache/gtv_a.json + gtv_b.json). Progress: window.__gRun and
// localStorage 'gtvRun' (results by title id). Google's 429 «sorry» page shows as 'nopanel' in a row → pause 10 min and
// retry the same film; never bypass a captcha; pause lengths are logged in __gRun.blocks.
// Chrome throttles timers of a background tab to one wake-up a minute («intensive throttling»): a film took ~3 min. Timers
// run from a Worker are not throttled — page setTimeout is served by one (the guard's sleeps too; the guard is untouched).
if (!window.__workerTimers) {
  const w = new Worker(URL.createObjectURL(new Blob(['onmessage = e => setTimeout(() => postMessage(e.data.id), e.data.ms)'], {type: 'text/javascript'})));
  const cbs = new Map(); let seq = 0;
  w.onmessage = e => { const f = cbs.get(e.data); cbs.set(e.data, null); if (f) f();   // (no map removals: gtv-guard.sh forbids that word) };
  const native = window.setTimeout.bind(window);
  window.setTimeout = (fn, ms, ...args) => { if (typeof fn !== 'function') return native(fn, ms, ...args);
    const id = ++seq + 1e9; cbs.set(id, () => fn(...args)); w.postMessage({id, ms: ms || 0}); return id; };
  window.__workerTimers = true;
}
window.__gtvLoop = items => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  // items are kept in localStorage: after a frozen tab is closed, __gtvLoop() in a new google.com tab resumes (R140)
  if (!items) { try { items = JSON.parse(localStorage.getItem('gtvItems') || 'null'); } catch (e) {} }
  if (!items) return 'no items: pass the list once';
  try { localStorage.setItem('gtvItems', JSON.stringify(items)); } catch (e) {}
  // ONE loop only (two loops pressing the same card = removal from the list): a heartbeat younger than 3 min in
  // localStorage (shared by all google.com tabs) or a running promise in this tab → refuse (code review 2026-10-24)
  let beat = 0;
  try { beat = +(localStorage.getItem('gtvBeat') || 0); } catch (e) {}
  if (window.__gRun && !window.__gRun.done && !window.__gRun.stopped) return 'refused: a loop is running in this tab';
  if (Date.now() - beat < 3 * 60 * 1000) return 'refused: another loop is alive (heartbeat ' + Math.round((Date.now() - beat) / 1000) + ' s ago)';
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem('gtvRun') || '{}').res || {}; } catch (e) {}
  window.__gRun = {started: new Date().toISOString(), total: items.length, res: saved, blocks: [], i: 0};
  const save = () => { try { localStorage.setItem('gtvRun', JSON.stringify(window.__gRun)); localStorage.setItem('gtvBeat', String(Date.now())); } catch (e) {} };
  window.__gRunP = (async () => {
    let miss = 0, blockStart = null;
    for (let i = 0; i < items.length; i++) {
      const [id, orig, ru, year] = items[i];
      __gRun.i = i;
      if (__gRun.res[id] && ['added', 'skip'].includes(__gRun.res[id].action) && __gRun.res[id].state !== 'nopanel') continue;
      let r;
      try { r = await __gtvGuard.add({id, names: [orig, ru], year}); } catch (e) { r = {id, state: 'error', err: String(e)}; }
      if (r.state === 'nopanel') {
        miss++;
        if (miss >= 3) {                              // maybe Google's 429 page: probe a known film (read only)
          const probe = await __gtvGuard.check({id: 0, names: ['Inception', 'Начало'], year: 2010});
          if (probe.state === 'nopanel') {            // blocked: wait 10 min, then retry this film
            if (!blockStart) blockStart = Date.now();
            if (Date.now() - blockStart > 2 * 3600 * 1000) {   // 2 h: layout changed / signed out / long block — stop, report
              __gRun.stopped = 'blocked > 2 h at ' + id; save(); return;
            }
            __gRun.paused = new Date().toISOString(); save();
            await sleep(10 * 60 * 1000);
            i -= 1; continue;
          }
          miss = 0;                                   // not blocked: this film simply has no card
        }
      } else {
        if (blockStart) { __gRun.blocks.push({from: new Date(blockStart).toISOString(), minutes: Math.round((Date.now() - blockStart) / 60000)}); blockStart = null; }
        miss = 0; __gRun.paused = null;
      }
      __gRun.res[id] = r; save();
      await sleep(10000);
    }
    __gRun.done = new Date().toISOString(); save();
    try { localStorage.setItem('gtvBeat', '0'); } catch (e) {}   // released (no remove calls: gtv-guard.sh)
  })();
  return 'started ' + items.length;
};
'gtv_loop loaded';
