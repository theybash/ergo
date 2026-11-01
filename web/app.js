/* Command center, browser side.
 *
 * No framework, on purpose. The whole interface is a list of cards and one terminal, and a
 * framework would be more code to keep working for years than the thing it renders.
 *
 * Two live connections:
 *   /api/events        server-sent events. "something changed" plus machine health.
 *   /ws/agent/<id>     a WebSocket per open terminal. Binary frames are terminal bytes and
 *                      travel untouched in both directions; text frames are JSON control.
 */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let STATE = { agents: [], cords: [], jobs: [], health: null, limits: {} };
let drawingFrom = null;      // set while a cord is being drawn between two cards

/* -- theme ------------------------------------------------------------------ */

/* Three states, and the middle one is the default: an explicit choice stamps the root element,
 * and following the system stamps nothing so prefers-color-scheme decides. */
const THEMES = ['system', 'light', 'dark'];

/* Work, demo and personal. It lives in the browser because the operator said so when asked whether
 * it should survive a reboot: "don't overengineer then, reload or reboot, last setting is fine until
 * i change it". So there is no server state and no request to make.
 *
 * What it does NOT do is as important as what it does. It never reaches Telegram, and it never
 * changes whether a job runs: a work-tagged agent with a job at 2pm runs it while you are in work
 * mode. The threat is somebody glancing at the screen, not an attacker. */
const MODES = ['personal', 'work', 'demo'];

function mode(next) {
  if (next) localStorage.setItem('cc-mode', next);
  const held = localStorage.getItem('cc-mode');
  return MODES.includes(held) ? held : 'personal';
}

/* An agent with no tags at all shows everywhere. That is the default for every agent the platform
 * has ever made, and a new agent vanishing from the page until it is tagged would read as a bug. */
function showsIn(agent, which) {
  const tags = agent.views;
  if (!Array.isArray(tags) || !tags.length) return true;
  return tags.includes(which);
}

function theme(next) {
  const now = next ?? localStorage.getItem('theme') ?? 'system';
  localStorage.setItem('theme', now);
  if (now === 'system') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.dataset.theme = now;
  $('#theme').innerHTML = `<svg viewBox="0 0 16 16" width="15" height="15"><use href="#i-${now}"/></svg>`;
  $('#theme').title = { system: 'Following the system', light: 'Light', dark: 'Dark' }[now];
  if (term) term.options.theme = termTheme();
  return now;
}

function tok(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/* The terminal takes its colours from the same tokens as everything else, so it changes with
 * the toggle instead of being a dark rectangle sitting in a light page. */
function termTheme() {
  const dark = matchMedia('(prefers-color-scheme: dark)').matches;
  const isDark = document.documentElement.dataset.theme === 'dark'
    || (!document.documentElement.dataset.theme && dark);
  return {
    background: tok('--bg') || (isDark ? '#0f0e0d' : '#ffffff'),
    foreground: tok('--text'),
    cursor: tok('--accent'),
    cursorAccent: tok('--bg'),
    selectionBackground: tok('--accent-bg'),
    selectionForeground: tok('--text'),
    black: isDark ? '#292524' : '#1c1917',
    red: tok('--low'),
    green: tok('--up'),
    yellow: tok('--waiting'),
    blue: tok('--accent'),
    magenta: tok('--accent'),
    cyan: tok('--accent'),
    white: tok('--text-dim'),
    brightBlack: tok('--text-faint'),
    brightRed: tok('--low'),
    brightGreen: tok('--accent'),
    brightYellow: tok('--waiting'),
    brightBlue: tok('--accent'),
    brightMagenta: tok('--accent'),
    brightCyan: tok('--accent'),
    brightWhite: tok('--text'),
  };
}

/* -- small helpers ---------------------------------------------------------- */

/* Lower case and never uppercased by CSS. "2M" in a label reads as two million, which is a real
 * misreading next to a token count in the same card. */
function ago(ts) {
  if (!ts) return '';
  const t = typeof ts === 'string' ? Date.parse(ts) / 1000 : ts;
  const d = Math.max(0, Date.now() / 1000 - t);
  if (d < 60) return `${Math.round(d)}s ago`;
  if (d < 3600) return `${Math.round(d / 60)}m ago`;
  if (d < 86400) return `${Math.round(d / 3600)}h ago`;
  return `${Math.round(d / 86400)}d ago`;
}

function big(n) {
  if (n == null) return 'none';
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(n >= 1e5 ? 0 : 1)}k`;
  return String(n);
}

function toast(msg, bad = false) {
  $('.toast')?.remove();
  const el = document.createElement('div');
  el.className = 'toast' + (bad ? ' bad' : '');
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), bad ? 6000 : 2600);
}

async function api(path, method = 'GET', body) {
  const r = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `${r.status}`);
  return data;
}

/* -- health readout --------------------------------------------------------- */

function renderHealth(h) {
  if (!h) return;
  const L = STATE.limits || {};
  const disk = (h.disks || []).find((d) => d.mount === '/') || (h.disks || [])[0];
  const items = [];

  const memLow = h.memory.available_gb < (L.mem_avail_gb ?? 1.5);
  items.push(['free', `${h.memory.available_gb.toFixed(1)}`, 'GB', memLow]);

  /* A busy processor is doing its job, so this never turns red. Colouring normal load teaches
   * the eye to ignore the colour, which costs you the one time it matters. */
  items.push(['cpu', `${Math.round(h.cpu.percent)}`, '%', false]);

  if (h.battery && h.battery.percent != null) {
    const low = !h.battery.plugged && h.battery.percent < (L.battery_pct ?? 20);
    items.push([h.battery.plugged ? 'charge' : 'battery', `${Math.round(h.battery.percent)}`, '%', low]);
  }
  if (disk) {
    const freePct = 100 - disk.percent;
    items.push(['disk', `${disk.free_gb.toFixed(0)}`, 'GB', freePct < (L.disk_free_pct ?? 10)]);
  }
  const press = (h.pressure || {}).memory;
  if (press != null) items.push(['pressure', press.toFixed(2), '', press > (L.pressure ?? 1)]);

  $('#readout').innerHTML = items.map(([k, v, u, low]) =>
    `<span class="m${low ? ' low' : ''}"><span class="k">${k}</span>` +
    `<span class="v">${v}${u ? `<span class="unit">${u}</span>` : ''}</span></span>`).join('');
}

/* -- cards ------------------------------------------------------------------ */

function cardHTML(a) {
  const green = a.state === 'green';
  /* "tokens", not "context". The number is everything the agent has read across every turn, which
   * is what a bill is made of. Calling it context would suggest the size of its current window,
   * which is a different and much smaller number. */
  const cost = a.cost_usd != null
    ? ['cost', `$${a.cost_usd.toFixed(2)}`]
    : ['tokens', big(a.context_tokens) + ' read'];

  /* An agent's own server, and only when there is one to say something about. It keeps running while
   * the agent sleeps, so this line is not tied to green: a grey agent with a live server is the
   * normal way a screen keeps working overnight. The error is shown rather than just "down",
   * because "down" sends you looking and the reason is already known here. */
  const s = a.server || {};
  const server = s.up ? `up on ${s.port}`
    : s.error ? `down — ${esc(s.error).slice(0, 60)}`
    : s.wanted ? 'starting' : '';

  return `<article class="card${drawingFrom === a.id ? ' source' : ''}${
    drawingFrom && drawingFrom !== a.id ? ' picking' : ''}" data-id="${a.id}">
    <div class="top">
      <span class="${a.working ? 'pulse' : 'dot' + (green ? ' green' : '')}"
            title="${a.working ? 'working' : green ? 'awake' : 'asleep'}"></span>
      <span class="nm"><a href="#/a/${a.id}">${esc(a.name)}</a></span>
      ${a.role ? `<span class="role">${esc(a.role)}</span>` : ''}
      ${a.schedule ? `<span class="alarm" title="scheduled">alarm</span>` : ''}
      ${a.waiting ? `<button class="held" data-do="held" title="${a.waiting === 1
        ? 'One message you left, not yet delivered. Click to read it or delete it.'
        : `${a.waiting} messages you left, not yet delivered. Click to read or delete them.`}">${
        a.waiting} undelivered</button>` : ''}
      <span class="grow"></span>
      <span class="when">${ago(a.last_message_at)}</span>
    </div>

    <div class="said${a.last_message ? '' : ' none'}" title="${esc(a.last_message).slice(0, 900)}">${
      a.last_message ? esc(a.last_message.slice(0, 420)) : 'Has not said anything yet.'}</div>

    <div class="rows">
      <div class="r"><span class="label">turns</span><span class="v">${a.turns}</span></div>
      <div class="r"><span class="label">${cost[0]}</span><span class="v">${cost[1]}</span></div>
      ${server ? `<div class="r"><span class="label">server</span><span class="v">${server}</span></div>` : ''}
    </div>

    <div class="act">
      <!-- Open, not Chat. There was a message box on the card as well as the conversation inside
           the agent page, which is two ways to say the same thing and two places to look for what
           you said. The card is a card: it shows the agent and it opens it. -->
      <a class="btn small" href="#/a/${a.id}">Open</a>
      ${green
        ? `<button class="btn small" data-do="sleep">Sleep</button>`
        : `<button class="btn small" data-do="wake">Wake</button>`}
      <button class="btn small" data-do="cord" ${green ? '' : 'disabled title="wake it first"'}>Cord</button>
      <span class="grow"></span>
      <button class="btn small danger" data-do="hide" title="Remove the card. The agent and its work are untouched.">Hide</button>
    </div>
  </article>`;
}

function renderCards() {
  const all = STATE.agents;
  const which = mode();
  const list = all.filter((a) => showsIn(a, which));
  const gone = all.length - list.length;
  $('#viewmode').textContent = which[0].toUpperCase() + which.slice(1);
  /* Say how many are hidden by the mode. A card that is simply missing reads as an agent that has
   * gone, and you go looking for it in the wrong place. */
  $('#viewmode').title = gone
    ? `${gone} agent${gone > 1 ? 's' : ''} hidden in ${which} view. Click to change.`
    : 'Which agents this page shows';
  $('#empty').hidden = all.length > 0;
  $('#cards').hidden = list.length === 0;
  $('#cards').innerHTML = list.map(cardHTML).join('')
    + (gone ? `<div class="quiet-note">${gone} more hidden in ${which} view.</div>` : '');
  $('#show-hidden').hidden = !(STATE.hidden || []).length;
  $('#show-hidden').textContent = `Hidden (${(STATE.hidden || []).length})`;
  renderCords();
  renderJobs();
  renderWires();
}

function renderCords() {
  /* The same rule as the wires: once a cord has been finished longer than the trace lasts it is
   * history, whether or not the daemon has got round to forgetting it. */
  const cs = (STATE.cords || []).filter((c) => {
    const age = finishedAgo(c);
    return age == null || age <= TRACE_MS;
  });
  const box = $('#cords');
  box.hidden = cs.length === 0;
  const name = (id) => STATE.agents.find((a) => a.id === id)?.name || id;
  box.innerHTML = cs.map((c) => `<div class="c">
      <span class="kind ${c.kind}">${c.kind}</span>
      <span class="path">${esc(name(c.from))} &rarr; ${esc(name(c.to))}</span>
      <span class="inst">${esc(c.instruction)}</span>
      ${c.kind === 'call' ? `<span class="st">${c.used}/${c.budget}</span>` : ''}
      <span class="st ${c.state}">${c.state.replace('_', ' ')}</span>
      ${c.state === 'running' && c.kind === 'call'
        ? `<button class="btn small" data-hangup="${c.id}">Hang up</button>` : ''}
    </div>`).join('');
}

/* -- the chat --------------------------------------------------------------- */

/* One source of truth for the past, one for the present. The session file holds the settled
 * conversation as structured data, including every tool call, but Claude Code writes an answer into
 * it once, at the end of the turn: a 350 word reply landed as a single write 19 seconds after the
 * question. The agent's own socket carries the turn while it is still being written.
 *
 * Before this, the present tense was read off the terminal's screen with regular expressions, and
 * every box drawing character Claude Code moved broke it. The screen scraper is gone. The engine
 * behind the chat now streams its turn as JSON, so the browser is handed data instead of a picture.
 *
 * So: history from the file, the message still arriving from the socket. The file has no present,
 * and the socket keeps no past. */
let chatFor = null, chatTimer = null, chatSock = null, chatRetry = null, chatEndTimer = null;
let chatShown = [], chatWant = 30, chatFetchedAt = 0;
/* The turn in flight. `chatLive` is the whole reply so far and not an increment, because the server
 * resends the full text on every delta: a frame lost in a tunnel then costs nothing. `chatTools` are
 * the calls this turn has made, kept only until it settles, because the record holds the real ones
 * from that moment, with their input and their result. */
let chatLive = '', chatTools = [], chatNotes = [];
let chatThinking = false, chatMode = 'off';
/* Your own message, held here from the moment you send it until the record catches up. The server
 * takes the text and answers; the turn reaches the session file a moment after that. Reading the
 * record straight away therefore misses it, and the next read is seconds later, which looks exactly
 * like a message that went nowhere. */
let chatEcho = null;

/* The asks waiting for an answer, in the order they arrived. An ask is a question the engine sends
 * and then waits on: a permission ask before it runs something, or a question ask it wrote itself.
 *
 * Nothing here survives a socket. The server re-sends the permission mode and every waiting ask
 * whenever somebody attaches, so the cards are built from frames and a phone that slept misses
 * none of them.
 *
 * Nothing here times out either, which is what the owner asked for. A card that expired on its own
 * would either answer for him or throw away an answer he was still typing. */
let chatAsks = [];
/* The permission mode the agent is in now, which is what the switcher in the composer shows. */
let chatPerm = '';

/* The three modes the server takes. It refuses any other, so the switcher offers no other. */
const PERM_MODES = ['acceptEdits', 'plan', 'bypassPermissions'];

/* How many settled rows to hold. A conversation is read from the bottom, so the top is worth
 * fetching only when somebody scrolls to it. */
const CHAT_FIRST = 30, CHAT_MORE = 60, CHAT_MOST = 200;

function unmountChat() {
  if (chatTimer) clearInterval(chatTimer);
  if (chatRetry) clearTimeout(chatRetry);
  if (chatEndTimer) clearTimeout(chatEndTimer);
  chatTimer = null; chatRetry = null; chatEndTimer = null;
  const holder = document.getElementById('chatpending');
  if (holder) holder.innerHTML = '';
  chatShown = []; chatWant = CHAT_FIRST; chatFetchedAt = 0;
  chatNotes = []; chatThinking = false; chatMode = 'off';
  chatEcho = null;
  chatAsks = [];
  setPermMode('');
  clearLive();
  paintNotes();
  paintThinking();
  /* Cleared before the close, so the reconnect below sees a chat that has moved on and gives up. */
  chatFor = null;
  try { chatSock?.close(); } catch {}
  chatSock = null;
}

/* The chat opens its own socket rather than borrowing the terminal tab's.
 *
 * Either would carry it: the frames the chat wants and the bytes the terminal wants travel on the
 * same `/ws/agent/<id>`, text and binary. Its own wins because the two tabs own their sockets at
 * different moments. `openTerminal` and `closeTerminal` build and drop theirs whenever the terminal
 * tab is looked at, and the chat would then have to survive a socket closing under it for a reason
 * that has nothing to do with the chat. One owner each is less to go wrong.
 *
 * `wake=0`, as the old screen reader had it: opening the chat tab to read yesterday's conversation
 * must not wake a sleeping agent. The size is asked large on purpose. A terminal is fitted to its
 * smallest watcher, so asking for 150 columns never shrinks anybody: if a phone is attached at 40,
 * it is already 40. */
function chatOpen(id) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  chatSock = new WebSocket(`${proto}://${location.host}/ws/agent/${id}?rows=46&cols=150&wake=0`);
  chatSock.binaryType = 'arraybuffer';
  chatSock.onmessage = (ev) => {
    /* Binary on this socket is raw terminal output, which is the other tab's business. */
    if (typeof ev.data !== 'string') return;
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }
    /* The terminal's own notices travel here too, `attached` and `grey` and the rest. They carry a
     * `type`, the chat's frames carry a `t`, so each side reads only its own. */
    if (msg.t) chatFrame(msg);
  };
  /* A phone that sleeps loses its socket without telling anybody, and wakes to a chat that never
   * updates again. Reattaching costs nothing and is the whole catch up: the server sends mode,
   * state, and the text of a turn in flight, every time somebody attaches. */
  chatSock.onclose = () => {
    if (chatFor !== id) return;
    chatRetry = setTimeout(() => { if (chatFor === id) chatOpen(id); }, 1500);
  };
}

/* The turn as it happens. Every frame is a statement about now, never a patch on something said
 * earlier, so one arriving late or not at all leaves the chat correct rather than half drawn. */
function chatFrame(m) {
  switch (m.t) {
    /* Which engine this agent is running. "off" means nothing is running, which is the resting
     * state of a sleeping agent and not a fault. */
    case 'mode':
      chatMode = m.mode || 'off';
      $('#tab-chat').dataset.mode = chatMode;
      paintThinking();
      break;
    case 'state':
      chatThinking = m.state === 'thinking';
      paintThinking();
      break;
    case 'live':
      if (m.phase === 'start') {
        chatLive = ''; chatTools = []; settleSoon(false); paintLive();
      } else if (m.phase === 'delta') {
        chatLive = m.text || ''; settleSoon(false); paintLive();
      } else if (m.phase === 'end') {
        paintLive(); settleSoon(true);
      }
      break;
    case 'tool':
      chatTools.push({ name: m.name || 'tool', summary: m.summary || '' });
      paintLive();
      break;
    case 'settled':
      settleSoon(false);
      clearLive();
      if (chatFor) paintHistory(chatFor, { force: true });
      break;
    case 'echo':
      if (m.text) { chatEcho = { id: chatFor, text: m.text, at: Date.now(), held: false }; drawEcho(); }
      break;
    case 'note':
      noteChat(m.text);
      break;
    /* An ask, drawn as a card under the conversation. A repeated id is a card already on screen:
     * the server re-sends every waiting ask on each attach, so a reconnect would otherwise stack
     * the same question twice. */
    case 'ask':
      if (!m.id || chatAsks.some((a) => a.id === m.id)) break;
      if (m.kind === 'question' && !(m.questions || []).length) {
        noteChat('An ask arrived with no question in it.');
        break;
      }
      chatAsks.push(m);
      paintPending();
      break;
    /* Answered elsewhere, or withdrawn. The card goes without a word. Whoever answered it in
     * another window knows they did, and a toast would explain something nobody asked about. */
    case 'ask_done':
      dropAsk(m.id);
      break;
    case 'perm_mode':
      setPermMode(m.mode);
      break;
  }
}

/* `end` says the writing stopped. `settled` says the record has it. Between the two the reply must
 * stay on screen: dropping it at `end` blanked a finished answer for the second or two the session
 * file took to catch up, and it came back looking like a bug.
 *
 * If `settled` never arrives, this settles the turn anyway. Without it a reply that lost its last
 * frame would sit above its own settled twin for as long as the tab stayed open, the same words
 * twice. */
function settleSoon(arm) {
  if (chatEndTimer) { clearTimeout(chatEndTimer); chatEndTimer = null; }
  if (!arm) return;
  const id = chatFor;
  chatEndTimer = setTimeout(() => {
    chatEndTimer = null;
    if (chatFor !== id) return;
    clearLive();
    paintHistory(id, { force: true });
  }, 8000);
}

/* The message still arriving, kept as one element under everything the record holds. It is the only
 * thing that changes while a turn runs, so the settled list above it is left alone: rebuilding that
 * three times a second threw away scroll position, text selection and every tool row you had opened.
 *
 * paintHistory knows this element by its id and puts it back after a rebuild, so the tool rows live
 * inside it too. They go when it goes, which is right: the record then holds the real calls. */
function paintLive() {
  const log = $('#chatlog');
  if (!chatLive && !chatTools.length) { clearLive(); return; }
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  let node = document.getElementById('chatlive');
  if (!node) {
    node = document.createElement('div');
    node.id = 'chatlive';
    log.appendChild(node);
  }
  const html = (chatLive ? chatRow({ kind: 'text', who: 'agent', text: chatLive, live: true }) : '')
    + chatTools.map((t) => '<div class="tool running">'
        + `<span class="name">${esc(t.name)}</span>`
        + `<span class="gist">${esc(t.summary)}</span></div>`).join('');
  /* Compared before it is written. The browser rebuilds the subtree on every assignment, and doing
   * that for an unchanged string drops any text the operator had selected inside it. */
  if (node.innerHTML !== html) node.innerHTML = html;
  paintThinking();
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function clearLive() {
  document.getElementById('chatlive')?.remove();
  chatLive = ''; chatTools = [];
  paintThinking();
}

/* A one off notice from the server, "The terminal exited." and its like. It sits under the log
 * rather than in it, because the log is rebuilt from the record on every paint and a note is not in
 * the record. Only the last few are kept: a notice is worth one read. */
function paintNotes() {
  const box = document.getElementById('chatnotes');
  if (!box) return;
  box.innerHTML = chatNotes.map((n) =>
    `<div class="note${n.going ? ' going' : ''}">${esc(n.text)}</div>`).join('');
  box.hidden = !chatNotes.length;
}

/* One place that adds a notice. The server sends them and a refused answer writes one, and both cap
 * the list at the same three. */
let noteSeq = 0;

/* A notice is worth one read. It stays a second, fades, and goes, so the chat is not carrying a
   sentence about something that finished a minute ago. The timer is per note, because two can
   arrive together and the second must not inherit the first one's remaining time. */
const NOTE_SHOWN = 4000, NOTE_FADE = 1000;
function noteChat(text) {
  if (!text) return;
  const note = { text, id: ++noteSeq, going: false };
  chatNotes.push(note);
  if (chatNotes.length > 3) chatNotes.shift();
  paintNotes();
  setTimeout(() => {
    note.going = true;
    paintNotes();
    setTimeout(() => {
      const i = chatNotes.indexOf(note);
      if (i >= 0) chatNotes.splice(i, 1);
      paintNotes();
    }, NOTE_FADE);
  }, NOTE_SHOWN);
}

/* Three dots, while the agent is working and has written nothing yet. They go the moment the first
 * words arrive, because the words say the same thing better. They also stay away while the terminal
 * is the engine: the chat cannot show a turn it is not being told about. */
function paintThinking() {
  const dots = document.getElementById('chatthinking');
  if (!dots) return;
  /* No dots while an ask waits. The engine is not thinking then, it is waiting on us, and dots
   * over a card asking to run a command say the opposite of what is true. */
  dots.hidden = !(chatThinking && !chatLive && !chatAsks.length && chatMode !== 'terminal');
}

/* -- asks ------------------------------------------------------------------- */

/* The cards under the conversation, one per ask. They are added and removed one at a time and never
 * rebuilt as a block, because a rebuild would throw away the words somebody was typing into the
 * free answer of a different card. */
function paintPending() {
  const box = document.getElementById('chatpending');
  if (!box) return;
  /* Compared as text on both sides. A card carries its id in a data attribute, and a data attribute
   * is always a string, so a number from the server would match nothing and the card would be
   * dropped and drawn again on every frame. */
  const want = new Set(chatAsks.map((a) => String(a.id)));
  for (const node of [...box.children]) if (!want.has(node.dataset.ask)) node.remove();
  const have = new Set([...box.children].map((n) => n.dataset.ask));
  for (const ask of chatAsks) {
    if (have.has(String(ask.id))) continue;
    box.appendChild(ask.kind === 'perm' ? permCard(ask) : questionCard(ask));
  }
  paintThinking();
}

function dropAsk(id) {
  chatAsks = chatAsks.filter((a) => a.id !== id);
  paintPending();
}

/* A permission ask. The command is the whole card, so it is shown in full and never shortened: the
 * button says Approve, and the only thing that makes that safe is seeing every character of what it
 * approves. A cut command hides its own tail, which is exactly where a pipe into a shell sits. The
 * summary is the fallback for a tool that has no command to show, a file write or an edit. */
function permCard(ask) {
  const node = document.createElement('div');
  node.className = 'pending perm';
  node.dataset.ask = ask.id;
  const shown = (ask.input && ask.input.command) || ask.summary || '';
  node.innerHTML = `<div class="q">Allow <span class="k">${esc(ask.tool || 'tool')}</span>?</div>`
    + `<pre>${esc(shown)}</pre>`
    + '<div class="act"><button type="button" class="no">Deny</button>'
    + '<button type="button" class="yes">Approve</button></div>';
  node.querySelector('.no').onclick = () => answerPerm(node, ask.id, false);
  node.querySelector('.yes').onclick = () => answerPerm(node, ask.id, true);
  return node;
}

async function answerPerm(node, id, allow) {
  busyCard(node, true);
  if (await postAnswer(id, { allow })) dropAsk(id);
  else busyCard(node, false);
}

/* A question the agent asked, with the options it wrote. One block per question, because an ask may
 * carry several and each one is answered on its own. Every label and every description is model
 * output, so all of it is escaped before it reaches the page. */
function questionCard(ask) {
  const qs = ask.questions || [];
  const node = document.createElement('div');
  node.className = 'pending question';
  node.dataset.ask = ask.id;
  node.innerHTML = qs.map((q, i) => {
    const opts = (q.options || []).map((o) =>
      `<button type="button" class="opt" data-label="${esc(o.label || '')}">`
      + `<span class="txt"><b>${esc(o.label || '')}</b>`
      + (o.description ? `<em>${esc(o.description)}</em>` : '')
      + '</span></button>').join('');
    return `<div class="qb" data-q="${i}">`
      + (q.header ? `<div class="hd">${esc(q.header)}</div>` : '')
      + `<div class="q">${esc(q.question || '')}</div>`
      + opts
      + (q.multiSelect ? '<div class="hint">Pick as many as you want, then Send.</div>' : '')
      + '<form class="free"><input type="text" autocomplete="off" placeholder="Or say it yourself">'
      + '<button type="submit">Send</button></form>'
      + '</div>';
  }).join('');

  for (const block of node.querySelectorAll('.qb')) {
    const q = qs[Number(block.dataset.q)] || {};
    for (const b of block.querySelectorAll('.opt')) {
      b.onclick = () => {
        /* One option answers the question the moment it is tapped. Several options are collected
         * first, because with multiSelect the person is not finished until they say so. */
        if (!q.multiSelect) return answerQuestion(node, block, ask.id, q.question, b.dataset.label);
        b.classList.toggle('on');
      };
    }
    block.querySelector('.free').onsubmit = (ev) => {
      ev.preventDefault();
      const typed = block.querySelector('input').value.trim();
      const picked = [...block.querySelectorAll('.opt.on')].map((b) => b.dataset.label);
      /* Taps and words both count. Dropping the picked labels because somebody also typed would
       * make those taps do nothing, so both go, in the order they were given. */
      const answer = [...picked, ...(typed ? [typed] : [])].join(', ');
      if (answer) answerQuestion(node, block, ask.id, q.question, answer);
    };
  }
  return node;
}

/* One answer per question, keyed by the question's own text exactly as it arrived. That text is
 * what the engine matches the answer back to, so it is copied, never tidied. */
async function answerQuestion(node, block, id, question, answer) {
  busyCard(node, true);
  const ok = await postAnswer(id, { answers: { [question || '']: answer } });
  busyCard(node, false);
  if (!ok) return;
  /* An ask may hold more than one question. The answered one steps aside and the card goes when the
   * last of them is answered. */
  block.remove();
  if (!node.querySelector('.qb')) dropAsk(id);
}

/* A second tap while the first is still in flight would answer the same ask twice. */
function busyCard(node, on) {
  node.classList.toggle('busy', on);
  for (const b of node.querySelectorAll('button')) b.disabled = on;
}

/* The one place that posts an answer. On ok the card goes at once rather than waiting for the
 * server's `ask_done`, because a tap has to feel finished. A refusal leaves the card exactly where
 * it was, so the ask is not lost, and puts the reason in the notes box under the log: a toast is a
 * bad home for a reason you may need to read twice. */
async function postAnswer(id, body) {
  const agent = chatFor;
  if (!agent) return false;
  try {
    const r = await api(`/api/agents/${agent}/answer`, 'POST', { id, ...body });
    if (r && r.ok === false) throw new Error(r.error || 'the answer was refused');
    return true;
  } catch (e) {
    if (chatFor === agent) noteChat(e.message);
    return false;
  }
}

/* The permission mode, shown in the composer bar. An agent running a mode this list does not hold,
 * "default" from before these three were the set, is named as it stands rather than reported as one
 * of the three: a switcher naming the wrong mode is worse than one naming a mode it cannot set. */
function setPermMode(mode) {
  chatPerm = mode || '';
  const sel = document.getElementById('chatperm');
  if (!sel) return;
  const rows = PERM_MODES.slice();
  if (chatPerm && !rows.includes(chatPerm)) rows.push(chatPerm);
  /* Nothing known yet, so nothing is claimed. Without this first option the select would show the
   * first mode on the list and look like an answer. */
  const head = chatPerm ? '' : '<option value="" selected>mode</option>';
  sel.innerHTML = head + rows.map((m) =>
    `<option value="${esc(m)}"${m === chatPerm ? ' selected' : ''}>${esc(m)}</option>`).join('');
  sel.title = chatPerm ? `Permission mode: ${chatPerm}` : 'Permission mode';
}

/* Which engine the agent should run. The tab you are looking at is the only thing that knows, so
 * the browser says it rather than the server guessing. Quiet on failure: an agent that is asleep,
 * or a daemon that does not know this route yet, must not put a red toast over a conversation you
 * were reading. */
async function postMode(id, want) {
  if (!id) return;
  try { await api(`/api/agents/${id}/mode`, 'POST', { mode: want }); }
  catch (e) { console.warn('mode', want, 'refused:', e.message); }
}

/* Markdown, the parts an agent actually writes: headings, lists, tables, code, quotes, links, bold
 * and italic. Line based rather than a library, because there is no build step and no npm here. It
 * is escaped before a single rule runs, so an agent writing a bracket or a tag cannot reach the DOM.
 *
 * Not a general parser. It does not nest a list inside a quote, and it does not need to: this is a
 * chat, and the aim is that a table looks like a table, not that every corner of CommonMark works. */

/* A URL safe to put in href. An agent writing javascript: or data: gets its link flattened to text
 * rather than a working bomb. Everything else, including a plain relative path, is allowed. */
function safeHref(url) {
  return /^(https?:\/\/|mailto:|\/|#)/i.test(url.trim()) ? url.trim() : null;
}

/* The inside of a line. Code spans are split out first and left entirely alone, because what is
 * inside a backtick is literal and must not be read as bold or as a link. Everything between the
 * code spans then gets links, bold and italic, in that order. */
function inline(s) {
  return s.split(/(`[^`]+`)/).map((seg) => {
    if (seg.length > 1 && seg[0] === '`' && seg[seg.length - 1] === '`') {
      return '<code>' + seg.slice(1, -1) + '</code>';
    }
    seg = seg.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, url) => {
      const href = safeHref(url);
      return href ? '<a href="' + href + '" target="_blank" rel="noopener noreferrer">' + label + '</a>' : label;
    });
    seg = seg.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    seg = seg.replace(/__([^_]+)__/g, '<strong>$1</strong>');
    seg = seg.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    seg = seg.replace(/(^|[^\w])_([^_\n]+)_/g, '$1<em>$2</em>');
    return seg;
  }).join('');
}

/* A pipe row split into its cells, with the empty cell an outer pipe leaves at each end dropped. */
function cells(line) {
  const parts = line.split('|');
  if (parts[0].trim() === '') parts.shift();
  if (parts.length && parts[parts.length - 1].trim() === '') parts.pop();
  return parts.map((c) => c.trim());
}

const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const TSEP = /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/;

function light(text) {
  const lines = esc(text).replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let i = 0;
  let para = [];

  const flush = () => {
    if (para.length) out.push('<p>' + para.map(inline).join('<br>') + '</p>');
    para = [];
  };

  while (i < lines.length) {
    const line = lines[i];

    // A fenced code block, and an unclosed fence at the end of a streaming message reads as code to
    // the end rather than flipping the rest of the message in and out of a box as it arrives.
    if (/^\s*```/.test(line)) {
      flush();
      const body = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) body.push(lines[i++]);
      if (i < lines.length) i++;
      out.push('<pre>' + body.join('\n') + '</pre>');
      continue;
    }

    if (RULE.test(line)) { flush(); out.push('<hr>'); i++; continue; }

    const head = line.match(/^(#{1,6})\s+(.*)$/);
    if (head) {
      flush();
      // # and ## are both the top of a chat, so both become h3; deeper levels step down from there.
      const level = head[1].length <= 2 ? 3 : Math.min(head[1].length + 1, 6);
      out.push('<h' + level + '>' + inline(head[2].trim()) + '</h' + level + '>');
      i++;
      continue;
    }

    // A table: a pipe row with a dashed separator directly under it. Without the separator the same
    // row is just text that happens to hold a pipe, and is left alone.
    if (line.includes('|') && i + 1 < lines.length && TSEP.test(lines[i + 1])) {
      flush();
      const header = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim() !== '') rows.push(cells(lines[i++]));
      const th = header.map((c) => '<th>' + inline(c) + '</th>').join('');
      const trs = rows.map((r) => '<tr>' + header.map((_, k) => '<td>' + inline(r[k] || '') + '</td>').join('') + '</tr>').join('');
      out.push('<div class="tablewrap"><table><thead><tr>' + th + '</tr></thead><tbody>' + trs + '</tbody></table></div>');
      continue;
    }

    // A run of list items of one kind. A blank line or anything that is not an item ends it.
    const bullet = /^\s*[-*+]\s+(.*)$/.test(line);
    const number = /^\s*\d+[.)]\s+(.*)$/.test(line);
    if (bullet || number) {
      flush();
      const ordered = number;
      const re = ordered ? /^\s*\d+[.)]\s+(.*)$/ : /^\s*[-*+]\s+(.*)$/;
      const items = [];
      while (i < lines.length) {
        const m = lines[i].match(re);
        if (!m) break;
        items.push('<li>' + inline(m[1]) + '</li>');
        i++;
      }
      const tag = ordered ? 'ol' : 'ul';
      out.push('<' + tag + '>' + items.join('') + '</' + tag + '>');
      continue;
    }

    // The marker is &gt; here, not >, because the whole text was escaped before any of these rules
    // ran. Every other rule uses characters esc leaves alone; the quote is the one that does not.
    if (/^\s*&gt;\s?/.test(line)) {
      flush();
      const q = [];
      while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) q.push(lines[i++].replace(/^\s*&gt;\s?/, ''));
      out.push('<blockquote>' + q.map(inline).join('<br>') + '</blockquote>');
      continue;
    }

    if (line.trim() === '') { flush(); i++; continue; }

    para.push(line);
    i++;
  }
  flush();
  return out.join('');
}

/* The keys a phone keyboard does not have. Written straight to the terminal, which is what a dialog
 * is waiting for: it wants a keypress, not a line of text. Sending text would type into the dialog
 * instead of answering it. */
const KEYS = { up: '\x1b[A', down: '\x1b[B', left: '\x1b[D', right: '\x1b[C',
               enter: '\r', esc: '\x1b', tab: '\t', shifttab: '\x1b[Z' };

function sendKey(name) {
  const seq = KEYS[name];
  if (!seq) return;
  /* Whichever socket is open. The chat borrows the terminal tab's when there is one. */
  const s = (sock && sock.readyState === 1) ? sock
          : (chatSock && chatSock.readyState === 1) ? chatSock : null;
  if (!s) return toast('No terminal attached to send that to.', true);
  s.send(JSON.stringify({ type: 'input', data: seq }));
}

/* A question the agent asked with its own tool. The whole thing is in the record: the question, the
 * options, their descriptions, and whether several may be picked. Worth drawing properly, because on
 * a phone the dialog in the terminal is unreadable and this is the only way to see the choices. */
function askRow(row) {
  let input;
  try { input = JSON.parse(row.inputRaw || 'null'); } catch { input = null; }
  const qs = (input && input.questions) || [];
  if (!qs.length) return '';
  return qs.map((q) => `<div class="ask"><div class="q">${esc(q.question || '')}</div>`
    + `<ol>${(q.options || []).map((o) =>
        `<li><b>${esc(o.label || '')}</b>${o.description ? ' — ' + esc(o.description) : ''}</li>`
      ).join('')}</ol>`
    + `<div class="who">answered${q.multiSelect ? ', several allowed' : ''}</div></div>`).join('');
}

/* Drawn last, so it sits under everything the record holds and above the agent's live reply, which
 * is where a message you just sent belongs. */
function drawEcho() {
  document.getElementById('chatecho')?.remove();
  if (!chatEcho || chatEcho.id !== chatFor) return;
  const waited = Date.now() - chatEcho.at;
  const mark = chatEcho.held ? 'waiting for it to wake'
             : waited > 30000 ? 'not in the record yet' : 'sending';
  const log = $('#chatlog');
  const node = document.createElement('div');
  node.id = 'chatecho';
  node.className = 'msg operator echo';
  node.innerHTML = `<div class="body">${light(chatEcho.text)}</div>`
    + `<span class="mark">${mark}</span>`;
  const live = document.getElementById('chatlive');
  if (live) log.insertBefore(node, live);
  else log.appendChild(node);
}

/* The record has it when a message of yours carries the same words. Whitespace is loosened because
 * the record keeps the text as the terminal took it, and a trailing newline is not a difference. */
function echoLanded(rows) {
  if (!chatEcho) return false;
  const want = chatEcho.text.replace(/\s+/g, ' ').trim();
  return rows.some((r) => r.kind === 'text' && r.who === 'operator'
                   && (r.text || '').replace(/\s+/g, ' ').trim() === want);
}

function chatRow(row) {
  if (row.kind === 'tool' && row.tool === 'AskUserQuestion') {
    const drawn = askRow(row);
    if (drawn) return drawn;
  }
  if (row.kind === 'tool') {
    return `<details class="tool"><summary><span class="name">${esc(row.tool || 'tool')}</span>`
      + `<span class="gist">${esc((row.input || '').split('\n')[0]).slice(0, 120)}</span></summary>`
      + `<pre>${esc(row.input || '')}</pre></details>`;
  }
  if (row.kind === 'result') {
    return `<details class="tool${row.error ? ' failed' : ''}"><summary>`
      + `<span class="name">${row.error ? 'failed' : 'result'}</span>`
      + `<span class="gist">${esc((row.text || '').split('\n')[0]).slice(0, 120)}</span></summary>`
      + `<pre>${esc(row.text || '')}</pre></details>`;
  }
  const who = row.who === 'operator' ? 'you' : 'agent';
  return `<div class="msg ${row.who}${row.live ? ' live' : ''}">`
    + `<span class="who">${who}${row.live ? ' · writing' : ''}</span>`
    + `<div class="body">${light(row.text || '')}</div></div>`;
}

/* Enough of a row to tell it from a changed version of itself, without keeping a copy of it. */
function rowKey(row) {
  return [row.kind, row.who, row.tool || '', row.id || '',
          (row.text || row.input || '').length].join('\u0000');
}

/* The settled conversation, from the session file. Fetched on a slow timer, because each call reads
 * the tail of a file on disk, and appended rather than rebuilt when it has only grown. */
async function paintHistory(id, { force = false } = {}) {
  let rows = [];
  try {
    rows = (await api(`/api/agents/${id}/messages?n=${chatWant}`)).messages || [];
  } catch { return; }
  chatFetchedAt = Date.now();

  const keys = rows.map(rowKey);
  const grewOnly = !force && chatShown.length && chatShown.length <= keys.length
    && chatShown.every((r, i) => r.key === keys[i]);

  const log = $('#chatlog');
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  const live = $('#chatlive');
  document.getElementById('chatecho')?.remove();

  if (grewOnly) {
    const extra = rows.slice(chatShown.length);
    if (extra.length) {
      const html = extra.map(chatRow).join('');
      if (live) live.insertAdjacentHTML('beforebegin', html);
      else log.insertAdjacentHTML('beforeend', html);
    }
  } else {
    log.innerHTML = rows.map(chatRow).join('') || '<div class="msg"><div class="body">'
      + 'Nothing said yet.</div></div>';
    if (live) log.appendChild(live);
  }

  chatShown = rows.map((r, i) => ({
    key: keys[i],
    settledText: r.kind === 'text' && r.who === 'agent' ? (r.text || '').replace(/\s+/g, ' ').trim() : '',
  }));
  if (chatEcho && chatEcho.id === id && echoLanded(rows)) chatEcho = null;
  drawEcho();
  if (atBottom) log.scrollTop = log.scrollHeight;

  /* Only ever how much of the record is on screen. There used to be a second line here, shown
     while the agent worked, explaining that a message appears as it is written and then again
     from the record. That described the screen scrape this chat no longer uses, and the streaming
     text says it better than a sentence about streaming text. */
  const more = rows.length >= chatWant && chatWant < CHAT_MOST ? ' Scroll up for older.' : '';
  $('#chatnote').textContent = `${rows.length} entries from this agent's session file.${more}`;
}

async function paintChat(id, opts) {
  await paintHistory(id, opts);
  paintLive();
}

/* Older messages are fetched only when you go looking for them. */
function chatScrolled() {
  const log = $('#chatlog');
  if (log.scrollTop > 40 || chatWant >= CHAT_MOST || !chatFor) return;
  chatWant = Math.min(chatWant + CHAT_MORE, CHAT_MOST);
  const before = log.scrollHeight;
  paintHistory(chatFor, { force: true }).then(() => {
    /* Keep the line you were reading where it was, rather than jumping to the top. */
    log.scrollTop += log.scrollHeight - before;
  });
}

/* Measure, do not guess. The stack above this tab is a breadcrumb that wraps, a file row and a tab
 * strip, and it came to 258px on a laptop and 307px on a phone. A constant in the stylesheet was
 * wrong on one of them either way, and being wrong here pushes the box you type into off the bottom
 * of the screen. */
function fitChat() {
  const tab = $('#tab-chat');
  if (!tab || tab.hidden) return;
  const top = tab.getBoundingClientRect().top;
  const room = Math.max(220, Math.round(window.innerHeight - top - 10));
  tab.style.height = room + 'px';
}

addEventListener('resize', fitChat);
/* A phone hides its address bar as you scroll, which changes the viewport without a resize event. */
if (window.visualViewport) visualViewport.addEventListener('resize', fitChat);

function mountChat(id) {
  if (chatFor === id) return;
  unmountChat();
  chatFor = id;
  $('#chatlog').innerHTML = '';
  /* The mode the record holds, shown at once. The socket sends `perm_mode` a moment later and that
   * is the real one, but a switcher that stays blank until then reads as broken. */
  setPermMode(STATE.agents.find((x) => x.id === id)?.permission_mode || '');
  chatOpen(id);
  paintChat(id, { force: true });
  /* The record is still read on a timer, but slowly, and only as a safety net. The socket says
   * `settled` the moment a turn lands in the session file, and that is what repaints the
   * conversation now. This is here for the turn whose last frame went missing, and for a record
   * changed by something other than this chat: a cord delivering a message, or a person typing in
   * the terminal. */
  chatTimer = setInterval(() => {
    if (!chatFor) return;
    if (Date.now() - chatFetchedAt > 8000) paintHistory(chatFor);
  }, 2000);
}

/* -- wires ------------------------------------------------------------------ */

/* Cords drawn as cables hanging between two cards. It is a convenience, in the operator's own word,
 * and it earns its place for one reason: a transfer lives about two seconds. Without a trace left
 * behind, the only visible evidence that anything happened is a number changing on a card you were
 * not looking at.
 *
 * Cards are not dragged. The grid puts them where it puts them, and the wire follows. Dragging into
 * a patchbay layout was deliberately left for later, or never. */
const TRACE_MS = 30000;
const seen = new Map();      // cord id -> { cord, goneAt }
let pointer = null;

/* How long ago a cord finished, by the server's own clock rather than this browser's. The server
 * reports both, so there is no skew to worry about and no need to guess. */
function finishedAgo(c) {
  if (c.state === 'running' || !c.finished) return null;
  const now = STATE.server_time || (Date.now() / 1000);
  return Math.max(0, (now - c.finished) * 1000);
}

function noteCords() {
  const now = Date.now();
  const live = new Set();
  for (const c of STATE.cords || []) {
    const age = finishedAgo(c);
    /* Do not take on a cord that finished longer ago than the trace lasts. The daemon is supposed to
     * drop a finished cord from its list, and when it does not, this used to draw it for ever: the
     * local timer expired, the entry was deleted, and the next refresh put it straight back with a
     * fresh timestamp. A failed call stayed on screen for twenty minutes that way. */
    if (age != null && age > TRACE_MS) continue;
    live.add(c.id);
    seen.set(c.id, { cord: c, goneAt: age == null ? null : now - age });
  }
  for (const [id, row] of seen) {
    /* A cord the daemon has forgotten is still worth drawing for a moment. */
    if (!live.has(id) && row.goneAt == null) row.goneAt = now;
    if (row.goneAt != null && now - row.goneAt > TRACE_MS) seen.delete(id);
  }
}

function cardBox(id) {
  const el = document.querySelector(`.card[data-id="${id}"]`);
  return el ? el.getBoundingClientRect() : null;
}

/* Where a cable leaves a card, and how far it sags. Side by side, it hangs below both of them like a
 * patch lead. One above the other, it goes bottom to top, because a cable that looped under a card
 * to reach the one below it would cross the card in between. */
function cable(from, to) {
  const dx = (to.left + to.width / 2) - (from.left + from.width / 2);
  const dy = (to.top + to.height / 2) - (from.top + from.height / 2);
  const across = Math.abs(dx) >= Math.abs(dy);
  const a = across
    ? { x: from.left + from.width / 2, y: from.bottom }
    : { x: from.left + from.width / 2, y: dy > 0 ? from.bottom : from.top };
  const b = across
    ? { x: to.left + to.width / 2, y: to.bottom }
    : { x: to.left + to.width / 2, y: dy > 0 ? to.top : to.bottom };
  const sag = across
    ? Math.min(90, 28 + Math.abs(dx) * 0.16)
    : Math.min(50, 16 + Math.abs(dy) * 0.05);
  const mid = { x: (a.x + b.x) / 2, y: Math.max(a.y, b.y) + sag };
  return `M ${a.x} ${a.y} Q ${mid.x} ${mid.y} ${b.x} ${b.y}`;
}

function wireColour(c) {
  if (c.state === 'failed') return tok('--down');
  if (c.state === 'timed_out') return tok('--low');
  if (c.state === 'completed') return tok('--up');
  return c.kind === 'call' ? tok('--waiting') : tok('--accent');
}

function renderWires() {
  const svg = $('#wires');
  noteCords();
  const parts = [];

  for (const { cord, goneAt } of seen.values()) {
    const from = cardBox(cord.from);
    const to = cardBox(cord.to);
    /* Either card can be off the page: hidden by the view, or scrolled away. A wire to nowhere is
     * worse than no wire, so it simply is not drawn. */
    if (!from || !to) continue;
    const width = cord.kind === 'call' ? 2.4 : 1.8;
    parts.push(`<path class="${goneAt ? 'done' : 'live'}" d="${cable(from, to)}"
      stroke="${wireColour(cord)}" stroke-width="${width}"></path>`);
  }

  /* The first tap chose a card; this is the lead following your finger until the second tap. On a
   * phone there is no pointer and nothing is drawn, which is why the two taps are the interaction
   * and this is only a hint. */
  if (drawingFrom && pointer) {
    const from = cardBox(drawingFrom);
    if (from) {
      const a = { x: from.left + from.width / 2, y: from.bottom };
      const mid = { x: (a.x + pointer.x) / 2, y: Math.max(a.y, pointer.y) + 30 };
      parts.push(`<path class="pending" d="M ${a.x} ${a.y} Q ${mid.x} ${mid.y} ${pointer.x} ${pointer.y}"
        stroke="${tok('--accent')}" stroke-width="1.8"></path>`);
    }
  }

  svg.innerHTML = parts.join('');
  svg.hidden = parts.length === 0;
}

addEventListener('resize', renderWires);
addEventListener('scroll', renderWires, { passive: true });
addEventListener('pointermove', (ev) => {
  if (!drawingFrom) return;
  pointer = { x: ev.clientX, y: ev.clientY };
  renderWires();
});
/* A cord that finishes leaves a trace for half a minute, so the layer has to be redrawn while
 * nothing else is happening. Twice a second is enough for a fade nobody is staring at. */
setInterval(renderWires, 500);

/* -- the network ------------------------------------------------------------ */

/* Everything here is a pair. The daemon takes the measurement every twenty seconds at most and
 * hands the same one to every watcher, so this asking on a timer is cheap: three pings, not three
 * pings per browser. */
let NET = null;

async function refreshNet() {
  try {
    NET = await api('/api/network');
    renderNet();
  } catch { /* the daemon will be back; the strip keeps the last reading */ }
}

function ms(v) { return v == null ? '—' : `${v}ms`; }

function renderNet() {
  const box = $('#net');
  if (!NET) { box.hidden = true; return; }
  box.hidden = false;
  const l = NET.latency || {};
  const w = NET.wifi || {};
  const d = NET.drops || {};
  const s = NET.speed;
  /* The gateway figure is the one that goes bad on this laptop, so it is first and it is the one
   * that turns red. The internet figure beside it is what makes the first one mean anything. */
  const bad = (l.gateway?.avg_ms ?? 0) > 30 || (l.gateway?.loss_pct ?? 0) > 0;
  box.innerHTML = `<div class="c">
      <span class="kind ${bad ? '' : 'transfer'}">${bad ? 'router' : 'network'}</span>
      <span class="path">${esc(w.ssid || NET.gateway?.device || 'link')}</span>
      <span class="inst">${esc(NET.verdict || '')}</span>
      <span class="st${bad ? ' failed' : ''}">router ${ms(l.gateway?.avg_ms)}</span>
      <span class="st">out ${ms(l.internet?.avg_ms)}</span>
      ${w.rate_mbit ? `<span class="st">${w.rate_mbit} Mb/s at ${w.signal_pct}%</span>` : ''}
      ${d.disconnects != null ? `<span class="st">${d.disconnects} drop${
        d.disconnects === 1 ? '' : 's'}</span>` : ''}
      ${s ? `<span class="st${s.error ? ' failed' : ''}">${s.error
        ? 'speed test failed' : `${s.down_mbit} down / ${s.up_mbit} up`}</span>` : ''}
      <button class="btn small" id="speedtest">Speed test</button>
    </div>`;
}

/* -- the schedule ----------------------------------------------------------- */

const WHEN = (j) => (j.watch ? `when files change in ${j.watch}`
  : j.at ? `daily ${j.at}` : `every ${short(j.every)}`);

function short(seconds) {
  const n = Number(seconds || 0);
  if (n % 3600 === 0) return `${n / 3600}h`;
  if (n % 60 === 0) return `${n / 60}m`;
  return `${n}s`;
}

function nextAt(ts) {
  if (!ts) return 'never';
  const d = new Date(ts * 1000);
  const today = new Date().toDateString() === d.toDateString();
  return (today ? '' : d.toLocaleDateString(undefined, { weekday: 'short' }) + ' ')
    + d.toTimeString().slice(0, 5);
}

/* Rerun is the button this strip exists for. A job that was due while the laptop was shut is
 * reported as missed and deliberately not run late, because whether a late run still makes sense is
 * a judgement the platform cannot make. So it has to be offered, here and on the phone. */
function renderJobs() {
  const js = STATE.jobs || [];
  const box = $('#jobs');
  box.hidden = js.length === 0;
  const name = (id) => STATE.agents.find((a) => a.id === id)?.name || id;
  box.innerHTML = js.map((j) => `<div class="c" data-job="${j.id}">
      <span class="kind ${j.enabled ? 'transfer' : ''}">${j.enabled ? WHEN(j) : 'off'}</span>
      <span class="path">${esc(name(j.agent))}</span>
      <span class="inst" title="${esc(j.last_summary || '')}">${esc(j.task)}</span>
      ${j.last_outcome ? `<span class="st ${outcomeClass(j.last_outcome)}">${
        esc(j.last_outcome)}${j.missed > 1 ? ` \u00d7${j.missed}` : ''}</span>` : ''}
      <span class="st">next ${nextAt(j.next)}</span>
      <button class="btn small" data-job-do="run">Run now</button>
      <button class="btn small" data-job-do="toggle">${j.enabled ? 'Off' : 'On'}</button>
      <button class="btn small danger" data-job-do="delete">Delete</button>
    </div>`).join('');
}

/* The outcome borrows the cord colours, which already mean the same things: green for finished,
 * red for broken, amber for ran-but-nobody-can-tell. */
function outcomeClass(outcome) {
  if (outcome === 'succeeded') return 'completed';
  if (outcome === 'failed') return 'failed';
  if (outcome === 'missed' || outcome === 'unclear' || outcome === 'no reply') return 'timed_out';
  return '';
}

function askJob() {
  const sel = $('#job-agent');
  sel.innerHTML = STATE.agents.map((a) =>
    `<option value="${a.id}">${esc(a.name)}</option>`).join('');
  if (!STATE.agents.length) return toast('Start an agent first: a job names one.', true);
  $('#dlg-job').showModal();
}

/* -- drawing a cord --------------------------------------------------------- */

/* The PRD has cords drawn by hand between cards. A canvas with a dragged line does not work
 * with a thumb, and this console is used from a phone, so the drawing is two taps: the source
 * card, then the target. Same gesture, same direction, no mouse required. */
function startDrawing(id) {
  drawingFrom = id;
  renderCards();
  toast('Now tap the agent to connect to. Escape to cancel.');
}

function cancelDrawing() {
  pointer = null;
  if (!drawingFrom) return;
  drawingFrom = null;
  renderCards();
}

function askCord(from, to) {
  const a = STATE.agents.find((x) => x.id === from);
  const b = STATE.agents.find((x) => x.id === to);
  $('#cord-who').innerHTML = `<strong>${esc(a.name)}</strong> &rarr; <strong>${esc(b.name)}</strong>`;
  $('#form-cord').dataset.from = from;
  $('#form-cord').dataset.to = to;
  setKind('transfer');
  $('#dlg-cord').showModal();
}

function setKind(kind) {
  $$('#cord-kind button').forEach((b) => b.classList.toggle('on', b.dataset.kind === kind));
  $('#form-cord').dataset.kind = kind;
  $('#cord-explain').textContent = kind === 'transfer'
    ? 'Carries their last message plus this instruction. One use, then the cord is gone.'
    : 'A conversation. It ends when the caller has what it wanted, or after 8 messages.';
}

/* -- the terminal ----------------------------------------------------------- */

let term = null, fit = null, sock = null, ro = null, current = null;

function openTerminal(id, { wake = false } = {}) {
  closeTerminal();

  term = new Terminal({
    fontFamily: '"JetBrains Mono", ui-monospace, monospace',
    fontSize: 12.5,
    lineHeight: 1.35,
    letterSpacing: 0,
    cursorBlink: true,
    scrollback: 8000,
    theme: termTheme(),
    allowProposedApi: true,
  });
  fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open($('#term'));
  fit.fit();

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const q = `rows=${term.rows}&cols=${term.cols}&wake=${wake ? 1 : 0}`;
  sock = new WebSocket(`${proto}://${location.host}/ws/agent/${id}?${q}`);
  sock.binaryType = 'arraybuffer';

  const status = (t) => { $('#t-status').textContent = t; };
  status('connecting');

  sock.onmessage = (ev) => {
    /* Binary is terminal output and goes straight through, escape sequences and all. That is
     * what makes this 1:1 rather than a rendering of the conversation. */
    if (ev.data instanceof ArrayBuffer) { term.write(new Uint8Array(ev.data)); return; }
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === 'attached') {
      status(`live · pid ${msg.pid}`);
      queueMicrotask(() => sendResize());
    } else if (msg.type === 'grey') {
      status('asleep');
      term.write('\r\n\x1b[2m  This agent is asleep. Waking it resumes the same conversation.\x1b[0m\r\n');
      /* No special button here. The page header already shows Wake when an agent is grey, and a
       * second one in a different place is two controls for one action. */
      refresh();
    } else if (msg.type === 'exited') {
      status(`exited ${msg.status ?? ''}`.trim());
      term.write('\r\n\x1b[2m  the terminal closed\x1b[0m\r\n');
    } else if (msg.type === 'error') {
      status('error');
      toast(msg.message, true);
    }
  };
  sock.onclose = () => { if (current === id) status('detached'); };
  sock.onerror = () => status('error');

  /* Keystrokes go back as binary, so a paste with a newline in it is bytes rather than an
   * accidental submit. */
  term.onData((d) => {
    if (sock?.readyState === 1) sock.send(new TextEncoder().encode(d));
  });

  ro = new ResizeObserver(() => { try { fit.fit(); sendResize(); } catch {} });
  ro.observe($('#term'));
}

function sendResize() {
  if (sock?.readyState === 1 && term) {
    sock.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }));
  }
}

function closeTerminal() {
  ro?.disconnect(); ro = null;
  try { sock?.close(); } catch {}
  sock = null;
  term?.dispose(); term = null; fit = null;
}

/* -- routing ---------------------------------------------------------------- */

function route() {
  const m = location.hash.match(/^#\/a\/([a-z0-9-]+)/);
  if (m) return showAgent(m[1]);
  closeTerminal(); unmountView(); unmountChat(); current = null;
  /* Both, and this is why: leaving the chat tab for the dashboard never went through showTab, so the
   * class that stops the page scrolling stayed on the body and the dashboard could not be scrolled
   * at all. */
  document.body.classList.remove('on-agent', 'chatting');
  $('#page-agent').hidden = true;
  $('#page-dash').hidden = false;
  renderCards();
}

function showAgent(id) {
  const a = STATE.agents.find((x) => x.id === id);
  /* Which tab was open, read before the tab strip is rebuilt below. */
  const came_from = current, was = $('#tabs .tab.on')?.dataset.tab;
  $('#page-dash').hidden = true;
  $('#page-agent').hidden = false;
  if (!a) { $('#a-name').textContent = 'unknown agent'; return; }

  $('#a-name').textContent = a.name;
  $('#a-sub').textContent = `${a.cwd} · ${a.session_id.slice(0, 8)}${a.model ? ' · ' + a.model : ''}`;
  $('#view-path').textContent = `views/${a.id}/view.js`;

  /* The bar is this agent's header now, so the class goes on before anything is measured. */
  document.body.classList.add('on-agent');
  renderAgentList(id);
  updateAgentActions(a);
  renderFiles(a);
  /* Moving to a different agent drops the previous terminal. Without this, going from one agent's
   * terminal to another's page leaves the first socket open, still counted as a watcher and still
   * holding that terminal at this browser's size. */
  if (current !== id) { closeTerminal(); unmountView(); unmountChat(); }
  current = id;

  /* Terminal is leftmost and always in the same place, so it is the one tab you can hit without
   * reading. The agent's pages sit to the right of it, in the order the agent chose. */
  const pages = a.pages || [];
  const over = pages.find((p) => p.truncated);
  $('#tabs').innerHTML =
    '<button class="tab" data-tab="terminal">Terminal</button>'
    + '<button class="tab" data-tab="chat">Chat</button>'
    + pages.map((p) => `<button class="tab" data-tab="${p.slug}">${esc(p.title)}</button>`).join('')
    + (over ? `<span class="tabnote" title="${over.truncated} pages written, ${pages.length} shown">`
              + `${over.truncated - pages.length} more</span>` : '');

  /* Leftmost is not the same as selected. The agent says which page opens, through a `default` file
   * beside its modules, and the console honours that. With no pages at all the terminal opens,
   * because landing on an empty tab is a worse greeting than a working one. */
  /* On a phone the chat opens, whatever the agent chose. A 40-column terminal is not a thing you
   * arrive at willingly, and a screen an agent wrote is a considered choice rather than a greeting. */
  /* Switching agents from the list keeps you where you were. Reading one agent's conversation and
   * then another's is the reason the list exists, and being dropped into a terminal each time would
   * undo that. Only the two tabs every agent has can carry over: an agent's own pages are its own. */
  const carry = came_from && came_from !== id && (was === 'chat' || was === 'terminal') ? was : null;
  showTab(carry || (onPhone() ? 'chat' : (a.default_page || 'terminal')));
}

/* Rewritten as plain markup on purpose. The first version patched the existing button in place
 * and set element.dataset to a fresh object, which does nothing: dataset is a read only map. The
 * button then never changed from Wake to Sleep, and nothing reported an error. */
/* The list beside an agent's conversation. Ordered as the dashboard orders it, so the master is
 * first and the rest fall by when they last did something, which is the order you look for them in. */
function renderAgentList(id) {
  const box = $('#agentlist');
  const list = STATE.agents || [];
  box.innerHTML = '<div class="head">Agents</div>' + list.map((a) => {
    const awake = a.state === 'green';
    return `<a href="#/a/${a.id}" class="${a.id === id ? 'on ' : ''}${awake ? 'awake' : ''}"
      title="${esc(a.name)}${awake ? ', awake' : ', asleep'}">`
      + '<span class="dot"></span>'
      + `<span class="nm">${esc(a.name)}</span>`
      + (a.waiting ? `<span class="n">${a.waiting}</span>` : '')
      + '</a>';
  }).join('');
}

function updateAgentActions(a) {
  const bar = $('#a-actions');
  if (!a) { bar.innerHTML = ''; return; }
  const green = a.state === 'green';
  bar.innerHTML =
    (green ? '<button class="btn" data-do="sleep">Sleep</button>'
           : '<button class="btn primary" data-do="wake">Wake</button>')
    + `<button class="btn" data-do="cord"${green ? '' : ' disabled'}>Cord</button>`
    // "Where it shows", not "Views": a view in this project is a screen an agent wrote, and the
    // button that tags an agent for work, demo or personal is a different thing entirely.
    + '<button class="btn" data-do="views">Where it shows</button>'
    + '<button class="btn" data-do="sendfile">Send a file</button>'
    // The model is a property of the agent, so it belongs on the agent's own bar. It is read by
    // `claude` at startup and nowhere else, so a change waits for the next wake.
    + `<select class="btn" id="a-model" title="The model this agent runs. Takes effect next wake">${
        modelOptions(a.model)}</select>`;
}

/* The models on offer. Exact ids for the ones worth naming, so that "opus 5" stays 5 when the
 * `opus` alias moves on; plain aliases for sonnet and haiku, where the latest is what is wanted.
 * Everything here is passed to `claude --model` untouched, and the daemon keeps no list of its own. */
const MODELS = [['', 'default'], ['claude-opus-5', 'opus 5'], ['claude-opus-4-8', 'opus 4.8'],
                ['claude-opus-4-6', 'opus 4.6'], ['sonnet', 'sonnet'], ['haiku', 'haiku']];

function modelOptions(chosen) {
  const rows = MODELS.slice();
  /* An agent already running something not on the list keeps it, named as it stands. A select that
   * does not hold the current value shows the first option instead, which would report an agent as
   * running the default when it is not. */
  if (chosen && !rows.some(([v]) => v === chosen)) rows.push([chosen, chosen]);
  return rows.map(([v, t]) =>
    `<option value="${esc(v)}"${v === (chosen || '') ? ' selected' : ''}>${esc(t)}</option>`).join('');
}

/* One place that decides what counts as a phone, matching the stylesheet's own breakpoint. */
const onPhone = () => matchMedia('(max-width:640px)').matches;

function showTab(which) {
  $$('.tab').forEach((t) => t.classList.toggle('on', t.dataset.tab === which));
  $('#tab-terminal').hidden = which !== 'terminal';
  $('#tab-chat').hidden = which !== 'chat';
  $('#tab-view').hidden = which === 'terminal' || which === 'chat';

  /* The chat holds its own socket, so leaving the tab drops it. Left open it stays a watcher, and a
   * terminal is fitted to its smallest watcher, so it would fix the real terminal to this browser's
   * width for everybody else. */
  if (which !== 'chat') { unmountChat(); document.body.classList.remove('chatting'); }
  /* Set after mountChat, never before: mountChat begins by unmounting, and stripping the class there
   * put the page back to scrolling the moment the tab opened. */
  if (which === 'chat') {
    mountChat(current);
    /* The tab you are looking at decides which engine the agent runs. Chat is `claude -p`, which
     * streams its turn as data; the terminal is the pty. Only these two say anything: a view is a
     * page the agent wrote, and it does not care what is running behind it. */
    postMode(current, 'chat');
    document.body.classList.add('chatting');
    fitChat();
    return;
  }
  if (which !== 'terminal') { mountPage(current, which); return; }
  postMode(current, 'terminal');

  /* The terminal connects the first time it is looked at, not when the page opens. Attaching is not
   * free: it replays the scrollback and, because a terminal is fitted to the smallest watcher, it
   * resizes the real terminal for everybody else too. Doing that for a page you opened to read a
   * chart would be rude to whoever is working in it. */
  if (current && !sock) openTerminal(current);
  else if (term) { try { fit.fit(); sendResize(); } catch {} }
}

/* -- undelivered messages --------------------------------------------------- */

/* Fetched when the dialog opens rather than carried in the console's state. The count belongs on a
 * card; the full text of every undelivered note for every agent does not, and would travel on every
 * refresh for the sake of something you look at rarely. */
let heldFor = null;

async function showHeld(id) {
  const a = STATE.agents.find((x) => x.id === id);
  heldFor = id;
  $('#held-who').textContent = `Undelivered for ${a?.name || id}`;
  $('#heldlist').innerHTML = '<div class="none">Reading.</div>';
  $('#dlg-held').showModal();
  await paintHeld();
}

async function paintHeld() {
  try {
    const { held } = await api(`/api/agents/${heldFor}/held`);
    if (!held.length) {
      $('#heldlist').innerHTML = '<div class="none">Nothing waiting.</div>';
      $('#held-all').disabled = true;
      return;
    }
    $('#held-all').disabled = false;
    $('#heldlist').innerHTML = held.map((m) => `<div class="h">
        <span class="ts">${ago(m.ts)}</span>
        <span class="txt">${esc(m.text)}</span>
        <button type="button" class="x" data-held-drop="${m.id}" title="Delete this one">&times;</button>
      </div>`).join('');
  } catch (e) {
    $('#heldlist').innerHTML = `<div class="none">${esc(e.message)}</div>`;
  }
}

/* -- files ------------------------------------------------------------------ */

function size(n) {
  if (n >= 1048576) return `${(n / 1048576).toFixed(1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

function renderFiles(a) {
  const set = $('#fileset');
  set.innerHTML = (a?.files || []).map((f) =>
    `<a href="/file/${a.id}/${encodeURIComponent(f.name)}" download>${esc(f.name)}` +
    `<span class="sz">${size(f.bytes)}</span>` +
    `<button class="drop-x" data-do="unfile" data-name="${esc(f.name)}" ` +
    `title="Remove from the console. The agent's own copy is untouched.">&times;</button></a>`).join('');
  $('#dropmsg').textContent = a?.inbox_count
    ? `Send a file (${a.inbox_count} sent so far)`
    : 'Send a file to this agent';
  /* Hidden when there is nothing to show. Sending is a button in the bar; this row exists for files
   * an agent has offered you, and an empty one was costing a row of space on every agent. */
  $('#files').hidden = !(a?.files || []).length;
}

/* One PUT per file, with the name in the address and the bytes as the body. No multipart, because
 * that needs a parser on the server and the standard library dropped its own in 3.13. */
async function sendFiles(agentId, list) {
  const drop = $('#drop');
  drop.classList.add('busy');
  try {
    for (const file of list) {
      $('#dropmsg').textContent = `Sending ${file.name}`;
      const r = await fetch(`/api/agents/${agentId}/inbox/${encodeURIComponent(file.name)}`, {
        method: 'PUT',
        headers: { 'Content-Type': file.type || 'application/octet-stream' },
        body: file,
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || `${r.status}`);
      toast(data.told_agent === 'held'
        ? `${file.name} sent. The agent is told when it wakes.`
        : `${file.name} sent, and the agent has been told where it is.`);
    }
  } catch (e) {
    toast(e.message, true);
  } finally {
    drop.classList.remove('busy');
    refresh();
  }
}

/* -- agent-authored view ---------------------------------------------------- */

/* The frame is sandboxed without allow-same-origin, which gives the view an origin of its own
 * that matches nothing. It cannot read this page, and each view cannot read any other. Adding
 * allow-same-origin would let it remove its own sandbox, so the two are never combined. */
let viewFrame = null, viewAbort = null, viewOf = null;

/* Taking a view down matters as much as putting one up. This is a single page and the address bar
 * only changes its fragment, so nothing reloads when you move between agents: a view left in the
 * document keeps running, keeps its timers, and keeps asking the platform for data for an agent you
 * are no longer looking at. The listener has to go with it, or one accumulates per mount. */
function unmountView() {
  viewAbort?.abort();
  viewAbort = null;
  viewFrame = null;
  viewOf = null;
  $('#view-holder').innerHTML = '';
}

function mountPage(id, slug) {
  const a = STATE.agents.find((x) => x.id === id);
  const key = `${id}/${slug}`;
  if (viewOf === key && viewFrame) { $('#view-empty').hidden = true; return; }
  unmountView();
  const exists = (a?.pages || []).some((p) => p.slug === slug);
  $('#view-empty').hidden = exists;
  if (!exists) return;

  /* One frame per page, mounted when its tab is shown and dropped when you leave it. Each frame
   * gets its own origin that matches nothing, so an agent's pages cannot read each other either,
   * which is the same rule that stops them reading the console. */
  const f = document.createElement('iframe');
  f.setAttribute('sandbox', 'allow-scripts');
  f.setAttribute('referrerpolicy', 'no-referrer');
  f.src = `/view/${id}/${slug}/`;
  $('#view-holder').appendChild(f);
  viewFrame = f;
  viewOf = key;
  viewAbort = new AbortController();

  /* A view has no network of its own, so everything it needs comes through here. It may ask for
   * data by name and it may ask the daemon to fetch an address, which is answer 30 of the daemon
   * grill: any address. What it may never do is reach the network itself, because then a stranger's
   * code would be talking to the stranger's server from inside this browser. */
  window.addEventListener('message', async (ev) => {
    if (ev.source !== f.contentWindow) return;
    const { id: rid, name, args } = ev.data || {};
    if (!rid) return;
    let reply;
    try {
      reply = { id: rid, ok: true, value: await bridge(name, args, a.id) };
    } catch (e) {
      reply = { id: rid, ok: false, error: e.message };
    }
    f.contentWindow.postMessage(reply, '*');
  }, { signal: viewAbort.signal });
  f.addEventListener('load', () => {
    f.contentWindow.postMessage({ boot: true, module: `/view/${id}/view.js`,
      tokens: ['--bg', '--surface', '--border', '--text', '--text-dim', '--text-faint',
        '--accent', '--accent-bg', '--up', '--down', '--waiting', '--low']
        .reduce((o, k) => (o[k] = tok(k), o), {}) }, '*');
  });
}

/* The bridge, and the whole list of it. There is no catalog and no key store behind this any more:
 * the decision of 18 August was that an agent holds its own keys and runs its own server, so the
 * platform is a way through rather than a gatekeeper.
 *
 * These four are answered here in the browser from state it already has, so a screen showing which
 * agents exist costs no request. */
const LOCAL = {
  'platform.state': () => ({ agents: STATE.agents.map((a) => ({
    id: a.id, name: a.name, state: a.state, working: a.working })) }),
  'platform.agents': () => STATE.agents.map((a) => ({ id: a.id, name: a.name, role: a.role })),
  'platform.health': () => STATE.health,
  'platform.limits': () => STATE.limits,
};

async function bridge(name, args, agentId) {
  if (LOCAL[name]) return LOCAL[name](args, agentId);
  /* Data by name, and the daemon works out whether that name is a file the agent wrote or a route
   * on its own server. The screen never learns which, so an agent can move from one to the other
   * without touching the screen it already wrote. */
  if (name === 'platform.data') {
    return api(`/api/data/${agentId}/${encodeURIComponent((args && args.name) || '')}`);
  }
  if (name === 'platform.available') return api(`/api/agents/${agentId}/data`);
  /* The agent id travels so the fetch can be traced back to the screen that wanted it in the log.
   * It is not a permission. Every screen may ask for every address. */
  if (name === 'platform.fetch') return api('/api/fetch', 'POST', { ...(args || {}), agent: agentId });
  throw new Error(`${name} is not something a screen can ask the platform for`);
}

/* -- live state ------------------------------------------------------------- */

async function refresh() {
  try {
    const s = await api('/api/state');
    STATE = s;
    renderHealth(s.health);
    if ($('#page-agent').hidden) renderCards();
    else if (current) {
      const a = s.agents.find((x) => x.id === current);
      renderAgentList(current);
      updateAgentActions(a);
      renderFiles(a);
    }
  } catch (e) {
    toast(`cannot reach the laptop: ${e.message}`, true);
  }
}

function listen() {
  const es = new EventSource('/api/events');
  es.addEventListener('health', (ev) => {
    STATE.health = JSON.parse(ev.data);
    renderHealth(STATE.health);
  });
  /* Events say what changed and the page asks for the detail. Sending the whole state on every
   * event would mean every agent's token counts travelling to announce one card going green. */
  es.addEventListener('agents', () => refresh());
  es.addEventListener('cords', () => refresh());
  es.addEventListener('jobs', () => refresh());
  refreshNet();
  setInterval(refreshNet, 20000);
  es.onerror = () => { /* EventSource reconnects on its own; nothing to do. */ };
}

/* -- wiring ----------------------------------------------------------------- */

document.addEventListener('click', async (ev) => {
  /* This button sits inside a download link, so without stopping the event the file downloads at
   * the same moment it is removed. */
  const drop = ev.target.closest('[data-do="unfile"]');
  if (drop) {
    ev.preventDefault();
    ev.stopPropagation();
    try {
      await api(`/api/agents/${current}/unfile`, 'POST', { name: drop.dataset.name });
      refresh();
    } catch (e) { toast(e.message, true); }
    return;
  }

  /* The schedule strip. Handled before the card delegation below, because a job row is not a card
   * and would otherwise fall through to it. */
  const jobBtn = ev.target.closest('[data-job-do]');
  if (jobBtn) {
    const row = jobBtn.closest('[data-job]');
    const job = row?.dataset.job;
    const what = jobBtn.dataset.jobDo;
    jobBtn.disabled = true;
    try {
      if (what === 'run') {
        const said = await api(`/api/jobs/${job}/run`, 'POST', {});
        toast(said.said || 'running it now');
      } else if (what === 'toggle') {
        const on = jobBtn.textContent.trim() === 'On';
        await api(`/api/jobs/${job}`, 'PATCH', { enabled: on });
      } else if (what === 'delete') {
        await api(`/api/jobs/${job}`, 'DELETE');
      }
    } catch (e) { toast(e.message, true); }
    finally { jobBtn.disabled = false; refresh(); }
    return;
  }

  const card = ev.target.closest('.card');
  const btn = ev.target.closest('[data-do],[data-hangup],[data-kind],[data-tab]');

  if (btn?.dataset.tab) return showTab(btn.dataset.tab);
  if (btn?.dataset.kind) return setKind(btn.dataset.kind);

  if (btn?.dataset.hangup) {
    try { await api(`/api/cords/${btn.dataset.hangup}/hangup`, 'POST', { summary: 'ended from the console' }); }
    catch (e) { toast(e.message, true); }
    return;
  }

  /* Second tap of a drawn cord. */
  if (drawingFrom && card && card.dataset.id !== drawingFrom && !btn) {
    const to = card.dataset.id, from = drawingFrom;
    cancelDrawing();
    return askCord(from, to);
  }

  if (!btn?.dataset.do) return;
  const id = card?.dataset.id || current;
  if (!id) return;
  const what = btn.dataset.do;

  try {
    if (what === 'views') return askViews(id);
    if (what === 'sendfile') { $('#files').hidden = false; return $('#pick').click(); }
    if (what === 'held') return showHeld(id);
    if (what === 'cord') return startDrawing(id);
    if (what === 'hide') {
      await api(`/api/agents/${id}/hide`, 'POST', {});
      return toast('Card hidden. The agent and its work are untouched.');
    }
    btn.disabled = true;
    await api(`/api/agents/${id}/${what}`, 'POST', {});
    // Reconnect after waking only if the terminal is the tab being looked at. Waking from the view
    // tab should not silently open a connection behind it.
    if (what === 'wake' && current === id && !$('#tab-terminal').hidden) openTerminal(id);
    if (what === 'sleep' && current === id) $('#t-status').textContent = 'asleep';
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    refresh();
  }
});

document.addEventListener('click', async (ev) => {
  if (!ev.target.closest('#speedtest')) return;
  const btn = ev.target.closest('#speedtest');
  btn.disabled = true;
  btn.textContent = 'Measuring';
  try {
    /* It moves five megabytes, so it takes a couple of seconds on a good line and longer on a bad
     * one. Which is the point: the number is only interesting when the line is bad. */
    const got = await api('/api/network/speed', 'POST', {});
    if (NET) NET.speed = got;
    renderNet();
    toast(got.error ? `Speed test failed: ${got.error}`
                    : `${got.down_mbit} Mbit down, ${got.up_mbit} up.`, !!got.error);
  } catch (e) {
    toast(e.message, true);
    renderNet();
  }
});

/* Every dialog closes three ways now: the cross, the backdrop, and Escape. On a phone the buttons at
 * the bottom could fall below the fold, and a dialog with no reachable way out means reloading the
 * page. */
document.addEventListener('click', (ev) => {
  const x = ev.target.closest('.dlg-x');
  if (x) { ev.preventDefault(); return x.closest('dialog')?.close(); }
  /* A click that lands on the dialog element itself, rather than on anything inside it, is a click on
   * the backdrop: the element fills the viewport while its box is only the middle. */
  const dlg = ev.target.closest('dialog');
  if (dlg && ev.target === dlg) dlg.close();
});

document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') cancelDrawing();
});

$('#theme').onclick = () => theme(THEMES[(THEMES.indexOf(theme()) + 1) % THEMES.length]);
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => theme());

$('#new-model').innerHTML = modelOptions('');

$('#new-agent').onclick = $('#new-agent-2').onclick = () => $('#dlg-new').showModal();

$('#a-actions').addEventListener('change', async (ev) => {
  if (ev.target.id !== 'a-model' || !current) return;
  const want = ev.target.value;
  const name = STATE.agents.find((a) => a.id === current)?.name || current;
  try {
    await api(`/api/agents/${current}`, 'PATCH', { model: want || null });
    await refresh();
    toast(`${name} will run ${want || 'the default'} the next time it wakes.`);
  } catch (e) { toast(e.message, true); }
});
$('#new-job').onclick = () => askJob();

$('#chatsay').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const box = $('#chat-text');
  const text = box.value.trim();
  if (!text || !current) return;
  box.value = '';
  box.style.height = 'auto';
  /* Before the request, not after. This is the whole point: the message is on screen in the time it
   * takes to let go of the button. */
  chatEcho = { id: current, text, at: Date.now(), held: false };
  drawEcho();
  const log = $('#chatlog');
  log.scrollTop = log.scrollHeight;
  try {
    /* wake true: you are looking at this conversation and waiting for an answer, which is exactly
     * the case the holding behaviour is not for. */
    const r = await api(`/api/agents/${current}/message`, 'POST', { text, wake: true });
    /* A held message is in the mailbox and will never appear in the record until the agent wakes,
     * so the mark has to say so rather than counting seconds against it. */
    if (chatEcho) chatEcho.held = r.outcome === 'held';
    drawEcho();
    paintChat(current, { force: true });
    /* The record needs a moment. Two more looks cover it without making the poll faster for the
     * whole conversation. */
    setTimeout(() => { if (chatFor === current) paintHistory(current, { force: true }); }, 600);
    setTimeout(() => { if (chatFor === current) paintHistory(current, { force: true }); }, 1600);
  } catch (e) {
    chatEcho = null;
    drawEcho();
    toast(e.message, true);
    box.value = text;
  }
});

$('#chatlog').addEventListener('scroll', chatScrolled, { passive: true });

/* The switcher, which is Shift+Tab without a terminal. The server answers with the mode it settled
 * on and that is what the switcher shows afterwards, because the agent is the authority on its own
 * mode. A refusal puts the old name back rather than leaving a name that is not true. */
$('#chatperm').addEventListener('change', async (ev) => {
  const want = ev.target.value;
  const was = chatPerm;
  if (!current || !want) return setPermMode(was);
  try {
    const r = await api(`/api/agents/${current}/permission_mode`, 'POST', { mode: want });
    setPermMode(r.mode || want);
  } catch (e) {
    setPermMode(was);
    toast(e.message, true);
  }
});

$('#chatkeys-toggle').addEventListener('click', () => {
  $('#chatkeys').classList.toggle('open');
  fitChat();
});

$('#termkeys-toggle').addEventListener('click', () => $('#termkeys').classList.toggle('open'));

/* Both key rows, the chat's and the terminal's, through one listener. Only the chat repaints: its
 * picture is read from a file that the keypress has just changed, while the terminal draws itself
 * over its own socket and needs nothing from us. */
document.addEventListener('click', (ev) => {
  const btn = ev.target.closest('[data-key]');
  if (!btn) return;
  ev.preventDefault();
  sendKey(btn.dataset.key);
  if (!$('#tab-chat').hidden) setTimeout(() => paintChat(current, { force: true }), 250);
});

$('#chat-text').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); $('#chatsay').requestSubmit(); }
});

/* Grows with what you type, up to the cap in the stylesheet. */
$('#chat-text').addEventListener('input', (ev) => {
  ev.target.style.height = 'auto';
  ev.target.style.height = Math.min(ev.target.scrollHeight, 112) + 'px';
});

$('#viewmode').onclick = () => {
  mode(MODES[(MODES.indexOf(mode()) + 1) % MODES.length]);
  renderCards();
};

/* Tagging one agent. It sits on the agent's own page rather than on the card, because the card's
 * buttons are the things you do often and this is a setting. */
function askViews(id) {
  const a = STATE.agents.find((x) => x.id === id);
  if (!a) return;
  const f = $('#form-views');
  f.dataset.agent = id;
  $('#views-who').textContent = `Where ${a.name} shows`;
  const tags = Array.isArray(a.views) && a.views.length ? a.views : MODES;
  $$('#form-views input').forEach((box) => { box.checked = tags.includes(box.value); });
  $('#dlg-views').showModal();
}

$('#form-views').addEventListener('submit', async (ev) => {
  if (ev.submitter?.value !== 'ok') return;
  const picked = $$('#form-views input').filter((b) => b.checked).map((b) => b.value);
  try {
    await api(`/api/agents/${ev.target.dataset.agent}`, 'PATCH', { views: picked });
    await refresh();
    toast(picked.length ? `Shows in ${picked.join(', ')}.` : 'Shows everywhere.');
  } catch (e) { toast(e.message, true); }
});

$('#form-job').addEventListener('submit', async (ev) => {
  if (ev.submitter?.value !== 'ok') return;
  const every = $('#job-every').value;
  try {
    /* One or the other, never both: the daemon refuses a job with a time of day and an interval,
     * because there would be no answer to when it is next due. */
    await api('/api/jobs', 'POST', {
      agent: $('#job-agent').value,
      task: $('#job-task').value,
      at: every ? null : $('#job-at').value,
      every: every ? Number(every) : null,
      quiet: !!$('#job-quiet').value,
    });
    $('#job-task').value = '';
    await refresh();
    toast('Scheduled.');
  } catch (e) { toast(e.message, true); }
});

$('#form-new').addEventListener('submit', async (ev) => {
  if (ev.submitter?.value !== 'ok') return;
  const f = new FormData(ev.target);
  const send = (createDir) => api('/api/agents', 'POST', {
    name: f.get('name'), cwd: f.get('cwd'),
    model: f.get('model') || null, permission_mode: f.get('permission_mode'),
    create_dir: createDir,
  });
  try {
    let a = await send(false);
    /* The daemon says the folder is not there rather than failing, so the choice to make it is
     * yours. Seeing the full path here is also the moment a mistyped one is caught. */
    if (a.needs_dir) {
      if (!confirm(`${a.path} does not exist yet.\n\nCreate it and start the agent there?`)) return;
      a = await send(true);
    }
    await refresh();
    location.hash = `#/a/${a.id}`;
  } catch (e) { toast(e.message, true); }
});

$('#form-cord').addEventListener('submit', async (ev) => {
  if (ev.submitter?.value !== 'ok') return;
  const d = ev.target.dataset;
  try {
    await api('/api/cords', 'POST', {
      kind: d.kind, from: d.from, to: d.to,
      instruction: new FormData(ev.target).get('instruction'),
    });
    ev.target.reset();
    refresh();
  } catch (e) { toast(e.message, true); }
});

$('#heldlist').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('[data-held-drop]');
  if (!btn) return;
  try {
    await api(`/api/agents/${heldFor}/unhold`, 'POST', { id: btn.dataset.heldDrop });
    await paintHeld();
    refresh();
  } catch (e) { toast(e.message, true); }
});

$('#form-held').addEventListener('submit', async (ev) => {
  if (ev.submitter?.value !== 'all') return;
  /* Deleting one is undoable by retyping it. Deleting the lot is not, so this one asks. */
  if (!confirm('Delete every undelivered message for this agent?')) return;
  try {
    const r = await api(`/api/agents/${heldFor}/unhold`, 'POST', {});
    toast(`${r.deleted} deleted.`);
    refresh();
  } catch (e) { toast(e.message, true); }
});

$('#pick').addEventListener('change', (ev) => {
  if (current && ev.target.files.length) sendFiles(current, [...ev.target.files]);
  ev.target.value = '';
});

/* Dragging onto the page anywhere would be friendlier and is not worth it: a stray drop while
 * reading is a file sent to an agent you did not mean. The target is the target. */
const drop = $('#drop');
['dragenter', 'dragover'].forEach((k) => drop.addEventListener(k, (ev) => {
  ev.preventDefault(); drop.classList.add('over');
}));
['dragleave', 'drop'].forEach((k) => drop.addEventListener(k, () => drop.classList.remove('over')));
drop.addEventListener('drop', (ev) => {
  ev.preventDefault();
  const list = [...(ev.dataTransfer?.files || [])];
  if (current && list.length) sendFiles(current, list);
});

$('#t-interrupt').onclick = () => {
  if (sock?.readyState === 1) sock.send(JSON.stringify({ type: 'interrupt' }));
};
$('#t-fit').onclick = () => { try { fit.fit(); sendResize(); } catch {} };

/* The message box exists next to a live terminal for one reason: a long message pasted into a
 * terminal interface can be submitted at its first newline. Sending it as one piece is the
 * server's job, so it goes as a control message rather than as keystrokes. */
const say = $('#say-text');
say.addEventListener('input', () => {
  say.style.height = 'auto';
  say.style.height = Math.min(say.scrollHeight, 180) + 'px';
});
say.addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); $('#say').requestSubmit(); }
});
$('#say').addEventListener('submit', (ev) => {
  ev.preventDefault();
  const text = say.value.trim();
  if (!text) return;
  if (sock?.readyState === 1) {
    sock.send(JSON.stringify({ type: 'submit', text }));
    say.value = ''; say.style.height = 'auto';
  } else if (current) {
    api(`/api/agents/${current}/message`, 'POST', { text })
      .then(() => { say.value = ''; })
      .catch((e) => toast(e.message, true));
  }
});

window.addEventListener('hashchange', route);
theme();
refresh().then(route);
listen();
