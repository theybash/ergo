/* The Files tab: everything the agent handed you, newest first, and the way to send it one.

   A tap on a text file opens it whole over the screen, the way a tool call opens; markdown is
   drawn as the chat draws it. A tap on an image opens the pinch zoom view. Anything else, and
   the arrow on every row, downloads.

   The tab's red numeral counts files newer than the last time this tab was open for this agent.
   That mark lives in this browser only, because it is a convenience: lost, it shows the files
   as new once more and nothing else goes wrong. */

import { useEffect, useRef, useState } from 'react'
import cc from '../../data/api'
import useBackClose from '../../shell/useBackClose'
import ImageView from './ImageView'
import Prose from './Prose'
import { put, size } from './Files'
import useScrollMemory from '../../shell/scrollMemory'

export const TEXT = /\.(md|markdown|txt|log|csv|tsv|json|jsonl|ya?ml|py|js|jsx|ts|sh|html?|css|xml|ini|toml)$/i
export const IMAGE = /\.(png|jpe?g|gif|webp|svg)$/i
const MD = /\.(md|markdown)$/i

const down = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M12 4v11M8 11l4 4 4-4M5 20h14"></path>
  </svg>
)

/* Agents wrap their markdown at about 100 characters. The chat keeps every line break, which
   suits a message, but a whole document then reads ragged on a phone. So a line that only
   continues the one before it is joined to it. Code fences are left exactly as written. */
// A line that starts its own block: a heading, a table row, a quote, a list item or a rule.
const BLOCK = /^\s*(#|\||>|[-*+]\s|\d+[.)]\s|([-*_])(\s*\2){2,}\s*$)/

function unwrap(text) {
  return text.replace(/\r\n?/g, '\n').split('```').map(function (part, i) {
    if (i % 2) { return part }
    const out = []
    for (const line of part.split('\n')) {
      const prev = out.length ? out[out.length - 1] : ''
      const joins = line.trim() && prev.trim() && !BLOCK.test(line) && !/^\s*(#|\|)/.test(prev)
      if (joins) { out[out.length - 1] = prev + ' ' + line.trim() } else { out.push(line) }
    }
    return out.join('\n')
  }).join('```')
}

function seenKey(id) { return 'cc-files-seen:' + id }

function readSeen(id) {
  try { return Number(window.localStorage.getItem(seenKey(id))) || 0 } catch { return 0 }
}

function newest(files) {
  return (files || []).reduce(function (m, f) { return Math.max(m, f.modified || 0) }, 0)
}

/* How many files arrived since this tab was last open. Read by the tab label. */
export function unseen(id, files) {
  const seen = readSeen(id)
  return (files || []).filter(function (f) { return (f.modified || 0) > seen }).length
}

export function markSeen(id, files) {
  try { window.localStorage.setItem(seenKey(id), String(newest(files))) } catch { /* private window */ }
}

function when(t) {
  if (!t) { return '' }
  const d = new Date(t * 1000)
  const today = new Date()
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (d.toDateString() === today.toDateString()) { return time }
  return d.toLocaleDateString([], { day: 'numeric', month: 'short' }) + ', ' + time
}

/* One text file whole, over the screen. Back or the cross closes it. */
export function FileView({ agent, name, onClose }) {
  const [text, setText] = useState(null)
  const [error, setError] = useState('')
  useBackClose(!!name, onClose)

  useEffect(function () {
    if (!name) { return undefined }
    let live = true
    setText(null)
    setError('')
    fetch(cc.fileUrl(agent, name))
      .then(function (r) {
        if (!r.ok) { throw new Error('That file is no longer there.') }
        return r.text()
      })
      .then(function (t) { if (live) { setText(t) } }, function (e) { if (live) { setError(e.message) } })
    return function () { live = false }
  }, [agent, name])

  if (!name) { return null }
  return (
    <div className="toolview" role="dialog" aria-modal="true" aria-label={name}>
      <div className="toolview-bar">
        <span className="toolview-name">{name}</span>
        <span className="fv-acts">
          <a className="toolview-x fv-dl" href={cc.fileUrl(agent, name)} download aria-label={'Download ' + name}>{down}</a>
          <button type="button" className="toolview-x" aria-label="Close" onClick={onClose}>×</button>
        </span>
      </div>
      <div className="toolview-body">
        {error ? <p className="toolview-note">{error}</p> : null}
        {text === null && !error ? <p className="toolview-note">Loading...</p> : null}
        {text !== null && MD.test(name) ? <Prose text={unwrap(text)} agent={agent} className="fv-prose" /> : null}
        {text !== null && !MD.test(name) ? <pre className="toolview-pre">{text}</pre> : null}
      </div>
    </div>
  )
}

export default function FilesTab({ id, files, inbox, active, onNote, onSent }) {
  // Its own place on the page, not the one the last screen left (shell/scrollMemory.js).
  useScrollMemory('files:' + id, active)
  const [open, setOpen] = useState('')
  const [image, setImage] = useState('')
  const [busy, setBusy] = useState(false)
  const picker = useRef(null)
  const list = (files || []).slice().sort(function (a, b) { return (b.modified || 0) - (a.modified || 0) })

  // Open is seen. A file that lands while the tab is open counts as seen too.
  useEffect(function () {
    if (active) { markSeen(id, files) }
  }, [active, id, files])

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

  function pick(f) {
    if (IMAGE.test(f.name)) { setImage(f.name); return }
    if (TEXT.test(f.name)) { setOpen(f.name); return }
    const a = document.createElement('a')
    a.href = cc.fileUrl(id, f.name)
    a.download = f.name
    a.click()
  }

  return (
    <div className="ft">
      {list.length ? (
        <ul className="ft-list">
          {list.map(function (f) {
            return (
              <li className="ft-row" key={f.name}>
                <button type="button" className="ft-open" onClick={function () { pick(f) }}>
                  <span className="ft-name">{f.name}</span>
                  <span className="ft-meta">{when(f.modified)} · {size(f.bytes)}</span>
                </button>
                <a className="file-dl ft-dl" href={cc.fileUrl(id, f.name)} download aria-label={'Download ' + f.name}>
                  {down}
                </a>
              </li>
            )
          })}
        </ul>
      ) : (
        <p className="ft-empty">No files from this agent yet.</p>
      )}

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
        className="file-drop ft-send"
        disabled={busy}
        onClick={function () { picker.current?.click() }}
      >
        {busy ? 'Sending' : inbox ? 'Send a file (' + inbox + ' sent so far)' : 'Send a file'}
      </button>

      <FileView agent={id} name={open} onClose={function () { setOpen('') }} />
      <ImageView
        src={image ? cc.fileUrl(id, image) : ''}
        alt={image}
        onClose={function () { setImage('') }}
      />
    </div>
  )
}
