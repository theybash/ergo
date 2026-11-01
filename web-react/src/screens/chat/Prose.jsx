/* Agent words, rendered. The renderer escapes the whole text before a single rule runs, so the
   HTML it returns holds no tag the agent wrote. That is the only reason this may be set as
   markup at all, and it is why the renderer and this component sit next to each other. */

import { memo } from 'react'
import { light } from './markdown'

function Prose({ text, agent, className = 'bubble wide', children }) {
  return (
    <div className={className}>
      <div dangerouslySetInnerHTML={{ __html: light(text || '', agent) }} />
      {children}
    </div>
  )
}

export default memo(Prose)
