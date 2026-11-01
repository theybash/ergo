/* Back closes this dialog or menu. While `open` is true the layer holds one history entry; back
 * takes it off and calls `close`. Any other close takes the entry off again. See shell/nav.js.
 * At a desk, Escape closes the layer on top too, and marks the key taken so the chat does not
 * also read it as stop. */

import { useEffect, useRef } from 'react'
import { newLayer, openLayer, closeLayer } from './nav'

const open_ = []

export default function useBackClose(open, close) {
  const shut = useRef(close)
  useEffect(function () { shut.current = close })

  useEffect(function () {
    if (!open) { return undefined }
    const mark = newLayer()
    openLayer(mark, function () { if (shut.current) { shut.current() } })
    open_.push(mark)
    function key(e) {
      if (e.key !== 'Escape' || e.defaultPrevented || open_[open_.length - 1] !== mark) { return }
      e.preventDefault()
      if (shut.current) { shut.current() }
    }
    // Capture, so the layer hears Escape before the chat's own handler on window.
    window.addEventListener('keydown', key, true)
    return function () {
      window.removeEventListener('keydown', key, true)
      const at = open_.indexOf(mark)
      if (at >= 0) { open_.splice(at, 1) }
      closeLayer(mark)
    }
  }, [open])
}
