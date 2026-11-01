/* What the agent is running in the turn that is still going. One chip per `tool` frame.

   These frames carry no id, so a live call cannot be paired with its result. They are thrown
   away on `settled` and the record's own tool and result rows, which do share an id, take their
   place. Nothing here survives the turn. */

import Chip from '../../primitives/Chip'
import { gist } from './gist'

export default function ToolChips({ tools }) {
  if (!tools || !tools.length) { return null }

  return (
    <div className="toolrow ch-tools-live">
      {tools.map(function (t, i) {
        return <Chip key={t.name + i} verb={t.name}>{' ' + gist(t.summary)}</Chip>
      })}
      <span className="summary">
        {tools.length === 1 ? '1 call this turn' : tools.length + ' calls this turn'}
      </span>
    </div>
  )
}
