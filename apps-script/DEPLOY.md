# Key Moments - Apps Script deploy

`Code.gs` is one web app that serves both the "Refresh now" trigger and the
favorites store. Deploying it is a one-time manual step; nothing in CI touches it.

---

## 1. Create the favorites spreadsheet

1. Make a **new** Google Sheet - do *not* use the MLN plays workbook.
   Name it something like `MLN Key Moments - Favorites`.
2. Rename the first tab to `favorites`.
3. Put these headers in row 1:

   | key | player_ids_json | updated_at_iso | note |
   |-----|-----------------|----------------|------|

   (The script creates the tab and headers if they are missing, so this step is
   just so you can see the data.)

## 2. Add the script

1. In that sheet: **Extensions -> Apps Script**.
2. Delete the placeholder `Code.gs` contents and paste this repo's
   `apps-script/Code.gs` in whole.
3. Save.

## 3. Mint the GitHub PAT

The refresh button needs permission to fire `workflow_dispatch`.

1. GitHub -> Settings -> Developer settings -> **Fine-grained tokens** -> Generate new token.
2. Repository access: **Only select repositories** -> `arpeterson93/numberball`.
3. Repository permissions: **Actions: Read and write**. Nothing else.
4. Set an expiry you are willing to renew (the button stops working when it lapses).
5. Copy the token.

## 4. Store the PAT in Script Properties

In the Apps Script editor: **Project Settings** (gear) -> Script Properties ->
Add script property.

| Property | Value |
|----------|-------|
| `GITHUB_PAT` | the token from step 3 |

The token lives only here. It is never sent to the browser - the page calls the
web app, and the web app calls GitHub.

## 5. Deploy as a web app

1. **Deploy -> New deployment**, type **Web app**.
2. Execute as: **Me**.
3. Who has access: **Anyone**.
4. Deploy, approve the OAuth consent screen, and copy the `/exec` URL.

## 6. Paste the URL into the page

Open `docs/js/config.js` and set **both** values to the same `/exec` URL
(one deployment answers both jobs):

```js
window.KM_CONFIG = {
  REFRESH_ENDPOINT: "https://script.google.com/macros/s/AKfyc.../exec",
  FAVORITES_ENDPOINT: "https://script.google.com/macros/s/AKfyc.../exec",
};
```

Commit and push - GitHub Pages picks it up on the next deploy.

## 7. Smoke test

- `<exec-url>?action=trigger_refresh` in a browser tab should return
  `{"triggered":true}` and a run should appear under the repo's Actions tab
  within a few seconds. A second call inside 60s returns a rate-limit error;
  that is the built-in cooldown, not a failure.
- `<exec-url>?key=test` should return `{"key":"test","player_ids":[],...}`.
- On the live page, star a player, reload in a private window, type the same
  display name, and confirm the star came back.

---

## Redeploying after a `Code.gs` edit

**Deploy -> Manage deployments -> edit (pencil) -> Version: New version -> Deploy.**
Editing the deployment keeps the same `/exec` URL, so `config.js` does not change.
Creating a *new* deployment mints a new URL and you would have to re-paste it.

## Threat model, stated plainly

There is no authentication. The favorites key is a slug of whatever display name
someone types, so two people who type "Alex" share one list, and anyone who
guesses a name can read or overwrite that list. Nothing sensitive is stored -
it is a list of player IDs. The page says as much in its favorites panel. If
that ever stops being acceptable, the fix is a real login, not a longer key.
