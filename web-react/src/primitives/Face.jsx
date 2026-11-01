/* An agent's face. Its portrait at /file/<id>/avatar.png when it has one, and the soft letter
   circle when it does not. The picture fills the same circle, so nothing moves when it loads.
   A portrait that fails once is not asked for again while the page lives. */

import { useState } from 'react'
import { fileUrl } from '../data/api'
import { faceColour } from '../data/faceColour'

export { faceColour }

const missing = new Set()

const IMG = { width: '100%', height: '100%', borderRadius: '50%', objectFit: 'cover', display: 'block' }

/* `size` draws the circle here. Without it, the className must give the size, as .fd-face does. */
/* onOpen: a tap on the portrait calls it with the picture's address. The letter is not a picture,
   so it does nothing. */
export default function Face({ id, name, size, as: Tag = 'span', className = '', style, onOpen, ...rest }) {
  const [, setFailed] = useState(false)
  const box = size ? {
    flex: 'none', width: size, height: size, borderRadius: '50%', display: 'inline-grid',
    placeItems: 'center', font: '600 ' + Math.round(size * 0.42) + 'px var(--font-ui)', color: '#0b0a09',
  } : null
  return (
    <Tag className={className} style={{ ...box, background: faceColour(id), overflow: 'hidden', ...style }} {...rest}
      onClick={onOpen && id && !missing.has(id) ? function (e) {
        e.stopPropagation(); e.preventDefault(); onOpen(fileUrl(id, 'avatar.png'))
      } : rest.onClick}>
      {!id || missing.has(id) ? String(name || id || '?').slice(0, 1).toUpperCase() : (
        <img src={fileUrl(id, 'avatar.png')} alt="" style={IMG}
          onError={function () { missing.add(id); setFailed(true) }} />
      )}
    </Tag>
  )
}
