/* A message as one line of plain words, for a preview. The card shows two lines of what the agent
   last said, and the markdown marks in it ("- ", "**", "`", "## ", a link's brackets) read as
   noise there. The chat still draws the message in full. */

export function plain(text) {
  return String(text || '')
    .split('\n')
    .map(function (line) {
      return line
        .replace(/^\s{0,3}(#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)/, '')
        .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
        .replace(/(\*\*|__)(.+?)\1/g, '$2')
        .replace(/`+([^`]*)`+/g, '$1')
        .replace(/(^|[\s(])[*_]([^*_\s][^*_]*)[*_](?=[\s).,:;!?]|$)/g, '$1$2')
        .trim()
    })
    .filter(Boolean)
    .join(' ')
}
