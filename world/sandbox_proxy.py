"""Allowlist forward proxy for the Work Room sandbox's egress -- run as its
own long-lived Docker container, dual-homed on both the sandbox's fully
isolated network (no route to the internet at all) and a real egress
network. Sandbox containers reach the internet ONLY through this proxy,
which only permits CONNECT/HTTP to a fixed set of known package-registry
hosts, plus the player-vetted BROWSE_ALLOWLIST_DOMAINS a script
may need for real, systematic data work -- e.g. a spike's finding that
one-URL-at-a-time browse_page tool calls can't do real multi-page crawling
as efficiently as a script would. Confirmed before this was written: a
container on the isolated network alone has zero direct route out (an
`apk add` inside one failed outright), and this proxy container's second
network attachment gives it real internet access to bridge the two.

CONNECT tunnels are relayed byte-for-byte, not TLS-terminated -- this
proxy never decrypts HTTPS traffic, it only decides (by hostname alone,
before the tunnel opens) whether a destination is on the allowlist. No
custom CA, no certificate handling, no visibility into request bodies.

Extending this to real data sites is deliberately narrow: only hosts the
PLAYER already vetted via BROWSE_ALLOWLIST_DOMAINS (the same list
/api/browse's Jev-classify-skip uses) are added here, via the
SANDBOX_EGRESS_EXTRA_HOSTS env var serve.py passes at container start --
never a general "let scripts hit any URL" relaxation. Ad-hoc browsing of an
arbitrary, not-yet-vetted site still has to go through browse_page's
per-URL Jev classify+escalate flow one at a time; a script here only ever
reaches domains that flow has already been bypassed for by explicit player
choice.
"""
import os
import re
import socket
import threading
import time

PROXY_PORT = 8899

# Deliberately a fixed, auditable list -- Python/Node/Debian's real
# package ecosystems plus GitHub (for git-based dependencies), not "the
# whole internet." Extend this list, don't remove the allowlist model,
# if something legitimate needs to be added later.
ALLOWED_HOSTS = {
    'pypi.org', 'files.pythonhosted.org', 'pythonhosted.org',
    'registry.npmjs.org', 'registry.yarnpkg.com',
    'deb.debian.org', 'security.debian.org', 'ftp.debian.org', 'snapshot.debian.org',
    'archive.ubuntu.com', 'security.ubuntu.com',
    'github.com', 'raw.githubusercontent.com', 'codeload.github.com', 'objects.githubusercontent.com', 'api.github.com',
}
# Player-vetted data-site hosts, passed in at container start (comma-
# separated) so this standalone script never has to import serve.py's env-
# loading machinery -- it runs in a different container/image entirely.
ALLOWED_HOSTS |= {h.strip().lower() for h in os.environ.get('SANDBOX_EGRESS_EXTRA_HOSTS', '').split(',') if h.strip()}


def is_allowed(host):
    return (host or '').lower() in ALLOWED_HOSTS


# Real constraint, found while auditing this for a possible GET-only
# restriction on data-site hosts: this proxy deliberately never
# terminates TLS (see the module docstring) -- CONNECT tunnels are relayed
# byte-for-byte, so it cannot see or restrict the HTTP method inside HTTPS,
# which is how virtually all real traffic works. A method restriction here
# would only cover the plain-HTTP path below, giving false confidence for
# the case that actually matters. Real HTTPS method-filtering would need
# MITM TLS termination (a custom CA, cert trust in the sandbox image) --
# deliberately NOT done; it's a much bigger, more invasive change than this
# allowlist model, and trades away the "never decrypts traffic" property on
# purpose. Practical takeaway: only add a domain to BROWSE_ALLOWLIST_DOMAINS
# if you're comfortable with a script having full read+write reachability
# to it, not just reads -- this proxy cannot make that distinction for you.


# Gap flagged live, same evening a scheduled-task bug (see
# _peer_gated_lane) burned ~$9 in an unrelated runaway loop: a script with
# real egress but no request cap could hammer an allowlisted site far harder
# than the deliberate, budget-capped browse_page tool loop ever would.
# Fixed-window per-host limit, independent of what's inside a request (this
# proxy never sees request bodies) -- generous enough for a normal pip/npm
# install or a real crawl, tight enough to stop a runaway loop from doing
# real damage to an external site or spinning forever unnoticed.
RATE_LIMIT_MAX_REQUESTS = 60
RATE_LIMIT_WINDOW_S = 60
_rate_lock = threading.Lock()
_request_log: dict[str, list[float]] = {}  # host -> [timestamps within the current window]


def rate_limited(host):
    host = (host or '').lower()
    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW_S
    with _rate_lock:
        times = _request_log.setdefault(host, [])
        while times and times[0] < cutoff:
            times.pop(0)
        if len(times) >= RATE_LIMIT_MAX_REQUESTS:
            return True
        times.append(now)
        return False


def relay(a, b):
    def pipe(src, dst):
        try:
            while True:
                data = src.recv(4096)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    t1 = threading.Thread(target=pipe, args=(a, b), daemon=True)
    t2 = threading.Thread(target=pipe, args=(b, a), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()


def handle_client(conn):
    try:
        conn.settimeout(10)
        request = b''
        while b'\r\n\r\n' not in request:
            chunk = conn.recv(4096)
            if not chunk:
                return
            request += chunk
            if len(request) > 65536:  # a request line this long is not a real proxy request
                return

        first_line = request.split(b'\r\n', 1)[0].decode(errors='replace')
        parts = first_line.split()
        if len(parts) < 2:
            return
        method, target = parts[0], parts[1]

        if method == 'CONNECT':
            # The common case -- pip/npm/apt/git all speak HTTPS, and a
            # CONNECT target is just "host:port", decided purely on
            # hostname before any bytes of the actual request are seen.
            host = target.rsplit(':', 1)[0]
            port_str = target.rsplit(':', 1)[1] if ':' in target else '443'
            port = int(port_str)
            if not is_allowed(host):
                conn.sendall(b'HTTP/1.1 403 Forbidden\r\n\r\n')
                return
            if rate_limited(host):
                conn.sendall(b'HTTP/1.1 429 Too Many Requests\r\n\r\n')
                return
            try:
                remote = socket.create_connection((host, port), timeout=10)
            except OSError:
                conn.sendall(b'HTTP/1.1 502 Bad Gateway\r\n\r\n')
                return
            conn.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            relay(conn, remote)
        else:
            # Plain HTTP proxying, for the rarer tool that doesn't default
            # to HTTPS -- same allowlist, decided from the Host header.
            headers = request.split(b'\r\n\r\n', 1)[0]
            m = re.search(rb'Host:\s*([^\r\n]+)', headers, re.IGNORECASE)
            host = m.group(1).decode(errors='replace').split(':')[0] if m else None
            if not host or not is_allowed(host):
                conn.sendall(b'HTTP/1.1 403 Forbidden\r\n\r\n')
                return
            if rate_limited(host):
                conn.sendall(b'HTTP/1.1 429 Too Many Requests\r\n\r\n')
                return
            try:
                remote = socket.create_connection((host, 80), timeout=10)
                remote.sendall(request)
            except OSError:
                conn.sendall(b'HTTP/1.1 502 Bad Gateway\r\n\r\n')
                return
            relay(conn, remote)
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('0.0.0.0', PROXY_PORT))  # nosec B104 -- deliberate dual-homed egress proxy
    server.listen(50)
    print(f'sandbox egress allowlist proxy listening on :{PROXY_PORT}', flush=True)
    while True:
        conn, _addr = server.accept()
        threading.Thread(target=handle_client, args=(conn,), daemon=True).start()


if __name__ == '__main__':
    main()
