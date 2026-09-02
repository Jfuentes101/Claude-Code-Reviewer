"""The byline is config; the engine stays robbie."""

from pathlib import Path

from robbie import branding
from robbie.anchor import render
from robbie.config import Config, RepoConfig, SlackConfig


def cfg(tmp_path: Path, **kw) -> Config:
    return Config(
        slack=SlackConfig(owner_id="U0"),
        repos=[RepoConfig(slug="acme/app", reviewer_login="rev", bare=tmp_path)],
        state_dir=tmp_path,
        **kw,
    )


def test_default_stays_robbie(tmp_path):
    assert cfg(tmp_path).bot_name == "robbie"


def test_signature_follows_set_name():
    branding.set_name("PR Ops")
    try:
        assert branding.signature() == "🤖 **Automated pre-review by PR Ops**"
        inline = render({"severity": "nitpick", "title": "t", "body": "x"}, None)
        assert inline.endswith("<sub>🤖 automated pre-review by PR Ops</sub>")
    finally:
        branding.set_name("robbie")


def test_machine_markers_are_not_branding():
    # dedupe keys must survive any rename — they are how a re-run recognizes
    # its own prior comments
    from robbie import publish

    src = Path(publish.__file__).read_text()
    assert "robbie-review sha=" in src
