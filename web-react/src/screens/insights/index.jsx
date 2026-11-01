/* The insights screen. The task cards the agents offer, first, because they wait on him. Then his
   list, the agents at work, and the drafts waiting for his tap.

   What is gone and why (from user feedback: "delete the old insights placeholder stuff"):
   the deck of three cards had no history to draw and said so, the usage table is the same one
   Config shows, and the widgets section never had a widget. */

import useFleet from '../../data/useFleet'
import Tasks from './Tasks'
import Board from './Board'
import Drafts from './Drafts'
import MailDrafts from '../mail/MailDrafts'
import { showsIn } from '../../shell/views'
import useScrollMemory from '../../shell/scrollMemory'

export default function Insights({ onScreen, view }) {
  // Its own place on the page, not the one the last screen left (shell/scrollMemory.js).
  useScrollMemory('screen:insights', true)
  const fleet = useFleet()

  // No screen title: the tab already says where he is.
  return (
    <section className="screen" id="screen-insights" aria-label="Insights">

      {fleet.error ? <p className="note">The daemon did not answer: {fleet.error}</p> : null}

      {/* What the agents offer comes first, above his list. */}
      <Tasks view={view} onScreen={onScreen} />
      <Board view={view} onScreen={onScreen} />
      <MailDrafts framed label="Email drafts" keep={function (d) {
        const a = (fleet.agents || []).find(function (x) { return x.id === d.agent })
        return !view || view === 'all' || (!!a && showsIn(a, view))
      }} />
      <Drafts />
    </section>
  )
}
