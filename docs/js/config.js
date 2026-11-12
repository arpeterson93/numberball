/* Deployment configuration.
 *
 * Both values are Google Apps Script Web App URLs (see apps-script/DEPLOY.md).
 * Paste the /exec URLs here after deploying; leaving one blank just disables
 * that feature - the rest of the page keeps working.
 */
window.KM_CONFIG = {
  // Fires a workflow_dispatch on .github/workflows/key_moments.yml.
  REFRESH_ENDPOINT: "https://script.google.com/macros/s/AKfycbwm83cpSNFulJ8i4Lmvj6kraY1ssHl4sNOYyBhWetSEgd4HDeakQ9yxSTg_fl4SskZ66g/exec",

  // Reads and writes the per-name favorites list.
  FAVORITES_ENDPOINT: "https://script.google.com/macros/s/AKfycbwm83cpSNFulJ8i4Lmvj6kraY1ssHl4sNOYyBhWetSEgd4HDeakQ9yxSTg_fl4SskZ66g/exec",

  // Web Push (VAPID) public key. Blank disables the Notifications setting. See push-notifications-plan.md.
  VAPID_PUBLIC_KEY: "BKyD-_binReXbbLdvyIjCLT5lg0UTCrBkPlKrkrMssGMsoVC-GL__QRMB86U68yGCGHOo1v61SQOCxusBjf5XNc",
};
