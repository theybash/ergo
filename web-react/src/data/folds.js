/* Which parents have their delegates unfolded. One list for the fleet and the chat rail, kept in
   this browser, so a fold opened in one place is open in the other. */

import { useCallback, useEffect, useState } from 'react'

const KEY = 'cc.folds'
const EVENT = 'cc-folds'

function read() {
  try { return JSON.parse(localStorage.getItem(KEY) || '{}') || {} } catch { return {} }
}

export default function useFolds() {
  const [folds, setFolds] = useState(read)
  useEffect(function () {
    function again() { setFolds(read()) }
    window.addEventListener(EVENT, again)
    return function () { window.removeEventListener(EVENT, again) }
  }, [])
  const toggle = useCallback(function (id) {
    const next = { ...read(), [id]: !read()[id] }
    try { localStorage.setItem(KEY, JSON.stringify(next)) } catch { /* private mode */ }
    setFolds(next)
    window.dispatchEvent(new Event(EVENT))
  }, [])
  return [folds, toggle]
}
