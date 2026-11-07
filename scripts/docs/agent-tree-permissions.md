# agent-tree-permissions

`agent-tree-permissions` repairs group permissions in a **fully quiesced** tree.
Every writer that can modify the tree must be stopped by its owner before the
tool starts and must remain stopped until it exits. The tool never stops a
service itself.

```bash
agent-tree-permissions /srv/tree --user owner --user worker --group shared
```

The command starts as root so it can open entries despite their old modes and
create one child per named user. Each child irreversibly drops to its assigned
UID, supplementary groups, and GID before changing anything. The root parent
only traverses directories and opens descriptors; it never changes ownership or
mode.

Preflight rejects the whole tree without mutation if it finds a symlink,
special file, nested mount (including a mount listed only in mountinfo),
unexpected owner, or regular file with more than one hard link. These checks
prevent metadata changes from escaping the selected tree.

Traversal opens every path component relative to a directory descriptor with
`O_NOFOLLOW`. Before each change, the parent reopens the entry relative to the
root descriptor and verifies its device, inode, type, mode, owner, group, and
link count against preflight. It passes that descriptor over a Unix socket to
the dropped child. The child repeats the checks and uses only `fchown` and
`fchmod`; it never receives a pathname to mutate. Renaming a parent or replacing
an intermediate or final component with a symlink therefore cannot redirect a
change outside the tree.

Directories gain group read/write/traverse and setgid. Regular files gain group
read/write, plus group execute only when some execute bit was already present.
Other existing ordinary mode bits and directory sticky bits are preserved;
setuid/setgid bits on regular files are not restored after `fchown`.

The normal invocation is the console entry point. Privileged deployment uses a
root-owned installed copy of this stdlib-only module, not an agent-writable
checkout or virtualenv.
