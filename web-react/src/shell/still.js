/* One answer to one question: has the reader asked for no motion?
   The shell, the cord and the voice overlay all need it, so it is read in one place. */

export default function still() {
  return Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches)
}
