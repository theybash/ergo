/* Two addresses this screen needs that src/data/api.js does not build.

   api.js has socketUrl, fileUrl and eventsUrl. It has no builder for the inbox, which is a PUT
   of raw bytes rather than JSON and so cannot go through its api() helper, and none for an
   agent's own page, which is an iframe src rather than a fetch. Both are one line, so they are
   here rather than in a hook nobody else would call. The base is read the same way api.js
   reads it, so pointing a dev server at the live daemon moves these too. */

const BASE = (import.meta.env && import.meta.env.VITE_CC_API) || ''

/* PUT the bytes here. The name is tidied by the daemon: only the last path component survives,
   unsafe characters become a dash, and an existing name gets -2. */
export const inboxUrl = (id, name) =>
  `${BASE}/api/agents/${id}/inbox/${encodeURIComponent(name)}`

/* One of the agent's own screens, wrapped by the daemon in a page with a strict policy. */
export const pageUrl = (id, slug) => `${BASE}/view/${id}/${slug}/`
