## The engine does the work

This extension carries no engine. It runs the same `semlayer` you would run in
a terminal and reads its `--json` output, so what you see here and what CI sees
are the same answers.

```
pipx install semlayer
```

Installed somewhere else? Set **`semlayer.cliPath`**.

Nothing here calls home, and no npm package ships inside the extension.
