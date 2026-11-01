/* A second page for the same agent. Density is different from the latency page on purpose: a table
 * wants rows, a single state wants one big number. They should not look like siblings, only like
 * they come from the same place. */
export function render(root, platform) {
  root.innerHTML = `
    <style>
      table{width:100%;border-collapse:collapse}
      th{text-align:left;font:400 11px/1.3 var(--font-ui);text-transform:uppercase;
         letter-spacing:.06em;color:var(--text-dim);padding:0 0 8px;border-bottom:1px solid var(--border)}
      td{padding:9px 0;border-bottom:1px solid var(--border);font-size:13px}
      td.n{text-align:right;font-family:var(--font-mono);font-variant-numeric:tabular-nums}
      tr:last-child td{border-bottom:none}
    </style>
    <table><thead><tr><th>Component</th><th style="text-align:right">Per minute</th></tr></thead>
    <tbody id="rows"></tbody></table>`;
  const parts = [['Streaming ASR', 0.021], ['LLM turn loop', 0.058], ['Neural TTS', 0.028],
                 ['Telephony carriage', 0.013]];
  const total = parts.reduce((s, [, v]) => s + v, 0);
  root.querySelector('#rows').innerHTML =
    parts.map(([k, v]) => `<tr><td>${k}</td><td class="n">$${v.toFixed(3)}</td></tr>`).join('')
    + `<tr><td><strong>Total</strong></td><td class="n"><strong>$${total.toFixed(3)}</strong></td></tr>`;
}
