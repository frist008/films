// Jellyfin library settings through the logged-in Jellyfin tab (owner, 2026-11-02: «Выключить кадры» — «И то, и другое»):
// the «Screen Grabber» image fetcher cuts a frame from the video as the poster of a film that is not identified yet, and
// the frame stays after identification. It is removed from the Movie image fetchers of every library; the rest of the
// options are sent back unchanged. Reads the libraries back and returns what each one has now.
// Usage (one javascript_tool call in the Jellyfin tab): await __jfLibrary.noScreenGrabber().
window.__jfLibrary = (() => {
  const GRABBER = 'Screen Grabber';
  async function noScreenGrabber() {
    const c = window.ApiClient;
    if (!c) return {error: 'no ApiClient: not a Jellyfin tab or not signed in'};
    const get = () => c.ajax({type: 'GET', url: c.getUrl('Library/VirtualFolders'), dataType: 'json'});
    const out = [];
    for (const lib of await get()) {
      const opts = lib.LibraryOptions || {};
      const movie = (opts.TypeOptions || []).find(t => t.Type === 'Movie');
      if (!movie || !(movie.ImageFetchers || []).includes(GRABBER)) continue;
      movie.ImageFetchers = movie.ImageFetchers.filter(f => f !== GRABBER);
      await c.ajax({type: 'POST', url: c.getUrl('Library/VirtualFolders/LibraryOptions'),
                    data: JSON.stringify({Id: lib.ItemId, LibraryOptions: opts}), contentType: 'application/json'});
      out.push(lib.Name);
    }
    const now = (await get()).map(l => {
      const m = ((l.LibraryOptions || {}).TypeOptions || []).find(t => t.Type === 'Movie');
      return [l.Name, m ? m.ImageFetchers : null];
    });
    return {changed: out, now, ok: now.every(([, f]) => !f || !f.includes(GRABBER))};
  }
  return {noScreenGrabber};
})();
