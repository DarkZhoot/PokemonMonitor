from contextlib import contextmanager


class AlreadyRunning(Exception):
    pass


@contextmanager
def exclusive(path):
    handle = open(path, "a+b")
    try:
        if __import__("os").name == "nt":
            import msvcrt

            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise AlreadyRunning() from None
        else:
            import fcntl

            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise AlreadyRunning() from None
        yield
    finally:
        handle.close()
