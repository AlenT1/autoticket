"""Shared I/O — input sources and output sinks (tracker-agnostic).

Stage 2 boundaries:
- ``sources/`` — :class:`Source` protocol + :class:`RawDocument`. Concrete
  impls: ``SingleFileSource`` (for f2j's bug-list input). The drive's
  ``GDriveSource``/``LocalFolderSource`` impls land here when drive's runner
  is rewired (step 12 of the merge plan).
- ``sinks/`` — :class:`TicketSink` protocol + ``Ticket`` shape + ``JiraSink``
  impl + plug-in strategies for identification/assignee/epic-router.
  (Built in step 9 of the merge plan.)
"""
