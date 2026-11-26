import asyncio
import logging
import random
import re
import string
from typing import Dict, List, Optional, Tuple

from telethon.tl.functions.messages import GetBotCallbackAnswerRequest

from core_app.core.shared_state import SharedState

logger = logging.getLogger(__name__)

_TOKEN_PATTERN = re.compile(r'(\d{8,10}:[a-zA-Z0-9_-]{35,})')


class BotFatherClient:
    """Encapsulates Telegram @BotFather menu navigation, token parsing, and /newbot dialogs."""

    def __init__(self):
        self._cached_mybots = None  # (msg_id, [{'username', 'callback_data'}])

    def invalidate_mybots_cache(self) -> None:
        """Clear cached /mybots response after revoking or creating bots."""
        self._cached_mybots = None

    @staticmethod
    def extract_token(text: Optional[str]) -> Optional[str]:
        """Parse a BotFather API token string from a message body."""
        if not text:
            return None
        match = _TOKEN_PATTERN.search(text)
        return match.group(1) if match else None

    @staticmethod
    def _find_button_data(msg, predicate):
        """Return the callback data bytes of the first inline button matching predicate."""
        if not msg or not getattr(msg, 'reply_markup', None):
            return None
        for row in msg.reply_markup.rows:
            for button in row.buttons:
                if hasattr(button, 'data') and button.data and predicate(button.text or ''):
                    return button.data
        return None

    async def _navigate_back_to_bot_list(self, client, token_msg) -> Optional[int]:
        """Navigate Level 2 ('Back to Bot') -> Level 1 ('Back to Bot List') -> Level 0 msg_id."""
        back_to_bot_data = self._find_button_data(token_msg, lambda txt: 'Back to Bot' in txt)
        if back_to_bot_data and getattr(token_msg, 'id', None):
            await client(GetBotCallbackAnswerRequest(peer='BotFather', msg_id=token_msg.id, data=back_to_bot_data))
            await asyncio.sleep(0.5)

        options_resp = await client.get_messages('BotFather', limit=1)
        if options_resp:
            back_to_list_data = self._find_button_data(options_resp[0], lambda txt: 'Back to Bot List' in txt)
            if back_to_list_data:
                await client(GetBotCallbackAnswerRequest(peer='BotFather', msg_id=options_resp[0].id, data=back_to_list_data))
                await asyncio.sleep(0.5)

        bf_resp = await client.get_messages('BotFather', limit=1)
        return bf_resp[0].id if bf_resp else None

    async def query_mybots_list(self, shared_state: SharedState) -> Tuple[Optional[int], List[Dict]]:
        """Send /mybots to @BotFather and parse its inline keyboard buttons."""
        if self._cached_mybots:
            logger.debug("[mybots] Using cached /mybots result.")
            return self._cached_mybots

        client = shared_state.client
        await client.send_message('BotFather', '/mybots')
        await asyncio.sleep(1)

        messages = await client.get_messages('BotFather', limit=1)
        if not messages or not messages[0].reply_markup:
            logger.warning("[mybots] No inline keyboard found in BotFather response.")
            return None, []

        msg = messages[0]
        bots = []
        for row in msg.reply_markup.rows:
            for button in row.buttons:
                if hasattr(button, 'data') and button.data:
                    bots.append({
                        'username': button.text.lstrip('@'),
                        'callback_data': button.data,
                    })

        logger.info(f"[mybots] Found {len(bots)} bots: {[b['username'] for b in bots]}")
        self._cached_mybots = (msg.id, bots)
        return self._cached_mybots

    async def click_bot_button(self, shared_state: SharedState, bf_msg_id: int, callback_data: bytes) -> Optional[int]:
        """Click a bot entry in Level 0 /mybots list and return Level 1 options message ID."""
        client = shared_state.client
        await client(GetBotCallbackAnswerRequest(peer='BotFather', msg_id=bf_msg_id, data=callback_data))
        await asyncio.sleep(0.5)

        resp = await client.get_messages('BotFather', limit=1)
        if not resp or not resp[0].reply_markup:
            logger.warning("[mybots] No inline keyboard after clicking bot button.")
            return None
        return resp[0].id

    async def get_token_and_navigate_back(self, shared_state: SharedState, options_msg_id: int) -> Tuple[Optional[str], Optional[int]]:
        """Open API Token screen from Level 1, extract the token, and navigate back to Level 0."""
        del options_msg_id
        client = shared_state.client
        resp = await client.get_messages('BotFather', limit=1)
        if not resp or not resp[0].reply_markup:
            return None, None

        token_btn_data = self._find_button_data(resp[0], lambda txt: txt == 'API Token')
        if not token_btn_data:
            logger.warning("[mybots] 'API Token' button not found.")
            return None, None

        await client(GetBotCallbackAnswerRequest(peer='BotFather', msg_id=resp[0].id, data=token_btn_data))
        await asyncio.sleep(0.5)

        token_resp = await client.get_messages('BotFather', limit=1)
        token = None
        token_msg = token_resp[0] if token_resp else None
        if token_msg:
            text = token_msg.text or ''
            logger.debug("[mybots] Received token response from BotFather (length=%d).", len(text))
            token = self.extract_token(text)
            if not token:
                logger.warning(f"[mybots] Could not parse token from: {repr(text[:200])}")

        new_bf_msg_id = await self._navigate_back_to_bot_list(client, token_msg)
        return token, new_bf_msg_id

    async def revoke_token_and_navigate_back(self, shared_state: SharedState, token_msg_id: int) -> Tuple[Optional[str], Optional[int]]:
        """Revoke the current bot token from Level 2, parse the replacement, and return to Level 0."""
        del token_msg_id
        client = shared_state.client
        resp = await client.get_messages('BotFather', limit=1)
        if not resp or not resp[0].reply_markup:
            return None, None

        revoke_btn_data = self._find_button_data(resp[0], lambda txt: 'Revoke' in txt)
        if not revoke_btn_data:
            logger.warning("[mybots] 'Revoke current token' button not found.")
            return None, None

        await client(GetBotCallbackAnswerRequest(peer='BotFather', msg_id=resp[0].id, data=revoke_btn_data))
        await asyncio.sleep(0.5)

        confirm_resp = await client.get_messages('BotFather', limit=1)
        if confirm_resp:
            confirm_data = self._find_button_data(
                confirm_resp[0],
                lambda txt: any(k in txt.lower() for k in ('yes', 'confirm', 'revoke')),
            )
            if confirm_data:
                await client(GetBotCallbackAnswerRequest(peer='BotFather', msg_id=confirm_resp[0].id, data=confirm_data))
                await asyncio.sleep(0.5)

        new_token_resp = await client.get_messages('BotFather', limit=1)
        new_token = None
        new_token_msg = new_token_resp[0] if new_token_resp else None
        if new_token_msg:
            text = new_token_msg.text or ''
            logger.debug("[mybots] Received revoked token response from BotFather (length=%d).", len(text))
            new_token = self.extract_token(text)
            if not new_token:
                logger.warning(f"[mybots] Could not parse new token from: {repr(text[:200])}")

        new_bf_msg_id = await self._navigate_back_to_bot_list(client, new_token_msg)
        self.invalidate_mybots_cache()
        return new_token, new_bf_msg_id

    async def create_new_bot(self, shared_state: SharedState) -> Dict:
        """Run a /newbot conversation with @BotFather and return status plus credentials."""
        async with shared_state.client.conversation('BotFather', timeout=15) as conv:
            await conv.send_message('/newbot')
            resp = await conv.get_response()
            logger.info(f"[provision] BotFather: {resp.text[:200]}")

            if 'How are we going to call it' not in resp.text:
                logger.warning(f"[provision] BotFather refused to create new bot. Response: {resp.text:.200s}")
                if 'too many attempts' in resp.text.lower():
                    match = re.search(r'in (\d+) seconds', resp.text)
                    wait_time = int(match.group(1)) if match else 10
                    return {"status": "rate_limited", "wait_seconds": wait_time}
                return {"status": "limit_reached"}

            await conv.send_message("TDrive Worker Node")
            resp = await conv.get_response()
            logger.info(f"[provision] Name response: {resp.text[:200]}")
            if 'choose a username' not in resp.text:
                logger.error(f"[provision] Unexpected response when setting name: {resp.text:.200s}")
                return {"status": "retry"}

            random_suffix = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
            username = f"tdrive_worker_{random_suffix}_bot"
            await conv.send_message(username)
            resp = await conv.get_response()
            logger.info(f"[provision] Username response: {resp.text[:200]}")

            if 'Sorry, this username is already taken' in resp.text:
                random_suffix = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
                username = f"tdrive_{random_suffix}_bot"
                await conv.send_message(username)
                resp = await conv.get_response()

            if 'Done!' not in resp.text:
                logger.error(f"[provision] Failed to create bot {username}. Response: {resp.text:.200s}")
                return {"status": "retry"}

            token = self.extract_token(resp.text)
            if not token:
                logger.error("[provision] Failed to extract token from BotFather.")
                return {"status": "retry"}

            bot_id = int(token.split(':')[0])
            return {"status": "created", "bot_id": bot_id, "token": token, "username": username}
