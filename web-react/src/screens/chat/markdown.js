/* Markdown, the parts an agent actually writes: headings, lists, tables, code, quotes, links,
   bold and italic. Ported line for line from light() in web/app.js, which is the reference
   implementation.

   Line based rather than a library, because there is no room for one here either. It is escaped
   before a single rule runs, so an agent writing a bracket or a tag cannot reach the DOM.

   Not a general parser. It does not nest a list inside a quote, and it does not need to: this is
   a chat, and the aim is that a table looks like a table, not that every corner of CommonMark
   works. */

import { fileUrl } from '../../data/api'

/* Whose outbox an image is read from. Set by light() for the one synchronous render it does. */
let AGENT = ''

export function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  })
}

/* A URL safe to put in href. An agent writing javascript: or data: gets its link flattened to
   text rather than a working bomb. Everything else, including a plain relative path, is allowed. */
export function safeHref(url) {
  return /^(https?:\/\/|mailto:|\/|#)/i.test(url.trim()) ? url.trim() : null
}

/* The inside of a line. Code spans are split out first and left entirely alone, because what is
   inside a backtick is literal and must not be read as bold or as a link. Everything between the
   code spans then gets links, bold and italic, in that order. */
function inline(s) {
  return s.split(/(`[^`]+`)/).map(function (part) {
    let seg = part
    // The tags built below are set aside until bold and italic have run, then put back. The
    // underscore in target="_blank" paired with one in a file name like JPEG_2026..., and a
    // camera photo's src broke.
    const kept = []
    const keep = function (html) { kept.push(html); return '\u0000' + (kept.length - 1) + '\u0000' }
    if (seg.length > 1 && seg[0] === '`' && seg[seg.length - 1] === '`') {
      return '<code>' + seg.slice(1, -1) + '</code>'
    }
    // An image is a file in the agent's own outbox, named bare or by its path there. Nothing
    // else loads: a web address in an image would tell its owner that the owner read the message.
    seg = seg.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, function (all, alt, url) {
      // A file the owner sent the agent, shown in his own message: inbox:<agent>/<name>.
      const sent = /^inbox:([a-z0-9-]+)\/([^/:]+)$/.exec(url)
      if (sent) {
        const src = fileUrl(sent[1], sent[2].replace(/&amp;/g, '&')) + '?box=inbox'
        return keep('<a class="md-img" href="' + src + '" target="_blank" rel="noopener noreferrer">'
          + '<img src="' + src + '&thumb=1" alt="' + alt + '" loading="lazy"></a>')
      }
      const m = /^(?:.*\/\.platform\/outbox\/([^/]+)\/)?([^/:]+)$/.exec(url)
      if (!m || !AGENT) { return all }
      // A delegate may save into its parent's outbox, and a picture there must still show. A delegate's id starts
      // with its parent's, so that outbox is read too. Any other agent's stays shut.
      const box = m[1] && AGENT.indexOf(m[1] + '-') === 0 ? m[1] : AGENT
      // The chat loads a small copy; the link, which the zoom view opens, is the whole file.
      const src = fileUrl(box, m[2].replace(/&amp;/g, '&'))
      return keep('<a class="md-img" href="' + src + '" target="_blank" rel="noopener noreferrer">'
        + '<img src="' + src + '?thumb=1" alt="' + alt + '" loading="lazy"></a>')
    })
    seg = seg.replace(/(^|[^!])\[([^\]]+)\]\(([^)\s]+)\)/g, function (_, pre, label, url) {
      const href = safeHref(url)
      return pre + (href
        ? keep('<a href="' + href + '" target="_blank" rel="noopener noreferrer">') + label + keep('</a>')
        : label)
    })
    seg = seg.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    seg = seg.replace(/__([^_]+)__/g, '<strong>$1</strong>')
    seg = seg.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    seg = seg.replace(/(^|[^\w])_([^_\n]+)_/g, '$1<em>$2</em>')
    return seg.replace(/\u0000(\d+)\u0000/g, function (_, n) { return kept[+n] })
  }).join('')
}

/* A pipe row split into its cells, with the empty cell an outer pipe leaves at each end dropped. */
function cells(line) {
  const parts = line.split('|')
  if (parts[0].trim() === '') { parts.shift() }
  if (parts.length && parts[parts.length - 1].trim() === '') { parts.pop() }
  return parts.map(function (c) { return c.trim() })
}

const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/
const BOXRULE = /^\s*`([^`]*[─━═]{3,}[^`]*)`\s*$/
const TSEP = /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/

export function light(text, agent) {
  AGENT = agent || ''
  const lines = esc(text).replace(/\r\n?/g, '\n').split('\n')
  const out = []
  let i = 0
  let para = []

  function flush() {
    if (para.length) { out.push('<p>' + para.map(inline).join('<br>') + '</p>') }
    para = []
  }

  while (i < lines.length) {
    const line = lines[i]

    // A fenced code block, and an unclosed fence at the end of a streaming message reads as code
    // to the end rather than flipping the rest of the message in and out of a box as it arrives.
    if (/^\s*```/.test(line)) {
      flush()
      const body = []
      i++
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { body.push(lines[i++]) }
      if (i < lines.length) { i++ }
      // A copy button on every code block. The chat's log handles the click; see Log.jsx.
      out.push('<div class="codeblock"><button type="button" class="code-copy" aria-label="Copy code">Copy</button><pre>' + body.join('\n') + '</pre></div>')
      continue
    }

    if (RULE.test(line)) { flush(); out.push('<hr>'); i++; continue }

    // A line that is one code span of box rule, like the Insight notes' top and bottom lines
    // (each drew as an empty code box). It draws as a thin rule,
    // and any words on it, "★ Insight", stay as a plain line under it.
    const boxed = line.match(BOXRULE)
    if (boxed) {
      flush()
      const words = boxed[1].replace(/[─━═]{3,}/g, ' ').trim()
      out.push('<hr>' + (words ? '<p>' + words + '</p>' : ''))
      i++
      continue
    }

    const head = line.match(/^(#{1,6})\s+(.*)$/)
    if (head) {
      flush()
      // # and ## are both the top of a chat, so both become h3; deeper levels step down from there.
      const level = head[1].length <= 2 ? 3 : Math.min(head[1].length + 1, 6)
      out.push('<h' + level + '>' + inline(head[2].trim()) + '</h' + level + '>')
      i++
      continue
    }

    // A table: a pipe row with a dashed separator directly under it. Without the separator the
    // same row is just text that happens to hold a pipe, and is left alone.
    if (line.includes('|') && i + 1 < lines.length && TSEP.test(lines[i + 1])) {
      flush()
      const header = cells(line)
      i += 2
      const rows = []
      while (i < lines.length && lines[i].includes('|') && lines[i].trim() !== '') {
        rows.push(cells(lines[i++]))
      }
      const th = header.map(function (c) { return '<th>' + inline(c) + '</th>' }).join('')
      const trs = rows.map(function (r) {
        return '<tr>' + header.map(function (_, k) {
          return '<td>' + inline(r[k] || '') + '</td>'
        }).join('') + '</tr>'
      }).join('')
      out.push('<div class="tablewrap"><table><thead><tr>' + th + '</tr></thead><tbody>'
        + trs + '</tbody></table></div>')
      continue
    }

    // A run of list items of one kind. A blank line or anything that is not an item ends it.
    const bullet = /^\s*[-*+]\s+(.*)$/.test(line)
    const number = /^\s*\d+[.)]\s+(.*)$/.test(line)
    if (bullet || number) {
      flush()
      const ordered = number
      const re = ordered ? /^\s*\d+[.)]\s+(.*)$/ : /^\s*[-*+]\s+(.*)$/
      const items = []
      // The number the list starts at is the author's, so a list that begins at 3 says 3.
      const first = ordered ? parseInt(lines[i].match(/^\s*(\d+)/)[1], 10) : 1
      while (i < lines.length) {
        const m = lines[i].match(re)
        if (!m) {
          // Blank lines between items keep one list, as in any markdown: a list written with a
          // gap between its items was drawn as six lists that each said 1.
          let j = i
          while (j < lines.length && !lines[j].trim()) { j++ }
          if (j > i && j < lines.length && re.test(lines[j])) { i = j; continue }
          break
        }
        items.push('<li>' + inline(m[1]) + '</li>')
        i++
      }
      const tag = ordered ? 'ol' : 'ul'
      const start = ordered && first !== 1 ? ' start="' + first + '"' : ''
      out.push('<' + tag + start + '>' + items.join('') + '</' + tag + '>')
      continue
    }

    // The marker is &gt; here, not >, because the whole text was escaped before any of these
    // rules ran. Every other rule uses characters esc leaves alone; the quote is the one that
    // does not.
    if (/^\s*&gt;\s?/.test(line)) {
      flush()
      const q = []
      while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) {
        q.push(lines[i++].replace(/^\s*&gt;\s?/, ''))
      }
      out.push('<blockquote>' + q.map(inline).join('<br>') + '</blockquote>')
      continue
    }

    if (line.trim() === '') { flush(); i++; continue }

    para.push(line)
    i++
  }
  flush()
  return out.join('')
}

export default light
