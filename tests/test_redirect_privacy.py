import pytest
from livros_baltigo.transport_audit import redirect_summary


def test_redirect_retains_hostname_only():
    data = redirect_summary('https://new.example/private/SECRET?token=HIDDEN','https://old.example/')
    assert data == {'redirect_host':'new.example', 'redirect_same_origin':False}
    assert 'SECRET' not in str(data) and 'HIDDEN' not in str(data)


def test_relative_redirect_stays_same_origin():
    assert redirect_summary('/new?q=hidden','https://old.example/') == {'redirect_host':'old.example', 'redirect_same_origin':True}


@pytest.mark.parametrize('target',['http://old.example/','https://127.0.0.1/','https://name:password@other.example/'])
def test_unsafe_redirects_are_not_logged_or_followed(target):
    assert redirect_summary(target,'https://old.example/') == {'redirect_unsafe':True}
