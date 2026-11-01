/* The keys a phone keyboard does not have.

   Written straight to the terminal, which is what a dialog is waiting for: it wants a keypress,
   not a line of text. Sending text would type into the dialog instead of answering it. They are
   in the chat composer as well as the terminal tab, because a permission dialog drawn inside the
   TERMINAL can only be answered this way. */

export const SEQ = {
  up: '\x1b[A',
  down: '\x1b[B',
  left: '\x1b[D',
  right: '\x1b[C',
  enter: '\r',
  esc: '\x1b',
  tab: '\t',
  shifttab: '\x1b[Z',
}

/* The five the phone gets, in the order a dialog needs them. */
export const PHONE_KEYS = [
  { id: 'shifttab', text: 'Shift Tab', label: 'Shift Tab' },
  { id: 'up', text: 'Up', label: 'Arrow up' },
  { id: 'down', text: 'Down', label: 'Arrow down' },
  { id: 'enter', text: 'Enter', label: 'Enter' },
  { id: 'esc', text: 'Esc', label: 'Escape' },
]

export default SEQ
