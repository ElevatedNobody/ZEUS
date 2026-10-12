"""
python -m crudex_ops <command> - the operator command line of ZEUS.

The BAT files call it by this name. The code lives in `agni_ops`, the package's
name from before the ZEUS rename; that name stays importable and runnable
(`python -m agni_ops ...`) so scripts, shortcuts and scheduled tasks written
before the rename keep working. This package only forwards to it.
"""

from agni_ops import *  # noqa: F401,F403
