/* The folded state of an answered ask. One quiet line: what was decided, and for a
   command, which one. Put it inside a <section className="ask done"> so the card
   collapses to this line and the rest of the card goes away. */

export default function Receipt({ verdict, detail, negative = false }) {
  return (
    <p className="receipt">
      <span className={negative ? 'verdict no' : 'verdict'}>{verdict}</span>
      {detail ? <code>{detail}</code> : null}
    </p>
  )
}
