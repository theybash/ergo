/* The three views of the fleet, in the order the switcher shows them. The old console kept the
   same three words and the same browser only rule: the view changes which agent cards the page
   draws and nothing else. It never reaches Telegram and it never stops a job running. */

export const VIEWS = ['work', 'demo', 'personal']

export const VIEW_LABELS = { all: 'All', work: 'Work', demo: 'Demo', personal: 'Personal' }

/* The menu's order. All first: every agent, whatever it is tagged. */
export const MENU_VIEWS = ['all', 'work', 'demo', 'personal']

/* An agent with no tags shows everywhere. That is every agent the platform has ever made, and
   a new agent vanishing until it is tagged would read as a bug. The fleet and the chat rail
   both use this, so one view hides the same agents on both screens. */
export function showsIn(agent, which) {
  if (!which || which === 'all') { return true }
  const tags = agent.views
  if (!Array.isArray(tags) || !tags.length) { return true }
  return tags.includes(which)
}

export default VIEWS
