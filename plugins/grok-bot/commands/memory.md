---
description: Recall Substrate memory before acting
---

Call `memory_search` with your intended action in the query before
irreversible operations. Expand contested facts with `memory_expand` and
fetch proof with `memory_evidence`. If memory reports
`authorization_required`, show the exact `verification_uri_complete` URL to
the user and ask them to approve it in a browser.
