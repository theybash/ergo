/* The messages in an agent's chat that the owner did not type. The daemon and Claude Code write them
   into the session as user rows, so the record says they are his. Drawn as his bubble they bury
   his real words, so each kind is read here and drawn as a system card instead.

     prefix                                      label                      from
     [scheduled job ID]                          Scheduled job ID           cron.py
     [Your scheduled job ID, a fresh run: x]     Scheduled job ID: x        cron.py
     [Task report]                               Task report                owners.py
     [Task yes] / [Task plan] / [Task no]        Insights: Yes, Plan, No    tasks.py, a card tap
     [About his own task ID: title] words        About his task: title      the task pin, his words
     [the owner, from the HH:MM alarm screen. ..]   Alarm screen, HH:MM        phone.py, his words
     [the owner, on a call with the front desk, about ..]   the front desk, about an alarm     voice.py, his words
     [Voice note from the owner, transcribed]       Voice note                 voicenote.py, his words
     [from the owner, over Telegram]                Telegram                   telegram.py, his words
     [From the assistant bar]                    Assistant bar              assist.py, his words
     [From the call with the owner]                 From the call              agentcall.py
     [the owner is on a voice call with you ..]     On a call, he asks         agentcall.py
     [the owner called you, ..] / [Your call ..]    Call, ..                   agentcall.py
     [you are on a call from X, over a cord]     Call from X                cords.py
     [you are calling X, over a cord]            Calling X                  cords.py
     [from X over a transfer cord]               Transfer from X            cords.py
     [X answered]                                X answered                 cords.py
     [From X, an outside agent ..] / [From X]    Note from X                outside.py, agents
     [@ergo in WhatsApp, chat]                   WhatsApp, chat             whatsapp.py
     [Alarm failed]                              Alarm failed               phone.py
     [a file arrived from the owner]                File arrived               files.py
     [A message that arrived before this one]    Held message               broker.py, mailbox.py
     [N messages arrived for you, oldest first]  N held messages            mailbox.py
     a task with "From the call with the owner"     From the front desk                voice.py
     <pasted_content ..>                         what is inside             Claude Code
     <task-notification>                         Background task            Claude Code
     <command-name> / <local-command-stdout>     Ran /x, Claude Code        Claude Code

   read() gives the parts to draw, in order, or null when the text is his own:
     { card: label, text }     a card, a few lines shown and the rest behind Show more
     { receipt: label, text }  a one line card, for a card tap or the machine part of his message
     { mine: text }            his own words, drawn as his bubble */

const ANSI = /\x1b\[[0-9;]*m/g // eslint-disable-line no-control-regex

function tags(text) {
  return String(text).replace(ANSI, '').replace(/<[^>]+>/g, '').replace(/\n{3,}/g, '\n\n').trim()
}

function mine(text) {
  const t = String(text || '').trim()
  return t ? [{ mine: t }] : []
}

/* The rest of a message that has a machine part in front: his words, or another kind. */
function rest(text) {
  const t = String(text || '').trim()
  if (!t) { return [] }
  return read(t) || mine(t)
}


/* Each kind its own element (from user feedback: "for each of these cards, it should become
   a different element of ui"). A part with `el` is drawn by Elements.jsx; `card` and `receipt`
   stay as the quiet fallback. `fold` marks the small ones that may join a run of others. */
const el = (kind, fields) => [{ el: kind, ...fields }]

/* The text inside a "begin text from ... / end of text from ..." block, or the text itself. */
function inside(text) {
  const t = String(text || '')
  const m = /--- begin text from [^\n]*---\n([\s\S]*?)\n--- end of text from [^\n]*---/.exec(t)
  return (m ? m[1] : t).trim()
}

/* Sentences that open many job tasks and say how to run, not what the job is. cron._name skips
   the same ones, so the chat and the daemon call a job by the same name. */
const SETUP = /^(COLD START\b|This runs as a fresh session|Before anything else|You are\b|Use only the\b|(Work|Personal) side\b)/i
export function jobName(task) {
  const said = String(task || '').split(/\s+/).join(' ').split(/(?<=[.!?:])\s/)
  let first = said.find(function (x) { return x && !SETUP.test(x) }) || said[0] || 'A job'
  first = first.replace(/\s*\([^)]*\)/g, '').replace(/[.:]+$/, '')
  return first.length <= 60 ? first : first.slice(0, 57).trim() + '...'
}

/* A fresh run's result, left for the agent: "Task: <its opening>", the transcript, the reply. */
function freshRun(id, outcome, after) {
  const task = (/^\s*Task: ([^\n]*)/.exec(after) || [])[1] || ''
  const text = inside(after)
  if (/^ASK CHECK\b/.test(task)) {
    // Its first plain line is what it did. The insight boxes are for the chat, not this line.
    const line = text.replace(/`?★[^\n]*\n[\s\S]*?\n`?─+`?(\n|$)/g, '').split('\n').map(function (l) { return l.replace(/[`*#]/g, '').trim() })
      .find(function (l) { return l && !/^[★─]/.test(l) && !/^─+$/.test(l) }) || 'Nothing new'
    return el('askcheck', { outcome: outcome, text: line, fold: true })
  }
  return el('job', { id: id, outcome: outcome, name: jobName(task), text: text, fold: true })
}

/* Each rule: a pattern on the start of the text, and what to draw. m is the match, after is the
   text after the match. The first rule that matches wins. */
const RULES = [
  [/^<pasted_content id="[^"]*">\n?([\s\S]*?)\n?<\/pasted_content id="[^"]*">/,
    (m, after) => rest(m[1]).concat(rest(after))],
  [/^<task-notification>[\s\S]*?<\/task-notification>/, function (m, after) {
    const summary = /<summary>([\s\S]*?)<\/summary>/.exec(m[0])
    const status = /<status>([\s\S]*?)<\/status>/.exec(m[0])
    const st = status ? status[1].trim() : ''
    return el('event', {
      icon: st === 'completed' ? 'done' : 'task', tone: st === 'failed' || st === 'killed' ? 'bad' : '',
      text: 'Background task ' + (st || 'news') + (summary ? ': ' + tags(summary[1]) : ''), fold: true,
    }).concat(rest(after))
  }],
  [/^<command-name>([^<]*)<\/command-name>/, (m) => el('event', { icon: 'command', text: 'Ran ' + m[1].trim(), fold: true })],
  [/^<local-command-(stdout|stderr|caveat)>/, (m, after) => el('event', { icon: 'command', text: tags(m[0] + after).split('\n')[0] || 'Claude Code', fold: true })],
  [/^\[scheduled job ([0-9a-z-]+)(, a fresh session)?\]/,
    (m, after) => el('job', { id: m[1], outcome: 'started', name: jobName(after), text: after.trim(), fold: true })],
  [/^\[Your scheduled job ([0-9a-z-]+), a fresh run: ([^\]\n]*)\]/,
    (m, after) => freshRun(m[1], m[2].trim(), after)],
  [/^\[Your watch job ([0-9a-z-]+)\]/, (m, after) => el('job', { id: m[1], outcome: '', name: 'Watch job', text: after.trim(), fold: true })],
  [/^\[Nag run\]/, (m, after) => el('job', { outcome: '', name: 'Nag run', text: after.trim(), fold: true })],
  [/^\[Task report\]/, (m, after) => el('report', { text: after.trim(), fold: true })],
  [/^\[Task yes\] ([^\n]*)/, (m) => el('answer', { answer: 'Yes', title: m[1].trim() })],
  [/^\[Task plan\] ([^\n]*)/, (m) => el('answer', { answer: 'Plan', title: m[1].trim() })],
  [/^\[Task no(, his reason)?\] ([^\n]*)/, function (m, after) {
    const why = (/His reason: ([\s\S]*)/.exec(after) || [])[1] || ''
    return el('answer', { answer: 'No', title: m[2].trim(), why: why.trim() })
  }],
  [/^\[About your feed post ([0-9a-z]+)(?: \(([^)\n]*)\))?:\n([\s\S]*?)\](?=\n\n|\s*$)/,
    (m, after) => el('feed', { id: m[1], kind: m[2] || '', post: m[3].trim() }).concat(mine(after))],
  [/^\[About your call to him, which he declined to answer in the chat: ([^\n]*)\]/,
    (m, after) => el('quote', { label: 'A call you declined', text: m[1] }).concat(mine(after))],
  [/^\[About this line of your OPEN\.md, [0-9a-z-]+: ([^\n]*)\]/,
    (m, after) => el('quote', { label: 'A line of your OPEN.md', text: m[1] }).concat(mine(after))],
  [/^\[About (his own )?task ([0-9a-f]{6,16}): (.*)\]/,
    (m, after) => el('task', { id: m[2], title: m[3].replace(/\. He has taken it up again; it is not done\.$/, '') }).concat(mine(after))],
  // The task title may hold a bracket, so the pattern runs to the task id, not the first "]".
  [/^\[the owner, from the (\d\d:\d\d) alarm screen\. (?:The alarm (?:is for his task "([^\n]*?)", task [0-9a-f]+|has no task))\] ?/,
    (m, after) => from('from the ' + m[1] + ' alarm' + (m[2] ? ', ' + m[2] : ''), after)],
  [/^\[the owner, on a call with the front desk, about the alarm for his task "([^\n]*?)", task [0-9a-f]+\.[^\]]*\]/,
    (m, after) => from('to the front desk, about ' + m[1], after)],
  [/^\[Voice note from the owner, transcribed\]/, (m, after) => from('voice note', after)],
  [/^\[From the assistant bar(, a voice memo, transcribed)?\] ?/,
    function (m, after) {
      // "He added his screen (app: X)." is the daemon's line, not his: it goes in the source.
      const screen = /\n*He added his screen(?: \(app: ([^,)]*)[^)]*\))?\.\n?/.exec(after)
      const said = screen ? after.replace(screen[0], '\n\n') : after
      const where = screen ? ', your screen' + (screen[1] ? ' in ' + screen[1] : '') : ''
      return from((m[1] ? 'assistant bar, voice memo' : 'assistant bar') + where, said.trim())
    }],
  [/^\[from (?:the operator|the owner), over Telegram\]/, (m, after) => from('over Telegram', after)],
  [/^\[From the call with the owner\]/, (m, after) => el('call', { title: 'From your call', text: after.trim() })],
  [/^\[the owner is on a voice call with you now and asks\]/, (m, after) => el('call', { title: 'On a call, you ask', text: after.trim() })],
  [/^\[([Tt]he owner called you|Your call with the owner), ([^\]\n]*)\]/,
    (m, after) => el('call', { title: (/called you$/.test(m[1]) ? 'You called' : 'Call with you') + ', ' + m[2], text: after.trim() })],
  [/^\[you are on a call from ([^\]\n]*), over a cord\]/, (m, after) => el('cord', { who: m[1], title: m[1] + ' called', text: after.trim() })],
  [/^\[you are calling ([^\]\n]*), over a cord\]/, (m, after) => el('cord', { who: m[1], title: 'Calling ' + m[1], text: after.trim() })],
  [/^\[from ([^\]\n]*) over a transfer cord\]/, (m, after) => el('cord', { who: m[1], title: m[1] + ' passed this on', text: inside(after) })],
  [/^\[([^\]\n]{1,40}) answered\]/, (m, after) => el('agent', { who: m[1], rel: 'answered', text: inside(after) })],
  [/^\[Report from ([^,\]\n]+), your delegate\]/, (m, after) => el('agent', { who: m[1], rel: 'your delegate', text: after.trim() })],
  [/^\[From ([^,\]\n]+), your parent\]/, (m, after) => el('agent', { who: m[1], rel: 'your parent', text: after.trim() })],
  [/^\[From ([^,\]\n]+), [^\]\n]*an outside agent[^\]\n]*\]/, (m, after) => el('agent', { who: m[1], rel: 'outside agent', text: inside(after) })],
  [/^\[(?:From|from) ([^,\]\n]{1,40})\]/, (m, after) => el('agent', { who: m[1], rel: '', text: after.trim() })],
  [/^\[@ergo in WhatsApp(?:, ([^\]\n]*))?\]/, (m, after) => el('wa', { chat: m[1] || '', text: inside(after) })],
  [/^\[WhatsApp, likely for the owner to act on, Jev [0-9.]+\]/, (m, after) => el('wa', { chat: '', text: inside(after) })],
  [/^\[Alarm failed\]/, (m, after) => el('event', { icon: 'alarm', tone: 'bad', text: after.trim() || 'An alarm did not reach the phone' })],
  [/^\[Email sent\]/, (m, after) => el('event', { icon: 'mail', text: after.trim(), fold: true })],
  [/^\[a file arrived from (?:the operator|[Tt]he owner)\]/, (m, after) => el('event', { icon: 'file', text: after.trim().split('\n')[0] || 'A file arrived', fold: true })],
  [/^\[Choice [0-9a-z-]+\] To "([\s\S]*?)", the owner picked: ([\s\S]*)$/,
    (m) => el('quote', { label: 'Your choice', text: m[1] }).concat(mine(m[2]))],
  [/^\[Cross-session delivery notice\]/, (m, after) => el('event', { icon: 'command', text: tags(after).split('\n')[0] || 'Claude Code', fold: true })],
  [/^\[Image: [^\]\n]*\]/, (m, after) => rest(after)],
  [/^\[attachment could not be downloaded\]/,
    (m, after) => el('event', { icon: 'file', tone: 'bad', text: 'An attachment could not be downloaded' }).concat(rest(after))],
]

/* His own words, with where they came from: a voice note, an alarm reply, Telegram. */
function from(source, after) {
  const words = mine(after)
  return words.length ? [{ ...words[0], source: source }] : el('event', { icon: 'phone', text: source, fold: true })
}

/* Held messages go in front of a new one: the held part, then "[Now]", then the new message.
   Each held message is drawn as itself again, his own words as his bubble, marked as having
   waited (from user feedback: two of his messages vanished into a "2 messages" fold). */
const HELD = /^\[(A message that arrived before this one|(\d+) messages arrived for you, oldest first)\]/
const NOW = '\n\n[Now]\n\n'
const AGO = /^--- ([^\n]*?) ago ---$/m

function heldParts(block, many) {
  const out = []
  const pieces = many ? block.split(/^--- (?=[^\n]*? ago ---$)/m) : [block]
  pieces.forEach(function (piece) {
    let text = piece
    let when = ''
    const m = many ? /^([^\n]*?) ago ---\n?/.exec(piece) : null
    if (m) { when = m[1] + ' ago'; text = piece.slice(m[0].length) }
    text = text.trim()
    if (!text) { return }
    const parts = read(text) || mine(text)
    parts.forEach(function (p, i) { out.push(i === 0 ? { ...p, waited: when || 'earlier' } : p) })
  })
  return out
}

/* A task the front desk passed on after a call: no prefix, but the call rides under this heading. */
const FROM_DESK = /\n\nFrom the call with the owner, last \d+ turns\n/

export function read(text) {
  const t = String(text || '').trim()
  const held = HELD.exec(t)
  if (held) {
    const cut = t.lastIndexOf(NOW)
    const before = cut >= 0 ? t.slice(held[0].length, cut) : t.slice(held[0].length)
    return heldParts(before.trim(), !!held[2] || AGO.test(before))
      .concat(cut >= 0 ? rest(t.slice(cut + NOW.length)) : [])
  }
  for (let i = 0; i < RULES.length; i += 1) {
    const m = RULES[i][0].exec(t)
    if (m) { return RULES[i][1](m, t.slice(m[0].length)) }
  }
  if (FROM_DESK.test(t)) { return el('call', { title: 'From the front desk, after a call', text: t }) }
  return null
}

/* The answer a question card sent, as Claude Code writes it into the tool's result. */
const ANSWERED = /^(?:Your questions have been answered|The user answered): ([\s\S]*?)(?:\. You can now continue[\s\S]*|\. Read the answers carefully[\s\S]*)?$/

export function answered(text) {
  const m = ANSWERED.exec(String(text || '').trim())
  if (!m) { return null }
  // "question"="answer" pairs, one to a line, as he would say them.
  return m[1].replace(/"([^"\n]*)"="([^"\n]*)",? ?/g, '$1 $2\n').trim()
}
