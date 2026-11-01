/* The orb. A wireframe globe on a canvas: points spread over a sphere, thin edges between the
   near neighbours, nearer points brighter and larger. Pale ink on a transparent ground, so it
   sits on whatever the page is. No dependency, no network, plain 2D canvas.

   The voice drives it. setLevel(v) takes 0 in silence and 1 at a shout, and three things move:

     rotation   the globe turns 1x when silent and up to 4x at a shout
     push       every point slides out from the centre, up to 12% of the radius, each on its
                own phase, so the globe breathes rather than swells
     brightness points and edges gain ink, up to 45% of the way to full white

   The level is smoothed here, fast on the way up and slow on the way down, so a single loud
   frame does not make it flicker. A reader who asked for no motion gets a still wireframe:
   no turn, no push, drawn once.

   Orb(canvas, options) -> { setLevel(v), destroy() }
   options: nodes (how many points, default from the canvas size), thr (edge reach, 0 to 2).

   The geometry follows the "connecting" orb in thinking-orbs by Jakub Antalik (MIT), rewritten
   here so the mock carries no package. */

var TAU = Math.PI * 2;
var GOLDEN = Math.PI * (3 - Math.sqrt(5));

/* One repeatable pseudo random number per pair of inputs. */
function hash(x, y) {
  var n = Math.sin(x * 12.9898 + y * 78.233) * 43758.5453;
  return n - Math.floor(n);
}

/* Smooth value noise. The points drift on it, so the wireframe writhes instead of sitting rigid. */
function noise(x, y) {
  var xi = Math.floor(x), yi = Math.floor(y);
  var fx = x - xi, fy = y - yi;
  fx = fx * fx * (3 - 2 * fx);
  fy = fy * fy * (3 - 2 * fy);
  var a = hash(xi, yi), b = hash(xi + 1, yi), c = hash(xi, yi + 1), d = hash(xi + 1, yi + 1);
  return a + (b - a) * fx + (c - a) * fy + (a - b - c + d) * fx * fy;
}

/* A fibonacci sphere spreads n points evenly, which gives the even mesh a globe needs. */
function fib(i, n) {
  var y = 1 - 2 * (i + 0.5) / n;
  var r = Math.sqrt(Math.max(0, 1 - y * y));
  var a = i * GOLDEN;
  return [r * Math.cos(a), y, r * Math.sin(a)];
}

/* Is the page dark? The mock pins the theme on the root, and the system setting answers otherwise. */
function isDark(el) {
  var node = el;
  while (node && node.getAttribute) {
    var t = node.getAttribute("data-theme");
    if (t === "dark") { return true; }
    if (t === "light") { return false; }
    node = node.parentElement;
  }
  if (typeof window.matchMedia !== "function") { return true; }
  return !window.matchMedia("(prefers-color-scheme: light)").matches;
}

function stillWanted() {
  return Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
}

/* One ink value. "w" is 0 for the brightest ink and 1 for the dimmest, as the reference grades it.
   "boost" pushes it toward white on a dark page and toward black on a light one. */
function shade(w, boost, dark) {
  var L = dark ? 1 - w : w;
  L = dark ? L + (1 - L) * boost : L * (1 - boost);
  return Math.round(Math.max(0, Math.min(1, L)) * 255);
}

export function Orb(canvas, options) {
  var opts = options || {};
  var ctx = canvas.getContext("2d");
  if (!ctx) { return { setLevel: function () {}, destroy: function () {} }; }

  var still = stillWanted();
  var dark = isDark(canvas);
  var size = 0, dpr = 1;
  var nodes = 0, thr = 0.72;
  var nodeR = 1.4, nodeDepth = 1.8, lineW = 0.8, rs = 1;

  /* target is what the voice asks for, level is what the orb shows after smoothing. */
  var target = 0, level = 0;
  var clock = 0, last = 0, frame = null, running = false;

  /* The canvas follows its CSS box. Device pixels are capped at 2, which is crisp on a phone
     and cheap on a laptop. */
  function measure() {
    var box = canvas.getBoundingClientRect();
    var w = Math.round(box.width) || canvas.clientWidth || 64;
    dpr = Math.min(2, window.devicePixelRatio || 1);
    size = w;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(w * dpr);
    /* Point count follows the box. The reference uses 8 at 20px and 41 at 64px, and 48 is as
       busy as a big orb should get. */
    nodes = opts.nodes || Math.max(8, Math.min(48, Math.round(8 + (w - 20) * 0.75)));
    /* Edge reach follows the point count, so every point keeps about five neighbours and a
       small orb reads as the same object as a big one, not as loose dust. */
    thr = opts.thr || Math.max(0.55, Math.min(1.05, Math.sqrt(21 / Math.max(1, nodes - 1))));
    /* Dots grow slower than the orb, so a small orb is not a blob and a big one is not a mess. */
    rs = Math.pow(w / 300, 0.6);
    var grow = w < 64 ? Math.pow(64 / w, 0.6) : 1;
    nodeR = 1.4 * grow;
    nodeDepth = 1.8 * grow;
    lineW = Math.max(0.6, 0.8 * rs);
  }

  function draw() {
    var half = size / 2;
    var radius = half * 0.8;
    var e = level;

    /* The camera turns on two axes. Yaw runs on, pitch rocks, both carried by the same clock,
       so a change of speed never jumps the picture. */
    var yaw = clock * 0.4;
    var pitch = 0.3 + 0.16 * Math.sin(clock * 0.17);
    var sp = Math.sin(pitch), cp = Math.cos(pitch);
    var sy = Math.sin(yaw), cy = Math.cos(yaw);

    var unit = [], proj = [], i, j;
    var push = 0.12 * e;
    for (i = 0; i < nodes; i++) {
      var p = fib(i, nodes);
      /* Each point drifts on its own slice of the noise field. */
      var x = p[0] + 0.3 * (noise(i * 0.31 + 9, clock * 0.8) - 0.5) * 2;
      var y = p[1] + 0.3 * (noise(i * 0.53 + 27, clock * 0.7) - 0.5) * 2;
      var z = p[2] + 0.3 * (noise(i * 0.77 + 55, clock * 0.9) - 0.5) * 2;
      var len = Math.sqrt(x * x + y * y + z * z) || 1;
      x /= len; y /= len; z /= len;
      unit.push([x, y, z]);

      /* The voice pushes the point out. Each one has its own phase, so the globe breathes. */
      var out = 1 + push * (0.7 + 0.3 * Math.sin(clock * 2.4 + i * 1.7));
      var ox = x * out, oy = y * out, oz = z * out;
      var rx = ox * cy + oz * sy;
      var rz = -ox * sy + oz * cy;
      var ry = oy * cp - rz * sp;
      var depth = oy * sp + rz * cp;
      var zn = Math.max(0, Math.min(1, (depth + 1) / 2));
      proj.push([half + rx * radius, half - ry * radius, depth, zn, i]);
    }

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size, size);

    /* Edges first, under the points. Two points are joined when they are near on the sphere,
       and the line fades in as they close, so nothing pops. */
    var edgeInk = shade(0.42, 0.45 * e, dark);
    ctx.lineWidth = lineW;
    for (i = 0; i < nodes; i++) {
      for (j = i + 1; j < nodes; j++) {
        var dx = unit[i][0] - unit[j][0];
        var dy = unit[i][1] - unit[j][1];
        var dz = unit[i][2] - unit[j][2];
        var d = Math.sqrt(dx * dx + dy * dy + dz * dz);
        if (d >= thr) { continue; }
        var mid = (proj[i][3] + proj[j][3]) / 2;
        var a = (1 - d / thr) * (0.3 + 0.55 * mid) * (1 + 0.45 * e);
        if (a < 0.02) { continue; }
        ctx.strokeStyle = "rgba(" + edgeInk + "," + edgeInk + "," + edgeInk + "," + Math.min(1, a) + ")";
        ctx.beginPath();
        ctx.moveTo(proj[i][0], proj[i][1]);
        ctx.lineTo(proj[j][0], proj[j][1]);
        ctx.stroke();
      }
    }

    /* Far points draw first, so the near ones sit on top. */
    proj.sort(function (a, b) { return a[2] - b[2]; });
    for (i = 0; i < proj.length; i++) {
      var pt = proj[i];
      var zn2 = pt[3];
      var beat = still ? 1 : 1 + 0.2 * Math.sin(clock * 4.6 + pt[4] * 2.7);
      var r = Math.max(0.3, (nodeR + nodeDepth * zn2) * beat * rs);
      var ink = shade(0.55 - 0.45 * zn2, 0.45 * e, dark);
      ctx.fillStyle = "rgb(" + ink + "," + ink + "," + ink + ")";
      ctx.beginPath();
      ctx.arc(pt[0], pt[1], r, 0, TAU);
      ctx.fill();
    }
  }

  function step(now) {
    var dt = last ? Math.min(0.05, (now - last) / 1000) : 0.016;
    last = now;
    /* Fast up, slow down. A shout lifts the orb at once, silence lets it settle. */
    var k = target > level ? 0.45 : 0.10;
    level += (target - level) * k;
    if (level < 0.001) { level = 0; }
    /* A loud voice spins the globe up to four times its resting speed. */
    clock += dt * (1 + 3 * level);
    draw();
    frame = window.requestAnimationFrame(step);
  }

  function start() {
    if (running || still) { return; }
    running = true;
    last = 0;
    frame = window.requestAnimationFrame(step);
  }

  function stop() {
    running = false;
    if (frame) { window.cancelAnimationFrame(frame); frame = null; }
  }

  function onVisibility() {
    if (document.visibilityState === "hidden") { stop(); } else { start(); }
  }

  function refresh() {
    dark = isDark(canvas);
    measure();
    if (still || !running) { draw(); }
  }

  measure();
  if (still) {
    /* One representative frame, then nothing moves. */
    clock = 3;
    level = 0;
    draw();
  } else {
    draw();
    start();
    document.addEventListener("visibilitychange", onVisibility);
  }

  var themeWatch = null;
  if (typeof MutationObserver === "function") {
    themeWatch = new MutationObserver(refresh);
    themeWatch.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "class"] });
  }
  var resizeWatch = null;
  if (typeof ResizeObserver === "function") {
    resizeWatch = new ResizeObserver(refresh);
    resizeWatch.observe(canvas);
  } else {
    window.addEventListener("resize", refresh);
  }

  return {
    setLevel: function (v) {
      target = Math.max(0, Math.min(1, Number(v) || 0));
      if (still) { return; }
      start();
    },
    destroy: function () {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("resize", refresh);
      if (themeWatch) { themeWatch.disconnect(); themeWatch = null; }
      if (resizeWatch) { resizeWatch.disconnect(); resizeWatch = null; }
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
    }
  };
}
