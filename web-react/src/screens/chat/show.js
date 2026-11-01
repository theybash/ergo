/* Which rows belong to a show card, apart from the card itself, so Turn and Log can tell
   without loading the card. */

export const SHOW_TOOL = 'mcp__platform__show'
/* A feed post is drawn in the chat as the same card the feed shows. */
export const POST_TOOL = 'mcp__platform__feed_post'

/* The tool's own answer, {"shown": kind, ...}. The card already says it, so the row is not drawn. */
const SHOWN = /^\s*\{\s*("shown":\s*"(choice|tasks|report)"|"id":\s*"[a-f0-9]+",\s*"posted":\s*true)/

export function isShowResult(row) {
  return row.kind === 'result' && !row.error && SHOWN.test(row.text || '')
}

/* His pick, as the line the agent got. The daemon folds it into its card; this is the case where
   the card is too far back to be on screen. */
const PICKED = /^\[Choice [A-Za-z0-9_-]+\] .*? picked: (.+)$/s

export function picked(row) {
  if (row.kind !== 'text' || row.who !== 'operator') { return null }
  const m = PICKED.exec(String(row.text || '').trim())
  return m ? m[1] : null
}

/* A card the tool refused is not drawn. It stays a plain tool call, with its error, in the work. */
export function isShowCard(row) {
  return row.kind === 'tool' && (row.tool === SHOW_TOOL || row.tool === POST_TOOL) && !!row.inputRaw &&
    !row.refused
}
