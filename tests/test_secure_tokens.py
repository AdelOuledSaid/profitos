import re
from profitos.runtime import gen_token, token_digest


def test_token_digest_is_not_plaintext():
    token = gen_token()
    digest = token_digest(token)
    assert digest != token
    assert len(digest) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_token_digest_is_deterministic():
    assert token_digest("abc") == token_digest("abc")
    assert token_digest("abc") != token_digest("abcd")


def test_generated_tokens_are_distinct():
    assert gen_token() != gen_token()
