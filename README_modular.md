# Modular MBOX Reader

This branch adds a modular entry point while retaining the original `mbox_reader_pro.py` as a fallback.

Run `py run_mbox_reader.py` from the repository root (Python 3.10+).

Modules: `app.py` initializes the window; `ui.py` renders the interface; `archives.py` handles archives and indexing; `conversations.py` handles reply threads; `labels.py` manages labels; `attachments.py` handles attachments and HTML; `exports.py` handles PDF/EML exports; `core.py` contains parsing and database helpers.

This is a structural refactor, not yet a performance fix. Test with a small archive before using large archives. Keep private emails and caches out of Git.
