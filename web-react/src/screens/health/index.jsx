/* Health, the screen. The readout itself is HealthBody, which the Config screen also holds, so
   ?screen=health and the Health section of Config show the same thing. */

import useFleet from '../../data/useFleet'
import HealthBody from './HealthBody'

export default function Health({ onScreen }) {
  const fleet = useFleet({ health: true })

  return (
    <section className="screen" id="screen-health" aria-labelledby="health-h">
      <div className="screen-head">
        <h1 id="health-h">Health</h1>
        <div className="fleet-tools">
          <button type="button" className="btn sm" onClick={function () { onScreen('fleet') }}>
            Back to the fleet
          </button>
        </div>
      </div>

      <HealthBody fleet={fleet} />
    </section>
  )
}
