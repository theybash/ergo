/* A question from the agent with a few options. It does not stop the agent: the agent goes on,
   and his tap reaches it later as one machine line,

     [Choice <call id>] To "<question>", the owner picked: <label>

   The call id ties the line to this card. The daemon finds the line in history and writes the
   pick onto the card, so the line is never drawn as his message. Once picked, the card folds to
   one quiet line: the question and what he chose. */

import { useState } from 'react'
import cc from '../../data/api'

function label(o) {
  return String((o && typeof o === 'object' ? o.label : o) || '')
}

export default function ChoiceCard({ data, row, agent }) {
  const [mine, setMine] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const question = String(data.question || '')
  const options = (Array.isArray(data.options) ? data.options : []).filter(label)
  const answer = row.answer || mine

  if (answer) {
    return (
      <p className="sw-receipt">
        <span>{question}</span> <b>{answer}</b>
      </p>
    )
  }

  async function pick(text) {
    if (busy || !agent) { return }
    setBusy(true)
    setError('')
    const asked = question.replace(/\s+/g, ' ').slice(0, 200)
    try {
      const r = await cc.send(agent, `[Choice ${row.id}] To "${asked}", the owner picked: ${text}`, true)
      // The route answers ok false when the message did not reach the agent.
      if (r && r.ok === false) { throw new Error('That did not reach the agent.') }
      setMine(text)
    } catch (e) {
      setError(e.message || 'That did not reach the agent.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="sw-card" aria-label={question}>
      <p className="sw-q">{question}</p>
      <ul className="options">
        {options.map(function (o) {
          const text = label(o)
          const more = o && typeof o === 'object' && o.description ? String(o.description) : ''
          return (
            <li key={text}>
              <button type="button" className="option" disabled={busy}
                onClick={function () { pick(text) }}>
                <b>{text}</b>
                {more ? <em className="ch-odesc">{more}</em> : null}
              </button>
            </li>
          )
        })}
      </ul>
      {error ? <p className="sw-error">{error}</p> : null}
    </section>
  )
}
