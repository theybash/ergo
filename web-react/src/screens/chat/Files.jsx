/* The drop target. It shows only while a file is dragged over the page, so a file can be
   dropped on any agent. What the agent handed you lives in the Files tab (FilesTab.jsx), which
   reuses put and size from here.

   The inbox is only a count on the agent record, so the strip can name how many were sent and
   not what they were. */

import { useRef, useState } from 'react'
import { inboxUrl } from './urls'

export function size(n) {
  if (n >= 1048576) { return (n / 1048576).toFixed(1) + ' MB' }
  if (n >= 1024) { return Math.round(n / 1024) + ' KB' }
  return n + ' B'
}

/* One PUT per file, with the name in the address and the bytes as the body. No multipart,
   because that needs a parser on the daemon and the standard library dropped its own in 3.13. */
export async function put(id, file) {
  const r = await fetch(inboxUrl(id, file.name), {
    method: 'PUT',
    headers: { 'Content-Type': file.type || 'application/octet-stream' },
    body: file,
  })
  const data = await r.json().catch(function () { return {} })
  if (!r.ok) { throw new Error(data.error || String(r.status)) }
  return data
}

export default function Files({ id, inbox, dragging, onNote, onSent }) {
  const [busy, setBusy] = useState(false)
  const picker = useRef(null)

  if (!dragging && !busy) { return null }

  async function sendAll(chosen) {
    setBusy(true)
    try {
      for (const file of chosen) {
        const data = await put(id, file)
        onNote(data.told_agent === 'held'
          ? file.name + ' sent. The agent is told when it wakes.'
          : file.name + ' sent, and the agent has been told where it is.')
      }
    } catch (e) {
      onNote(e.message)
    } finally {
      setBusy(false)
      onSent()
    }
  }

  function drop(e) {
    e.preventDefault()
    const chosen = [...(e.dataTransfer ? e.dataTransfer.files : [])]
    if (chosen.length) { sendAll(chosen) }
  }

  return (
    <div
      className={busy ? 'files ch-busy' : 'files'}
      id="files-strip"
      onDragOver={function (e) { e.preventDefault() }}
      onDrop={drop}
    >
      <span className="files-label">Drop to send</span>

      <input
        ref={picker}
        className="sr-only"
        type="file"
        multiple
        aria-label="Choose a file to send to this agent"
        onChange={function (e) {
          const chosen = [...e.target.files]
          e.target.value = ''
          if (chosen.length) { sendAll(chosen) }
        }}
      />
      <button
        type="button"
        className="file-drop"
        disabled={busy}
        onClick={function () { picker.current?.click() }}
      >
        {busy ? 'Sending' : inbox ? 'Send a file (' + inbox + ' sent so far)' : 'Send a file'}
      </button>
    </div>
  )
}
