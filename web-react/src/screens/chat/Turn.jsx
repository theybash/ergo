/* One row from the session file, drawn by its kind. The daemon gives three kinds and one of
   them has a special case, exactly as chatRow() in web/app.js has them:

     text   who is "operator" or "agent". Yours is a bubble, the agent's runs on the page.
     tool   one line, the tool name and the first line of its input.
     result the answer to a tool call, quieter still, red when it failed.

   The special case is AskUserQuestion. The whole question is in the record, options and
   descriptions and all, so it is drawn as the question it was rather than as a tool call. On a
   phone the dialog in the terminal is unreadable, and this is the only way to see the choices. */

import { memo } from 'react'
import Chip from '../../primitives/Chip'
import Prose from './Prose'
import { gist } from './gist'
import SystemCard from './SystemCard'
import { read, answered } from './kinds'
import ShowCard from './ShowCard'
import Element from './Elements'
import { isShowCard, isShowResult, picked } from './show'

/* The time of day, mono, under a message. The daemon sends ISO 8601 with a Z. */
function stamp(at) {
  if (!at) { return '' }
  const d = new Date(at)
  if (isNaN(d.getTime())) { return '' }
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

const oneLine = (s) => String(s || '').split('\n')[0].slice(0, 120)

/* Claude Code writes Stop into the record as a message from him. He did not type it. */
const STOPPED = /^\[Request interrupted by user[^\]]*\]$/

/* A question the agent asked with its own tool, read back out of the record. */
function Asked({ row }) {
  let input = null
  try { input = JSON.parse(row.inputRaw || 'null') } catch { input = null }
  const questions = (input && input.questions) || []
  if (!questions.length) { return null }

  return (
    <div className="ch-asked">
      {questions.map(function (q, i) {
        return (
          <div className="ch-asked-q" key={q.question || i}>
            <p className="ch-asked-head">{q.question || ''}</p>
            <ol className="ch-asked-list">
              {(q.options || []).map(function (o) {
                return (
                  <li key={o.label}>
                    <b>{o.label || ''}</b>
                    {o.description ? <em>{o.description}</em> : null}
                  </li>
                )
              })}
            </ol>
            <p className="ch-asked-foot">
              {q.multiSelect ? 'answered, several allowed' : 'answered'}
            </p>
          </div>
        )
      })}
    </div>
  )
}

/* The files he attached ride in his message as "[Attached: <path>]", which is what the agent
   needs. He sees the picture instead: an image in the agent's inbox becomes a thumbnail that opens
   in the zoom view, and any other file becomes its name. */
const ATTACHED = /\[Attached: ([^\]\n]*\/\.platform\/inbox\/([a-z0-9-]+)\/([^/\]\n]+))\]/g
const PICTURE = /\.(jpe?g|png|gif|webp)$/i

function attachments(text) {
  return String(text || '').replace(ATTACHED, function (all, path, agent, name) {
    return PICTURE.test(name) ? `![${name}](inbox:${agent}/${name})` : `Attached: ${name}`
  })
}

function Turn({ row, agent }) {
  if (row.kind === 'text' && row.who === 'operator' && STOPPED.test(String(row.text || '').trim())) {
    return <p className="ch-note ch-stopped">Stopped.</p>
  }

  // The summary Claude Code wrote when it compacted the conversation, folded.
  if (row.compacted) {
    return (
      <details className="ch-compacted">
        <summary>Compacted</summary>
        <Prose text={row.text} agent={agent} className="ch-compacted-body" />
      </details>
    )
  }

  // A card from the show tool, its own answer, and his pick on a card no longer on screen.
  if (isShowCard(row)) { return <ShowCard row={row} agent={agent} /> }
  if (isShowResult(row)) { return null }
  const pick = picked(row)
  if (pick !== null) { return <p className="sw-receipt">You picked <b>{pick}</b></p> }

  if (row.kind === 'tool' && row.tool === 'AskUserQuestion' && row.inputRaw) {
    const drawn = Asked({ row })
    if (drawn) { return drawn }
  }

  if (row.kind === 'tool') {
    // One chip, one line. The full input is the title, so hovering says the rest without a
    // fold that would move every line below it.
    return (
      <div className="toolrow ch-tap" data-tool={row.id || undefined} title={row.input || ''}>
        <Chip verb={row.tool || 'tool'}>{' ' + gist(row.input)}</Chip>
      </div>
    )
  }

  // A question card he answered. Claude Code writes the answer as the tool's result.
  if (row.kind === 'result' && !row.error && answered(row.text) !== null) {
    return <SystemCard receipt label="Answered the question card" text={answered(row.text)} at={stamp(row.at)} />
  }

  if (row.kind === 'result') {
    return (
      <div className={(row.error ? 'toolrow ch-failed' : 'toolrow') + ' ch-tap'} data-tool={row.id || undefined} title={row.text || ''}>
        <span className="summary">
          {(row.error ? 'failed' : 'result') + ' · ' + oneLine(row.text)}
        </span>
      </div>
    )
  }

  const mine = row.who === 'operator'
  // A message the daemon or Claude Code wrote in his name. His own words inside it stay his bubble.
  const parts = mine ? read(row.text) : null
  if (parts) {
    return parts.map(function (p, i) {
      if (p.mine !== undefined) {
        // His words, with where they came from above them, and a held message's wait below.
        return (
          <article className="turn you" key={i}>
            {p.source ? <p className="el-source">{p.source}</p> : null}
            <Prose text={attachments(p.mine)} agent={agent} className="bubble" />
            <p className="stamp">{p.waited ? 'sent ' + p.waited + ', reached it ' + stamp(row.at) : stamp(row.at)}</p>
          </article>
        )
      }
      // A held one shows how long it waited: the row's time is when it reached the agent.
      const at = p.waited ? p.waited : i === parts.length - 1 || parts[i + 1].mine === undefined ? stamp(row.at) : ''
      if (p.el) { return <Element key={i} part={p} agent={agent} at={at} /> }
      return (
        <SystemCard
          key={i}
          receipt={p.receipt !== undefined}
          label={p.card || p.receipt}
          text={p.text}
          agent={agent}
          at={i === parts.length - 1 || parts[i + 1].mine === undefined ? stamp(row.at) : ''}
        />
      )
    })
  }

  return (
    <article className={mine ? 'turn you' : 'turn them'}>
      <Prose text={mine ? attachments(row.text) : row.text} agent={agent} className={mine ? 'bubble' : 'bubble wide'} />
      <p className="stamp">{stamp(row.at)}</p>
    </article>
  )
}

/* Rows keep their objects across fetches, so a row that did not change does not draw again when a
   word streams in or a key is pressed. */
export default memo(Turn)
