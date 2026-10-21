"""Linux parent-death guard: never leave an inference process after worker death."""

import ctypes
import os
import signal
import sys


def main():
    parent = int(sys.argv[1])
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise SystemExit("Cannot set parent-death protection")
    if os.getppid() != parent:
        raise SystemExit("Worker already stopped")
    os.execvp(sys.argv[2], sys.argv[2:])


if __name__ == "__main__":
    main()
