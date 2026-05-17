"""Module entry: enables ``python -m ntasker`` -- used by ``serve --detach``
to spawn a backgrounded child without depending on the ``ntasker`` script
being on PATH (e.g. inside isolated `uv tool run` environments).
"""

from __future__ import annotations

from ntasker.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
