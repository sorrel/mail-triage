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


def test_a_keystroke_can_only_do_what_a_click_would():
    """The keystroke presses the row's own button rather than calling choose()
    behind its back, so the two cannot drift apart. Every button the row draws
    has one, held mail included."""
    source = read("app.js")
    assert "if (KEYSTROKES[offer.action])" in source
    assert 'button.dataset.key = KEYSTROKES[offer.action];' in source
    assert 'current.querySelector(`.row-actions button[data-key="${event.key}"]`)' in source
    assert "button.click();" in source


def test_accepting_the_proposed_folder_costs_one_keystroke():
    """"f" used to open the folder box, so agreeing with a destination already
    on the screen took f then Return. Return alone now opens the box, which is
    the case where a choice is actually being made."""
    source = read("app.js")
    assert 'const KEYSTROKES = { file: "f", bin: "b", skip: "s" };' in source
    assert 'event.key === "Enter" && event.target === current' in source


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


def test_held_mail_is_acted_on_on_the_same_terms_as_anything_else():
    """Mirrors routes._permitted, which is where it is actually enforced. One
    key, no question, for filing as well as binning: the guard says what it
    noticed and keeps the mail out of an unattended run, and somebody looking
    at the row has already read the reason beside it."""
    source = read("app.js")
    held_block = source.split('veto_kind === "deletion"')[1]
    assert '{ label: "Bin", action: "bin" }, { label: "Skip", action: "skip" }' in held_block
    assert "Bin anyway" not in source
    assert "override: true," in held_block


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


def test_the_picker_sends_the_override_held_mail_needs():
    """The server refuses to file a held message without an explicit
    per-message override, so a folder typed into a held row must carry one or
    the choice comes back a 400."""
    source = read("app.js")
    assert 'const override = Boolean(proposal.veto) && proposal.veto_kind !== "deletion";' in source
    assert 'choose(proposal.id, "file", article, override, name);' in source


def test_applying_shows_each_message_leaving():
    source = read("app.js")
    assert "async function showDeparture" in source
    assert '"binning…"' in source and '"filed"' in source
    assert "data-leaving" in read("app.css")


def test_applying_asks_for_one_message_at_a_time():
    """A press of ten used to be one request: nothing on the page changed
    until the last message had moved, and only then did the rows start
    leaving. Now each message is asked for on its own, so the word on the row
    is up whilst Mail is working on it."""
    source = read("app.js")
    assert "body: JSON.stringify({ batch, decisions: [decision] })" in source
    assert "if (article) markActing(article, decision.action);" in source


def test_a_row_leaving_never_holds_up_the_next_message():
    """The departures are chained to each other so they stay in order, and
    awaited once at the end — never inside the loop, which is what made the
    animation additive with the moves rather than concurrent with them."""
    source = read("app.js")
    assert "departures = departures.then(() => showDeparture(" in source
    assert "await showDeparture" not in source
    assert "await departures;" in source


def test_a_message_that_did_not_move_stays_and_says_so():
    source = read("app.js")
    assert "function markStalled" in source
    assert '"did not move"' in source
    assert "data-stalled" in read("app.css")


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


def test_the_folder_box_is_still_a_keystroke_away_when_the_proposal_is_wrong():
    """"f" now files to the proposed folder. Return opens the box instead, and
    "f" falls through to it on a message that has nowhere to file to."""
    source = read("app.js")
    assert 'event.key === "f" || (event.key === "Enter" && event.target === current)' in source
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


def test_nothing_in_the_browser_stops_to_ask_before_filing():
    """Filing is what the page is for. The guards still hold mail back from an
    unattended run and still print their reason on the row; what they no
    longer do is put a dialog between a deliberate press and the move."""
    source = read("app.js")
    markup = read("index.html")
    assert "confirmOverride" not in source
    assert "Are you sure" not in markup
    assert 'id="confirm"' not in markup
