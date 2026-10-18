"""Bounded compilation cache for command parsing; same public matching semantics.

The atlas traverses more than the stdlib's small shared regex cache. Keeping a
bounded cache local to these parsers avoids recompilation without changing any
pattern, matching flags or captured user payload. No global monkeypatching.
"""
from __future__ import annotations
import re as _re
from functools import lru_cache

@lru_cache(maxsize=2048)
def compile(pattern, flags=0):
    return _re.compile(pattern, flags)

def search(pattern, string, flags=0):
    return compile(pattern, flags).search(string)
def match(pattern, string, flags=0):
    return compile(pattern, flags).match(string)
def fullmatch(pattern, string, flags=0):
    return compile(pattern, flags).fullmatch(string)
def findall(pattern, string, flags=0):
    return compile(pattern, flags).findall(string)
def finditer(pattern, string, flags=0):
    return compile(pattern, flags).finditer(string)
def split(pattern, string, maxsplit=0, flags=0):
    return compile(pattern, flags).split(string, maxsplit)
def sub(pattern, repl, string, count=0, flags=0):
    return compile(pattern, flags).sub(repl, string, count)
def subn(pattern, repl, string, count=0, flags=0):
    return compile(pattern, flags).subn(repl, string, count)
def __getattr__(name):
    return getattr(_re, name)
