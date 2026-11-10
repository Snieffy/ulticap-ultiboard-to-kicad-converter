"""The KIUT window and the KIUT Viewer handing designs to each other.

- **One KIUT window.** Started while one is open (by hand, a file association, the viewer's *Open
  in KIUT…*), KIUT hands its files to the open window, which comes to the front, and ends.
- **Viewers, as many as wanted; KIUT has one of its own** -- the viewer that last sent it a design
  (*Open in KIUT…*), else the one it started last. Its *Open in Viewer* shows the design there;
  when that viewer is closed, it starts a new one, which becomes its own. Other viewers are never
  touched.

Each listens, while it runs, on a local channel: the KIUT window on WINDOW, each viewer on one of
its own (`viewer_channel(id)`; a viewer KIUT starts is given its id). A named pipe on Windows, a
socket file on Linux and macOS (Python's multiprocessing.connection); per user and per KIUT folder,
guarded by a key in a file only the user can read.

    Listener(channel, handle).start() / .stop()   receiving: handle(paths, sender) -> reply
    offer(channel, paths, sender=None)            sending: the reply, or None (no one listens)

A message names files (none: only to the front) and, from a viewer, its own channel id. Replies:
OPENED, SHOWN (that design was shown already: only brought to the front), BUSY (the KIUT window is
converting), or an error text.
"""
import getpass
import hashlib
import os
import secrets
import sys
import tempfile
import threading
import uuid
from multiprocessing.connection import Client, Listener as _MPListener
from pathlib import Path
from typing import Callable, List, Optional, Sequence

APP_DIR = Path(__file__).resolve().parents[2]
WINDOW = 'window'
OPENED, SHOWN, BUSY = 'opened', 'shown', 'busy'
REPLY_TIMEOUT = 5.0             # seconds an open window may take to answer (it may be loading a file)
BUSY_TEXT = 'KIUT is busy — try again when it has finished.'


def viewer_channel(viewer_id: str) -> str:
    return f'viewer-{viewer_id}'


def new_viewer_id() -> str:
    return uuid.uuid4().hex[:12]


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:                       # no user name to be had: one channel for all
        return 'user'


def _tag(name: Optional[str] = None) -> str:
    """This user's and this KIUT folder's (or `name`'s) part of the channels' names."""
    base = name if name is not None else str(APP_DIR).lower()
    return hashlib.sha256(f'{_user()}|{base}'.encode('utf-8')).hexdigest()[:12]


def address(channel: str, name: Optional[str] = None) -> str:
    if sys.platform == 'win32':
        return rf'\\.\pipe\kiut-{channel}-{_tag(name)}'
    return os.path.join(tempfile.gettempdir(), f'kiut-{channel}-{_tag(name)}.sock')


def key_file(name: Optional[str] = None) -> Path:
    return Path(tempfile.gettempdir()) / f'kiut-{_tag(name)}.key'


def _key(name: Optional[str] = None) -> bytes:
    """The channels' key: a file in the temporary folder, readable by its user only, made when
    missing."""
    path = key_file(name)
    try:
        data = path.read_bytes()
        if len(data) >= 32:
            return data
    except OSError:
        pass
    data = secrets.token_bytes(32)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
    return data


def corrected_original(path: Path) -> Path:
    """A design file's original for its copy in <name>_corrected (the KIUT window hands the viewer
    the working copy); any other file as it is."""
    path = Path(path)
    folder = path.parent
    if folder.name.lower().endswith('_corrected') and (folder / '_corrected.json').is_file():
        original = folder.parent / path.name
        if original.is_file():
            return original
    return path


def same_file(a, b) -> bool:
    try:
        return a is not None and b is not None and os.path.samefile(a, b)
    except OSError:
        return False


def same_files(a: Sequence, b: Sequence) -> bool:
    return len(a) == len(b) and all(same_file(x, y) for x, y in zip(a, b))


class Listener:
    """The receiving side: handle(paths, sender) is called for every message, in the listening
    thread -- the program hands it to its own thread -- and returns the reply."""

    def __init__(self, channel: str, handle: Callable[[List[Path], Optional[str]], str],
                 name: Optional[str] = None):
        self.channel, self.handle, self.name = channel, handle, name
        self._mp: Optional[_MPListener] = None

    def start(self) -> bool:
        """Listening; False when the channel is someone's already (or can't be had)."""
        addr = address(self.channel, self.name)
        if ping(self.channel, self.name):
            return False                    # one is listening already (a pipe name can be had twice)
        if sys.platform != 'win32' and os.path.exists(addr):
            try:
                os.unlink(addr)             # left by a program that ended without closing it
            except OSError:
                return False
        try:
            self._mp = _MPListener(addr, authkey=_key(self.name))
        except OSError:
            return False
        threading.Thread(target=self._serve, daemon=True).start()
        return True

    def _serve(self) -> None:
        while self._mp is not None:
            try:
                conn = self._mp.accept()
            except Exception:
                if self._mp is None:
                    return
                continue
            try:
                msg = conn.recv()
                if isinstance(msg, tuple) and msg and msg[0] == 'ping':
                    conn.send('pong')
                elif isinstance(msg, tuple) and len(msg) == 3 and msg[0] == 'open':
                    try:
                        conn.send(self.handle([Path(p) for p in msg[1]], msg[2]))
                    except Exception as e:
                        conn.send(f'error: {e}')
            except Exception:
                pass
            finally:
                conn.close()

    def stop(self) -> None:
        mp, self._mp = self._mp, None
        if mp is not None:
            try:
                mp.close()
            except Exception:
                pass


def _send(channel: str, msg: tuple, name: Optional[str], timeout: float) -> Optional[str]:
    try:
        conn = Client(address(channel, name), authkey=_key(name))
    except Exception:
        return None
    try:
        conn.send(msg)
        if not conn.poll(timeout):
            return None
        return conn.recv()
    except Exception:
        return None
    finally:
        conn.close()


def ping(channel: str, name: Optional[str] = None) -> bool:
    return _send(channel, ('ping',), name, 1.0) == 'pong'


def offer(channel: str, paths: Sequence, sender: Optional[str] = None,
          name: Optional[str] = None) -> Optional[str]:
    """Files (none: only to the front) offered on a channel, from a viewer `sender` (its id): the
    reply, or None when no one answers. On Windows the offering program, the one in front, allows
    the other to come to the front."""
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)   # ASFW_ANY
        except Exception:
            pass
    return _send(channel, ('open', [str(p) for p in paths], sender), name, REPLY_TIMEOUT)
