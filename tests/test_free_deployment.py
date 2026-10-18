"""Free-hosting regressions; only synthetic credentials and provider-free checks."""

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway import serve
from gateway.migrate import migrate
from gateway.pricing import PriceBook
from gateway.redis_store import RedisPolicy, RedisStore
from gateway.telemetry import PostgresTelemetryRepository


DATABASE = 'postgresql://owner:fixture-password@ep-fixture.us-west-2.aws.neon.tech/gateway?sslmode=verify-full'
REDIS = 'rediss://default:fixture-password@fixture.upstash.io:6379/0?ssl_cert_reqs=required&ssl_check_hostname=true'


def configure_startup(monkeypatch):
    monkeypatch.setenv('GATEWAY_ENV', 'production')
    monkeypatch.setenv('GATEWAY_DATASTORE_NETWORK', 'tls')
    monkeypatch.setenv('GATEWAY_RUN_MIGRATIONS', 'true')
    monkeypatch.setenv('DATABASE_URL', DATABASE)
    monkeypatch.setenv('REDIS_URL', REDIS)


def test_startup_migrates_before_serving(monkeypatch):
    configure_startup(monkeypatch)
    calls = []
    monkeypatch.setattr(serve, 'migrate', lambda dsn: calls.append(('migrate', dsn)))
    monkeypatch.setattr(serve.uvicorn, 'run', lambda *args, **kwargs: calls.append(('serve', kwargs)))
    serve.main()
    assert [call[0] for call in calls] == ['migrate', 'serve']
    assert calls[0][1] == DATABASE


def test_local_startup_does_not_migrate_without_opt_in(monkeypatch):
    monkeypatch.delenv('GATEWAY_RUN_MIGRATIONS', raising=False)
    monkeypatch.setattr(serve, 'migrate', lambda dsn: pytest.fail('Compose owns local migrations'))
    calls = []
    monkeypatch.setattr(serve.uvicorn, 'run', lambda *args, **kwargs: calls.append(True))
    serve.main()
    assert calls == [True]


def test_startup_migration_failure_never_serves_or_echoes_secrets(monkeypatch, capsys):
    configure_startup(monkeypatch)
    def fail(dsn):
        raise RuntimeError('fixture-password must not appear in logs')
    monkeypatch.setattr(serve, 'migrate', fail)
    monkeypatch.setattr(serve.uvicorn, 'run', lambda *args, **kwargs: pytest.fail('Must not serve after migration failure'))
    with pytest.raises(SystemExit) as exc:
        serve.main()
    assert exc.value.code == 1
    output = capsys.readouterr().err
    assert 'RuntimeError' in output and 'fixture-password' not in output


@pytest.mark.parametrize('name,value', [
    ('DATABASE_URL', DATABASE.replace('verify-full', 'require')),
    ('REDIS_URL', REDIS.replace('rediss://', 'redis://')),
    ('REDIS_URL', 'https://fixture.upstash.io'),
    ('REDIS_URL', REDIS.replace('ssl_check_hostname=true', 'ssl_check_hostname=false')),
])
def test_startup_rejects_insecure_or_rest_urls_before_migrating(monkeypatch, name, value):
    configure_startup(monkeypatch)
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(serve, 'migrate', lambda dsn: pytest.fail('Must validate before connecting'))
    monkeypatch.setattr(serve.uvicorn, 'run', lambda *args, **kwargs: pytest.fail('Must not serve invalid configuration'))
    with pytest.raises(SystemExit):
        serve.main()


def test_neon_pooler_is_rejected_before_session_lock_connection(monkeypatch):
    monkeypatch.setattr('gateway.migrate.psycopg.connect', lambda *args, **kwargs: pytest.fail('Must use direct endpoint'))
    with pytest.raises(ValueError, match='direct Neon endpoint'):
        migrate(DATABASE.replace('ep-fixture.', 'ep-fixture-pooler.'))


def test_upstash_native_url_builds_verified_tls_client(monkeypatch):
    monkeypatch.setenv('REDIS_URL', REDIS)
    store = RedisStore.from_env()
    from redis.asyncio.connection import SSLConnection
    assert store.client.connection_pool.connection_class is SSLConnection
    options = store.client.connection_pool.connection_kwargs
    assert options['ssl_check_hostname'] is True
    assert options['ssl_cert_reqs'] == 'required'
    assert options['host'] == 'fixture.upstash.io'


def test_routine_health_never_touches_dependencies_but_readiness_does(monkeypatch):
    calls = {'redis': 0, 'postgres': 0}
    class Client:
        async def ping(self):
            calls['redis'] += 1
            return True
        async def aclose(self):
            pass
    repository = PostgresTelemetryRepository('postgresql://fixture:fixture@127.0.0.1:1/gateway')
    async def ready():
        calls['postgres'] += 1
        return True
    monkeypatch.setattr(repository, 'ready', ready)
    app = create_app(gateway_api_key='liveness-fixture', providers={}, model_aliases={},
                     price_book=PriceBook('fixture', {}), telemetry_repository=repository,
                     redis_store=RedisStore(Client(), RedisPolicy()))
    with TestClient(app) as client:
        for _ in range(30):
            assert client.get('/health').json() == {'status': 'ok'}
        assert calls == {'redis': 0, 'postgres': 0}
        assert client.get('/readyz').json() == {'status': 'ready'}
        assert calls == {'redis': 1, 'postgres': 1}
        assert client.get('/health').status_code == 200
        assert calls == {'redis': 1, 'postgres': 1}
