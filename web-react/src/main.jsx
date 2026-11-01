import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles/app.css'
import App from './App.jsx'
import { installAwayReload } from './shell/awayReload'
import './styles/embed.css'

/* Inside Ergo Launcher's chat drawer (?embed=1): only the screen, no top bar and no tab bar. The
   launcher adds "glass" while the drawer is part way open, so home shows through, and takes it
   off when he drags it all the way up (from user feedback: "it should become the full chat
   with no transparent background"). */
if (new URLSearchParams(window.location.search).has('embed')) {
  document.documentElement.classList.add('embed')
}
window.ccGlass = function (on) { document.documentElement.classList.toggle('glass', !!on) }

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

// Back after a long time away, the page starts over, but never under a call. See shell/awayReload.js.
installAwayReload()

// The service worker does nothing. It exists so the page can be installed.
try {
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/sw.js').catch(function () {})
    })
  }
} catch {
  // An old browser blocks the check itself. The page still runs.
}
