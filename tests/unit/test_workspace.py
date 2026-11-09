import pytest
from blackbox.tools.workspace import local_path
from blackbox.storage.files import clean_text, clean_value


@pytest.mark.parametrize('path',['../.ssh/id_rsa','/etc/shadow','note/../../file','.env','.ssh/config','cookies.sqlite','passwords.txt'])
def test_denied_path(tmp_path,path):
    with pytest.raises(ValueError):
        local_path(tmp_path,path)


def test_symlink_escape(tmp_path):
    (tmp_path/'link').symlink_to('/etc')
    with pytest.raises(ValueError):
        local_path(tmp_path,'link/hostname')


def test_redaction():
    value=clean_text('\x1b[31mtest\x00 password=abc token-unrelated\n-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n-----END OPENSSH PRIVATE KEY-----')
    assert '\x1b' not in value and 'abc' not in value and 'OPENSSH' not in value
    assert clean_value({'api_key':'abc', 'stdout':'password=abc'}) == {'api_key':'[REDACTED]', 'stdout':'password=[REDACTED]'}


def test_quoted_json_and_bearer_are_redacted():
    output=clean_text('{"password": "do-not-log", "api_key":"also-private"} Bearer aaa.bbb.ccc')
    assert 'do-not-log' not in output and 'also-private' not in output and 'aaa.bbb.ccc' not in output
