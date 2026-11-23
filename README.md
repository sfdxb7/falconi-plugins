# Falconi plugins for Hermes

Hermes plugins used by the Falconi app. Each folder installs with Hermes's own installer at a full commit:

```
hermes plugins install sfdxb7/falconi-plugins/plugins/garmoosha-push --ref <commit> --enable
hermes plugins install sfdxb7/falconi-plugins/plugins/garmoosha-voice --ref <commit> --enable
hermes plugins install sfdxb7/falconi-plugins/plugins/garmoosha-mail --ref <commit> --enable
hermes plugins install sfdxb7/falconi-plugins/plugins/garmoosha-screen --ref <commit> --enable
```

- `plugins/garmoosha-push`: tells the Falconi Android app when a reply finishes or a bot needs an OK, with the app closed. Notes are encrypted to the phone before they leave Hermes; the push relay only carries ciphertext.
- `plugins/garmoosha-voice`: lets Falconi's live voice calls use this Hermes's own ChatGPT sign-in instead of an OpenAI API key. The token never leaves Hermes.
- `plugins/garmoosha-mail`: gives an agent its own email account (Gmail, iCloud Mail, Fastmail, Yahoo Mail or any IMAP/SMTP server) connected with an app password. It can search, read and draft; it sends only after the person approves that exact message. Ported from Umbrel's murmur-mail ([getumbrel/hermes-agent-umbrel](https://github.com/getumbrel/hermes-agent-umbrel) @ fc05603, MIT, Copyright (c) 2026 Umbrel; see its LICENSE). Settings are `GARMOOSHA_MAIL_*` keys in the agent's `.env`; an existing `MURMUR_MAIL_*` key is read as a fallback.
- `plugins/garmoosha-screen`: lets an agent, as a last resort, ask the person to take over its browser for a step only they can do, then wait until they hand it back. Ported from Umbrel's murmur-screen (MIT, Copyright (c) 2026 Umbrel; see its LICENSE).

The folder and plugin names keep their original `garmoosha-*` names so existing installs and lookups (plugin keys, routes, approval rules) keep working.

MIT licensed. The mail and screen folders also carry Umbrel's MIT notice for the code they started from.
