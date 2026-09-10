"""Make lance-py's test helpers importable: its fake embedding function and FakeChat are the no-GPU stand-ins here too.

Appended, not prepended: this directory is already first on ``sys.path`` under
pytest's default import mode, so a ``test_cli`` here shadows lance-py's and
``from test_vectors import fake`` still resolves next door.
"""

from __future__ import annotations

import sys
from pathlib import Path

LANCE_PY_TESTS = Path(__file__).resolve().parents[2] / "lance-py" / "tests"
if str(LANCE_PY_TESTS) not in sys.path:
    sys.path.append(str(LANCE_PY_TESTS))
