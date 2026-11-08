# Offline page

Each run writes `index.html`, so a user can see every saved photo and video with its post text, with no internet access.

## Sub-features

- `page-posts` lists each post that has a saved photo or video, newest first, with its text and a footer notice.
- `page-media` shows each saved photo and video.
- `page-filter` shows only posts that tag the user's children.

## How to get to it (user POV)

- Open the `Your archive:` link that the run prints.

## Driving it with capture.sh

Preconditions:

- The baseline preconditions pass.
- A `sync-download` run exists in `.verify-runs/RUN_ID`, and its audit has run. `before` equals `posts with files` in `audit.json`.

- **Open.** Use a browser tool in a background page, and open `file://<checkout>/.verify-runs/RUN_ID/index.html`.
- **Filter.** Run this function in the page. It clicks the real checkbox and returns counts only: `async () => { const visible = () => [...document.querySelectorAll('article')].filter(a => getComputedStyle(a).display !== 'none').length; const box = document.querySelector('#tagged'); const before = visible(); box.click(); const checked = visible(); box.click(); const unchecked = visible(); const sources = [...document.querySelectorAll('img')].map(i => i.src); let ok = 0, bad = 0; await Promise.all(sources.map(s => new Promise(r => { const i = new Image(); i.onload = () => { ok++; r(); }; i.onerror = () => { bad++; r(); }; i.src = s; }))); return { before, checked, unchecked, ok, bad }; }`. `before` equals `unchecked` and equals `posts with files`. `checked` is above 0 and below `before`, when the account has posts that tag a child. `bad` is 0.
- **Proof.** Save the returned object as `.verify-evidence/RUN_ID/page-filter.json`.
- **Close.** Close the page that you opened.

## Gotchas

- Do not take a screenshot or a snapshot. They put children's photos, names or post text into the session.
- A browser tool can return the titles and URLs of the owner's other tabs. Do not repeat them.
- The filter finds only tagged posts.
- The page lists every post in `feed.json` that has a saved photo or video, including posts that have left the feed.
