/* Which tab a screen lights. Config holds Integrations and Health, so a deep link to either
   lights the Config tab on the phone bar and the Config pill on the desktop strip. */
export function tabOf(screen) {
  return screen === 'integrations' || screen === 'health' ? 'config' : screen
}

export default tabOf
