/* A screen that lives in its own file, fetched the first time it opens.

   A page can stay open on the phone for days. When a new build lands, the old hashed files are
   gone from dist, so a page from the old build asks for a file the daemon no longer has. React
   would then drop the whole app and leave a blank page. Instead the page reloads once, which
   fetches the new build. The stamp in sessionStorage stops a reload loop when the file is
   missing for some other reason: a second failure within a minute goes through as an error. */

import { lazy } from 'react'

const KEY = 'cc-lazy-reload'

export default function lazyFile(load) {
  return lazy(function () {
    return load().catch(function (err) {
      let last = 0
      try { last = Number(sessionStorage.getItem(KEY)) || 0 } catch { /* storage blocked */ }
      if (Date.now() - last < 60000) { throw err }
      try { sessionStorage.setItem(KEY, String(Date.now())) } catch { throw err }
      window.location.reload()
      // The page is going away. A promise that never settles keeps React waiting until it does.
      return new Promise(function () {})
    })
  })
}
