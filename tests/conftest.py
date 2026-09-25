from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from livros_baltigo.config import Settings
from livros_baltigo.models import Book, Quota, SearchPage
from livros_baltigo.storage import Store


@pytest.fixture
def settings(tmp_path):
    return Settings(bot_token="123456:" + "X" * 35, admin_ids=frozenset({123}),
                    base_url="https://catalog.example", user_id="987", user_key="secret_session_key",
                    data_dir=tmp_path, file_hosts=frozenset({"cdn.example"}))


@pytest.fixture
def book():
    return Book("42", "abc123", "Livro de demonstração", "Autor de teste", "Portuguese", "pdf", "1900",
                description="Uma descrição de teste.", size=100)


@pytest.fixture
async def store(tmp_path):
    value = Store(tmp_path / "library.sqlite3")
    await value.init()
    return value


class FakeContent:
    def __init__(self, body, chunksize=17):
        self.body, self.chunksize = body, chunksize

    async def iter_chunked(self, n):
        for index in range(0, len(self.body), min(n, self.chunksize)):
            yield self.body[index:index + min(n, self.chunksize)]


class Response:
    def __init__(self, payload=None, *, body=None, status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        if body is None:
            body = json.dumps(payload).encode()
        self.content = FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected HTTP request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.photos = []
        self.documents = []
        self.answers = []
        self.markups = []
        self.ident = 0

    async def call(self, method, values=None):
        if method == "getMe":
            return {"username": "bot_de_teste", "id": 123456, "is_bot": True}
        if method == "getWebhookInfo":
            return {"url": ""}
        return True

    async def message(self, chat, text, keyboard=None):
        self.ident += 1
        message = {"message_id": self.ident, "text": text, "chat": {"id": chat, "type": "private"}}
        self.sent.append((chat, text, keyboard))
        return message

    async def edit(self, chat, message, text, keyboard=None):
        self.edits.append((chat, message, text, keyboard))
        return {"message_id": message, "text": text, "chat": {"id": chat, "type": "private"}}

    async def markup(self, chat, message, keyboard):
        self.markups.append((chat, message, keyboard))

    async def answer(self, ident, text="", alert=False):
        self.answers.append((ident, text, alert))

    async def photo(self, chat, data, caption, keyboard):
        self.photos.append((chat, data, caption, keyboard))

    async def document(self, chat, path: Path, filename, caption):
        self.documents.append((chat, path.read_bytes(), filename, caption))
        return {"document": {"file_id": "FAKE_FILE_ID"}}


@pytest.fixture
def telegram():
    return FakeTelegram()


@pytest.fixture
def source(book):
    fake = AsyncMock()
    fake.search.return_value = SearchPage([book], 1, False)
    fake.details.return_value = book
    fake.cover.return_value = None
    fake.quota.return_value = Quota(20, 2)
    fake.file_info.return_value = ("https://cdn.example/a.pdf", "pdf")
    async def download(url, extension, path, progress):
        data = b"%PDF-1.4\n% test fixture; no copyrighted book content\n"
        path.write_bytes(data)
        await progress(len(data), len(data))
        return len(data)
    fake.download.side_effect = download
    return fake

# Laboratório adicional: tráfego externo desativado nos testes locais.
# A única exceção de rede local é loopback para os servidores de teste já existentes.
def pytest_addoption(parser):
    parser.addoption('--qa-live', action='store_true', help='Habilita uma consulta real autorizada.')
    parser.addoption('--qa-download', action='store_true', help='Habilita no máximo um arquivo autorizado.')
    parser.addoption('--qa-env', default='.env.qa')
    parser.addoption('--qa-selectors', default='qa.selectors.example.json')
    parser.addoption('--qa-output', default='relatorios')


def pytest_configure(config):
    config.addinivalue_line('markers', 'live: teste externo explicitamente autorizado, desativado por padrão')


@pytest.fixture(autouse=True)
def qa_no_external_network(request, monkeypatch):
    if request.node.get_closest_marker('live') and request.config.getoption('--qa-live'):
        return
    import ipaddress
    import socket
    real_connect, real_connect_ex, real_getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
    def allowed(address):
        if isinstance(address, (str, bytes)):  # socket Unix do event loop, sem rede externa
            return True
        try:
            return ipaddress.ip_address(address[0]).is_loopback
        except (ValueError, TypeError):
            return address[0] == 'localhost'
    def connect(sock, address):
        if not allowed(address):
            raise AssertionError('A bateria local bloqueou uma conexão externa.')
        return real_connect(sock, address)
    def connect_ex(sock, address):
        if not allowed(address):
            raise AssertionError('A bateria local bloqueou uma conexão externa.')
        return real_connect_ex(sock, address)
    def getaddrinfo(host, *args, **kwargs):
        if host not in (None, 'localhost', b'localhost'):
            try:
                if not ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host).is_loopback:
                    raise ValueError
            except ValueError:
                raise AssertionError('A bateria local bloqueou uma consulta DNS externa.') from None
        return real_getaddrinfo(host, *args, **kwargs)
    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', connect_ex)
    monkeypatch.setattr(socket, 'getaddrinfo', getaddrinfo)
