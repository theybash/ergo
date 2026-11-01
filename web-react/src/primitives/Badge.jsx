/* Two shapes live under one name, because the mock marks two different things.
   needs, ok, run, fail and quiet are round .badge pills that report a state.
   master and work are the square .ac-tag, which names an identity, not a state. */

const TAGS = ['master', 'work']

export default function Badge({ kind, children, id, onClick }) {
  if (TAGS.indexOf(kind) !== -1) {
    return <span className="ac-tag">{children}</span>
  }

  // A badge that takes you somewhere is a control, so it is a button and not a label.
  if (onClick) {
    return (
      <button type="button" className={'badge ' + kind} id={id} onClick={onClick}>
        {children}
      </button>
    )
  }

  return <span className={'badge ' + kind} id={id}>{children}</span>
}
