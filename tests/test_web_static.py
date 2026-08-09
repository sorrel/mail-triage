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


def test_the_page_offers_no_form_given_its_own_csp_forbids_form_action():
    """form-action 'none' is set on every response. A <form> here would be a
    bet on how each browser scopes that directive."""
    assert "<form" not in read("index.html")


def test_the_override_confirmation_defaults_to_leaving_the_mail_alone():
    """Escape, the backdrop and the autofocused button must all mean "no"."""
    markup = read("index.html")
    source = read("app.js")
    assert 'id="confirm-no" autofocus' in markup
    assert 'dialog.returnValue = "no"' in source
    assert 'dialog.returnValue === "yes"' in source


def test_a_keystroke_can_only_do_what_a_click_would():
    """The keystroke presses the row's own button rather than calling choose()
    behind its back, so the two cannot drift apart — and a button that stops
    to ask is given no key at all. Blanket-refusing every held row was the
    earlier rule, and it refused "b" on mail whose Bin button asks nothing."""
    source = read("app.js")
    assert "if (!offer.confirm && KEYSTROKES[offer.action])" in source
    assert 'button.dataset.key = KEYSTROKES[offer.action];' in source
    assert 'current.querySelector(`.row-actions button[data-key="${event.key}"]`)' in source
    assert "button.click();" in source


def test_filing_has_no_single_keystroke_of_its_own():
    """"f" opens the folder box: a choice, not an action."""
    assert 'const KEYSTROKES = { bin: "b", skip: "s" };' in read("app.js")


def test_a_freshly_drawn_list_puts_you_on_a_message():
    """Every row shortcut acts on the message you are on. With focus left on
    <body> — which is where rebuilding the list puts it — they all silently
    did nothing whilst looking exactly as though they should work."""
    source = read("app.js")
    assert "function focusFirstRow()" in source
    assert "focusFirstRow();" in source
    # Never taken from whatever already holds it: the folder box is often
    # focused when a reload lands.
    assert "if (active && active !== document.body && active.isConnected) return;" in source


def test_the_message_you_are_on_is_visible_when_clicked_as_well_as_tabbed_to():
    """:focus-visible alone styles nothing after a mouse click, so the next
    keystroke looks ignored when it went exactly where it should have."""
    styles = read("app.css")
    assert ".row:focus," in styles
    assert ".row:focus-within { box-shadow: inset 3px 0 0 var(--file); }" in styles


def test_a_bill_or_a_may_need_a_reply_is_never_offered_the_bin():
    """Mirrors routes._permitted, which is where it is actually enforced."""
    source = read("app.js")
    attention_block = source.split('veto_kind === "deletion"')[1]
    # After the deletion branch, the only action offered is filing.
    assert 'action: "file"' in attention_block
    assert 'action: "bin"' not in attention_block.split("return [")[-1]


def test_file_is_not_offered_when_there_is_nowhere_to_file_to():
    """An unplaced message has no folder, so filing it would move nothing and
    report nothing — a silent no-op, which is the failure mode this project
    least tolerates."""
    source = read("app.js")
    assert 'proposal.folder ? [{ label: "File", action: "file" }] : []' in source


def test_every_filable_message_gets_a_folder_box():
    """Not only the ones with nowhere to go. A bill whose predicted folder is
    not in the filing account needs it, and so does anything you simply want
    filed somewhere else."""
    source = read("app.js")
    assert "function picker(" in source
    assert 'if (proposal.action !== "delete") {' in source
    assert 'api("/api/folders")' in source


def test_the_picker_still_asks_before_filing_a_bill():
    """Choosing a folder must not become a way around the confirmation."""
    source = read("app.js")
    assert "confirmationFor(proposal)" in source
    assert "if (question && !(await confirmOverride(question)))" in source


def test_applying_shows_each_message_leaving():
    source = read("app.js")
    assert "async function showDeparture" in source
    assert '"binning…"' in source and '"filed"' in source
    assert "data-leaving" in read("app.css")


def test_the_departure_animation_respects_reduced_motion():
    source = read("app.js")
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in source
    assert "if (REDUCED_MOTION.matches) return Promise.resolve();" in source


def test_the_folder_box_is_a_combobox_not_a_dropdown():
    """Typed, fuzzy-matched, and keyboard-driven end to end."""
    source = read("app.js")
    assert 'input.setAttribute("role", "combobox")' in source
    assert "rankFolders(input.value.trim(), folders, expected)" in source
    assert "<select" not in source


def test_the_expected_folder_leads_so_return_accepts_it():
    source = read("app.js")
    assert "const expected = proposal.folder || proposal.held_folder || null;" in source


def test_f_opens_the_folder_box_rather_than_filing_blind():
    source = read("app.js")
    assert 'if (current && event.key === "f")' in source
    assert "input.focus();" in source


def test_typing_in_the_box_does_not_reach_the_pages_own_shortcuts():
    """Inside the box, j and k and f are letters somebody is typing."""
    assert "event.stopPropagation();" in read("app.js")


def test_the_matcher_ships_as_its_own_file():
    assert (STATIC / "fuzzy.js").is_file()
    assert '<script src="/fuzzy.js" defer></script>' in read("index.html")


def test_filed_rows_leave_slowly_enough_to_be_watched():
    styles = read("app.css")
    assert "opacity 520ms" in styles
    assert "height 420ms" in styles


def test_arrows_move_between_messages_as_well_as_j_and_k():
    """The habit still works; the arrows mean nobody has to know it exists."""
    source = read("app.js")
    assert 'event.key === "ArrowDown" || (!typing && event.key === "j")' in source
    assert 'event.key === "ArrowUp" || (!typing && event.key === "k")' in source


def test_left_and_right_move_along_the_controls_of_a_message():
    source = read("app.js")
    assert 'event.key === "ArrowRight" || event.key === "ArrowLeft"' in source
    assert "function moveAlong(row, step)" in source


def test_going_off_the_left_hand_end_returns_to_the_message():
    """Otherwise focus is stranded among the buttons with no way back to the
    up/down navigation."""
    assert "if (next < 0) row.focus();" in read("app.js")


def test_arrow_navigation_does_not_also_scroll_the_page():
    source = read("app.js")
    assert source.count("event.preventDefault();") >= 4


def test_apply_can_be_pressed_from_the_keyboard_and_says_so():
    """Reachable mid-word in the folder box, which is where you are when you
    have just finished choosing."""
    source = read("app.js")
    markup = read("index.html")
    assert 'event.key === "Enter" && (event.metaKey || event.ctrlKey)' in source
    assert "<kbd>⌘↵</kbd>" in markup


def test_the_shortcuts_are_written_down_on_the_page():
    markup = read("index.html")
    assert 'class="legend"' in markup
    for key in ("<kbd>f</kbd>", "<kbd>b</kbd>", "<kbd>s</kbd>", "<kbd>→</kbd>"):
        assert key in markup, key


def test_the_confirmation_can_be_answered_without_a_mouse():
    """The page's own shortcuts stop at an open dialog, which left this box
    with nothing but Tab and Escape — an interruption to a keyboard flow
    rather than part of one."""
    source = read("app.js")
    markup = read("index.html")
    assert 'getElementById("confirm").addEventListener("keydown"' in source
    assert '["arrowleft", "arrowright", "arrowup", "arrowdown"].includes(key)' in source
    assert 'key === "l" || key === "n"' in source
    assert 'key === "f" || key === "y"' in source
    # And the letters are shown, not folklore.
    assert "Leave it <kbd>L</kbd>" in markup
    assert "File it <kbd>F</kbd>" in markup


def test_the_confirmation_arrows_land_on_the_safe_answer_first():
    """From nowhere in particular, leftmost is "Leave it"."""
    assert "const next = at < 0 ? 0 :" in read("app.js")
