"""One fixed model endpoint per role; credentials never enter the sandbox."""
import json
from pathlib import Path
import socketserver
import threading
import time
from urllib import request, error


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Broker:
    def __init__(self, directory, settings, keys, log):
        self.path = Path(directory) / 'model.sock'
        self.log, self.lock = Path(log), threading.Lock()
        self.trace_counter=0
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(130)
                tick = time.monotonic()
                role, status, usage = None, 400, None
                trace=None
                try:
                    raw = self.rfile.readline(2_000_001)
                    if len(raw) > 2_000_000 or not raw.endswith(b'\n'):
                        raise ValueError('REQUEST_TOO_LARGE')
                    packet = json.loads(raw)
                    role, body = packet['role'], packet['body']
                    cfg = settings[role]
                    if role not in ('agent', 'guard') or body['model'] != cfg['model']:
                        raise ValueError('MODEL_MISMATCH')
                    if body.get('stream') is not False or body['max_tokens'] > cfg['max_output_tokens']:
                        raise ValueError('INVALID_MODEL_PARAMETERS')
                    if role=='guard':
                        # Compiler failures can precede Runtime session creation.
                        # Retain the exact guard protocol without credential headers.
                        with owner.lock:
                            owner.trace_counter+=1
                            trace='guard-traces/'+f'{owner.trace_counter:04d}'
                        target=owner.log.parent/(trace+'-request.json')
                        target.parent.mkdir(exist_ok=True)
                        target.write_text(json.dumps(body,ensure_ascii=False))
                    # Destination is host-owned, never accepted from the packet.
                    req = request.Request(cfg['endpoint'], data=json.dumps(body).encode(),
                        headers={'Authorization': 'Bearer ' + keys[role], 'Content-Type': 'application/json'})
                    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
                    with opener.open(req, timeout=cfg['timeout_seconds']) as response:
                        answer = response.read(2_000_001)
                        if len(answer) > 2_000_000:
                            raise ValueError('RESPONSE_TOO_LARGE')
                        status = response.status
                    value = json.loads(answer)
                    usage = value.get('usage')
                    result = {'ok': True, 'value': value}
                except error.HTTPError as exc:
                    status = exc.code
                    result = {'ok': False, 'error': f'UPSTREAM_HTTP_{exc.code}'}
                    exc.close()
                except Exception as exc:
                    result = {'ok': False, 'error': type(exc).__name__}
                if trace:
                    (owner.log.parent/(trace+'-response.json')).write_text(json.dumps(
                        {'http_status':status,**result},ensure_ascii=False))
                with owner.lock:
                    with owner.log.open('a') as f:
                        f.write(json.dumps({'role': role, 'http_status': status, 'usage': usage,
                            'latency_ms': round((time.monotonic()-tick)*1000, 2),
                            'error': result.get('error'),'guard_trace':trace}) + '\n')
                self.wfile.write(json.dumps(result).encode() + b'\n')

        self.server = socketserver.ThreadingUnixStreamServer(str(self.path), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.path.unlink(missing_ok=True)


def exchange(role, body):
    import socket
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(125)
        sock.connect('/broker/model.sock')
        sock.sendall(json.dumps({'role': role, 'body': body}).encode() + b'\n')
        with sock.makefile('rb') as f:
            raw = f.readline(2_000_001)
        if len(raw) > 2_000_000:
            raise RuntimeError('BROKER_RESPONSE_TOO_LARGE')
        reply = json.loads(raw)
        if not reply['ok']:
            raise RuntimeError(reply['error'])
        return reply['value']
