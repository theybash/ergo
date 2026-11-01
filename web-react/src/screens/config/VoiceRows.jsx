/* Who sounds like what on a call: The front desk, and every agent that can ring the owner. One row each, a
   voice picker and a play button, then the switch for opening an agent's call before he answers.

   A pick saves the moment it is made, like every other control on Config. The play button plays
   a few seconds the daemon made once in that voice. Some of the front desk's voices exist only on a live
   call and cannot be recorded, so those have no play button. An agent picks only from the voices
   that can be recorded, so its opening line and its call sound the same. A daemon without the
   setting draws nothing. */

import { useEffect, useRef, useState } from 'react'
import cc from '../../data/api'

function PlayGlyph({ on }) {
  return (
    <svg className="cf-play-glyph" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      {on ? <path d="M8 6h3v12H8zM13 6h3v12h-3z" /> : <path d="M8 5.5v13l10.5-6.5z" />}
    </svg>
  )
}

function VoiceRow({ who, label, value, choices, recordable, playing, onPlay, onPick }) {
  const [error, setError] = useState('')
  async function pick(e) {
    setError('')
    try { await onPick(who, e.target.value) } catch (err) { setError(err.message || 'That did not save.') }
  }
  const canPlay = recordable.includes(value)
  return (
    <li className="cf-row">
      <span className="cf-name">{label}</span>
      <div className="cf-controls">
        <div className="cf-selects">
          <label className="cf-hide" htmlFor={'cf-voice-' + who}>{'Voice for ' + label}</label>
          <select id={'cf-voice-' + who} className="cf-select" value={value} onChange={pick}>
            {choices.map(function (v) { return <option key={v} value={v}>{v}</option> })}
          </select>
          {canPlay ? (
            <button
              type="button"
              className="cf-play"
              aria-label={(playing ? 'Stop ' : 'Play ') + value}
              title={playing ? 'Stop' : 'Hear ' + value}
              onClick={function () { onPlay(who, value) }}
            >
              <PlayGlyph on={playing} />
            </button>
          ) : null}
        </div>
      </div>
      {error ? <p className="cf-err">{error}</p> : null}
    </li>
  )
}

export default function VoiceRows({ fleet }) {
  const [held, setHeld] = useState(null)
  const [playing, setPlaying] = useState('')
  const player = useRef(null)

  useEffect(function () {
    let live = true
    cc.voiceSettings().then(function (d) { if (live) { setHeld(d) } }, function () {})
    return function () {
      live = false
      if (player.current) { player.current.pause() }
    }
  }, [])

  if (!held || !held.voices || !held.voice_choices) { return null }

  async function pick(who, voice) {
    setHeld(await cc.saveVoiceSettings({ voices: { [who]: voice } }))
  }

  function play(who, voice) {
    if (player.current) { player.current.pause(); player.current = null }
    if (playing === who) { setPlaying(''); return }
    const a = new Audio(cc.voicePreviewUrl(voice))
    player.current = a
    setPlaying(who)
    a.addEventListener('ended', function () { setPlaying('') })
    a.addEventListener('error', function () { setPlaying('') })
    a.play().catch(function () { setPlaying('') })
  }

  async function prewarm(e) {
    setHeld(await cc.saveVoiceSettings({ prewarm: e.target.checked }))
  }

  async function record(e) {
    setHeld(await cc.saveVoiceSettings({ record_calls: e.target.checked }))
  }

  const agents = (fleet && fleet.agents) || []
  const recordable = held.voice_choices.agent
  return (
    <>
      <ul className="cf-rows agentcard">
        <VoiceRow
          who="desk"
          label="The front desk"
          value={held.voices.desk}
          choices={held.voice_choices.desk}
          recordable={recordable}
          playing={playing === 'desk'}
          onPlay={play}
          onPick={pick}
        />
        {agents.map(function (a) {
          return (
            <VoiceRow
              key={a.id}
              who={a.id}
              label={a.name || a.id}
              value={held.voices[a.id] || held.agent_voice}
              choices={held.voice_choices.agent}
              recordable={recordable}
              playing={playing === a.id}
              onPlay={play}
              onPick={pick}
            />
          )
        })}
      </ul>
      <label className="cf-check cf-prewarm">
        <input type="checkbox" checked={held.prewarm !== false} onChange={prewarm} />
        Open the call before I answer
      </label>
      {/* An older daemon sends no record_calls, and then there is nothing to switch. */}
      {typeof held.record_calls === 'boolean' ? (
        <label className="cf-check cf-prewarm">
          <input type="checkbox" checked={held.record_calls} onChange={record} />
          Record calls, both voices, kept on the laptop
        </label>
      ) : null}
    </>
  )
}
