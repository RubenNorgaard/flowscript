"""Recordings over a text stream (SPEC 10.4).

A device usually has one console, shared by its boot messages, its logs and the
recording.  The text transport puts each recorded frame on a line of its own:

    #FS:<the frame's bytes in hex, its length prefix included>
    #T:<unix time in ms>          the time the lines after it arrived (added by a viewer)

Everything else is console text.  Lines survive what a serial terminal does to a byte
stream, can be saved with `tee` or `>`, and read back by `flowscript replay` and
`flowscript view` like a binary recording.

    dec = Decoder()                      # text or binary, detected from the first bytes
    for kind, value in dec.feed(chunk):  # "frame" (bytes), "time" (ms), "text" (str),
        ...                              # "bad" (str: a damaged #FS line, dropped)
    dec.close()                          # a last line without a newline
"""

from .record import Reader, TAG_NAMES

# frame tags: a recording's (SPEC 10.2) and an observation stream's (10.5, observe.OBS_TAGS)
KNOWN_TAGS = frozenset(TAG_NAMES) | frozenset(range(16, 25))

FRAME_MARK = b"#FS:"
TIME_MARK = b"#T:"


def encode_frame(frame):
    """One recorded frame (varint length + body) as a text line."""
    return FRAME_MARK + bytes(frame).hex().encode("ascii") + b"\n"


def encode_time(ms):
    return TIME_MARK + str(int(ms)).encode("ascii") + b"\n"


def split_frames(data):
    """Complete binary frames at the start of data -> (frames, rest)."""
    frames, r = [], Reader(data)
    while r.pos < len(data):
        start = r.pos
        try:
            n = r.varint()
            r.take(n)
        except ValueError:
            r.pos = start
            break
        frames.append(bytes(data[start:r.pos]))
    return frames, bytes(data[r.pos:])


def looks_binary(data, final=False):
    """True / False once the first bytes tell a binary recording from text; None while
    there are too few bytes to say.  Binary: the first two frames (or all of the data,
    when final) parse as frames with known tags.  Text fails this at once: a printable
    second byte is no tag, and a line break pair (\\r\\n) is not followed by a frame."""
    r = Reader(data)
    for _ in range(2):
        start = r.pos
        try:
            n = r.varint()
            if n == 0:
                return False
            tag = r.byte()
            if tag not in KNOWN_TAGS:
                return False
            r.take(n - 1)
        except ValueError as e:
            if str(e) != "truncated":
                return False
            return False if final else None
        if final and r.pos == len(data):
            return True
    return True


def decode_frame_line(line):
    """The frame on a '#FS:' line (bytes after the marker) or None if it is damaged:
    bad hex, or a length prefix that doesn't match (a line cut short or run together)."""
    try:
        frame = bytes.fromhex(line.strip().decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return None
    frames, rest = split_frames(frame)
    return frame if len(frames) == 1 and not rest else None


class Decoder:
    """Bytes in any chunks -> ("frame", bytes) / ("time", ms) / ("text", str) / ("bad", str).

    mode: None (detect), "text" or "binary".  In text mode `lines` (if given) is called
    with every complete input line (bytes, without the newline), e.g. to echo it."""

    def __init__(self, mode=None, lines=None):
        if mode not in (None, "text", "binary"):
            raise ValueError("mode must be None, 'text' or 'binary'")
        self.mode = mode
        self.lines = lines
        self.buf = b""

    def feed(self, data):
        self.buf += bytes(data)
        if self.mode is None:
            b = looks_binary(self.buf)
            if b is None:
                return []
            self.mode = "binary" if b else "text"
        if self.mode == "binary":
            frames, self.buf = split_frames(self.buf)
            return [("frame", f) for f in frames]
        out = []
        while True:
            i = self.buf.find(b"\n")
            if i < 0:
                break
            line, self.buf = self.buf[:i], self.buf[i + 1:]
            self._line(line, out)
        return out

    def close(self):
        """The input ended: a last text line without a newline (a cut-off frame is dropped)."""
        out = []
        if self.mode is None and self.buf:        # short input: decide on what there is
            self.mode = "binary" if looks_binary(self.buf, final=True) else "text"
            if self.mode == "binary":
                frames, self.buf = split_frames(self.buf)
                out += [("frame", f) for f in frames]
        if self.mode == "text" and self.buf:
            self._line(self.buf, out)
        self.buf = b""
        return out

    def _line(self, line, out):
        if self.lines:
            self.lines(line)
        line = line.rstrip(b"\r")
        i = line.find(FRAME_MARK)
        if i >= 0:
            before = line[:i].strip()
            if before:                            # console text the frame was written into
                out.append(("text", before.decode("utf-8", "replace")))
            frame = decode_frame_line(line[i + len(FRAME_MARK):])
            if frame is None:
                out.append(("bad", line[i:i + 40].decode("ascii", "replace")))
            else:
                out.append(("frame", frame))
            return
        if line.startswith(TIME_MARK):
            try:
                out.append(("time", int(line[len(TIME_MARK):].strip())))
                return
            except ValueError:
                pass
        if line.strip():
            out.append(("text", line.decode("utf-8", "replace")))


def recording_bytes(chunks, mode=None):
    """The binary recording in a stream of chunks (text or binary): yields frame bytes."""
    dec = Decoder(mode)
    for chunk in chunks:
        for kind, value in dec.feed(chunk):
            if kind == "frame":
                yield value
    for kind, value in dec.close():
        if kind == "frame":
            yield value
