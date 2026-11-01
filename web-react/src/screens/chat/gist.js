/* One readable line for a tool call. The daemon sends a tool's input as "key: value" lines, and
   the first line is often the least useful one: "command: ls" before the description that says
   what it is for, or a raw list of questions. This picks the line a person would read. */

const BEST = ['description', 'question', 'file_path', 'path', 'url', 'query', 'pattern',
  'command', 'prompt', 'subject', 'title', 'name']

export function gist(input) {
  const text = String(input || '')
  const lines = text.split('\n')
  const fields = {}
  lines.forEach(function (line) {
    const m = /^([a-z_]+): (.*)$/.exec(line)
    if (m && !(m[1] in fields)) { fields[m[1]] = m[2] }
  })
  if (fields.questions) {
    const q = /['"]question['"]: ['"]([^'"]+)/.exec(fields.questions)
    if (q) { fields.question = q[1] }
  }
  for (const k of BEST) {
    const v = fields[k]
    if (v && v.trim() && !/^[[{]/.test(v.trim())) { return v.trim().slice(0, 120) }
  }
  return (lines[0] || '').slice(0, 120)
}
