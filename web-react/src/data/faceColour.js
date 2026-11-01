/* One soft colour per agent, picked from its id, so a face is the same colour every day. */
const FACES = ['#9fc7ff', '#f2c48d', '#b7e4a0', '#e9b0d8', '#c9b8ff', '#8fe0d2', '#f5a3a3', '#e6dc8a']

export function faceColour(id) {
  let h = 0
  for (const ch of String(id || '')) { h = (h * 31 + ch.charCodeAt(0)) >>> 0 }
  return FACES[h % FACES.length]
}
