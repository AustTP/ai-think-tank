"""RFC 6238 TOTP for tank-owned accounts.

Google Authenticator and this module use the same shared secret and clock.
The secret is the credential. The six-digit code is derived on demand and is
never stored.

Stdlib only. No network, no external package.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

DEFAULT_DIGITS = 6
DEFAULT_PERIOD = 30
DEFAULT_ALGORITHM = 'SHA1'
_ALGORITHMS = ('SHA1', 'SHA256', 'SHA512')
_RECOVERY_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'


def generate_secret(byte_len=20):
    """A new base32 secret with no padding, the same shape most sites show."""
    return base64.b32encode(secrets.token_bytes(byte_len)).decode('ascii').rstrip('=')


def normalize_secret(secret):
    if not isinstance(secret, str):
        raise ValueError('secret must be a string')
    value = ''.join(secret.split()).strip().upper().rstrip('=')
    if not value:
        raise ValueError('secret is empty')
    if any(ch not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567' for ch in value):
        raise ValueError('secret is not valid base32')
    # Decode once so a bad length or bad alphabet fails here, not at login.
    base64.b32decode(value + '=' * (-len(value) % 8), casefold=True)
    return value


def _algorithm(name):
    value = (name or DEFAULT_ALGORITHM).upper().replace('-', '')
    if value not in _ALGORITHMS:
        raise ValueError(f'unsupported TOTP algorithm: {name}')
    return value


def hotp(secret, counter, digits=DEFAULT_DIGITS, algorithm=DEFAULT_ALGORITHM):
    normalized = normalize_secret(secret)
    key = base64.b32decode(
        normalized + '=' * (-len(normalized) % 8), casefold=True
    )
    digest = hmac.new(
        key, struct.pack('>Q', int(counter)),
        getattr(hashlib, _algorithm(algorithm).lower()),
    ).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** int(digits))).zfill(int(digits))


def code(secret, at=None, digits=DEFAULT_DIGITS, period=DEFAULT_PERIOD,
         algorithm=DEFAULT_ALGORITHM):
    now = time.time() if at is None else float(at)
    counter = int(now // int(period))
    return hotp(secret, counter, digits=digits, algorithm=algorithm)


def code_info(secret, at=None, digits=DEFAULT_DIGITS, period=DEFAULT_PERIOD,
              algorithm=DEFAULT_ALGORITHM):
    now = time.time() if at is None else float(at)
    period = int(period)
    return {
        'code': code(secret, at=now, digits=digits, period=period, algorithm=algorithm),
        'expiresIn': max(0, int(period - (now % period))),
        'period': period,
        'digits': int(digits),
        'algorithm': _algorithm(algorithm),
    }


def verify(secret, candidate, at=None, digits=DEFAULT_DIGITS, period=DEFAULT_PERIOD,
           algorithm=DEFAULT_ALGORITHM, window=1):
    """Constant-time check with one time-step of drift by default."""
    if not isinstance(candidate, str):
        return False
    candidate = candidate.strip()
    if not candidate.isdigit() or len(candidate) != int(digits):
        return False
    now = time.time() if at is None else float(at)
    counter = int(now // int(period))
    for offset in range(-int(window), int(window) + 1):
        expected = hotp(secret, counter + offset, digits=digits, algorithm=algorithm)
        if hmac.compare_digest(expected, candidate):
            return True
    return False


def parse_otpauth(uri):
    """Parse otpauth://totp/... into the fields we store.

    Accepts the URI a site shows as a QR code, plus the manual base32 key.
    """
    if not isinstance(uri, str) or not uri.strip():
        raise ValueError('otpauth URI is empty')
    parsed = urllib.parse.urlparse(uri.strip())
    if parsed.scheme != 'otpauth' or parsed.netloc.lower() != 'totp':
        raise ValueError('not an otpauth://totp URI')
    query = urllib.parse.parse_qs(parsed.query)
    secret = (query.get('secret') or [''])[0]
    if not secret:
        raise ValueError('otpauth URI has no secret')
    label = urllib.parse.unquote(parsed.path.lstrip('/'))
    issuer = (query.get('issuer') or [''])[0]
    if not issuer and ':' in label:
        issuer = label.split(':', 1)[0]
    account = label.split(':', 1)[1] if ':' in label else label
    return {
        'secret': normalize_secret(secret),
        'issuer': issuer.strip(),
        'account': account.strip(),
        'algorithm': _algorithm((query.get('algorithm') or [DEFAULT_ALGORITHM])[0]),
        'digits': int((query.get('digits') or [DEFAULT_DIGITS])[0]),
        'period': int((query.get('period') or [DEFAULT_PERIOD])[0]),
    }


def provisioning_uri(secret, account, issuer='', digits=DEFAULT_DIGITS,
                     period=DEFAULT_PERIOD, algorithm=DEFAULT_ALGORITHM):
    secret = normalize_secret(secret)
    issuer = (issuer or '').strip()
    account = (account or '').strip()
    if not account:
        raise ValueError('account label is required')
    label = f'{issuer}:{account}' if issuer else account
    query = {
        'secret': secret,
        'algorithm': _algorithm(algorithm),
        'digits': str(int(digits)),
        'period': str(int(period)),
    }
    if issuer:
        query['issuer'] = issuer
    return 'otpauth://totp/' + urllib.parse.quote(label, safe='') + '?' + urllib.parse.urlencode(query)


def recovery_codes(count=10, groups=2, group_len=4):
    codes = []
    for _ in range(int(count)):
        groups_out = []
        for _ in range(int(groups)):
            groups_out.append(''.join(secrets.choice(_RECOVERY_ALPHABET)
                                      for _ in range(int(group_len))))
        codes.append('-'.join(groups_out))
    return codes
