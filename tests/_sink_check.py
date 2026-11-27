"""A pytest plugin (``-p tests._sink_check``) for ``test_untested_changes.py``: every
``selector._select`` call the loaded tests make runs twice, without and with
``file_links``' sink, and the two selections must be equal. So the sink never changes what
``select_tests`` returns, over the whole selector test corpus.

The number of calls checked goes to the file in ``SIFTWISE_SINK_CHECK_COUNT``."""

import atexit
import os
from typing import Any

from siftwise.core import selector

_original = selector._select
_checked = 0


def _with_sink_too(*args: Any, **kwargs: Any) -> selector.Selection:
    global _checked
    if "sink" in kwargs or len(args) > 15:  # file_links' own call: it has a sink already
        return _original(*args, **kwargs)
    plain = _original(*args, **kwargs)
    with_sink = _original(*args, **{**kwargs, "sink": selector._LinkSink()})
    assert with_sink == plain, "the file_links sink changed a selection"
    _checked += 1
    return plain


selector._select = _with_sink_too


@atexit.register
def _report() -> None:
    path = os.environ.get("SIFTWISE_SINK_CHECK_COUNT")
    if path:
        with open(path, "w", encoding="utf-8") as out:
            out.write(str(_checked))
