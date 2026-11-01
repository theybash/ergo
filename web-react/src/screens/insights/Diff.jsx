/* The unified diff. The body scrolls sideways on its own, so long lines never widen the page.
   The rows themselves come from the shared primitive, because the chat shows the same rows. */

import Badge from '../../primitives/Badge'
import DiffRows from '../../primitives/DiffRows'
import { DIFF } from './data'

export default function Diff() {
  return (
    <div className="diff">
      <div className="diff-head">
        <span>{DIFF.file}</span>
        <Badge kind="ok">+{DIFF.added}</Badge>
        <Badge kind="fail">-{DIFF.removed}</Badge>
      </div>
      <DiffRows rows={DIFF.rows} />
    </div>
  )
}
