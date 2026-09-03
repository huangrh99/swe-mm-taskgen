"""Ark Responses / AIDP Chat adapters, migrated from the audited alpha-seed engines.

Credentials are loaded as literal assignments, never executed or persisted.
Full provider responses are retained separately from the parsed annotation.
"""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import time
import threading
from email.utils import parsedate_to_datetime
from report_pipeline.paths import WORKSPACE_ROOT

ROOT = WORKSPACE_ROOT
PROFILES = {
    'k3': {'protocol': 'responses', 'endpoint': 'https://ark-cn-beijing.bytedance.net/api/v3',
           'model': 'ep-20260817150115-9fx8h', 'key_name': 'ARK_API_KEY', 'key_file': 'kimi_key_env.sh'},
    'gemini': {'protocol': 'chat', 'endpoint': 'https://modelhub-gateway.invalid/api/modelhub/online/v2/crawl',
               'model': 'gemini-3.7-flash', 'api_version': '2024-03-01-preview',
               'key_name': 'AIDP_API_KEY', 'key_file': 'gemini_key_env.sh'},
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def retry_after(exc):
    value = getattr(getattr(exc, 'response', None), 'headers', {}).get('retry-after')
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def load_key(profile, key_file=None):
    name = profile['key_name']
    if os.environ.get(name):
        return os.environ[name]
    if key_file is None:
        raise ValueError('Required credential environment variable is absent; pass --key-file explicitly: ' + name)
    path = Path(key_file)
    for line in path.read_text().splitlines():
        match = re.fullmatch(r'\s*(?:export\s+)?' + re.escape(name) + r'\s*=\s*(.*?)\s*', line)
        if match:
            parts = shlex.split(match[1], comments=True)
            if len(parts) != 1 or not parts[0] or '$(' in parts[0] or '`' in parts[0]:
                raise ValueError('Credential must be a literal assignment: ' + name)
            return parts[0]
    raise ValueError('Required credential is absent: ' + name)


def data_url(path):
    from PIL import Image
    path = Path(path)
    with Image.open(path) as image:
        mime = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}.get(image.format)
        if mime is None or getattr(image, 'n_frames', 1) != 1:
            raise ValueError('Only validated static PNG/JPEG/WebP images are supported')
        image.verify()
    return 'data:' + mime + ';base64,' + base64.b64encode(path.read_bytes()).decode('ascii')


def request_body(profile, packet, images, system, max_tokens):
    text = json.dumps(packet, ensure_ascii=False)
    params = {'model': profile['model'], 'temperature': 1.0, 'top_p': 0.95}
    if profile['protocol'] == 'responses':
        content = [{'type': 'input_text', 'text': text}]
        content += [{'type': 'input_image', 'image_url': data_url(p)} for p in images]
        body = dict(params, instructions=system,
                    input=[{'role': 'user', 'content': content}])
        if max_tokens:
            body['max_output_tokens'] = max_tokens
        return body
    content = [{'type': 'text', 'text': text}]
    content += [{'type': 'image_url', 'image_url': {'url': data_url(p)}} for p in images]
    body = dict(params, messages=[{'role': 'system', 'content': system},
                                 {'role': 'user', 'content': content}], stream=False)
    if max_tokens:
        body['max_tokens'] = max_tokens
    return body


def extract_annotation(response, protocol):
    if protocol == 'responses':
        if response.get('status') != 'completed':
            raise ValueError('Provider response was not completed')
        text = ''.join(part.get('text', '') for item in response.get('output', [])
                       if item.get('type') == 'message' for part in item.get('content', [])
                       if part.get('type') == 'output_text')
    else:
        choices = response.get('choices') or []
        if not choices or choices[0].get('finish_reason') != 'stop':
            raise ValueError('Provider response is truncated, refused, or lacks a completed choice')
        text = choices[0]['message'].get('content') or ''
    # Preserve the full response separately. Only unwrap an initial reasoning block / JSON fence.
    text = re.sub(r'^\s*<(think|thinking|reasoning)>[\s\S]*?</\1>\s*', '', text, count=1, flags=re.I).strip()
    match = re.fullmatch(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
    return json.loads(match[1] if match else text)


class ApiEvaluator:
    def __init__(self, backend, model=None, key_file=None, attempts=1, min_interval=1.0,
                 max_tokens=16384, client_factory=None, sleep=time.sleep, cooldown_path=None):
        if backend not in PROFILES or not 1 <= attempts <= 3 or min_interval < 0 or max_tokens < 0:
            raise ValueError('Invalid API configuration')
        self.backend, self.profile = backend, dict(PROFILES[backend])
        if model:
            self.profile['model'] = model
        self.key_file, self.attempts, self.min_interval = key_file, attempts, min_interval
        self.max_tokens, self.client_factory, self.sleep = max_tokens, client_factory, sleep
        self.last_start = 0.0
        self.pacing_lock = threading.Lock()
        self.not_before = 0.0
        self.cooldown_path = Path(cooldown_path) if cooldown_path else None

    def persistent_cooldown(self, extend_seconds=None):
        if self.cooldown_path is None:
            return 0.0
        path = self.cooldown_path
        with path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            deadline = json.loads(path.read_text())['not_before_unix'] if path.exists() else 0.0
            if extend_seconds is not None:
                deadline = max(deadline, time.time() + extend_seconds)
                path.write_text(json.dumps({'not_before_unix': deadline}) + '\n')
            return max(0, deadline - time.time())

    def wait_for_slot(self):
        # Waiting must not hold the lock: in-flight 429 responses extend cooldown.
        while True:
            with self.pacing_lock:
                now = time.monotonic()
                persistent = self.persistent_cooldown()
                if max(persistent, self.not_before - now) > 60:
                    raise ValueError('API rate-limit cooldown; defer pending work')
                delay = max(0, persistent, self.last_start + self.min_interval - now, self.not_before - now)
                if delay <= 0:
                    self.last_start = now
                    return
            self.sleep(delay)

    def __call__(self, *, packet, image_paths, system_prompt, schema, workdir, timeout):
        directory = Path(workdir)
        # Reserve an invocation before writing payloads; interrupted work is immutable.
        with (directory / '10_invocation_started.json').open('x') as marker:
            marker.write(json.dumps({'started_at_unix': time.time()}) + '\n')
        system = Path(system_prompt).read_text() + '\nOutput JSON matching this schema:\n' + Path(schema).read_text()
        payload = request_body(self.profile, packet, image_paths, system, self.max_tokens)
        key = load_key(self.profile, self.key_file)
        def save(name, value):
            path = directory / name
            encoded = json.dumps(value, ensure_ascii=False, indent=2).replace(key, '[REDACTED]')
            path.write_text(encoded + '\n')
            return path
        request_path = save('10_api_request.json', payload)
        save('10_api_invocation.json', {'backend': self.backend, 'profile': self.profile,
            'timeout': timeout, 'attempt_limit': self.attempts, 'sdk_max_retries': 0,
            'request_sha256': digest(request_path), 'prompt_sha256': digest(system_prompt),
            'schema_sha256': digest(schema)})
        if self.client_factory:
            factory = self.client_factory
        else:
            from openai import OpenAI, AzureOpenAI
            factory = OpenAI if self.profile['protocol'] == 'responses' else AzureOpenAI
        kwargs = {'api_key': key, 'timeout': timeout, 'max_retries': 0}
        if self.profile['protocol'] == 'responses':
            kwargs['base_url'] = self.profile['endpoint']
        else:
            kwargs.update(azure_endpoint=self.profile['endpoint'], api_version=self.profile['api_version'],
                          default_headers={'X-TT-LOGID': 'swe-pr-archive-verifier'})
        client = factory(**kwargs)
        try:
            for attempt in range(1, self.attempts + 1):
                self.wait_for_slot()
                attempt_started = time.monotonic()
                try:
                    call = client.responses.create if self.profile['protocol'] == 'responses' else client.chat.completions.create
                    response = call(**payload).model_dump()
                except Exception as exc:
                    status = getattr(exc, 'status_code', None)
                    retry = status in (408, 409, 429) or isinstance(status, int) and status >= 500 or type(exc).__name__ in ('APIConnectionError', 'APITimeoutError')
                    delay = retry_after(exc)
                    if status == 429:
                        with self.pacing_lock:
                            self.not_before = max(self.not_before, time.monotonic() + (delay or 2))
                            self.persistent_cooldown(delay or 2)
                    save(f'10_attempt_{attempt:02d}.json', {'status': 'failed', 'error_type': type(exc).__name__,
                         'http_status': status, 'retryable': retry, 'retry_after_seconds': delay,
                         'elapsed_seconds': time.monotonic() - attempt_started})
                    # Do not retry earlier than the server asks, or block indefinitely.
                    if not retry or attempt == self.attempts or delay is not None and delay > 60:
                        raise ValueError('API request failed; see sanitized attempt record') from None
                    self.sleep(max(min(2 ** attempt, 10), delay or 0))
                    continue
                full = save(f'10_provider_response_{attempt:02d}.json', response)
                save(f'10_attempt_{attempt:02d}.json', {'status': 'received', 'response_sha256': digest(full),
                     'elapsed_seconds': time.monotonic() - attempt_started})
                response = json.loads(full.read_text())
                annotation = extract_annotation(response, self.profile['protocol'])
                raw = save('09_model_raw.json', annotation)
                return annotation, {'backend': self.backend, 'model': response.get('model'),
                    'requested_model': self.profile['model'], 'effort': 'provider_auto', 'attempts': attempt,
                    'usage': response.get('usage'), 'raw_response': str(raw), 'raw_response_sha256': digest(raw),
                    'provider_response': str(full), 'provider_response_sha256': digest(full),
                    'request': str(request_path), 'request_sha256': digest(request_path)}
        finally:
            client.close()
