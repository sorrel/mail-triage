"""The properties of the page that must never regress.

Asserted against the shipped files rather than through a browser: each one
is a security property a careless edit would remove silently, and none of
them would show up as a broken page.
"""

from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "mail_triage" / "web" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text()


def test_the_page_exists_with_its_stylesheet_and_script():
    for name in ("index.html", "app.css", "app.js"):
        assert (STATIC / name).is_file(), name


def test_the_iframe_is_sandboxed_without_allow_same_origin():
    """allow-scripts plus allow-same-origin lets a framed page remove its own
    sandbox. The two must never appear together."""
    markup = read("index.html")
    assert 'sandbox="allow-scripts allow-forms"' in markup
    assert "allow-same-origin" not in markup
    assert "allow-top-navigation" not in markup
    # Popups buy nothing for an unsubscribe form and let sender-controlled
    # code open windows over the interface.
    assert "allow-popups" not in markup


def test_the_iframe_sends_no_referrer():
    """Otherwise the opening URL — which carries the token — reaches the
    sender in a Referer header."""
    assert 'referrerpolicy="no-referrer"' in read("index.html")


def test_the_tab_link_cannot_be_used_as_a_window_opener():
    assert 'rel="noopener noreferrer"' in read("index.html")


def test_the_page_has_no_inline_script_or_style():
    """The CSP forbids both, so an inline handler would silently stop working."""
    markup = read("index.html")
    assert "<script>" not in markup
    assert "<style>" not in markup
    assert "onclick=" not in markup
    assert "onload=" not in markup


def test_the_script_never_assigns_markup():
    """Sender-supplied subjects reach the DOM. As text, always."""
    source = read("app.js")
    # The property access, not the bare word: the module's own comment names
    # innerHTML to explain why it is never used, and that must stay sayable.
    assert ".innerHTML" not in source
    assert ".outerHTML" not in source
    assert ".insertAdjacentHTML" not in source
    assert "document.write" not in source


def test_the_script_checks_a_target_is_https_before_using_it():
    """An anchor href is covered by no content policy, and the value comes
    from a header the sender wrote."""
    source = read("app.js")
    assert 'url.protocol === "https:"' in source
    assert "httpsTarget(candidate.target)" in source


def test_the_iframe_is_reset_when_the_dialog_closes_not_when_a_button_is_clicked():
    """Escape closes a dialog without the button. Without this the sender's
    page keeps running, invisibly."""
    source = read("app.js")
    assert 'getElementById("frame").addEventListener("close"' in source
    assert 'src = "about:blank"' in source


def test_the_token_placeholder_is_present_and_holds_no_real_secret():
    markup = read("index.html")
    assert 'name="triage-token" content="__TOKEN__"' in markup


def test_no_external_resource_is_referenced():
    """No web fonts, no CDN: the page makes no request off this machine
    except the unsubscribe iframe the user opens deliberately."""
    for name in ("index.html", "app.css", "app.js"):
        text = read(name)
        assert "http://" not in text, name
        # The only https in the sources is the scheme test in app.js.
        assert "https://" not in text, name


def test_the_stylesheet_carries_a_dark_mode_and_respects_reduced_motion():
    styles = read("app.css")
    assert "prefers-color-scheme: dark" in styles
    assert "prefers-reduced-motion" in styles


def test_the_chosen_state_is_told_in_words_not_only_colour():
    """The spec: no meaning is carried by colour alone."""
    source = read("app.js")
    assert '"will file"' in source
    assert '"will bin"' in source
