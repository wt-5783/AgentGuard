"""Bounded loopback clients for optional workstation services."""
import json
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener
from urllib.error import HTTPError

from .review.clients import _NoRedirect


class LocalServiceClient:
    def __init__(self, base, token=None, *, token_header='Authorization'):
        url = urlsplit(base)
        if (url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1')
                or url.username or url.password or url.query or url.fragment
                or url.path not in ('', '/') or not url.port):
            raise ValueError('Explicit loopback service address required')
        self.base, self.token, self.token_header = base.rstrip('/'), token, token_header

    def request(self, path, data=None):
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('Service-relative path required')
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers[self.token_header] = ('Bearer ' if self.token_header == 'Authorization' else '') + self.token
        req = Request(self.base + path, data=None if data is None else json.dumps(data).encode(), headers=headers)
        try:
            with build_opener(ProxyHandler({}), _NoRedirect()).open(req, timeout=5) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise RuntimeError('Local service HTTP ' + str(code)) from None
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError('Service response limit exceeded')
        return json.loads(raw)


def evaluation_client():
    import os
    from pathlib import Path
    token = os.environ.get('AGENTGUARD_EVAL_TOKEN')
    path = os.environ.get('AGENTGUARD_EVAL_TOKEN_FILE')
    if not token and path:
        try:
            token = Path(path).read_text(encoding='utf-8').strip()
        except OSError:
            pass
    return LocalServiceClient(os.environ.get('AGENTGUARD_EVAL_URL', 'http://127.0.0.1:8510'),
        token, token_header='X-Bench-Token')


def console_environment(environment, settings):
    """The presentation process receives UI/operator tokens, never model keys."""
    result = dict(environment)
    for name in ('AGENTGUARD_HOST_TOKEN', settings['agent']['api_key_env'], settings['guard']['api_key_env']):
        result.pop(name, None)
    return result
