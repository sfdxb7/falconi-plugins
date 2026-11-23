# garmoosha-push

A Hermes plugin that sends Falki's Android app its notifications: a reply finished, and an approval
waiting. The approval note has a lock-screen Approve once / Deny.

    hermes plugins install sfdxb7/garmoosha/plugins/garmoosha-push --enable

Then restart Hermes. Notifications while Falki is closed need this plugin. Without it, the app
says so in Settings.

## How it works

- The app registers through HermesLink with `POST /api/plugins/garmoosha-push/devices`, sending
  `{mode, publicKey, auth, token?, label?}`. Every route depends on Hermes's own sign-in
  (`_require_token`, or the plugin host's admission) and answers 401/403 without it. There are two modes:
  - `fcm`: store build. Notes go to the Falki relay, which hands them to FCM.
  - `stream`: no Google. The app's "Always connected" service reads `GET …/events` (SSE).
- Registering answers `{device, key}`. The plugin keeps only `sha256(key)` and the signed-in
  caller's hash as the phone's owner. `GET …/events` and `POST …/act` need that key in
  `x-garmoosha-device-key`; `DELETE …/devices/<id>` takes the key or the owner, so signing the
  phone out (the app forgets with its key) revokes it. Another sign-in can't take an id over (403).
- `GET …/devices` lists the caller's phones for Settings: id, label, mode, created, delivery and
  `isThisPhone`, never a public key, auth secret or FCM token.
- Every note is encrypted to the phone's P-256 key (RFC 8291). Only the phone can read it.
- An approval note carries a per-phone token that lasts five minutes and is spent on first use.
  Only `sha256(token)` is kept in `acts.json`. The app answers with `POST …/act`, and the plugin settles that exact request with Hermes's
  `resolve_gateway_approval`. An answer from anywhere else spends the tokens.
- Approve once / Deny appear only when the note shows the whole command (one line, unclipped, a
  real command, not "Needs your OK", and no control or format character such as a bidi override or
  zero-width space), the command is low-risk (read-only programs and git reads on an allow-list,
  no shell syntax, nothing touching secrets: `low_risk`, the same rule as `shared/notes.ts` and the
  app's `PushRules`, all checked against `test/fixtures/approve-from-note.json`), and the approval was asked in a process that has itself
  served one of these routes (the one the app reaches). Hermes keeps pending approvals in that process's memory and has no supported way to
  answer them from another (v0.23.3: no control-socket verb; the API server's run approvals cover
  only its own runs). Anything else gets Open chat only, with no token. Elsewhere the note just opens the chat, and a stale token answers 409.
- Relay sends are signed with this Hermes's own Ed25519 key, kept in
  `<hermes root>/garmoosha-push/relay-key.pem` (mode 600). It's made once, atomically, only when
  absent. A key that can't be read stops relay sends with an error in the log and in `/status`;
  it is never replaced, as a new key would strand every phone pinned to the old one.
- `GET …/status?device=<id>` says `relayKey` (`ok`, `none`, `unreadable`), `approvals`
  (`this-process`), `pid` (the process answering), and how the last relay send to that phone went: `delivery.state` is `ok`,
  `pinned_elsewhere` (the relay answered 403: the token belongs to another Hermes's key),
  `unreachable`, `relay_error`, `relay_key_unreadable` or `unknown`.

## Settings

`plugins.entries.garmoosha-push.settings.relay` in Hermes's config, else the `GARMOOSHA_PUSH_RELAY`
environment variable, else `https://push.alfalasi.io`. Set it to `off` to send only to phones on their
own connection. Only https is accepted, except http to a relay on the same machine.
