/* What happened while you were away. It sits at the top of the log, above the oldest turn, and
   the x puts it away for this visit.

   Nothing feeds it yet. The daemon has no recap route, and the source it will read is the
   `away_summary` rows in the agent's session jsonl, which nothing writes or serves today. So
   the screen passes null and this draws nothing. It is kept because the shape is settled and
   the only missing piece is the route. */

export default function Recap({ recap, onClose }) {
  if (!recap) { return null }

  return (
    <section className="recap" id="recap" aria-labelledby="recap-k">
      <p className="recap-kicker" id="recap-k">{recap.kicker}</p>
      <p className="recap-body">{recap.body}</p>
      <button
        type="button"
        className="recap-x"
        id="recap-x"
        aria-label="Dismiss the recap"
        onClick={onClose}
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
          <path d="M6 6l12 12M18 6L6 18"></path>
        </svg>
      </button>
    </section>
  )
}
