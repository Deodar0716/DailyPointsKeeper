# -*- coding: utf-8 -*-
"""Find an installed desktop client without hardcoding anyone's drive letter.

Only used when the *portable* source of truth is missing:

* Qoder CN normally exposes its fingerprint in a plaintext cache under %APPDATA%,
  so this only runs as a fallback for `runtime-info.exe`.
* WorkBuddy needs the app's own Electron binary to decrypt its token file, so it
  is looked up on every run (config value wins; env wins over that).

Search is deliberately shallow and cheap: explicit config → env → a handful of
standard install roots → depth-1 directories on C:-F: whose name matches.
"""

import os
import string

_MAX_DEPTH = 1
_DRIVE_ROOTS = ["C:", "D:", "E:", "F:"]


def _std_roots():
    out = []
    for var in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "APPDATA"):
        v = os.environ.get(var, "")
        if v:
            out.append(v)
    return out


def _matches(name, needles):
    low = name.lower()
    return any(n.lower() in low for n in needles)


def find_file(marker_rel, needles, extra_roots=None, max_depth=_MAX_DEPTH):
    """Return the first path whose *marker* exists.

    marker_rel : relative path inside the install dir, e.g.
                 "resources/umid/runtime-info.exe"
    needles    : substrings the install directory name must contain
    """
    marker = marker_rel.replace("/", os.sep)
    for root in (extra_roots or []) + _std_roots():
        if not root or not os.path.isdir(root):
            continue
        for depth_dir in _candidates(root, needles, max_depth):
            p = os.path.join(depth_dir, marker)
            if os.path.isfile(p):
                return depth_dir
    for drive in _DRIVE_ROOTS:
        if not os.path.isdir(drive + os.sep):
            continue
        try:
            for name in os.listdir(drive + os.sep):
                if not _matches(name, needles):
                    continue
                d = os.path.join(drive + os.sep, name)
                if os.path.isfile(os.path.join(d, marker)):
                    return d
        except OSError:
            continue
    return ""


def find_exe(exe_name, needles, extra_roots=None):
    """Same, but the marker is a bare file at the install dir itself."""
    for root in (extra_roots or []) + _std_roots():
        if not root or not os.path.isdir(root):
            continue
        for d in _candidates(root, needles, _MAX_DEPTH):
            p = os.path.join(d, exe_name)
            if os.path.isfile(p):
                return p
    for drive in _DRIVE_ROOTS:
        try:
            for name in os.listdir(drive + os.sep):
                if not _matches(name, needles):
                    continue
                p = os.path.join(drive + os.sep, name, exe_name)
                if os.path.isfile(p):
                    return p
        except OSError:
            continue
    return ""


def _candidates(root, needles, max_depth):
    hits = []
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            full = os.path.join(d, n)
            if not os.path.isdir(full):
                continue
            if _matches(n, needles):
                hits.append(full)
            if depth < max_depth:
                stack.append((full, depth + 1))
        if len(hits) > 40:
            break
    return hits


def newest_version_dir(install_dir, sub=".qoder-versions"):
    """Client versions live in dated/versioned subdirectories."""
    base = os.path.join(install_dir, sub)
    try:
        names = [n for n in os.listdir(base) if os.path.isdir(os.path.join(base, n))]
    except OSError:
        return ""
    return sorted(names)[-1] if names else ""
