"""Measure what Apple Mail's local store occupies, folder by folder.

Read-only in the strongest sense available: this module stats files and lists
directories. It never opens a message, and it never writes anything.

Two measures are reported side by side, because they answer different
questions. *Envelope size* comes from the database and covers every message
Mail knows about, including bodies never downloaded. *Disk size* is what the
volume has actually committed. Where they disagree, that is a fact about the
account rather than a defect in the report.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from mail_triage.accounts import resolve_account_name
from mail_triage.folders import account_prefix, folder_path

MBOX_SUFFIX = ".mbox"
MAILDATA = "MailData"


@dataclass(frozen=True)
class DiskUsage:
    """What one account directory occupies, split by mailbox."""

    by_folder: dict[str, int] = field(default_factory=dict)
    loose_bytes: int = 0
    unreadable: tuple[str, ...] = ()


def folder_path_from_mbox(relative: Path) -> str:
    """Turn ``Parent.mbox/Child.mbox`` into the database's ``Parent/Child``.

    The correspondence between the two trees is exact, which is what lets the
    disk walk and the database be joined on the folder path alone.
    """
    return "/".join(
        part[: -len(MBOX_SUFFIX)] if part.endswith(MBOX_SUFFIX) else part
        for part in relative.parts
    )


def file_blocks(path: Path) -> int:
    """Bytes the volume has actually committed to ``path``.

    ``st_blocks``, not ``st_size``: a mail store is tens of thousands of small
    files, and apparent size understates real consumption badly once block
    rounding is counted. This matches what ``du`` reports, which is the figure
    someone deciding what to prune actually cares about.

    ``lstat`` rather than ``stat`` so a symlink is counted as the link it is,
    not a second copy of its target.
    """
    return path.lstat().st_blocks * 512


def _owning_mailbox(relative: Path) -> Path | None:
    """The nearest ancestor of ``relative`` that is a mailbox, if any.

    "Nearest" is the whole trick: a file inside ``Parent.mbox/Child.mbox``
    belongs to the child alone, so parents never absorb their children and
    the roll-up adds up.
    """
    for candidate in [relative, *relative.parents]:
        if candidate.name.endswith(MBOX_SUFFIX):
            return candidate
    return None


def account_disk_usage(account_dir: Path) -> DiskUsage:
    """Map each mailbox in ``account_dir`` to the bytes it alone occupies."""
    by_folder: dict[str, int] = {}
    loose = 0
    unreadable: list[str] = []
    if not account_dir.is_dir():
        return DiskUsage()

    def on_error(error: OSError) -> None:
        unreadable.append(str(getattr(error, "filename", "") or account_dir))

    for root, _dirs, files in os.walk(account_dir, onerror=on_error):
        root_path = Path(root)
        relative = root_path.relative_to(account_dir)
        owner = _owning_mailbox(relative)
        folder = folder_path_from_mbox(owner) if owner is not None else None
        for name in files:
            try:
                blocks = file_blocks(root_path / name)
            except OSError:
                unreadable.append(str(root_path / name))
                continue
            if folder is None:
                loose += blocks
            else:
                by_folder[folder] = by_folder.get(folder, 0) + blocks
    return DiskUsage(by_folder, loose, tuple(unreadable))


def maildata_usage(mail_root: Path) -> list[tuple[str, int]]:
    """Size each item in Mail's own MailData directory, largest first.

    Nothing here is mail. It is the envelope database, the search and
    junk-filter indexes, a cache of remote images from HTML messages, and
    settings. It is reported anyway because without it the accounts do not sum
    to what the store actually occupies, and a total that does not add up
    invites doubt about every other figure on the screen.
    """
    directory = mail_root / MAILDATA
    if not directory.is_dir():
        return []
    items: list[tuple[str, int]] = []
    for entry in directory.iterdir():
        try:
            if entry.is_dir():
                total = 0
                for root, _dirs, files in os.walk(entry, onerror=lambda _error: None):
                    for name in files:
                        try:
                            total += file_blocks(Path(root) / name)
                        except OSError:
                            continue
            else:
                total = file_blocks(entry)
        except OSError:
            continue
        items.append((entry.name, total))
    return sorted(items, key=lambda item: item[1], reverse=True)


@dataclass(frozen=True)
class FolderNode:
    """One mailbox, with the mailboxes nested inside it.

    Totals are derived by recursion rather than stored, so a node cannot fall
    out of agreement with its children.
    """

    path: str
    name: str
    own_disk_bytes: int | None
    envelope_bytes: int
    message_count: int
    children: tuple[FolderNode, ...] = ()

    @property
    def total_disk_bytes(self) -> int | None:
        """Own bytes plus every descendant's, or None when not stored locally.

        None is not zero. An account Mail knows about but does not keep on
        this Mac has no disk figure at all, and reporting nought would read as
        "this is empty" rather than "this is not here".
        """
        if self.own_disk_bytes is None:
            return None
        return self.own_disk_bytes + sum(
            child.total_disk_bytes or 0 for child in self.children
        )

    @property
    def total_envelope_bytes(self) -> int:
        return self.envelope_bytes + sum(
            child.total_envelope_bytes for child in self.children
        )

    @property
    def total_messages(self) -> int:
        return self.message_count + sum(child.total_messages for child in self.children)


@dataclass(frozen=True)
class AccountUsage:
    """One account's folder tree, with both measures throughout."""

    prefix: str
    name: str
    root: FolderNode
    on_disk: bool
    unreadable: tuple[str, ...] = ()


def _sort_key(node: FolderNode) -> tuple[int, int]:
    """Largest first, by disk where known and by envelope size otherwise."""
    return (node.total_disk_bytes or 0, node.total_envelope_bytes)


def _tree(
    entries: dict[str, tuple[int | None, int, int]],
    on_disk: bool,
    root_disk: int | None,
) -> FolderNode:
    """Assemble flat ``path -> (disk, envelope, count)`` entries into a tree.

    Intermediate folders absent from the input are synthesised with no figures
    of their own, so a child never loses its parent — Mail is perfectly happy
    to have a folder that holds nothing but other folders.
    """
    entries = dict(entries)
    for path in list(entries):
        parts = path.split("/")
        for depth in range(1, len(parts)):
            entries.setdefault("/".join(parts[:depth]), (0 if on_disk else None, 0, 0))

    def build(prefix: str) -> tuple[FolderNode, ...]:
        wanted_depth = len(prefix.split("/")) + 1 if prefix else 1
        nodes = [
            FolderNode(
                path=path,
                name=path.rsplit("/", 1)[-1],
                own_disk_bytes=entries[path][0],
                envelope_bytes=entries[path][1],
                message_count=entries[path][2],
                children=build(path),
            )
            for path in entries
            if path.startswith(f"{prefix}/" if prefix else "")
            and len(path.split("/")) == wanted_depth
        ]
        return tuple(sorted(nodes, key=_sort_key, reverse=True))

    return FolderNode(
        path="",
        name="",
        own_disk_bytes=root_disk,
        envelope_bytes=0,
        message_count=0,
        children=build(""),
    )


def build_account_usage(
    mailbox_sizes: list[tuple[str, int, int]],
    mail_root: Path,
    names: dict[str, str],
) -> list[AccountUsage]:
    """Join the database's per-mailbox totals to what is on disk.

    ``mailbox_sizes`` is ``EnvelopeReader.mailbox_sizes()``; ``mail_root`` is
    the ``V10`` directory; ``names`` is ``accounts.account_names()``.

    An account can appear on either side alone: one configured in Mail but
    with nothing cached locally has database rows and no directory, and a
    directory left behind by a removed account has the reverse. Both are
    reported — the second is exactly where forgotten mail hides.
    """
    from_db: dict[str, dict[str, tuple[int, int]]] = {}
    for url, count, total in mailbox_sizes:
        path = folder_path(url)
        if not path:
            continue
        from_db.setdefault(account_prefix(url), {})[path] = (count, total)

    directories = {
        entry.name.upper(): entry
        for entry in (sorted(mail_root.iterdir()) if mail_root.is_dir() else [])
        if entry.is_dir() and entry.name != MAILDATA
    }

    accounts: list[AccountUsage] = []
    for prefix, folders in sorted(from_db.items()):
        short_id = prefix.partition("://")[2].upper()
        directory = next(
            (entry for name, entry in directories.items() if name.startswith(short_id)),
            None,
        )
        usage = account_disk_usage(directory) if directory is not None else DiskUsage()
        on_disk = directory is not None
        merged: dict[str, tuple[int | None, int, int]] = {}
        canonical_by_fold = {path.casefold(): path for path in folders}
        for path, (count, total) in folders.items():
            merged[path] = (usage.by_folder.get(path) if on_disk else None, total, count)
        for path, blocks in usage.by_folder.items():
            # The database's capitalisation wins where the two agree apart
            # from case; a folder only on disk keeps its own name.
            canonical = canonical_by_fold.get(path.casefold())
            if canonical is None:
                merged[path] = (blocks, 0, 0)
            elif canonical != path:
                disk, total, count = merged[canonical]
                merged[canonical] = ((disk or 0) + blocks, total, count)
        accounts.append(
            AccountUsage(
                prefix=prefix,
                name=resolve_account_name(prefix, names),
                # Loose files sit at the root so the folders plus the root
                # equal what the account directory actually occupies.
                root=_tree(merged, on_disk, usage.loose_bytes if on_disk else None),
                on_disk=on_disk,
                unreadable=usage.unreadable,
            )
        )
    return sorted(accounts, key=lambda account: _sort_key(account.root), reverse=True)
