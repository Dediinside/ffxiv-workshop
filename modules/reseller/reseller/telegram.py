"""Bot API requests do not log token URLs; persistent successful-alert deduplication."""
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
from .analysis import telegram_text
from .net import ApiError, Cancelled, NoRedirect, check, wait
from .storage import atomic_json


def credentials(token, chat_id=None):
    if not isinstance(token, str) or not re.fullmatch(r'\d{5,}:[A-Za-z0-9_-]{20,}', token.strip()):
        raise ValueError('Введите токен бота от @BotFather')
    if chat_id is not None and not re.fullmatch(r'-?\d{1,20}', str(chat_id).strip()):
        raise ValueError('Chat ID должен быть числом (личный чат или группа)')


class Telegram:
    def __init__(self, token, opener=None):
        credentials(token)
        self._token, self.opener = token.strip(), opener or build_opener(NoRedirect())

    def call(self, method, payload, cancel=None):
        if method not in ('sendMessage', 'getUpdates', 'getMe'):
            raise ValueError('Метод Telegram не поддерживается')
        check(cancel)
        request = Request('https://api.telegram.org/bot' + self._token + '/' + method,
                          data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ApiError('Ответ Telegram слишком большой')
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get('ok') is not True:
                raise ApiError('Telegram отклонил запрос. Проверьте токен, Chat ID и /start.')
            return value['result']
        except HTTPError as exc:
            code = exc.code
            exc.close()
            # Never stringify the HTTPError: its URL contains the token.
            raise ApiError(f'Telegram HTTP {code}. Проверьте токен, Chat ID и /start; при 429 подождите.') from None
        except (URLError, TimeoutError, OSError, ValueError, KeyError):
            raise ApiError('Telegram недоступен или вернул неверный ответ. Доставка не подтверждена.') from None

    def send(self, chat_id, text, cancel=None):
        credentials(self._token, chat_id)
        return self.call('sendMessage', {'chat_id': str(chat_id), 'text': text[:4096],
                                        'link_preview_options': {'is_disabled': True}}, cancel)

    def chats(self, cancel=None):
        result = self.call('getUpdates', {'timeout': 0, 'limit': 100, 'allowed_updates': ['message']}, cancel)
        if not isinstance(result, list):
            raise ApiError('Не удалось прочитать список чатов')
        chats = {}
        for update in result:
            chat = update.get('message', {}).get('chat', {})
            if chat.get('type') == 'private' and isinstance(chat.get('id'), int):
                chats[str(chat['id'])] = str(chat.get('first_name', 'Личный чат'))[:80]
        return chats


class Notifier:
    def __init__(self, state_path, now=time.time, sleeper=wait):
        self.path, self.now, self.sleeper = Path(state_path), now, sleeper
        try:
            self.state = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(self.state, dict) or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in self.state.values()):
                raise ValueError()
        except (OSError, ValueError, TypeError):
            self.state = {}

    def eligible(self, deals, settings):
        now = self.now()
        output = []
        for deal in deals:
            if deal.historical:
                continue
            prefix = settings['chat_id'] + ':'
            exact = prefix + 'listing:' + deal.fingerprint
            group = prefix + f'item:{deal.sale_world_id}:{deal.item}:{deal.buy_world_id}:{deal.hq}'
            age = now - self.state.get(exact, -1e20)
            group_age = now - self.state.get(group, -1e20)
            if 0 <= age < 30 * 86400 or 0 <= group_age < settings['cooldown_minutes'] * 60:
                continue
            output.append(deal)
        return output

    def dispatch(self, deals, settings, bot, cancel=None, bypass_group=False, on_delivered=None):
        delivered, errors = 0, []
        now = self.now()
        self.state = {k: v for k, v in self.state.items() if 0 <= now - v < 30 * 86400}
        for deal in (deals if bypass_group else self.eligible(deals, settings)):
            if deal.historical:
                continue
            check(cancel)
            now = self.now()
            # Chat-specific state, plus per-item/world/quality cooldown across new listings.
            prefix = settings['chat_id'] + ':'
            exact = prefix + 'listing:' + deal.fingerprint
            group = prefix + f'item:{deal.sale_world_id}:{deal.item}:{deal.buy_world_id}:{deal.hq}'
            if exact in self.state or not bypass_group and 0 <= now - self.state.get(group, -1e20) < settings['cooldown_minutes'] * 60:
                continue
            try:
                bot.send(settings['chat_id'], telegram_text(deal), cancel)
            except Cancelled:
                raise
            except ApiError as exc:
                errors.append(str(exc))
                break
            self.state[exact] = self.state[group] = self.now()
            delivered += 1
            # Persist after each confirmed send; failures never count as delivered.
            try:
                try:
                    if on_delivered is not None:
                        on_delivered(deal)
                finally:
                    atomic_json(self.path, self.state)
            except OSError:
                errors.append('Сообщение отправлено, но защита от повторов не сохранена. Остановите уведомления и проверьте доступ к папке настроек.')
                break
            if delivered >= settings['alert_limit']:
                break
            self.sleeper(1.1, cancel)
        return delivered, errors
