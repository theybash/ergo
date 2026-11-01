/* What he was writing to each agent, kept until he sends it: the text and the attached files.

   He leaves the chat to take a screenshot or look something up, and Android may close the page
   behind him. The draft must be there when he comes back. So it lives in the browser, per agent:
   the text in localStorage, the files in IndexedDB, which can hold a picture whole. Both are this
   browser's only, which is right for a draft: it is his convenience, not a record.

   Every read and write is wrapped. A private window, a full disk or blocked storage loses the draft
   and nothing else. */

const TEXT = 'cc.draft.'
const DB = 'cc-drafts'
const STORE = 'files'

export function loadText(agent) {
  try { return window.localStorage.getItem(TEXT + agent) || '' } catch { return '' }
}

export function saveText(agent, text) {
  try {
    if (text) { window.localStorage.setItem(TEXT + agent, text) } else { window.localStorage.removeItem(TEXT + agent) }
  } catch { /* storage is off */ }
}

let opening = null

function db() {
  if (!opening) {
    opening = new Promise(function (resolve, reject) {
      try {
        const req = window.indexedDB.open(DB, 1)
        req.onupgradeneeded = function () { req.result.createObjectStore(STORE) }
        req.onsuccess = function () { resolve(req.result) }
        req.onerror = function () { reject(req.error) }
      } catch (e) { reject(e) }
    })
    opening.catch(function () { opening = null })
  }
  return opening
}

/* The held files for this agent, as File objects again, or none. */
export async function loadFiles(agent) {
  try {
    const d = await db()
    const rows = await new Promise(function (resolve, reject) {
      const req = d.transaction(STORE, 'readonly').objectStore(STORE).get(agent)
      req.onsuccess = function () { resolve(req.result || []) }
      req.onerror = function () { reject(req.error) }
    })
    return rows.map(function (r) { return new File([r.blob], r.name, { type: r.type }) })
  } catch { return [] }
}

export async function saveFiles(agent, files) {
  try {
    const d = await db()
    const rows = (files || []).map(function (f) { return { name: f.name, type: f.type, blob: f } })
    const store = d.transaction(STORE, 'readwrite').objectStore(STORE)
    if (rows.length) { store.put(rows, agent) } else { store.delete(agent) }
  } catch { /* storage is off */ }
}
