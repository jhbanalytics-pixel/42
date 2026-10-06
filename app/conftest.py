# Anchors pytest's import path at the repo root so tests import src.api.*.

# Win+Py3.13 SIGSEGV guard. CPython 3.13's LogRecord.__init__ sets taskName via
# asyncio.current_task() when logging.logAsyncioTasks is on; under pytest with
# asyncio loaded, that C call faults with an access violation on Windows. Any
# logger.error in a tested path (e.g. the system_events fatal marker) trips it.
# Test-only; production logging is unaffected.
import logging
import sys

if sys.platform == "win32":
    logging.logAsyncioTasks = False
