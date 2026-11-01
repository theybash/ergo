/* xterm.js, loaded from the daemon's own /vendor/ and from nowhere else.

   The files are the UMD builds copied out of web/vendor/, which is where the old console loads
   them from. They are not an npm package here on purpose: there is no dependency to update, no
   network fetch at runtime, and the daemon answers /vendor/* before the tailnet door, so the
   terminal draws on a phone that has not authenticated yet.

   Loaded when the terminal tab is first shown rather than at start up, because 290 KB of
   emulator is a lot to send to somebody who only wanted to read a conversation. */

let waiting = null

function tag(make) {
  return new Promise(function (ok, no) {
    const el = make()
    el.addEventListener('load', function () { ok() })
    el.addEventListener('error', function () { no(new Error('could not load ' + el.src || el.href)) })
    document.head.appendChild(el)
  })
}

export function loadXterm() {
  if (window.Terminal && window.FitAddon) { return Promise.resolve(window) }
  if (waiting) { return waiting }

  waiting = tag(function () {
    const link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/vendor/xterm.css'
    return link
  }).then(function () {
    return tag(function () {
      const s = document.createElement('script')
      s.src = '/vendor/xterm.js'
      return s
    })
  }).then(function () {
    return tag(function () {
      const s = document.createElement('script')
      s.src = '/vendor/addon-fit.js'
      return s
    })
  }).then(function () {
    return window
  }).catch(function (e) {
    // A failed load must not be remembered as a success, or the next tab open does nothing.
    waiting = null
    throw e
  })

  return waiting
}

function tok(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

/* The terminal takes its colours from the same tokens as everything else, so it changes with
   the theme toggle instead of being a dark rectangle sitting in a light page. */
export function termTheme() {
  const dark = window.matchMedia('(prefers-color-scheme: dark)').matches
  const isDark = document.documentElement.dataset.theme === 'dark'
    || (!document.documentElement.dataset.theme && dark)
  return {
    background: tok('--bg') || (isDark ? '#0f0e0d' : '#ffffff'),
    foreground: tok('--text'),
    cursor: tok('--accent'),
    cursorAccent: tok('--bg'),
    selectionBackground: tok('--accent-bg'),
    selectionForeground: tok('--text'),
    black: isDark ? '#292524' : '#1c1917',
    red: tok('--low'),
    green: tok('--up'),
    yellow: tok('--waiting'),
    blue: tok('--accent'),
    magenta: tok('--accent'),
    cyan: tok('--accent'),
    white: tok('--text-dim'),
    brightBlack: tok('--text-faint'),
    brightRed: tok('--low'),
    brightGreen: tok('--accent'),
    brightYellow: tok('--waiting'),
    brightBlue: tok('--accent'),
    brightMagenta: tok('--accent'),
    brightCyan: tok('--accent'),
    brightWhite: tok('--text'),
  }
}
