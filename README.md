# Falconi plugins for Hermes

Hermes plugins used by the Falconi app. Each folder installs with Hermes's own installer at a full commit:

```
hermes plugins install sfdxb7/falconi-plugins/plugins/garmoosha-push --ref <commit> --enable
hermes plugins install sfdxb7/falconi-plugins/plugins/garmoosha-voice --ref <commit> --enable
```

- `plugins/garmoosha-push`: tells the Falconi Android app when a reply finishes or a bot needs an OK, with the app closed. Notes are encrypted to the phone before they leave Hermes; the push relay only carries ciphertext.
- `plugins/garmoosha-voice`: lets Falconi's live voice calls use this Hermes's own ChatGPT sign-in instead of an OpenAI API key. The token never leaves Hermes.

The folder and plugin names keep their original `garmoosha-*` names so existing installs keep working.

MIT licensed.
