# Console smoke test

`smoke.mjs` opens each screen of the console in headless Chrome as a 400x850 phone. For each
screen it reports console errors and warnings, uncaught exceptions, failed network requests and a
blank page. It saves one screenshot per screen and a `report.json`.

It only navigates and takes screenshots. It never types, clicks, or sends anything.

A screen can still write when it opens: the chat marks an agent read and sets its mode. So the
script lets only GET, HEAD and OPTIONS reach the server. It answers any other request itself with
an empty 200 and lists it in `blocked` in the report. It does not stop WebSocket messages, such
as a terminal resize. So run it against the stage, not the live console.

## Run it

Node 22 or later. No npm packages. It needs `google-chrome` on the PATH.

```
node tools/smoke/smoke.mjs --out /tmp/smoke-now
```

Options:

- `--base URL`: the console to test. Default `http://127.0.0.1:8085`, the stage.
- `--out DIR`: where the screenshots and `report.json` go. Default `/tmp/smoke-<time>`.
- `--port N`: the Chrome debug port. Default 9471. Pick another one if two runs share the machine.
- `--wait MS`: how long each screen gets to settle. Default 6000.
- `--screens a,b`: test only these screens. Default: fleet, chat, insights, memory, config,
  recruit, integrations, health.
- `--expect REGEX`: a fault line that is normal. The script lists it as expected, not as a fault.
  You can give it more than once.
- `--strict`: no fault is expected. This also drops the built-in rule below.

Each run starts Chrome with a fresh `--user-data-dir` and `--password-store=basic`, and deletes
that folder at the end. So the run sees no saved sign-in and never waits on the keyring.

It also turns off the back-forward cache. A cached page keeps its streams open. After six screens
Chrome then has no free connection to the host, and the seventh screen hangs.

## Read the result

One line per screen: `ok` or `FAIL`, then the counts, then each fault. The exit code is 0 when
every screen is clean, 1 when one or more screens fail, and 2 when the script itself fails.

Built-in expected fault: `404 GET /api/voice/greeting`. The shell asks for the front desk's greeting on
every screen, and the server answers 404 until the owner records one.

Requests that Chrome cancels when the page moves on (`net::ERR_ABORTED`) go in `canceled` in the
report. They are not faults.

If the first navigation to a screen times out, the script tries once more and notes `retried`.
