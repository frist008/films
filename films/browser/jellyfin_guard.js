// Jellyfin «видел» through the logged-in Jellyfin tab, without an API key (owner, 2026-10-26: «Никакого ключа у тебя есть
// доступ в хром чтобы всё делать и там скрипты гонять если надо»). Same rule as films/sources/jellyfin_guard.py and
// plex_guard.py: an item Jellyfin already has as played is NEVER marked again (a second mark rewrites its «last played»
// date and breaks the owner's history); nothing is ever un-marked — this file has no such call.
// The tab's own ApiClient (jellyfin-web) does the requests as the signed-in user, who must be the owner's Jellyfin user.
// Loaded verbatim into a http://127.0.0.1:8096 / localhost:8096 tab by javascript_tool (text from
// `python -X utf8 -m films.sources.jellyfin_chrome code …`); .claude/hooks/jellyfin-chrome-guard.sh blocks any other JS
// that writes played state. Usage: await __jfGuard.mark(["<item id>", …], "<jellyfin user>").
window.__jfGuard = (() => {
  const played = async (c, uid, id) => !!(((await c.getItem(uid, id)) || {}).UserData || {}).Played;
  async function mark(items, user) {
    const c = window.ApiClient;
    if (!c) return {error: 'no ApiClient: not a Jellyfin tab or not signed in'};
    const me = await c.getCurrentUser();
    if (!me || me.Name !== user) return {error: 'wrong user', user: me ? me.Name : null};
    const out = [];
    for (const id of items) {
      try {
        if (await played(c, me.Id, id)) { out.push({item_id: id, already: true}); continue; }
        await c.markPlayed(me.Id, id, new Date());
        out.push({item_id: id, played: await played(c, me.Id, id)});
      } catch (e) {
        out.push({item_id: id, error: String(e && e.message || e).slice(0, 200)});
      }
    }
    return out;
  }
  return {mark};
})();
