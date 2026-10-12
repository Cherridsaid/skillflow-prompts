"""Whether this process carries an elevated token, asked from the test side.

Three test modules drive the Windows isolation of the writer, and the writer refuses to
start from an elevated process on purpose (`windows_process`, `TokenElevation`). A GitHub
Windows runner is elevated, so those tests cannot hold there: they are skipped, with the
reason said out loud, and they keep running on an ordinary desktop session.

The question is asked exactly as the product asks it of itself. It is deliberately **not**
readable from the environment: a skip that a variable can switch on is a skip that will be
switched on by accident, and the coverage would vanish without a word.
"""

import os


def elevated():
    """True only when the token is observed to be elevated.

    An answer that cannot be observed is `False`: the tests then run and fail with the
    product's own message, which is the current, readable behaviour. Guessing a skip here
    would hide the very boundary these tests exist to hold.
    """
    if os.name != "nt":
        return False
    try:
        import win32api
        import win32con
        import win32security as security

        token = security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        return bool(security.GetTokenInformation(token, security.TokenElevation))
    except Exception:  # noqa: BLE001 - an unobservable token is not an elevated one
        return False


ELEVATED = elevated()
REASON = (
    "elevated process: the writer refuses to start from one, so these tests cannot hold "
    "here; they run on an ordinary session"
)
