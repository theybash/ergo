/* Integrations. What the fleet is wired into, and what it is not wired into yet.
   Nothing here connects anything, the cards only report where each service stands.

   A card's state is what the daemon measured on this laptop, or "not measured". See
   IntegrationsBody.jsx and commandcenter/integrations.py. */

import IntegrationsBody from './IntegrationsBody'

// The shell passes onScreen to every screen. No card here leaves the page, so this one ignores it.
export default function Integrations() {
  return (
    <section className="screen" id="screen-integrations" aria-labelledby="int-h">
      <div className="screen-head">
        <h1 id="int-h">Integrations</h1>
        <p>What the daemon can check on this laptop.</p>
      </div>

      <IntegrationsBody />
    </section>
  )
}
