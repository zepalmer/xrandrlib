#!/usr/bin/env python

"""
This module contains miscellaneous utilities for xrandrlib.
"""

class LineBuffer(object):
    def __init__(self, lines):
        self.buffer = None
        self.iterator = lines.__iter__()
        self._ensure_buffer()
    def _ensure_buffer(self):
        if self.buffer is None:
            try:
                self.buffer = self.iterator.next()
            except StopIteration:
                return False
        return True
    def peek(self):
        if not self._ensure_buffer():
            raise StopIteration()
        return self.buffer
    def next(self):
        if not self._ensure_buffer():
            raise StopIteration()
        ret = self.buffer
        self.buffer = None
        return ret
    def has_next(self):
        return self._ensure_buffer()
