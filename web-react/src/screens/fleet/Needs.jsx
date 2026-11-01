/* Every ask waiting on you, one line each, gathered from every awake agent's own socket.
   Answering folds the row to a receipt, the way an ask card folds, and the row goes a moment
   later. The count under the heading is the same number the Fleet badge and the bottom tab
   show, because all three read it off this one list.

   With nothing waiting the section is not there at all: no heading, no empty line. The owner
   asked for that. The sockets that collect the asks keep running in Fleet, so the section is
   back the moment an agent asks for something. */

import Receipt from '../../primitives/Receipt'

const ACTS = {
  deny: { label: 'Deny', className: 'btn sm quiet' },
  approve: { label: 'Approve', className: 'btn sm primary' },
  answer: { label: 'Answer', className: 'btn sm' },
}

export default function Needs({ rows, onAnswer, boxRef }) {
  /* Nothing waiting and no receipt still folding away. Nothing to say, so nothing is drawn. */
  if (!rows.length) { return null }

  const pending = rows.filter(function (row) { return !row.receipt }).length

  return (
    <section className="needs" id="needs" aria-labelledby="needs-h" ref={boxRef}>
      <div className="section-head">
        <h3 id="needs-h">Needs you</h3>
        {pending > 0 ? <span className="needs-count" id="needs-count">{pending}</span> : null}
      </div>

      <ul className="needs-list" id="needs-list">
        {rows.map(function (row) {
          const cls = 'needs-row' + (row.receipt ? ' done' : '') + (row.gone ? ' gone' : '')
          return (
            <li className={cls} key={row.key}>
              {row.receipt ? (
                <Receipt
                  verdict={row.receipt.verdict}
                  detail={row.receipt.detail}
                  negative={row.receipt.negative}
                />
              ) : (
                <>
                  <span className="needs-who">{row.who}</span>
                  <span className="needs-what">
                    {row.what}
                    {row.mono ? <span className="mono">{row.mono}</span> : null}
                  </span>
                  <span className="needs-age">{row.age}</span>
                  <span className="needs-acts">
                    {row.acts.map(function (kind) {
                      return (
                        <button
                          key={kind}
                          type="button"
                          className={ACTS[kind].className}
                          disabled={row.busy}
                          onClick={function () { onAnswer(row, kind) }}
                        >
                          {ACTS[kind].label}
                        </button>
                      )
                    })}
                  </span>
                </>
              )}
            </li>
          )
        })}
      </ul>
    </section>
  )
}
