/* A view, written the way an agent is meant to write one.
 *
 * Read DESIGN.md first, then use only the tokens: the console may change the palette and this
 * has to follow without being edited. Ask the platform by name; there is no network here.
 *
 * A voice agent wants one large state and almost nothing else, so that is what this is. It
 * should not look like a positions table, only like it came from the same place.
 */

export function render(root, platform) {
  root.innerHTML = `
    <style>
      .wrap{display:flex;flex-direction:column;gap:24px;max-width:640px}
      .state{display:flex;align-items:baseline;gap:12px}
      .big{font-size:44px;line-height:1;letter-spacing:-0.01em}
      .dot{width:8px;height:8px;border-radius:50%;background:var(--text-faint);
           align-self:center}
      .dot.on{background:var(--up)}
      .rows{border-top:1px solid var(--border)}
      .r{display:flex;align-items:baseline;gap:12px;min-height:36px;
         border-bottom:1px solid var(--border)}
      .r:last-child{border-bottom:none}
      .r .v{margin-left:auto}
      .r .v.low{color:var(--low)}
    </style>
    <div class="wrap">
      <div>
        <div class="label">latency budget</div>
        <div class="state">
          <!-- Display serif, not mono: this is the one number the view is about, and it does
               not change. Mono with tabular figures is for numbers that tick. -->
          <span class="big" id="ms">800</span><span class="label">ms to first sound</span>
        </div>
      </div>
      <div class="rows" id="rows"></div>
    </div>`;

  const rows = root.querySelector('#rows');
  const line = (k, v, low) =>
    `<div class="r"><span class="label">${k}</span><span class="v num${low ? ' low' : ''}">${v}</span></div>`;

  async function tick() {
    try {
      const h = await platform.health();
      const s = await platform.state();
      const disk = (h.disks || []).find((d) => d.mount === '/') || (h.disks || [])[0];
      rows.innerHTML = [
        line('memory free', `${h.memory.available_gb.toFixed(1)} GB`, h.memory.available_gb < 1.5),
        line('processor', `${Math.round(h.cpu.percent)} %`),
        disk ? line('disk free', `${disk.free_gb.toFixed(0)} GB`, (100 - disk.percent) < 10) : '',
        line('agents awake', s.agents.filter((a) => a.state === 'green').length),
      ].join('');
    } catch (err) {
      rows.innerHTML = line('platform', String(err.message));
    }
  }

  tick();
  setInterval(tick, 4000);
}
