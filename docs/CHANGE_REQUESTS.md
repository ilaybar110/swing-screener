# Change Requests

Append-only log of requested changes to shared/foundation files. A bot that needs a
change to a file it doesn't own adds an entry here (never edits the file directly)
and works around it locally in the meantime. Bot 0 (or whoever currently owns the
target file) reviews and applies.

Template:

```
## YYYY-MM-DD — Bot N
File: path/to/file
Request: what you need changed
Why: what breaks/is-blocked without it
Status: open | applied | declined (+ reason)
```

---
