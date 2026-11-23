// YouTube trailer playlists ← data/cache/playlists_desired.js (window.__want), run on a www.youtube.com tab of the owner.
// Saved here (2026-10-24) because the helpers lived only in the page and a reload lost a half-done sync.
// Load this file, then the desired list (hook-checked), then: __plRunAll({key: playlistId, …}) — ids from config/private.toml.
// Progress: window.__plRun (and localStorage 'plRun'); every playlist ends with a read-back check (orderOk, notAdded, hidden).
window.__auth = async () => { const sap = document.cookie.match(/(?:^|; )SAPISID=([^;]+)/)[1];
  const ts = Math.floor(Date.now() / 1000); const s = `${ts} ${sap} https://www.youtube.com`;
  const h = [...new Uint8Array(await crypto.subtle.digest('SHA-1', new TextEncoder().encode(s)))].map(b => b.toString(16).padStart(2, '0')).join('');
  return `SAPISIDHASH ${ts}_${h} SAPISID1PHASH ${ts}_${h} SAPISID3PHASH ${ts}_${h}`; };
window.__yti = async (path, body) => { const cfg = ytcfg.data_; const ctx = structuredClone(cfg.INNERTUBE_CONTEXT);
  const r = await fetch(`/youtubei/v1/${path}?prettyPrint=false`, {method: 'POST', credentials: 'include',
    headers: {'content-type': 'application/json', authorization: await __auth(), 'x-origin': 'https://www.youtube.com',
              'x-goog-authuser': String(cfg.SESSION_INDEX || 0), 'x-youtube-client-name': String(cfg.INNERTUBE_CONTEXT_CLIENT_NAME),
              'x-youtube-client-version': cfg.INNERTUBE_CLIENT_VERSION}, body: JSON.stringify({context: ctx, ...body})});
  return r.json(); };
window.__walk = (o, key, out = []) => { if (o && typeof o === 'object') { if (o[key]) out.push(o[key]); for (const k in o) __walk(o[k], key, out); } return out; };
window.__sleep = ms => new Promise(x => setTimeout(x, ms));
// params 'wgYCCAA%3D' also returns the videos YouTube hides as unavailable
window.__plRead = async (pid, params) => { let b = await __yti('browse', params ? {browseId: 'VL' + pid, params} : {browseId: 'VL' + pid});
  let it = __walk(b, 'playlistVideoRenderer');
  for (let i = 0; i < 30; i++) { const tok = (__walk(b, 'continuationCommand')[0] || {}).token; if (!tok) break;
    b = await __yti('browse', {continuation: tok}); it = it.concat(__walk(b, 'playlistVideoRenderer')); }
  return it.map(v => [v.videoId, v.setVideoId]); };
window.__plEdit = (pid, actions) => __yti('browse/edit_playlist', {playlistId: pid, actions});
window.__plSync = async (pid, want) => {
  let cur = await __plRead(pid, 'wgYCCAA%3D'); const ws = new Set(want);
  const once = new Set();   // a video twice in the playlist: the extra copies go too
  const drop = cur.filter(([v]) => !ws.has(v) || (once.has(v) ? true : (once.add(v), false)))
    .map(([v, s]) => ({action: 'ACTION_REMOVE_VIDEO', setVideoId: s}));
  for (let i = 0; i < drop.length; i += 50) await __plEdit(pid, drop.slice(i, i + 50));
  const have = new Set(cur.map(x => x[0])); const add = want.filter(v => !have.has(v)).map(v => ({action: 'ACTION_ADD_VIDEO', addedVideoId: v}));
  for (let i = 0; i < add.length; i += 50) await __plEdit(pid, add.slice(i, i + 50));
  if (drop.length || add.length) await __sleep(6000);   // reads right after an edit return the old state
  cur = await __plRead(pid); const sid = Object.fromEntries(cur.map(([v, s]) => [v, s])); const order = want.filter(v => sid[v]);
  let moves = 0;
  if (cur.map(x => x[0]).join() !== order.join()) {
    for (let i = order.length - 2; i >= 0; i--) {
      await __plEdit(pid, [{action: 'ACTION_MOVE_VIDEO_BEFORE', setVideoId: sid[order[i]], movedSetVideoIdSuccessor: sid[order[i + 1]]}]); moves++;
      if (window.__plRun) window.__plRun.moving = `${pid}: ${moves}/${order.length - 1}`; } }
  await __sleep(6000);
  const all = await __plRead(pid, 'wgYCCAA%3D'); const vis = new Set((await __plRead(pid)).map(x => x[0]));
  const hidden = all.filter(([v]) => !vis.has(v));
  if (hidden.length) { await __plEdit(pid, hidden.map(([v, s]) => ({action: 'ACTION_REMOVE_VIDEO', setVideoId: s}))); await __sleep(6000); }
  const fin = (await __plRead(pid)).map(x => x[0]);
  return {removed: drop.length, added: add.length, moves, final: fin.length, want: want.length, hiddenRemoved: hidden.map(x => x[0]),
          notAdded: want.filter(v => !fin.includes(v)), extra: fin.filter(v => !ws.has(v)), orderOk: fin.join() === want.filter(v => fin.includes(v)).join()}; };
window.__plRunAll = ids => { window.__plRun = {started: new Date().toISOString(), res: {}};
  const save = () => { try { localStorage.setItem('plRun', JSON.stringify(window.__plRun)); } catch (e) {} };
  window.__plRunP = (async () => { for (const k of Object.keys(ids)) {
    window.__plRun.current = k; save();
    // a key missing from __want must not empty that playlist (code review 2026-10-24)
    if (!Array.isArray(__want[k])) { window.__plRun.res[k] = {err: 'no list in __want — skipped'}; continue; }
    try { window.__plRun.res[k] = await __plSync(ids[k], __want[k]); } catch (e) { window.__plRun.res[k] = {err: String(e)}; }
    save(); }
    window.__plRun.current = null; window.__plRun.done = new Date().toISOString(); save(); })();
  return 'started'; };
'playlist_sync loaded';
