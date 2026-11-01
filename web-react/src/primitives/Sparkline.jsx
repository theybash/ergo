/* The insight card sparkline. Numbers in, one filled line out.
   The stroke and the fill come from the stylesheet, so warn repaints it amber.
   Geometry matches the mock: 4px of side room, the top value at y=10, the lowest at y=height-4. */

function round(n) {
  return Math.round(n * 10) / 10
}

export default function Sparkline({ points, width = 280, height = 56, stroke = 'accent', label }) {
  const list = points && points.length ? points : [0, 0]
  const pad = 4
  const top = 10
  const floor = height - 4
  const hi = Math.max.apply(null, list)
  const lo = Math.min.apply(null, list)
  const span = hi - lo || 1
  const step = list.length > 1 ? (width - pad * 2) / (list.length - 1) : 0

  const xy = list.map(function (value, i) {
    return [round(pad + i * step), round(floor - ((value - lo) / span) * (floor - top))]
  })

  const line = 'M' + xy.map(function (p) { return p[0] + ' ' + p[1] }).join(' L')
  const area = line + ' L' + xy[xy.length - 1][0] + ' ' + height + ' L' + pad + ' ' + height + ' Z'
  const warn = stroke === 'warn' ? ' warn' : ''
  const last = xy[xy.length - 1]

  return (
    <svg
      className="spark"
      viewBox={'0 0 ' + width + ' ' + height}
      preserveAspectRatio="none"
      role="img"
      aria-label={label}
    >
      <path className={'area' + warn} d={area}></path>
      <path className={'line' + warn} d={line}></path>
      <circle className={warn.trim() || undefined} cx={last[0]} cy={last[1]} r="3"></circle>
    </svg>
  )
}
