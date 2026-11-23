"""garmoosha-push: notifications for Falki's Android app while it is closed.

The hooks (registered here) notice a finished reply and an approval waiting for the person; `push.py`
words the note, encrypts it to each registered phone (RFC 8291, the phone's own P-256 key) and either
signs it to the push relay (FCM) or files it for the app's own connection. The routes the app calls
(through HermesLink, with this Hermes's sign-in) are in ``dashboard/plugin_api.py``.
"""

from . import push


def register(ctx) -> None:
    try:
        relay = ctx.get_config("relay")
    except Exception:  # an older Hermes without plugin settings: the environment's, or the default
        relay = None
    if isinstance(relay, str) and relay.strip():
        push.configure(relay=relay.strip())
    ctx.register_hook("pre_approval_request", push.on_approval_request)
    ctx.register_hook("post_approval_response", push.on_approval_response)
    ctx.register_hook("post_llm_call", push.on_reply)
    ctx.register_hook("on_session_end", push.on_turn_end)
