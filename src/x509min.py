"""The expiry date of an X.509 certificate, read from its DER bytes (stdlib only, Python 3.8+).

A strict, minimal ASN.1 walker: it follows Certificate > tbsCertificate > validity and returns notAfter, nothing else (the subject, the issuer
and the key are never read or kept). Anything it does not understand (a truncated or oversized length, an indefinite length, a multi-byte tag,
trailing bytes, a date out of range) is None: it never raises and never loops (every step moves forward inside a buffer of at most MAX bytes).
`pem_to_der` takes the first CERTIFICATE block of a PEM text. Used by the root collector (src/collector.py) for the [tls] watch list.
"""
import base64
import calendar
import re
from datetime import datetime

MAX = 65536  # bytes of a certificate this module looks at; a real one is 1-3 KB
PEM = re.compile(r"-----BEGIN CERTIFICATE-----([A-Za-z0-9+/=\s]{1,%d})-----END CERTIFICATE-----" % MAX)


def _tlv(buf, pos, end):
    """(tag, content start, content end) of the element at pos, or None. Definite lengths of up to 4 bytes, written the shortest way."""
    if pos + 2 > end:
        return None
    tag, n = buf[pos], buf[pos + 1]
    pos += 2
    if tag & 0x1F == 0x1F:  # a tag number of more than one byte: not in a certificate's path
        return None
    if n >= 0x80:
        k = n & 0x7F
        if k == 0 or k > 4 or pos + k > end:  # 0x80 is the indefinite form (BER, not DER)
            return None
        n = int.from_bytes(buf[pos:pos + k], "big")
        if n < 0x80 or buf[pos] == 0:  # not the shortest form
            return None
        pos += k
    return (tag, pos, pos + n) if pos + n <= end else None


def _children(buf, start, end):
    """The elements inside [start, end) as [(tag, start, end)], or None when they do not fill it exactly."""
    out = []
    while start < end:
        t = _tlv(buf, start, end)
        if t is None:
            return None
        out.append(t)
        start = t[2]
    return out


def _time(buf, tag, start, end):
    """UTCTime (YYMMDDHHMMSSZ) or GeneralizedTime (YYYYMMDDHHMMSSZ) -> epoch seconds, or None."""
    s = bytes(buf[start:end])
    want = {0x17: 13, 0x18: 15}.get(tag)
    if want is None or len(s) != want or not s.endswith(b"Z") or not s[:-1].isdigit():
        return None
    d = s[:-1].decode("ascii")
    y = int(d[:2]) + (1900 if int(d[:2]) >= 50 else 2000) if tag == 0x17 else int(d[:4])
    rest = d[2:] if tag == 0x17 else d[4:]
    try:
        return calendar.timegm(datetime(y, int(rest[0:2]), int(rest[2:4]), int(rest[4:6]), int(rest[6:8]), int(rest[8:10])).timetuple())
    except ValueError:
        return None


def not_after(der):
    """notAfter of a DER certificate as epoch seconds (UTC); None if it is not one this module understands."""
    if not isinstance(der, (bytes, bytearray)) or not 8 <= len(der) <= MAX:
        return None
    cert = _tlv(der, 0, len(der))
    if cert is None or cert[0] != 0x30 or cert[2] != len(der):
        return None
    kids = _children(der, cert[1], cert[2])
    if not kids or kids[0][0] != 0x30:
        return None
    tbs = _children(der, kids[0][1], kids[0][2])  # [version], serial, signature, issuer, validity, subject, ...
    if not tbs:
        return None
    i = 1 if tbs[0][0] == 0xA0 else 0
    if len(tbs) < i + 5 or tbs[i][0] != 0x02 or tbs[i + 3][0] != 0x30:
        return None
    validity = _children(der, tbs[i + 3][1], tbs[i + 3][2])
    if not validity or len(validity) != 2:
        return None
    tag, start, end = validity[1]
    return _time(der, tag, start, end)


def pem_to_der(text):
    """The DER bytes of the first CERTIFICATE block of a PEM text, or None."""
    m = PEM.search(text) if isinstance(text, str) else None
    if not m:
        return None
    try:
        return base64.b64decode("".join(m.group(1).split()), validate=True)
    except ValueError:
        return None
