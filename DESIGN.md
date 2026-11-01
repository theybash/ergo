# Design

Read this before writing any interface for the platform. It applies to the console and to every
screen an agent writes.

This file exists because agents write their own screens. Ten agents left to their own taste
produce ten different interfaces on one page, and every one of them drifts toward the same
generic default. A locked system is what makes agent-authored UI possible rather than a mess.

---

## The tells to avoid

AI-generated interfaces converge because a model predicts the statistical centre of its
training data. Tailwind shipped indigo as its default in 2019, Tailwind dominates that data,
and so everything the model has not been told otherwise comes out purple.

Banned outright:

- **Purple, indigo, violet.** In any form, anywhere.
- **Gradients as decoration.** A gradient may encode data. It may not be background dressing.
- **Glassmorphism**, frosted panels, backdrop blur.
- **Drop shadows on cards.** Use a hairline border.
- **Border radius above 8px.** Nothing is a pill.
- **Emoji in the interface.** Not in headings, not in empty states, not as icons.
- **A centered hero with one call to action.** This is a console, not a landing page.
- **Three equal cards in a row** as a layout reflex.
- **Icons inside coloured circles.**
- **Inter as the only typeface.**

If a screen could belong to any product, it is wrong.

---

## Direction

**Editorial, not dashboard.** Closer to a well-set magazine page than to an admin template.
Serif headings, generous type, restrained colour, information dense where density earns its
place.

**Light by default.** Most agent tooling is dark-first. Ours is not, which alone makes it look
unlike everything else in the category.

**Colour is signal, never decoration.** A coloured element means something is up, down, live,
or broken. Nothing is coloured because the space looked empty.

---

## Tokens

Define these once as CSS custom properties. Never hardcode a colour anywhere else.

### Light, the default

```css
--bg:        #ffffff;   /* page */
--surface:   #ffffff;   /* cards, separated by border not shadow */
--border:    #e7e5e4;   /* hairline, warm grey */
--border-hi: #d6d3d1;   /* hover, focus */
--text:      #1c1917;   /* warm near-black, never pure #000 */
--text-dim:  #78716c;   /* secondary */
--text-faint:#a8a29e;   /* timestamps, units */
--accent:    #248064;   /* deep sea green, 4.83:1 on white */
--accent-bg: #f2fbf8;   /* accent wash, used sparingly */
```

### Dark

```css
--bg:        #0f0e0d;
--surface:   #17151400;  /* transparent, borders do the work */
--border:    #292524;
--border-hi: #44403c;
--text:      #e8e3dc;
--text-dim:  #a8a29e;
--text-faint:#78716c;
--accent:    #52bf9d;    /* mint, same hue lifted for the dark ground, 8.54:1 */
--accent-bg: #10201b;
```

### Semantic, both modes

```css
--up:      #4d7c0f;   /* green: online, gain */
--down:    #b91c1c;   /* red: offline error, loss */
--waiting: #b45309;   /* amber: working, pending */
--low:     #cc4b61;   /* a value in trouble: low battery, low memory, full disk */
```

**Greys are warm**, from the stone family, not blue-grey. This is what makes the light mode
feel like paper rather than a spreadsheet.

**One hue family, two lightnesses.** Deep sea green on white, mint on charcoal. The same
colour adjusted for its ground rather than two different ideas, so the product looks like
itself in both modes. Warm greys with a cool accent is the pairing that makes the light mode
read as printed rather than as an application.

Both clear their thresholds comfortably: `#248064` is 4.83:1 on white, `#52bf9d` is 8.54:1 on
the dark ground. Either is safe down to 11px label text.

**One accent per mode, and only one.** Green and red as *semantics* are data rather than
chrome, and appear only where a value genuinely rises, falls or fails.

### The trouble colour

`--low` is the only token deliberately identical in both modes: **4.42:1 on white and 4.36:1
on the dark ground**. It needed no per-mode variant, unlike the `--down` red, which manages
6.47:1 on white but only 2.98:1 on dark and therefore cannot be used there.

It is applied to the value and its bar together, so the state reads whether you are looking at
the number or scanning the shapes.

Thresholds belong in one named place, not scattered through rendering:

```js
const LIMIT = { memAvailGb: 1.5, batteryPct: 20, diskFreePct: 10, pressure: 1.0 };
```

**Some things never turn red.** A busy processor is doing its job. Colouring normal load
teaches the eye to ignore the colour, which costs you the one time it matters.

---

## Type

Three faces, each with one job.

```css
--font-display: "Instrument Serif", Georgia, serif;
--font-ui:      "Inter", system-ui, sans-serif;
--font-mono:    "JetBrains Mono", ui-monospace, monospace;
```

**Instrument Serif** for headings and any single large number. A serif in a developer console
is unusual and is most of why this will not look generic.

**Inter** for interface text, labels, body.

**JetBrains Mono** for anything that changes: prices, memory, timers, ids, terminal output.

**Always `font-variant-numeric: tabular-nums` on changing numbers.** Otherwise digits shift
width as they update and the whole card jitters.

Scale, and do not invent sizes between these:

```
11px  labels, units, timestamps        uppercase, 0.06em tracking
13px  body, most interface text
15px  card titles
28px  the one number a card is about   display serif
44px  page title                       display serif
```

---

## Space and shape

```
4px   radius on everything
1px   borders, always
8 / 12 / 16 / 24 / 40   the only spacing values
```

**Cards are separated by a hairline border, never a shadow.** Shadow is the single strongest
signal of a template.

**Density follows Linear.** Rows around 36px, little chrome, nothing floating. A dense screen
reads as a tool; an airy one reads as marketing.

---

## Light and dark

**Default follows the operating system**, through `prefers-color-scheme`.

**A toggle sits in the status bar**, top right, next to the health readout. It cycles system,
light, dark, and the choice is stored locally. An icon only, no label: sun, moon, and a small
half-filled circle for system.

Implementation rule that avoids the usual bug: define the complete light palette on `:root`,
redefine only the changed tokens inside `@media (prefers-color-scheme: dark)` guarded against
an explicit light override, then again under an explicit dark attribute so the toggle wins in
both directions. Never let a colour exist only inside a media query.

---

## Components

**Cards** are bordered rectangles. A label in 11px uppercase dim text, the thing itself, then
supporting rows in a two-column table with the value right-aligned and dim.

**The state dot** is 6px. Green for alive, warm grey for asleep. Never red for asleep, because
asleep is not an error.

**Working** is shown by motion, not colour: a small pulsing indicator. Motion is reserved for
this, so movement anywhere always means something is happening.

**Empty states get real design effort.** A new agent with no view is the first thing you will
see most days. It should say what it is waiting for, not "no data".

**Skeletons, not spinners**, wherever the shape of the content is known in advance.

**Numbers carry their unit in faint text**, at a smaller size, so the figure stays readable.

---

## What to take from whom

Worth looking at directly rather than describing:

**Linear** for density and restraint. How far information density goes without clutter, and
how little chrome a tool actually needs.

**Stripe** for metric discipline. A few numbers above the fold and nothing competing.

**Mercury and Ramp** for making financial data feel calm rather than urgent.

**Vercel** for skeletons; **Retool** for empty states. Both put as much effort into the
waiting state as the loaded one.

**Attio and Hex** for the one that matters most here: AI output as a first-class surface,
built into the interface, rather than a chat bubble floating on top of an older design. Our
agent cards are exactly that, and getting it wrong makes the whole thing look bolted together.

---

## Rules for agents writing views

Read this file first, and `UBIQUITOUS_LANGUAGE.md` with it. Every time, not once.

The palette stops your view looking foreign. The vocabulary stops it being named foreign, which is
the harder one to fix later, because a wrong colour is one edit and a wrong word spreads.

**Use the tokens.** Never a literal colour. The console may change the palette and your screen must
follow without being touched.

**Do not restyle the frame.** Your rectangle only. No attempt to change the page around you,
which will fail anyway, and no imitation of the console's own furniture.

**Match the density.** A view three times airier than its neighbours looks broken even when it
is beautiful alone.

**One accent, and only for meaning.** If your view uses orange for something that is not
notable, remove it.

**Numbers in mono with tabular figures.** Every time.

**Ask what the view is for before deciding how it looks.** A positions table wants density and
red and green. A voice agent wants one large state and almost nothing else. They should not
look like siblings, only like they come from the same place.

**One page per question, not one page with everything on it.** You can write several: another file
in your directory becomes another tab. Prefer that over a single screen with four unrelated blocks
stacked up, and do not build your own navigation inside your rectangle. The tabs are the platform's
job, and yours competing with them is how a page starts looking bolted on.
