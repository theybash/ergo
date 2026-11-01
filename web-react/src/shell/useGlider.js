/* The gliding pill. It measures the active tab and moves the sibling .glider span onto it.
   The pill only glides once it is first placed, so App adds html.motion after the first frame. */

import { useCallback, useEffect, useLayoutEffect } from 'react'

export default function useGlider(ref, activeKey, options) {
  const axis = (options && options.axis) || 'h'
  const selector = (options && options.selector) || '[aria-current="page"]'
  const parent = Boolean(options && options.parent)

  const place = useCallback(function () {
    const box = ref.current
    if (!box) { return }
    const glider = box.querySelector('.glider')
    let active = box.querySelector(selector)
    if (!glider) { return }
    // Nothing picked in view, as when a search on the rail hides the open agent: no pill at all,
    // rather than one left on whatever row is now in its place.
    if (!active) { glider.style.height = '0px'; glider.style.width = axis === 'v' ? '' : '0px'; return }
    if (parent) { active = active.parentNode }
    if (axis === 'v') {
      glider.style.height = active.offsetHeight + 'px'
      glider.style.transform = 'translateY(' + active.offsetTop + 'px)'
    } else {
      glider.style.width = active.offsetWidth + 'px'
      glider.style.transform = 'translateX(' + active.offsetLeft + 'px)'
    }
  }, [ref, axis, selector, parent])

  // Before paint, so the pill is never seen in the wrong place.
  useLayoutEffect(function () {
    place()
  }, [place, activeKey])

  // And again whenever the rows change under it. The agent rail reorders, and agents load or
  // hide, while the same agent stays picked: measured on the pick alone, the pill stayed on the
  // row that used to be there, and one agent looked picked while another was open.
  useEffect(function () {
    const box = ref.current
    if (!box || typeof MutationObserver === 'undefined') { return }
    const watch = new MutationObserver(place)
    watch.observe(box, { childList: true, subtree: true })
    return function () { watch.disconnect() }
  }, [ref, place])

  useEffect(function () {
    window.addEventListener('resize', place)
    // The display font changes the tab widths, so measure again once it lands.
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(place).catch(function () {})
    }
    return function () {
      window.removeEventListener('resize', place)
    }
  }, [place])

  return place
}
