"""Streaming citation validation: only [cN] markers that point at a context chunk survive (AC5)."""

import re

MARKER = re.compile(r"\[c(\d{1,4})\]")
# A marker may be split across deltas ("[c" + "1]"): hold back a trailing partial until it resolves.
_PARTIAL = re.compile(r"\[(c\d{0,4})?$")


class CitationFilter:
    def __init__(self, n_sources: int) -> None:
        self.n_sources = n_sources
        self.cited: list[int] = []  # valid marker numbers, in first-seen order
        self.dropped: list[str] = []  # markers stripped because they matched no source
        self._buf = ""

    def feed(self, text: str) -> tuple[str, list[int]]:
        """Returns (text safe to emit now, newly cited source numbers)."""
        self._buf += text
        partial = _PARTIAL.search(self._buf)
        cut = partial.start() if partial else len(self._buf)
        ready, self._buf = self._buf[:cut], self._buf[cut:]
        return self._clean(ready)

    def flush(self) -> tuple[str, list[int]]:
        ready, self._buf = self._buf, ""
        return self._clean(ready)

    def _clean(self, text: str) -> tuple[str, list[int]]:
        new: list[int] = []

        def check(m: re.Match[str]) -> str:
            n = int(m.group(1))
            if not 1 <= n <= self.n_sources:
                self.dropped.append(m.group(0))
                return ""
            if n not in self.cited:
                self.cited.append(n)
                new.append(n)
            return m.group(0)

        return MARKER.sub(check, text), new
