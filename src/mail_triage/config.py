"""Configuration loading for mail-triage.

Real configuration lives in ``local/config.toml`` which is never committed.
``config.example.toml`` in the repository root documents the shape without
containing anything personal.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_EXCLUSIONS = [
    "INBOX",
    "Junk",
    "Spam",
    "Sent*",
    "Drafts*",
    "Outbox",
    "Deleted*",
    "Trash",
    "Archive",
    "Recovered Messages*",
]


@dataclass(frozen=True)
class Source:
    """One account whose inbox gets triaged.

    ``trash`` is where a "delete" answer sends a message *from this account*.
    A bin is not a filing destination, so binning never crosses accounts and
    each source needs its own name for it — Apple Mail calls the iCloud one
    "Deleted Messages" and the Gmail one "[Gmail]/Bin".

    ``ignore`` lists folder patterns that represent no filing decision in this
    account, over and above the standard set. Gmail needs it: "[Gmail]/All
    Mail" holds every message in the account and must never be counted as a
    filing. Patterns are fnmatch globs, in which "[Gmail]" is a *character
    class* matching one of G m a i l — write "[[]Gmail]*" to match the
    literal bracket. See ``folders.is_excluded``.
    """

    name: str
    prefix: str
    inbox: str = "INBOX"
    trash: str = "Deleted Messages"
    ignore: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Config:
    """Runtime settings. Thresholds are probabilities in the range 0..1."""

    account_url_prefix: str
    local_dir: Path
    inbox_folder: str = "INBOX"
    training_accounts: list[str] = field(default_factory=list)
    training_exclusions: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUSIONS))
    confidence_threshold: float = 0.7
    auto_threshold: float = 0.9
    half_life_days: float = 365.0
    correction_weight: float = 10.0
    # Task 11C: how much of a sender's recent mail must be binned before
    # filing is vetoed outright. 1.0 (the default) means "veto only when
    # nothing at all has been filed in the window" — a mixed sender who
    # still gets filed sometimes is left alone, just shown with its ratio.
    delete_veto_ratio: float = 1.0
    # Task 11C: how many days back to compare filing against deletion. Must
    # match (or sit inside) the Trash's own rolling purge window — roughly
    # two months at the time this was measured — or the comparison mixes
    # lifetime filing history with a much shorter deletion history and looks
    # falsely balanced. Both are genuinely unknown quantities the user will
    # want to tune, hence config rather than a constant in code.
    deletion_window_days: int = 75
    # Where a "delete" answer in the review loop sends a message. It is a
    # move like any other — journalled and undoable — so this must name a
    # real mailbox in the account. "Deleted Messages" is Apple Mail's name
    # for the iCloud Trash; other providers differ, hence config.
    trash_folder: str = "Deleted Messages"
    # Every account whose inbox gets triaged this run. A legacy config naming
    # only ``account_url_prefix`` synthesises exactly one of these, so there
    # is a single code path downstream rather than two.
    sources: list[Source] = field(default_factory=list)
    # The account whose folder tree *is* the filing structure. Mail from every
    # source is filed into it, crossing accounts where it must, because there
    # is one place to look for filed mail rather than one per account.
    filing_account: str = "iCloud"
    filing_account_prefix: str = ""

    def __post_init__(self) -> None:
        """Guarantee at least one source, and a filing account to go with it.

        A ``Config`` constructed directly — as tests and callers predating
        several sources do — names only ``account_url_prefix``. Synthesising
        the source here rather than in ``load_config`` means every ``Config``
        has one however it was built, so nothing downstream needs to handle
        the empty case.
        """
        if not self.sources:
            object.__setattr__(
                self,
                "sources",
                [
                    Source(
                        name=self.filing_account,
                        prefix=self.account_url_prefix,
                        inbox=self.inbox_folder,
                        trash=self.trash_folder,
                    )
                ],
            )
        if not self.filing_account_prefix:
            object.__setattr__(self, "filing_account_prefix", self.sources[0].prefix)

    def source_for(self, prefix: str) -> Source | None:
        """The source owning ``prefix``, or None if it is not being triaged."""
        for source in self.sources:
            if source.prefix == prefix:
                return source
        return None

    @property
    def training_prefixes(self) -> list[str]:
        """Accounts to learn from; by default, only the account being triaged.

        The On My Mac archive holds older mail moved off the server yearly. It
        uses the same folder names, so it can be folded in by listing its prefix
        in ``training_accounts`` — no code change needed.
        """
        return self.training_accounts or [self.account_url_prefix]

    @property
    def model_path(self) -> Path:
        return self.local_dir / "model.json"

    @property
    def corrections_path(self) -> Path:
        return self.local_dir / "corrections.jsonl"

    @property
    def rules_path(self) -> Path:
        """Hard rules answering "where does this sender's mail go?"."""
        return self.local_dir / "rules.json"

    @property
    def never_personal_path(self) -> Path:
        """Senders vouched for as never awaiting a reply.

        Separate from ``rules.json``, which is keyed by sender and answers
        "where does this go?". This answers a different question — "could a
        person be writing to me?" — and lifts only the reply guard.
        """
        return self.local_dir / "never-personal.json"

    @property
    def journal_dir(self) -> Path:
        return self.local_dir / "journal"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_config(path: Path | None = None) -> Config:
    """Load configuration, defaulting to ``local/config.toml``."""
    if path is None:
        path = _project_root() / "local" / "config.toml"
    if not path.exists():
        raise FileNotFoundError(
            f"No configuration at {path}. Copy config.example.toml to local/config.toml "
            "and run 'mail-triage accounts' to find your account prefix."
        )
    values = tomllib.loads(path.read_text())
    local_dir = Path(values.pop("local_dir", path.parent))
    raw_sources = values.pop("source", [])
    account_name = values.pop("account_name", "iCloud")
    filing_prefix = values.pop("filing_account_prefix", "")
    filing_account = values.pop("filing_account", account_name)

    if raw_sources:
        try:
            sources = [Source(**entry) for entry in raw_sources]
        except TypeError as error:
            # Nearly always the TOML table-scoping trap: every top-level key
            # written *after* a [[source]] table belongs to that table, not to
            # the document. Naming the offending keys turns a baffling
            # TypeError into an instruction.
            allowed = ", ".join(f.name for f in fields(Source))
            stray = sorted(
                {key for entry in raw_sources for key in entry}
                - {f.name for f in fields(Source)}
            )
            raise ValueError(
                f"unknown key(s) in a [[source]] table: {', '.join(stray) or error}. "
                f"A source takes only: {allowed}. Note that in TOML every "
                "top-level setting must appear *before* the first [[source]] "
                "table — anything after one is read as part of it."
            ) from error
        if not filing_prefix:
            raise ValueError(
                "config with [[source]] tables must also set filing_account_prefix, "
                "naming the account whose folders mail is filed into"
            )
    else:
        # Legacy single-account shape. ``Config.__post_init__`` synthesises the
        # source from account_url_prefix, so there is nothing to build here —
        # only the check that the file named an account at all.
        if "account_url_prefix" not in values:
            raise ValueError("config must set account_url_prefix or [[source]] tables")
        sources = []
        filing_prefix = filing_prefix or values["account_url_prefix"]

    seen: set[str] = set()
    for source in sources:
        if source.prefix in seen:
            raise ValueError(
                f"prefix {source.prefix!r} appears more than once in [[source]]; "
                "each account may only be triaged once per run"
            )
        seen.add(source.prefix)

    # ``account_url_prefix`` remains the training default for a config that
    # never named one explicitly — training history is the filing account's.
    values.setdefault("account_url_prefix", filing_prefix or sources[0].prefix)
    return Config(
        local_dir=local_dir,
        sources=sources,
        filing_account=filing_account,
        filing_account_prefix=filing_prefix,
        **values,
    )
