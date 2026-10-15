"""Selected keyword trend endpoint contract and provider errors."""
from uuid import uuid4

from app.services import keyword_planner_service as planner


def login(client):
    email = f"history-{uuid4().hex}@example.com"
    payload = {"first_name": "Trend", "last_name": "Test", "email": email, "password": "SecurePass123"}
    client.post('/api/auth/register', json=payload)
    assert client.post('/api/auth/login', json={"email": email, "password": payload['password']}).status_code == 200


def test_history_uses_exact_keyword_and_locale(client, monkeypatch):
    login(client)
    def history(words, *, country, language):
        assert words == ['keyword research']
        assert (country, language) == ('in', 'en')
        return [{'keyword': words[0], 'monthly': [{'year': 2026, 'month': 'JANUARY', 'searches': 120}]}]
    monkeypatch.setattr(planner, 'historical_metrics', history)
    r = client.post('/api/tools/keyword-planner/history', json={'input': ' keyword research ', 'country': 'in', 'language': 'en'})
    assert r.status_code == 200
    assert r.json()['rows'][0]['monthly'][0]['searches'] == 120


def test_history_requires_auth(client):
    assert client.post('/api/tools/keyword-planner/history', json={'input': 'test'}).status_code == 401


def test_history_missing_connection(client, monkeypatch):
    login(client)
    def missing(*args, **kwargs):
        raise planner.NotConfiguredError('missing')
    monkeypatch.setattr(planner, 'historical_metrics', missing)
    response = client.post('/api/tools/keyword-planner/history', json={'input': 'test'})
    assert response.status_code == 200
    assert response.json()['configured'] is False
    assert response.json()['rows'] == []
