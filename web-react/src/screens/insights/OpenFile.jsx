/* A file an agent made, opened inside the app. A plain link downloads, and the phone's web view
   drops downloads, so a tap did nothing. Text opens in the Files tab's reader, a picture in the
   zoom view, and anything else still downloads. */

import { useState } from 'react'
import cc from '../../data/api'
import { FileView, IMAGE, TEXT } from '../chat/FilesTab'
import ImageView from '../chat/ImageView'

export default function OpenFile({ agent, name }) {
  const [open, setOpen] = useState(false)
  if (!name) { return null }
  function tap(e) {
    if (TEXT.test(name) || IMAGE.test(name)) { e.preventDefault(); setOpen(true) }
  }
  return (
    <>
      <a className="tk-link" href={cc.fileUrl(agent, name)} onClick={tap}>{name}</a>
      {open && TEXT.test(name) ? <FileView agent={agent} name={name} onClose={function () { setOpen(false) }} /> : null}
      <ImageView src={open && IMAGE.test(name) ? cc.fileUrl(agent, name) : ''} alt={name}
        onClose={function () { setOpen(false) }} />
    </>
  )
}
