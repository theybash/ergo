/* The agent asks before it runs a command. Under 720px the sheet class lifts this card off the
   bottom edge, where the thumb already is. Answering folds it to one line.

   The command is the whole card, so it is shown in full and never shortened: the button says
   Approve, and the only thing that makes that safe is seeing every character of what it
   approves. A cut command hides its own tail, which is exactly where a pipe into a shell sits.
   The summary is the fallback for a tool that has no command to show, a file write or an edit. */

import Receipt from '../../primitives/Receipt'

export default function ApprovalCard({ ask, receipt, busy, onAnswer }) {
  const shown = (ask.input && ask.input.command) || ask.summary || ''
  const tool = ask.tool || 'tool'
  const head = 'ask-' + ask.id

  return (
    <section
      className={receipt ? 'ask centre sheet done' : 'ask centre sheet'}
      aria-labelledby={head}
    >
      <span className="grab" aria-hidden="true"></span>
      <h3 id={head}>{'Allow ' + tool + '?'}</h3>
      <p className="why">This agent is waiting on you. Nothing runs until you answer.</p>
      <pre className="cmdblock">{shown}</pre>
      <div className="ask-foot">
        <button
          type="button"
          className="btn quiet"
          disabled={busy}
          onClick={function () { onAnswer(false) }}
        >
          Deny
        </button>
        <span className="pager">{tool}</span>
        <button
          type="button"
          className="btn primary"
          disabled={busy}
          onClick={function () { onAnswer(true) }}
        >
          Approve
        </button>
      </div>
      {receipt
        ? <Receipt verdict={receipt.verdict} detail={receipt.detail} negative={receipt.negative} />
        : null}
    </section>
  )
}
