/* A chip. With a verb it is a tool chip: the verb is mono, the rest is plain text.
   The running prop is still accepted so callers keep working, but it draws nothing: the amber
   dot it used to add is gone, and the space stays empty. */

export default function Chip({ verb, children }) {
  return (
    <span className={verb ? 'chip tool' : 'chip'}>
      {verb ? <span className="mono">{verb}</span> : null}
      {children}
    </span>
  )
}
