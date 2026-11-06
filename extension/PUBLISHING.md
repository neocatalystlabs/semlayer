# Publishing the extension

Two marketplaces, one `.vsix`. Cursor reads the Open VSX registry; VS Code
reads Microsoft's. Publishing to one does not publish to the other.

Everything below needs an account, so none of it can be done for you.

## Before the first publish

**1. Claim the publisher ID on both registries.** `semlayer` must be free on
each, and they are separate namespaces.

- VS Code Marketplace: create an Azure DevOps organisation, then a publisher
  at <https://marketplace.visualstudio.com/manage>. The publisher ID has to
  match `"publisher": "semlayer"` in `package.json`.
- Open VSX: sign in at <https://open-vsx.org> with GitHub, agree to the
  publisher agreement, then create the `semlayer` namespace.

**2. Create tokens.**

- Azure DevOps → User settings → Personal access tokens. Scope:
  **Marketplace → Manage**, organisation **All accessible organisations**.
  A token scoped to a single organisation fails with a 401 that does not say
  why.
- Open VSX → profile → Access Tokens.

**3. Store them as repository secrets** — `VSCE_PAT` and `OVSX_PAT` — if you
want the release workflow to publish. Never put either in `.npmrc`, an env
file, or the repo.

## Publishing

```bash
cd extension
npm ci
npm test            # position reconstruction
npm run package     # writes semlayer-<version>.vsix

npx vsce publish --no-dependencies --pat "$VSCE_PAT"
npx ovsx publish semlayer-*.vsix --pat "$OVSX_PAT"
```

`vsce publish` can bump the version for you (`vsce publish minor`). Do not use
it here: the version in `package.json` is what the `.vsix` and the changelog
already say, and having the command rewrite it mid-publish makes the artifact
you tested not the artifact you shipped.

## Checks the registries apply

- **The icon must be a PNG**, at least 128×128. Ours is the 512×512 brand mark.
- **The README is the marketplace page.** Relative image paths do not resolve
  there — any screenshot has to be an absolute `https://` URL, so put images on
  semlayer.dev or raw.githubusercontent.com rather than in the package.
- **`repository` must point at a public repo** or the page shows no source link.
- A `LICENSE.md` in the package becomes the licence tab. Ours is FSL-1.1-ALv2.

## Installing without a marketplace

The `.vsix` works on its own, which is the whole of the beta distribution plan:

```bash
code   --install-extension semlayer-0.1.0.vsix     # VS Code
cursor --install-extension semlayer-0.1.0.vsix     # Cursor
```

CI attaches the `.vsix` to every run as an artifact, so any commit is
installable without a publish.

## Still missing before a first publish

- **Screenshots.** The marketplace page is mostly a screenshot gallery, and
  there are none. They have to be of the real extension running, not of the
  design mock in `beta/ux/` — the mock shows surfaces this version does not
  implement (the agent chat panel, review as an editor tab).
- **A `semlayer.dev/vscode` page** to link to from the README.
