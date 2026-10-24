// Google TV watchlist guard (owner, 2026-10-24): «не надо, как в Плексе, сбрасывать то, что было добавлено по дате».
// The only allowed way to add films to the owner's Google TV watchlist: the «Хочу посмотреть» button of the film card in
// Google Search (google.com, same-origin iframe). The button TOGGLES: pressing it on a film already in the list REMOVES it
// and loses its original «added» date. So:
//   * the button is pressed only when aria-pressed === "false" (not in the list) — never when "true";
//   * the card must be the right film: title similar to one of the item's names and the card's year = item year ±1;
//   * after the press the card is reloaded and must show aria-pressed === "true" (verified), otherwise it is reported;
//   * nothing is ever removed, re-added or marked «Просмотрено» here.
// Loaded verbatim into a google.com tab by javascript_tool; .claude/hooks/gtv-guard.sh blocks any other JS/clicks
// that touch the watchlist button. Usage: await __gtvGuard.check(item) / await __gtvGuard.add(item),
// item = {id, names: [orig, ru, uk], year}.
window.__gtvGuard = (() => {
  const norm = s => (s || '').toLowerCase().replace(/ё/g, 'е').replace(/[«»"'’`:;,.!?()\[\]\-–—·]/g, ' ').replace(/\s+/g, ' ').trim();
  const sim = (a, b) => { a = norm(a); b = norm(b); if (!a || !b) return 0; if (a === b) return 1; if (a.includes(b) || b.includes(a)) return 0.8;
    const A = new Set(a.split(' ')), B = new Set(b.split(' ')); let n = 0; A.forEach(x => B.has(x) && n++); return n / Math.max(A.size, B.size); };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const load = async q => {
    const f = document.createElement('iframe');
    f.style.cssText = 'width:1200px;height:900px;position:fixed;left:-3000px;top:0';
    document.body.appendChild(f);
    await new Promise(r => { f.onload = r; f.src = '/search?hl=ru&q=' + encodeURIComponent(q); setTimeout(r, 20000); });
    await sleep(800);
    return f;
  };
  const read = f => {
    const doc = f.contentDocument;
    const btn = [...doc.querySelectorAll('[role=button]')].find(x => /Хочу посмотреть/.test(x.getAttribute('aria-label') || ''));
    const title = (doc.querySelector('[data-attrid="title"]') || {}).innerText || '';
    const sub = (doc.querySelector('[data-attrid="subtitle"]') || {}).innerText || '';
    return {btn, title, sub, pressed: btn ? btn.getAttribute('aria-pressed') : null};
  };
  const matches = (item, r) => {
    const y = +((r.sub.match(/\b(19|20)\d\d\b/) || [])[0] || 0);
    const s = Math.max(...item.names.filter(Boolean).map(n => sim(n, r.title)));
    return {ok: s >= 0.6 && y && Math.abs(y - item.year) <= 1, s, y};
  };
  const queries = item => [...new Set(item.names.filter(Boolean).map(n => `${n} ${item.year} фильм`))];
  async function check(item) {
    for (const q of queries(item)) {
      const f = await load(q);
      try {
        const r = read(f); const m = matches(item, r);
        if (r.btn && m.ok) return {id: item.id, q, state: r.pressed === 'true' ? 'in' : r.pressed === 'false' ? 'out' : 'unknown', title: r.title, sub: r.sub};
      } finally { f.remove(); }
    }
    return {id: item.id, state: 'nopanel'};
  }
  async function add(item) {
    const c = await check(item);
    if (c.state !== 'out') return {...c, action: 'skip'};          // in the list already / no card / unclear → never touch
    const f = await load(c.q);
    try {
      const r = read(f); const m = matches(item, r);
      if (!r.btn || !m.ok || r.pressed !== 'false') return {...c, action: 'skip', why: 'state changed'};
      r.btn.click();
      await sleep(2500);
    } finally { f.remove(); }
    const v = await check(item);
    return {...c, action: 'added', verified: v.state === 'in', after: v.state};
  }
  return {check, add, version: 1};
})();
