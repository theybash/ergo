/* The ten services, in the mock's order: what each one is for, never whether it is connected.

   Whether it is connected comes from the daemon, GET /api/integrations, and only for the services
   it can check on this laptop (commandcenter/integrations.py). Every other card says "not
   measured". The states are never written down here, so no install shows a service as connected
   when it is not.

   Tailscale is the one card the browser fills in: its scope is the host this page came from. */

export const NOTE = 'Connecting anything real asks which account it uses first.'

// Said under the grid, so a card the daemon cannot check is never read as a measurement.
export const MEASURED_NOTE =
  'Telegram, Gmail, OpenAI voice and Codex are checked by the daemon on this laptop. The '
  + 'Tailscale scope is the host this page came from. The other cards are not measured yet.'

const INTEGRATIONS = [
  {
    id: 'telegram',
    mark: 'TG',
    name: 'Telegram',
    use: 'The bridge that pushes notices and takes commands.',
  },
  {
    id: 'gmail',
    mark: 'GM',
    name: 'Gmail',
    use: 'Morning letters go here.',
  },
  {
    id: 'calendar',
    mark: 'GC',
    name: 'Google Calendar',
    use: "Today's meetings for the letters.",
  },
  {
    id: 'github',
    mark: 'GH',
    name: 'GitHub',
    use: 'The Ergo code repo.',
  },
  {
    id: 'tailscale',
    mark: 'TS',
    name: 'Tailscale',
    use: 'How the phone reaches this laptop.',
  },
  {
    id: 'obsidian',
    mark: 'OB',
    name: 'Obsidian vault',
    use: 'Second brain, the letters land here.',
  },
  {
    id: 'syncthing',
    mark: 'SY',
    name: 'Syncthing',
    use: 'Keeps the vault on the phone.',
  },
  {
    id: 'kite',
    mark: 'ZK',
    name: 'Zerodha Kite',
    use: 'Positions and a trading view.',
  },
  {
    id: 'openai-voice',
    mark: 'AI',
    name: 'OpenAI voice',
    use: 'Talk to the fleet, GPT Live.',
  },
  {
    id: 'codex',
    mark: 'CX',
    name: 'Codex',
    use: 'A second engine beside Claude.',
  },
]

export default INTEGRATIONS
