/* The agent asks you to choose, with the options it wrote itself. One block per question,
   because an ask may carry several and each one is answered on its own.

   One option answers the question the moment it is tapped. Several options are collected first,
   because with multiSelect the person is not finished until they say so. Taps and words both
   count: dropping the picked labels because somebody also typed would make those taps do
   nothing, so both go, in the order they were given.

   The answer is keyed by the question's own text exactly as it arrived. That text is what the
   engine matches the answer back to, so it is copied, never tidied.

   Skip answers nothing. The daemon denies the question tool, with words that say nobody chose,
   and the agent goes on without the answer or stops. It skips the whole ask, because the engine
   answers an ask once. */

import { useState } from 'react'
import Receipt from '../../primitives/Receipt'

export default function QuestionCard({ ask, receipt, busy, onAnswer, onFinished, onSkip }) {
  // Which blocks are answered, what is ticked in each, and what is typed in each. All keyed by
  // the question's place in the ask, because two questions may read the same.
  const [done, setDone] = useState({})
  const [picked, setPicked] = useState({})
  const [typed, setTyped] = useState({})

  const questions = ask.questions || []
  const head = 'ask-' + ask.id

  async function answer(i, q, text) {
    if (!text) { return }
    const ok = await onAnswer(q.question || '', text)
    if (!ok) { return }
    const next = { ...done, [i]: true }
    setDone(next)
    // The card goes when the last of its questions is answered, and the receipt says what was
    // chosen, not just that something was.
    if (questions.every(function (_, k) { return next[k] })) { onFinished(text) }
  }

  function tick(i, label) {
    const have = picked[i] || []
    setPicked({
      ...picked,
      [i]: have.includes(label)
        ? have.filter(function (l) { return l !== label })
        : have.concat([label]),
    })
  }

  function submit(i, q) {
    const words = (typed[i] || '').trim()
    const chosen = picked[i] || []
    answer(i, q, chosen.concat(words ? [words] : []).join(', '))
  }

  const open = questions.filter(function (_, i) { return !done[i] })

  return (
    <section className={receipt ? 'ask centre done' : 'ask centre'} aria-labelledby={head}>
      <h3 id={head}>{open.length > 1 ? 'The agent is asking' : (open[0] || {}).header || 'The agent is asking'}</h3>

      {questions.map(function (q, i) {
        if (done[i]) { return null }
        const chosen = picked[i] || []
        const field = 'q-' + ask.id + '-' + i

        return (
          <div className="ch-block" key={q.question || i}>
            <p className="ch-q">{q.question || ''}</p>

            <ul className="options">
              {(q.options || []).map(function (o) {
                const label = o.label || ''
                return (
                  <li key={label}>
                    <button
                      type="button"
                      className="option"
                      aria-pressed={q.multiSelect ? chosen.includes(label) : undefined}
                      disabled={busy}
                      onClick={function () {
                        if (q.multiSelect) { tick(i, label) } else { answer(i, q, label) }
                      }}
                    >
                      <b>{label}</b>
                      {o.description ? <em className="ch-odesc">{o.description}</em> : null}
                    </button>
                  </li>
                )
              })}
            </ul>

            {q.multiSelect
              ? <p className="ch-hint">Pick as many as you want, then Send.</p>
              : null}

            <label className="sr-only" htmlFor={field}>Something else</label>
            <input
              className="field"
              id={field}
              type="text"
              autoComplete="off"
              placeholder="Or say it yourself"
              value={typed[i] || ''}
              onChange={function (e) { setTyped({ ...typed, [i]: e.target.value }) }}
              onKeyDown={function (e) {
                if (e.key === 'Enter') { e.preventDefault(); submit(i, q) }
              }}
            />

            <div className="ask-foot">
              <button
                type="button"
                className="btn quiet"
                disabled={busy}
                onClick={onSkip}
              >
                Skip
              </button>
              <span className="pager">
                {questions.length > 1 ? 'question ' + (i + 1) + ' of ' + questions.length : ''}
              </span>
              <button
                type="button"
                className="btn primary"
                disabled={busy}
                onClick={function () { submit(i, q) }}
              >
                Send
              </button>
            </div>
          </div>
        )
      })}

      {receipt
        ? <Receipt verdict={receipt.verdict} detail={receipt.detail} negative={receipt.negative} />
        : null}
    </section>
  )
}
