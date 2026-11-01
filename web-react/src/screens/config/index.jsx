/* Config. Calm mode first, so it is one tap away on a hard day. What the fleet has used, then the settings of every agent, then what the front desk has to
   tell, then his prompt, then the prompt an agent reads when he asks for a task plan, then the CLAUDE.md files every agent reads, then who
   sounds like what on a call, then how the chat's microphone dictates, then the agents' saved
   logins, then the outside agents, then which WhatsApp chats the agents read, then what it is wired into, then how the
   machine is. Fourteen sections, in that order.

   Integrations and Health are the same bodies their own screens draw, so ?screen=integrations
   and ?screen=health still open and show the same thing. This screen owns one useFleet() and
   hands it to both the agent rows and the Health body, so the page opens one event stream. */

import { useState } from 'react'
import useFleet from '../../data/useFleet'
import IntegrationsBody from '../integrations/IntegrationsBody'
import HealthBody from '../health/HealthBody'
import AgentRows from './AgentRows'
import HiddenAgents from './HiddenAgents'
import UsageBody from './UsageBody'
import CalmMode from './CalmMode'
import InboxRows from './InboxRows'
import DeskPrompt from './DeskPrompt'
import cc from '../../data/api'
import TasksPrompt from './TasksPrompt'
import Instructions from './Instructions'
import DictationEngine from './DictationEngine'
import Logins from './Logins'
import CallSetup from './CallSetup'
import VoiceRows from './VoiceRows'
import WhatsApp from './WhatsApp'
import OutsideAgents from './OutsideAgents'
import { inApp } from '../../shell/native'
import ChimeRow from './ChimeRow'
/* Imported here, by the component that is always drawn for this screen, so the rules cannot
   leave the bundle when a child file goes. */
import './config.css'

/* The agents' voice prompt edits in the same box as the front desk's. */
const AGENT_VOICE = {
  id: 'cf-agent-voice-prompt', label: "The agents' voice prompt",
  load: cc.agentVoicePrompt, save: cc.saveAgentVoicePrompt, restore: cc.restoreAgentVoicePrompt,
}

// The shell passes onScreen to every screen. Nothing here leaves the page, so this one ignores it.
// theme and onTheme are the shell's: the switch moved here from the top bar.
export default function Config({ theme, onTheme }) {
  const fleet = useFleet({ health: true })
  const [inboxOpen, setInboxOpen] = useState(false)
  const waitingCount = fleet.inbox ? fleet.inbox.untold || 0 : 0

  return (
    <section className="screen" id="screen-config" aria-labelledby="config-h">
      <div className="screen-head">
        <h1 id="config-h">Config</h1>
      </div>

      <section className="cf-section" aria-labelledby="cf-theme-h" id="cf-theme">
        <h2 className="cf-head" id="cf-theme-h">Theme</h2>
        <div className="cf-meter">
          <div className="cf-meter-line cf-vm-line">
            <span className="cf-meter-label" id="cf-dark-h">Dark</span>
            <label className="cf-check">
              <input type="checkbox" checked={theme === 'dark'} aria-labelledby="cf-dark-h"
                onChange={function (e) { if (onTheme) { onTheme(e.target.checked ? 'dark' : 'light') } }} />
              On
            </label>
          </div>
        </div>
      </section>

      <section className="cf-section" aria-labelledby="cf-calm-section-h" id="cf-calm">
        <h2 className="cf-head" id="cf-calm-section-h">Calm</h2>
        <CalmMode />
      </section>

      <section className="cf-section" aria-labelledby="cf-usage-h">
        <UsageBody />
      </section>

      <section className="cf-section" aria-labelledby="cf-agents-h">
        <h2 className="cf-head" id="cf-agents-h">Agents</h2>
        <AgentRows fleet={fleet} />
      </section>

      <section className="cf-section" aria-labelledby="cf-hidden-h" id="cf-hidden">
        <h2 className="cf-head" id="cf-hidden-h">Hidden agents</h2>
        <HiddenAgents fleet={fleet} />
      </section>

      {/* Folded to its heading and a red count, opened by a tap (from user feedback:
          "Inbox should be collapsible and scrollable. This takes space"). */}
      <section className="cf-section" aria-labelledby="cf-inbox-h" id="cf-inbox">
        <h2 className="cf-head" id="cf-inbox-h">
          <button type="button" className="cf-fold" aria-expanded={inboxOpen}
            onClick={function () { setInboxOpen(!inboxOpen) }}>
            Inbox
            {waitingCount ? <span className="ac-count" aria-label={waitingCount + ' waiting'}>{waitingCount}</span> : null}
          </button>
        </h2>
        {inboxOpen ? <InboxRows fleet={fleet} /> : null}
      </section>

      <section className="cf-section" aria-labelledby="cf-desk-h" id="cf-desk">
        <h2 className="cf-head" id="cf-desk-h">The front desk</h2>
        <DeskPrompt />
      </section>

      <section className="cf-section" aria-labelledby="cf-agent-voice-h" id="cf-agent-voice">
        <h2 className="cf-head" id="cf-agent-voice-h">Agent calls</h2>
        <DeskPrompt doc={AGENT_VOICE} />
      </section>

      <section className="cf-section" aria-labelledby="cf-tasks-h" id="cf-tasks">
        <h2 className="cf-head" id="cf-tasks-h">Tasks</h2>
        <TasksPrompt />
      </section>

      <section className="cf-section" aria-labelledby="cf-md-h" id="cf-instructions">
        <h2 className="cf-head" id="cf-md-h">Instructions</h2>
        <Instructions />
      </section>

      <section className="cf-section" aria-labelledby="cf-voices-h" id="cf-voices">
        <h2 className="cf-head" id="cf-voices-h">Voices</h2>
        <VoiceRows fleet={fleet} />
      </section>

      <section className="cf-section" aria-labelledby="cf-chime-section-h">
        <h2 className="cf-head" id="cf-chime-section-h">Chime</h2>
        <ChimeRow />
      </section>

      <section className="cf-section" aria-labelledby="cf-dict-section-h">
        <h2 className="cf-head" id="cf-dict-section-h">Dictation</h2>
        <DictationEngine />
      </section>

      {inApp() ? (
        <section className="cf-section" aria-labelledby="cf-calls-h">
          <h2 className="cf-head" id="cf-calls-h">the front desk calls</h2>
          <CallSetup />
        </section>
      ) : null}

      <section className="cf-section" aria-labelledby="cf-logins-h">
        <h2 className="cf-head" id="cf-logins-h">Logins</h2>
        <Logins fleet={fleet} />
      </section>

      <section className="cf-section" aria-labelledby="cf-out-h">
        <h2 className="cf-head" id="cf-out-h">Outside agents</h2>
        <OutsideAgents />
      </section>

      <section className="cf-section" aria-labelledby="cf-wa-h" id="cf-whatsapp">
        <h2 className="cf-head" id="cf-wa-h">WhatsApp</h2>
        <WhatsApp />
      </section>

      <section className="cf-section" aria-labelledby="cf-int-h">
        <h2 className="cf-head" id="cf-int-h">Integrations</h2>
        <IntegrationsBody />
      </section>

      <section className="cf-section" aria-labelledby="cf-health-h">
        <h2 className="cf-head" id="cf-health-h">Health</h2>
        <HealthBody fleet={fleet} />
      </section>
    </section>
  )
}
