from mail_triage.config import Config
from mail_triage.corpus import build_corpus, normalise_sender, recency_weight, sender_domain
from mail_triage.envelope import MessageRow

NOW = 1_700_000_000
DAY = 86_400


def make_config(tmp_path, **overrides):
    values = dict(account_url_prefix="imap://AAAAAAAA", local_dir=tmp_path)
    values.update(overrides)
    return Config(**values)


def row(mailbox_url, date_sent=NOW, sender="a@example.com", subject="Hello"):
    return MessageRow(rowid=1, sender=sender, subject=subject,
                      date_sent=date_sent, mailbox_url=mailbox_url, read=True)


def test_sender_domain_extraction():
    assert sender_domain("Orders <orders@Shop.Example>") == "shop.example"
    assert sender_domain("plain@example.com") == "example.com"
    assert sender_domain("not-an-address") == ""


def test_sender_domain_with_plus_tag():
    assert sender_domain("user+tag@example.com") == "example.com"


def test_sender_domain_with_non_ascii_address():
    assert sender_domain("Nöme <ünïcödé@exämple.example>") == "exämple.example"


def test_normalise_sender_strips_display_name():
    assert normalise_sender("Orders <orders@Shop.Example>") == "orders@shop.example"


def test_normalise_sender_keeps_bare_address():
    assert normalise_sender("plain@example.com") == "plain@example.com"


def test_normalise_sender_casefolds_uppercase_address():
    assert normalise_sender("PLAIN@EXAMPLE.COM") == "plain@example.com"


def test_normalise_sender_returns_empty_for_malformed_input():
    assert normalise_sender("not-an-address") == ""


def test_normalise_sender_with_plus_tag():
    assert normalise_sender("user+tag@example.com") == "user+tag@example.com"


def test_normalise_sender_with_non_ascii_address():
    assert normalise_sender("Nöme <ünïcödé@exämple.example>") == "ünïcödé@exämple.example"


def test_recency_weight_is_one_today():
    assert recency_weight(NOW, NOW, 365.0) == 1.0


def test_recency_weight_halves_after_one_half_life():
    weight = recency_weight(NOW - 365 * DAY, NOW, 365.0)
    assert 0.49 < weight < 0.51


def test_recency_weight_decays_further_with_age():
    old = recency_weight(NOW - 730 * DAY, NOW, 365.0)
    recent = recency_weight(NOW - 30 * DAY, NOW, 365.0)
    assert old < recent < 1.0


def test_excluded_folders_are_dropped(tmp_path):
    config = make_config(tmp_path, training_exclusions=["INBOX", "Deleted*"])
    rows = [row("imap://AAAAAAAA/INBOX"), row("imap://AAAAAAAA/Orders")]
    corpus = build_corpus(rows, config, now=NOW)
    assert [example.folder for example in corpus] == ["orders"]


def test_only_training_accounts_are_learnt_from(tmp_path):
    config = make_config(tmp_path)  # training_accounts empty → iCloud only
    rows = [row("imap://AAAAAAAA/Orders"), row("local://BBBBBBBB/Archive Stuff")]
    corpus = build_corpus(rows, config, now=NOW)
    assert [example.folder for example in corpus] == ["orders"]


def test_folders_pool_when_a_second_account_is_enabled(tmp_path):
    config = make_config(
        tmp_path, training_accounts=["imap://AAAAAAAA", "local://BBBBBBBB"]
    )
    rows = [row("imap://AAAAAAAA/Orders"), row("local://BBBBBBBB/orders")]
    corpus = build_corpus(rows, config, now=NOW)
    assert {example.folder for example in corpus} == {"orders"}
    assert len(corpus) == 2


def test_undated_messages_are_dropped(tmp_path):
    config = make_config(tmp_path)
    corpus = build_corpus([row("imap://AAAAAAAA/Orders", date_sent=0)], config, now=NOW)
    assert corpus == []


def test_unparseable_sender_is_dropped(tmp_path):
    config = make_config(tmp_path)
    rows = [row("imap://AAAAAAAA/Orders", sender="not-an-address")]
    corpus = build_corpus(rows, config, now=NOW)
    assert corpus == []


def test_empty_sender_is_dropped(tmp_path):
    config = make_config(tmp_path)
    rows = [row("imap://AAAAAAAA/Orders", sender="")]
    corpus = build_corpus(rows, config, now=NOW)
    assert corpus == []


def test_training_example_sender_is_normalised(tmp_path):
    config = make_config(tmp_path)
    rows = [row("imap://AAAAAAAA/Orders", sender="Orders <ORDERS@Shop.Example>")]
    corpus = build_corpus(rows, config, now=NOW)
    assert corpus[0].sender == "orders@shop.example"


def test_year_is_derived_from_date_sent(tmp_path):
    config = make_config(tmp_path)
    # 1_700_000_000 is 2023-11-14T22:13:20Z (gmtime), so the training example
    # should carry year 2023.
    corpus = build_corpus([row("imap://AAAAAAAA/Orders", date_sent=NOW)], config, now=NOW)
    assert corpus[0].year == 2023
