import http.client
import json
import threading
from contextlib import contextmanager

from agent_usage_tracker.observation_server import make_server

TOKEN = 'fixture-token-123456789'


@contextmanager
def receiver(tmp_path):
    server = make_server(tmp_path / 'o.db', TOKEN, port=0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def request(address, method, path, body=None, auth=True, headers=None):
    connection = http.client.HTTPConnection(*address, timeout=3)
    fields = {'Content-Type': 'application/json'}
    if auth:
        fields['Authorization'] = 'Bearer ' + TOKEN
    fields.update(headers or {})
    connection.request(method, path, body=body, headers=fields)
    response = connection.getresponse()
    value = response.read()
    connection.close()
    return response.status, json.loads(value)


def test_http_bounds_retry_partial_private(tmp_path):
    with receiver(tmp_path) as address:
        assert request(address, 'GET', '/health', auth=False)[0] == 200
        assert request(address, 'GET', '/v1/report', auth=False)[0] == 401
        assert request(address, 'POST', '/v1/ingest', '{}', headers={'Authorization': 'Bearer wrong'})[0] == 401
        assert request(address, 'POST', '/v1/ingest', '{')[0] == 400
        assert request(address, 'POST', '/v1/ingest', '', headers={'Content-Length': str(8*1024*1024+1)})[0] == 413
        row = dict(version=1, kind='tool', id='a', timestamp='2026-01-01T00:00:00Z', status='error')
        envelope = dict(format='jsonl', source='desktop', stream='commands', role='primary', records=[row])
        assert request(address, 'POST', '/v1/ingest', json.dumps(envelope))[1]['accepted'] == 1
        assert request(address, 'POST', '/v1/ingest', json.dumps(envelope))[1]['duplicates'] == 1
        envelope['records'] = [{**row, 'status': 'ok'}, {**row, 'id': 'b'}]
        status, receipt = request(address, 'POST', '/v1/ingest', json.dumps(envelope))
        assert status == 409 and receipt['receipt']['accepted'] == 1
        status, report = request(address, 'GET', '/v1/report')
        assert status == 200 and report['coverage']['observations'] == 2
        assert request(address, 'GET', '/v1/report?x=1')[0] == 404
        envelope['records'] = [{**row, 'id': 'c'}, {'bad': True}]
        assert request(address, 'POST', '/v1/ingest', json.dumps(envelope))[0] == 400
        assert request(address, 'GET', '/v1/report')[1]['coverage']['observations'] == 2


def test_otlp_and_rejected_framing(tmp_path):
    with receiver(tmp_path) as address:
        body = json.dumps(dict(format='otlp', source='otlp', stream='metrics', role='primary', payload={}))
        assert request(address, 'POST', '/v1/ingest', body)[0] == 200
        assert request(address, 'POST', '/v1/ingest', body, headers={'Content-Type': 'text/plain'})[0] == 415
        assert request(address, 'POST', '/v1/ingest', body, headers={'Transfer-Encoding': 'chunked'})[0] == 400
