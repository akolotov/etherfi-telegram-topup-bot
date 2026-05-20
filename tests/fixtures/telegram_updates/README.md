# Telegram Update Fixtures

These fixtures seed the Telegram ingress harness before the real bot runtime exists.

Source classes:

- `message_plain_text`, `message_reply_text`, and `message_reaction_*` preserve real Telegram update shapes harvested in the Wabelfish project and anonymized.
- `message_command_start`, `callback_query_top_up`, and `callback_query_ignore` are synthetic-derived ether.fi fixtures based on real Telegram shapes. Replace them with live-harvested fixtures before using them as live acceptance evidence.
- `my_chat_member_block` preserves the real Telegram block update shape with ether.fi anonymized bot identity.

The fixtures are intended for adapter and smoke tests. They are not a substitute for live Telegram Web acceptance once the runtime is available.
