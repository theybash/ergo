/* The turn still arriving. Every delta the daemon sends is the whole reply so far, never a
   piece to append, so this draws `live` as it stands and nothing is accumulated here.

   `done` is a text the turn already finished: written before a tool call, or the whole reply
   between `settled` and the history bringing its saved row. It has no cursor, and it stays on
   screen until the record has it, because taking it away shrank the page under the reader. */

import Prose from './Prose'

export default function LiveReply({ text, agent, done }) {
  return (
    <article className={done ? 'turn them streaming said' : 'turn them streaming'} aria-live={done ? undefined : 'polite'}>
      <Prose text={text} agent={agent} className="bubble wide">
        {done ? null : <span className="cursor" aria-hidden="true"></span>}
      </Prose>
    </article>
  )
}
