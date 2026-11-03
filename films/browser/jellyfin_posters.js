// Posters in Jellyfin through the logged-in Jellyfin tab (owner, 2026-11-02: «у пункт назначения 4 … дибильный постер …
// Вначале найди и покажи потом правь»; each item approved one by one after its «сейчас → будет» picture). Text from
// `python -X utf8 -m films.sources.jellyfin_posters code <item id> …`. Never touches played state.
//   set([{id, name, url}], user)    — download the approved poster as Primary (Items/RemoteImages/Download), read back:
//                                     confirmed when the Primary image tag changed;
//   detach([{id, name, year}], user) — an item bound to someone else's film («Континенталь 2» → «Терминатор 2»): drop the
//                                     provider ids, ratings, overview, people, genres, studios, tags; set the year; lock the
//                                     item (LockData) so a refresh does not bind it again.
window.__jfPosters = (() => {
  const why = async e => e instanceof Response ? `HTTP ${e.status} ${(await e.text().catch(() => '')).slice(0, 300)}`
                                               : String(e && e.message || e).slice(0, 300);
  async function me(user) {
    const c = window.ApiClient;
    if (!c) return [null, {error: 'no ApiClient: not a Jellyfin tab or not signed in'}];
    const u = await c.getCurrentUser();
    if (!u || u.Name !== user) return [null, {error: 'wrong user', user: u ? u.Name : null}];
    return [u, null];
  }
  async function set(items, user) {
    const [u, err] = await me(user);
    if (err) return err;
    const c = window.ApiClient, out = [];
    for (const x of items) {
      const r = {item_id: x.id, name: x.name};
      try {
        const it = await c.getItem(u.Id, x.id);
        if (it.Name !== x.name) { out.push({...r, error: `changed since the plan: «${it.Name}»`}); continue; }
        const before = (it.ImageTags || {}).Primary || null;
        await c.ajax({type: 'POST', url: c.getUrl(`Items/${x.id}/RemoteImages/Download`,
                                                  {Type: 'Primary', ProviderName: 'TheMovieDb', ImageUrl: x.url})});
        const after = ((await c.getItem(u.Id, x.id)).ImageTags || {}).Primary || null;
        out.push({...r, confirmed: !!after && after !== before});
      } catch (e) {
        out.push({...r, error: await why(e)});
      }
    }
    return out;
  }
  async function detach(items, user) {
    const [u, err] = await me(user);
    if (err) return err;
    const c = window.ApiClient, out = [];
    for (const x of items) {
      const r = {item_id: x.id, name: x.name};
      try {
        const it = await c.getItem(u.Id, x.id);
        if (it.Name !== x.name || it.Type !== 'Movie') { out.push({...r, error: `changed since the plan: ${it.Type} «${it.Name}»`}); continue; }
        delete it.Trickplay;   // Jellyfin cannot read its own Trickplay DTO back (HTTP 500)
        Object.assign(it, {ProviderIds: {}, CommunityRating: null, CriticRating: null, Overview: '', Taglines: [],
                           Genres: [], GenreItems: [], Tags: [], Studios: [], People: [], OfficialRating: null,
                           PremiereDate: null, ProductionYear: x.year, LockData: true});
        await c.updateItem(it);
        const b = await c.getItem(u.Id, x.id);
        out.push({...r, confirmed: !Object.keys(b.ProviderIds || {}).length && b.CommunityRating == null &&
                                   !(b.People || []).length && b.ProductionYear === x.year && b.LockData === true,
                  year: b.ProductionYear});
      } catch (e) {
        out.push({...r, error: await why(e)});
      }
    }
    return out;
  }
  return {set, detach};
})();
