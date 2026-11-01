/* A scheduled job's result, shown in the chat for the owner where it happened. The agent never reads
   it. It is a system card: the title is the result's first line, which the job's brief asks to say
   what happened, and the job's name follows it, dimmer. A run that did not succeed says so. */

import SystemCard from './SystemCard'

function stamp(at) {
  if (!at) { return '' }
  const d = new Date(at * 1000)
  if (isNaN(d.getTime())) { return '' }
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export default function JobCard({ item, agent }) {
  return (
    <div data-shown={item.id}>
      <SystemCard
        label={item.name}
        text={item.text}
        agent={agent}
        at={stamp(item.at)}
        failed={item.outcome !== undefined && item.outcome !== 'succeeded'}
      />
    </div>
  )
}
