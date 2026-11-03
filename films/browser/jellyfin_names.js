// Russian names of films, series and seasons in Jellyfin through the logged-in Jellyfin tab, no API key (owner,
// 2026-11-01: «убедиться что в джелефине все названия фильмов на русском языке … Тоже самое для сезонов сериалов …
// Название серий не трогай»; «Сразу подтверждение каждый сериал и фильм»; Chrome instead of a key — owner, 2026-10-26).
// Names only: sets Name, locks it (LockedFields «Name», so a metadata refresh keeps it) and reads the item back.
// Refuses an episode, any other type, and an item whose name is no longer the planned one. Never touches played state.
// Text from `python -X utf8 -m films.sources.jellyfin_names code` (it ends with `await …`, so the result comes back to
// javascript_tool); usage: await __jfNames.rename([{id, kind, old, new}], user).
window.__jfNames = (() => {
  const TYPES = {movie: 'Movie', series: 'Series', season: 'Season'};
  async function rename(items, user) {
    const c = window.ApiClient;
    if (!c) return {error: 'no ApiClient: not a Jellyfin tab or not signed in'};
    const me = await c.getCurrentUser();
    if (!me || me.Name !== user) return {error: 'wrong user', user: me ? me.Name : null};
    const out = [];
    for (const x of items) {
      const r = {item_id: x.id, old: x.old, new: x.new};
      try {
        const it = await c.getItem(me.Id, x.id);
        if (!TYPES[x.kind] || it.Type !== TYPES[x.kind] || it.Name !== x.old) {
          out.push({...r, error: `changed since the plan: ${it.Type} «${it.Name}»`});
          continue;
        }
        // Read-only; Jellyfin cannot read its own Trickplay DTO back (HTTP 500 «TrickplayInfoDto … deserialization»,
        // 2026-11-01 on items with trickplay previews). The previews live apart and are not changed by this update.
        delete it.Trickplay;
        it.Name = x.new;
        it.LockedFields = [...new Set([...(it.LockedFields || []), 'Name'])];
        await c.updateItem(it);
        const back = await c.getItem(me.Id, x.id);
        out.push({...r, now: back.Name, confirmed: back.Name === x.new && (back.LockedFields || []).includes('Name')});
      } catch (e) {
        const why = e instanceof Response ? `HTTP ${e.status} ${(await e.text().catch(() => '')).slice(0, 300)}` : String(e && e.message || e);
        out.push({...r, error: why.slice(0, 400)});
      }
    }
    return out;
  }

  // Re-identify a film Jellyfin matched to the wrong one (owner, 2026-11-02: «Да, перепривязать» — «Луна» 2023 was bound
  // to a Philippine short). Only a Movie at the expected path with the expected wrong IMDb id; the search goes by the right
  // TMDB / IMDb id, the result with that TMDB id is applied (Identify in the web UI), then the item is polled until the
  // new ids land. The locked Name stays; the caller checks it.
  async function rebind(x, user) {
    const c = window.ApiClient;
    if (!c) return {error: 'no ApiClient: not a Jellyfin tab or not signed in'};
    const me = await c.getCurrentUser();
    if (!me || me.Name !== user) return {error: 'wrong user', user: me ? me.Name : null};
    const r = {item_id: x.id};
    try {
      const it = await c.getItem(me.Id, x.id);
      const ids = it.ProviderIds || {};
      if (it.Type !== 'Movie' || !(it.Path || '').endsWith(x.file) || (ids.Imdb || '') !== (x.wrong_imdb || ''))
        return {...r, error: `changed since the plan: ${it.Type} «${it.Name}» ${JSON.stringify(ids)}`};
      const post = (path, body, params) => c.ajax({type: 'POST', url: c.getUrl(path, params), data: JSON.stringify(body),
                                                    contentType: 'application/json', dataType: path.includes('Apply') ? undefined : 'json'});
      const found = await post('Items/RemoteSearch/Movie',
                               {ItemId: x.id, SearchInfo: {ProviderIds: {Tmdb: String(x.tmdb), Imdb: x.imdb}}});
      const hit = (found || []).find(f => String((f.ProviderIds || {}).Tmdb) === String(x.tmdb));
      if (!hit) return {...r, error: 'TMDB id not found by Jellyfin', found: (found || []).map(f => [f.Name, f.ProviderIds])};
      await post(`Items/RemoteSearch/Apply/${x.id}`, hit, {ReplaceAllImages: true});
      for (let i = 0; i < 18; i++) {   // javascript_tool lives ~45 s

        await new Promise(s => setTimeout(s, 2000));
        const back = await c.getItem(me.Id, x.id);
        if (String((back.ProviderIds || {}).Tmdb) === String(x.tmdb))
          return {...r, confirmed: true, name: back.Name, ids: back.ProviderIds, year: back.ProductionYear,
                  minutes: Math.round((back.RunTimeTicks || 0) / 6e8), locked: back.LockedFields};
      }
      return {...r, error: 'new ids did not appear in 36 s — read the item again later'};
    } catch (e) {
      const why = e instanceof Response ? `HTTP ${e.status} ${(await e.text().catch(() => '')).slice(0, 300)}` : String(e && e.message || e);
      return {...r, error: why.slice(0, 400)};
    }
  }
  return {rename, rebind};
})();
