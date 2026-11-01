/* One row per agent, every agent the daemon lists, whatever view is on. The view hides cards
   from a passer by; this is where the tags are set, so it must show the agents a view hides.
   The order is the daemon's, which is the order the Fleet grid draws.

   Each control saves the moment it changes. There is no Save button and no success message:
   the value simply stays. A failure puts one line under that row and the control goes back to
   what the daemon holds. */

import { useEffect, useState } from 'react'
import cc, { MODELS, PERMISSION_MODES, codexModels, permLabel } from '../../data/api'

/* The Codex models the signed-in ChatGPT account may use. A Claude name left on the record counts
   as the default: the daemon skips it. */
function CodexModels({ agent, model, save }) {
  const [list, setList] = useState([])
  useEffect(function () { codexModels().then(function (r) { setList(r.models || []) }, function () {}) }, [])
  const cur = /^(opus|sonnet|haiku|fable|claude)/i.test(model || '') ? '' : (model || '')
  const ids = list.map(function (m) { return m.id })
  return (
    <select className="cf-select" aria-label={'Codex model for ' + agent.name} value={cur}
      onChange={function (e) {
        const v = e.target.value
        save('model', v, function () { return cc.updateAgent(agent.id, { model: v || null }) })
      }}>
      <option value="">Codex default</option>
      {list.map(function (m) { return <option key={m.id} value={m.id}>{m.name}</option> })}
      {cur && !ids.includes(cur) ? <option value={cur}>{cur}</option> : null}
    </select>
  )
}
import { VIEWS, VIEW_LABELS } from '../../shell/views'

/* The tags an agent shows under. null or an empty list means all three, the same rule the
   daemon and showsIn() use. */
function tagsOf(agent) {
  return Array.isArray(agent.views) && agent.views.length ? agent.views : VIEWS
}

function Row({ agent, onSaved }) {
  /* What was just picked, held while the daemon is asked, so the control does not jump back
     to the old value for the length of one request. */
  const [held, setHeld] = useState({})
  const [error, setError] = useState('')

  async function save(field, value, call) {
    setHeld(function (h) { return { ...h, [field]: value } })
    setError('')
    try {
      await call()
      await onSaved()
    } catch (e) {
      setError(e.message || 'That did not save.')
    } finally {
      setHeld(function (h) { const next = { ...h }; delete next[field]; return next })
    }
  }

  const tags = 'views' in held ? held.views : tagsOf(agent)
  const model = 'model' in held ? held.model : (agent.model || '')
  const perm = 'perm' in held ? held.perm : (agent.permission_mode || '')

  /* A record on a model or a mode these lists do not hold keeps it, named as it stands. The
     chat's plus menu follows the same rule. */
  const models = !model || MODELS.includes(model) ? MODELS : MODELS.concat([model])
  const modes = !perm || PERMISSION_MODES.includes(perm) ? PERMISSION_MODES : PERMISSION_MODES.concat([perm])

  function flip(v) {
    const next = tags.includes(v)
      ? tags.filter(function (t) { return t !== v })
      : VIEWS.filter(function (t) { return t === v || tags.includes(t) })
    /* All three is stored as null, which is what the daemon does with a full list too. */
    const body = next.length === VIEWS.length ? null : next
    save('views', next, function () { return cc.updateAgent(agent.id, { views: body }) })
  }

  return (
    <li className="cf-row">
      <span className="cf-name">{agent.name}{agent.engine === 'codex' ? ' (Codex)' : agent.engine === 'opencode' ? ' (OpenCode)' : ''}</span>

      <div className="cf-controls">
        <fieldset className="cf-views">
          <legend className="cf-hide">{'Where ' + agent.name + ' shows'}</legend>
          {VIEWS.map(function (v) {
            const on = tags.includes(v)
            /* The last tag cannot be taken off. No tags at all means everywhere to the daemon,
               so the box would uncheck and then every box would come back checked. */
            const last = on && tags.length === 1
            return (
              <label key={v} className="cf-check">
                <input
                  type="checkbox"
                  checked={on}
                  disabled={last}
                  onChange={function () { flip(v) }}
                />
                {VIEW_LABELS[v]}
              </label>
            )
          })}
        </fieldset>

        <div className="cf-selects">
          {/* The model is passed to claude --model when the agent starts, so a change here
              applies on its next wake. A running agent keeps the model it woke with. */}
          {/* A Codex agent picks from its account's models (CodexModels), and from its next message. */}
          {agent.engine === 'codex' ? <CodexModels agent={agent} model={model} save={save} /> : <select
            className="cf-select"
            aria-label={'Model for ' + agent.name}
            value={model}
            onChange={function (e) {
              const v = e.target.value
              save('model', v, function () { return cc.updateAgent(agent.id, { model: v || null }) })
            }}
          >
            <option value="">Default model</option>
            {models.map(function (m) { return <option key={m} value={m}>{m}</option> })}
          </select>}

          {/* The permission mode goes through POST .../permission_mode, the same call the chat
              makes, and not through PATCH: API.md says the PATCH field is written unchecked and
              never reaches a live engine. The record is what the next wake uses; a live chat
              engine also takes the change at once. */}
          <select
            className="cf-select"
            aria-label={'Permission mode for ' + agent.name}
            value={perm}
            onChange={function (e) {
              const v = e.target.value
              save('perm', v, function () { return cc.setPermissionMode(agent.id, v) })
            }}
          >
            {!perm ? <option value="" disabled>no mode set</option> : null}
            {modes.map(function (m) { return <option key={m} value={m}>{permLabel(m)}</option> })}
          </select>
        </div>
      </div>

      {error ? <p className="cf-err">{error}</p> : null}
    </li>
  )
}

export default function AgentRows({ fleet }) {
  const { agents, refresh, loading } = fleet

  if (!agents.length) {
    return <p className="needs-empty">{loading ? 'Reading the fleet.' : 'No agents yet.'}</p>
  }

  return (
    <ul className="cf-rows agentcard">
      {agents.map(function (a) {
        return <Row key={a.id} agent={a} onSaved={refresh} />
      })}
    </ul>
  )
}
