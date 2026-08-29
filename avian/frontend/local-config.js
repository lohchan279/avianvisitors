/* Station settings for this fork.
 *
 * These are choices about one particular installation, not features. They
 * used to live as literals scattered through apt.js - which is both the
 * file this fork edits most and the file upstream edits most, so every one
 * of them was a standing merge conflict. Collected here they conflict
 * never: this file does not exist upstream, so nothing upstream can touch
 * it.
 *
 * apt.js reads each of these through a fallback, so it still runs correctly
 * if this file is missing or fails to load - a stock upstream apt.js keeps
 * upstream's defaults.
 *
 * Loaded from index.html before apt.js, and before the inline theme
 * resolver in <head> that reads themeDefault.
 */
window.AVIAN_LOCAL = {
  /* Collage bird sizing. Score is count ^ countExp, so a smaller exponent
   * flattens the difference between the bird heard 800 times and the one
   * heard twice. Upstream ships 0.65; 0.35 keeps the ordering readable
   * without shrinking the rarer birds into specks. */
  countExp: 0.35,

  /* Hosts allowed to offer the live audio stream over the public internet.
   * ghlyms.com is behind Cloudflare Access, so the stream is not actually
   * public - without this the player is hidden on any non-LAN host. */
  liveAudioHosts: ['ghlyms.com'],

  /* First-visit defaults, for a visitor with nothing saved. Anyone who has
   * picked a setting keeps their choice; these only fill the blank. */
  atlasDefault: 'cards',   // 'cards' | 'stamps'
  themeDefault: 'light'    // 'light' | 'dark' | 'auto' (auto = follow the OS)
};
