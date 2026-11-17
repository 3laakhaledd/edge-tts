import asyncio
import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

TEMP = tempfile.TemporaryDirectory()
os.environ['MCP_API_KEY'] = 'test-' + 'x' * 48
os.environ['PUBLIC_BASE_URL'] = 'http://localhost'
os.environ['AUDIO_DIR'] = TEMP.name
spec = importlib.util.spec_from_file_location('railway_mcp', Path(__file__).resolve().parents[1] / 'railway_mcp.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
from starlette.testclient import TestClient

VOICES = [{'name': 'ar-SA-ZariyahNeural', 'locale': 'ar-SA', 'gender': 'Female'},
          {'name': 'en-US-AriaNeural', 'locale': 'en-US', 'gender': 'Female'}]

class FakeSpeech:
    def __init__(self, *args, **kwargs):
        pass
    async def stream(self):
        yield {'type': 'audio', 'data': b'ID3mock-mp3'}

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app, base_url='http://localhost')
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        TEMP.cleanup()

    def test_controls(self):
        server.controls('+0%', '-100Hz', '+100%')
        for values in [('fast', '+0Hz', '+0%'), ('+101%', '+0Hz', '+0%')]:
            with self.assertRaises(ValueError):
                server.controls(*values)

    def test_signature(self):
        self.assertNotEqual(server.signature('a'*32, 100), server.signature('b'*32, 100))
        self.assertNotEqual(server.signature('a'*32, 100), server.signature('a'*32, 101))

    def test_auth_protocol_and_tool_invocation(self):
        client = self.client
        self.assertEqual(client.get('/health').status_code, 200)
        self.assertEqual(client.post('/mcp', json={}).status_code, 401)
        self.assertEqual(client.post('/mcp/', json={}).status_code, 401)
        headers = {'Authorization': 'Bearer ' + os.environ['MCP_API_KEY'],
                   'Accept': 'application/json, text/event-stream'}
        response = client.post('/mcp', headers=headers, json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
            'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
                       'clientInfo': {'name': 'test', 'version': '1'}}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('result', response.json())
        response = client.post('/mcp', headers=headers, json={
            'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual({t['name'] for t in response.json()['result']['tools']},
                         {'list_voices', 'generate_speech'})
        with patch.object(server, 'voices', AsyncMock(return_value=VOICES)), patch.object(server.edge_tts, 'Communicate', FakeSpeech):
            response = client.post('/mcp', headers=headers, json={
                'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                'params': {'name': 'generate_speech', 'arguments': {'text': 'Hello'}}})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()['result'].get('isError', False), response.text)
        expiry = int(time.time()) - 1
        file_id = 'a'*32
        token = server.signature(file_id, expiry)
        self.assertEqual(client.get(f'/audio/{file_id}.mp3?expires={expiry}&token={token}').status_code, 403)

    def test_download_and_tampering(self):
        client = self.client
        with patch.object(server, 'voices', AsyncMock(return_value=VOICES)), patch.object(server.edge_tts, 'Communicate', FakeSpeech):
            result = client.portal.call(server.generate_speech, 'مرحبا')
        self.assertEqual(result['voice'], 'ar-SA-ZariyahNeural')
        response = client.get(result['download_url'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'ID3mock-mp3')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(client.get(result['download_url'] + 'x').status_code, 403)

    def test_validation(self):
        async def checks():
            for text in ('', 'x'*20001):
                with self.assertRaises(ValueError):
                    await server.generate_speech(text)
            with patch.object(server, 'voices', AsyncMock(return_value=VOICES)):
                with self.assertRaises(ValueError):
                    await server.generate_speech('Hello', voice='invalid')
                self.assertEqual((await server.list_voices('ar'))['count'], 1)
        self.client.portal.call(checks)

    def test_failure_cleanup(self):
        class Broken(FakeSpeech):
            async def stream(self):
                yield {'type': 'audio', 'data': b'partial'}
                raise RuntimeError('upstream failed')
        with patch.object(server, 'voices', AsyncMock(return_value=VOICES)), patch.object(server.edge_tts, 'Communicate', Broken):
            with self.assertRaises(ValueError):
                self.client.portal.call(server.generate_speech, 'Hello')
        self.assertEqual(list(Path(TEMP.name).glob('*.part')), [])

if __name__ == '__main__':
    unittest.main()
