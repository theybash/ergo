/* A parent's delegates as a few small overlapping faces. Four at most, then a +N. */

import Face from './Face'

export default function FaceStack({ agents, size = 20 }) {
  const shown = agents.slice(0, 4)
  const more = agents.length - shown.length
  return (
    <span className="face-stack" aria-hidden="true">
      {shown.map(function (a, i) {
        return <Face key={a.id} id={a.id} name={a.name} size={size}
          style={{ marginLeft: i ? -Math.round(size * 0.3) : 0, boxShadow: '0 0 0 2px var(--bg)',
                   opacity: a.archived ? 0.45 : 1 }} />
      })}
      {more > 0 ? <span className="face-more">{'+' + more}</span> : null}
    </span>
  )
}
