"""The `aws login` credential path: `--profile` plumbing and the CRT-missing advisory (issue #1).

None of this reaches AWS. `main()` is replaced with a stub that raises the exception botocore
would, so the tests pin what `cli()` does with it: the exit code, and that the message names the
profile in play and the HTTPS mirror where one exists.
"""

import sys

import pytest
from botocore.exceptions import LoginTokenLoadError, MissingDependencyException

import warc2zip
from warc2zip import cli, format_login_provider_error

CC_INPUT = "s3://commoncrawl/crawl-data/CC-MAIN-2026-21/segments/1/warc/x.warc.gz"
LOGIN_ERROR = MissingDependencyException(
    msg='Using the login credential provider requires an additional dependency. You will need to pip install "botocore[crt]"'
)


def raising_main(error):
    def fake_main(*args, **kwargs):
        raise error

    return fake_main


def test_cli_explains_a_login_profile_without_crt(monkeypatch, capsys):
    monkeypatch.setattr(warc2zip, "main", raising_main(LOGIN_ERROR))
    monkeypatch.setattr(sys, "argv", ["warc2zip", CC_INPUT, "--profile", "cc-login"])
    with pytest.raises(SystemExit) as exc:
        cli()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "profile 'cc-login' uses the `aws login` credential provider" in err
    assert "botocore[crt]" in err
    assert "https://data.commoncrawl.org/crawl-data/CC-MAIN-2026-21/segments/1/warc/x.warc.gz" in err
    assert "Underlying error: Missing Dependency: Using the login" in err


def test_cli_reports_an_expired_login_session(monkeypatch, capsys):
    error = LoginTokenLoadError(error_msg="Token has expired, run `aws login` again")
    monkeypatch.setattr(warc2zip, "main", raising_main(error))
    monkeypatch.setattr(sys, "argv", ["warc2zip", CC_INPUT])
    with pytest.raises(SystemExit) as exc:
        cli()
    assert exc.value.code == 1
    assert "Error: Error loading login session token: Token has expired" in capsys.readouterr().err


def test_profile_is_passed_to_main_for_s3_only(monkeypatch, capsys):
    seen = {}

    def fake_main(input_file, output_path=None, **kwargs):
        seen.update(kwargs, input_file=input_file)
        return 0

    monkeypatch.setattr(warc2zip, "main", fake_main)
    monkeypatch.setattr(sys, "argv", ["warc2zip", CC_INPUT, "--profile", "cc"])
    assert cli() == 0
    assert seen["input_file"] == CC_INPUT and seen["profile"] == "cc"

    monkeypatch.setattr(sys, "argv", ["warc2zip", "local.warc.gz", "--profile", "cc"])
    with pytest.raises(SystemExit) as exc:
        cli()
    assert exc.value.code == 2  # argparse usage error
    assert "--profile is only valid for s3:// inputs" in capsys.readouterr().err


def test_message_names_the_profile_from_env_or_default(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    assert "profile 'default'" in format_login_provider_error(CC_INPUT, None, LOGIN_ERROR)
    monkeypatch.setenv("AWS_PROFILE", "from-env")
    assert "profile 'from-env'" in format_login_provider_error(CC_INPUT, None, LOGIN_ERROR)
    assert "profile 'explicit'" in format_login_provider_error(CC_INPUT, "explicit", LOGIN_ERROR)


def test_https_mirror_is_only_suggested_for_commoncrawl():
    other_bucket = format_login_provider_error("s3://my-bucket/x.warc.gz", None, LOGIN_ERROR)
    assert "data.commoncrawl.org" not in other_bucket
    assert "Alternatively" in other_bucket  # switching credential source still helps


def test_non_login_missing_dependency_gets_the_generic_message():
    error = MissingDependencyException(msg="Using S3 Express requires an additional dependency")
    message = format_login_provider_error(CC_INPUT, "cc", error)
    assert message.startswith("Error: this AWS operation requires awscrt.")
    assert "aws login" not in message and "Alternatively" not in message
    assert "botocore[crt]" in message
