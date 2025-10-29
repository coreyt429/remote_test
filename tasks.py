"""
/home/ubuntu/api/tasks.py
Celery tasks for remote tests
"""

from __future__ import annotations
from typing import Dict, List, Any, Tuple, Optional
import time
import re
import socket
import ssl
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET
import dns.resolver
import requests
from celery import shared_task
from oci import Oci


def _resolve_records(
    domain: str, record_type: str, timeout: float
) -> Tuple[List[str], str | None]:
    """Resolve A or SRV records for domain. Returns (records, error_message)."""
    resolver = dns.resolver.Resolver()
    resolver.lifetime = timeout
    resolver.timeout = timeout
    try:
        answers = resolver.resolve(domain, record_type)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return [], f"{type(exc).__name__}: {exc}"

    out: List[str] = []
    if record_type == "A":
        for r in answers:
            out.append(r.address)
    elif record_type == "SRV":
        for r in answers:
            target = r.target.to_text().rstrip(".")
            out.append(f"IN SRV {r.priority} {r.weight} {r.port} {target}")
    else:
        return [], f"Unsupported record type {record_type}"
    return out, None


def _ordered_state_names(records: Dict[str, List[str]]) -> List[str]:
    """
    Deterministic priority if multiple states could match:
      numeric states DESC (e.g., 13057, 13056), then 'original', then other labels alpha.
    """
    numeric, other = [], []
    for k in records.keys():
        try:
            numeric.append((int(k), k))
        except ValueError:
            other.append(k)
    ordered = [name for _, name in sorted(numeric, key=lambda x: x[0], reverse=True)]
    if "original" in other:
        other.remove("original")
        ordered.append("original")
    ordered.extend(sorted(other))
    return ordered


def _classify_state(retrieved: List[str], records: Dict[str, List[str]]) -> str:
    """Return which state the retrieved records match, else 'unknown'."""
    r_sorted = sorted(retrieved)
    for state in _ordered_state_names(records):
        if sorted(records.get(state, [])) == r_sorted:
            return state
    return "unknown"


def _render_line(
    domain: str, record_type: str, state: str, retrieved: List[str], error: str | None
) -> str:
    """Human-readable single-block text akin to the original prints (no colors)."""
    if error:
        return f"Error querying {record_type} records for {domain}: {error}"
    if state == "unknown":
        return f"{domain}: is in state unknown {sorted(retrieved)}"
    if state == "original":
        return f"{domain}: is in state original"
    return f"{domain}: is in state {state}" + (
        f" {sorted(retrieved)}" if record_type == "A" else ""
    )


@shared_task(name="dns.check_records")
def check_records(
    dns_data: Dict[str, Dict[str, List[str]]],
    timeout: float = 3.0,
    include_text: bool = True,
) -> Dict[str, Any]:
    """
    Classify each domain's live DNS records into one of the states provided in dns_data,
    or 'unknown' if it doesn't match any state.

    Args:
        dns_data: { domain: { "<state>": [values], ... }, ... }
                  States can be 'original', numeric like '13056'/'13057', or other labels.
        timeout:  per-query resolver timeout (seconds)
        include_text: include human-readable 'text' per domain

    Returns:
        {
          "summary": {"domains": N, "duration_sec": float},
          "results": {
            "<domain>": {
              "record_type": "A"|"SRV",
              "retrieved": [..],    # sorted
              "state": "<state>|unknown",
              "error": str|None,
              "text": str           # when include_text=True
            }, ...
          }
        }
    """
    started = time.time()
    results: Dict[str, Any] = {}

    for domain, records in dns_data.items():
        record_type = "SRV" if domain.startswith("_") else "A"
        retrieved, err = _resolve_records(domain, record_type, timeout)

        if err:
            state = "unknown"
            text = (
                _render_line(domain, record_type, state, [], err)
                if include_text
                else None
            )
            results[domain] = {
                "record_type": record_type,
                "retrieved": [],
                "state": state,
                "error": err,
                **({"text": text} if include_text else {}),
            }
            continue

        state = _classify_state(retrieved, records)
        text = (
            _render_line(domain, record_type, state, retrieved, None)
            if include_text
            else None
        )
        results[domain] = {
            "record_type": record_type,
            "retrieved": sorted(retrieved),
            "state": state,
            "error": None,
            **({"text": text} if include_text else {}),
        }

    return {
        "summary": {
            "domains": len(dns_data),
            "duration_sec": round(time.time() - started, 2),
        },
        "results": results,
    }


# Optional zeep import (OCI-over-SOAP). We guard it too.
try:
    from zeep import Client, Settings  # type: ignore
    from zeep.transports import Transport  # type: ignore

    HAVE_ZEEP = True
except Exception:  # pylint: disable=broad-exception-caught
    Client = Settings = Transport = None  # pylint: disable=invalid-name
    HAVE_ZEEP = False


# ---------- helpers ----------
def _http_get(
    url: str,
    *,
    timeout: float,
    auth: Optional[Tuple[str, str]] = None,
    verify_ssl: bool = True,
) -> Dict[str, Any]:
    """
    Perform a GET and return a compact result dict without raising.
    """
    try:
        r = requests.get(url, timeout=timeout, auth=auth, verify=verify_ssl)
        return {"ok": r.status_code == 200, "status": r.status_code, "error": None}
    except requests.RequestException as exc:
        return {"ok": False, "status": None, "error": f"{type(exc).__name__}: {exc}"}


def _apptest_dms(
    hostname: str, test_data: Dict[str, Any], *, timeout: float, verify_ssl: bool
) -> Dict[str, Any]:
    dms_url = test_data.get("dms_url")
    if not dms_url:
        return {"ok": False, "error": "Missing test_data.dms_url", "details": {}}

    http_url = f"http://{hostname}/{dms_url}"
    https_url = f"https://{hostname}/{dms_url}"

    http_res = _http_get(http_url, timeout=timeout, verify_ssl=verify_ssl)
    https_res = _http_get(https_url, timeout=timeout, verify_ssl=verify_ssl)
    ok = http_res["ok"] or https_res["ok"]
    return {
        "ok": ok,
        "error": None if ok else "DMS check failed",
        "details": {"http": http_res, "https": https_res},
    }


def _apptest_xsi_actions(
    hostname: str, test_data: Dict[str, Any], *, timeout: float, verify_ssl: bool
) -> Dict[str, Any]:
    user_id = test_data.get("user_id")
    password = test_data.get("password")
    if not user_id or not password:
        return {
            "ok": False,
            "error": "Missing user_id/password in test_data",
            "details": {},
        }

    test_cases = {
        "profile": f"com.broadsoft.xsi-actions/v2.0/user/{user_id}/profile",
        "calls": f"com.broadsoft.xsi-actions/v2.0/user/{user_id}/calls",
    }

    results = {}
    for label, rel in test_cases.items():
        urls = {
            "http": f"http://{hostname}/{rel}",
            "https": f"https://{hostname}/{rel}",
        }
        results[label] = {}
        for proto, url in urls.items():
            results[label][proto] = _http_get(
                url, timeout=timeout, auth=(user_id, password), verify_ssl=verify_ssl
            )

    ok = any(v["http"]["ok"] or v["https"]["ok"] for v in results.values())
    return {
        "ok": ok,
        "error": None if ok else "XSI-Actions checks failed",
        "details": results,
    }


def _apptest_xsi_events(
    hostname: str, test_data: Dict[str, Any], *, timeout: float, verify_ssl: bool
) -> Dict[str, Any]:
    user_id = test_data.get("user_id")
    password = test_data.get("password")
    if not user_id or not password:
        return {
            "ok": False,
            "error": "Missing user_id/password in test_data",
            "details": {},
        }

    rel = "com.broadsoft.xsi-events/v2.0/versions"
    http_url = f"http://{hostname}/{rel}"
    https_url = f"https://{hostname}/{rel}"

    http_res = _http_get(
        http_url, timeout=timeout, auth=(user_id, password), verify_ssl=verify_ssl
    )
    https_res = _http_get(
        https_url, timeout=timeout, auth=(user_id, password), verify_ssl=verify_ssl
    )
    ok = http_res["ok"] or https_res["ok"]
    return {
        "ok": ok,
        "error": None if ok else "XSI-Events check failed",
        "details": {"http": http_res, "https": https_res},
    }


def _apptest_ocs(hostname: str, test_data: Dict[str, Any]) -> Dict[str, Any]:
    try:
        my_oci = Oci()  # type: ignore
        my_oci.ocs = hostname
        my_oci.user_name = test_data.get("user_id")
        my_oci.password = test_data.get("password")
        ok = bool(my_oci.connect())
        return {
            "ok": ok,
            "error": None if ok else "OpenClientServer connect() returned False",
            "details": {},
        }
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "details": {}}


def _apptest_oci_over_soap(
    hostname: str, test_data: Dict[str, Any], *, timeout: float, verify_ssl: bool
) -> Dict[str, Any]:
    """
    Oci over Soap test
    """
    if not HAVE_ZEEP:
        return {"ok": False, "error": "zeep not available", "details": {}}

    user_id = test_data.get("user_id")
    password = test_data.get("password")
    if not user_id or not password:
        return {
            "ok": False,
            "error": "Missing user_id/password in test_data",
            "details": {},
        }

    wsdl = f"https://{hostname}/webservice/services/ProvisioningService?wsdl"

    try:
        sess = requests.Session()
        sess.verify = verify_ssl
        transport = Transport(session=sess, timeout=timeout)  # type: ignore
        settings = Settings(strict=False, xml_huge_tree=True)  # type: ignore
        client = Client(wsdl=wsdl, transport=transport, settings=settings)  # type: ignore
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return {
            "ok": False,
            "error": f"SOAP client init failed: {type(exc).__name__}: {exc}",
            "details": {},
        }

    # LoginRequest22V5
    session_id = (
        requests.utils.default_user_agent()
    )  # just a unique-ish string; your original used uuid
    login_request = f"""<?xml version="1.0" encoding="ISO-8859-1"?>
<BroadsoftDocument protocol="OCI" xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <sessionId xmlns="">{session_id}</sessionId>
  <command xsi:type="LoginRequest22V5" xmlns="" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
    <userId>{user_id}</userId>
    <password>{password}</password>
  </command>
</BroadsoftDocument>"""

    def _strip_default_ns(xml_text: str) -> str:
        return re.sub(r' xmlns="[^"]+"', "", xml_text, count=1).lstrip()

    try:
        resp = client.service.processOCIMessage(login_request)  # type: ignore
        xml1 = _strip_default_ns(resp)
        root = ET.fromstring(xml1)
        command = root.find("command")
        if command is None:
            return {
                "ok": False,
                "error": "Login response missing <command>",
                "details": {"login_raw": resp},
            }
        xsi_type = command.attrib.get(
            "{http://www.w3.org/2001/XMLSchema-instance}type", ""
        )
        if "Error" in xsi_type:
            summary = command.findtext("summaryEnglish")
            return {
                "ok": False,
                "error": f"Login failed: {summary or xsi_type}",
                "details": {"login_raw": resp},
            }
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return {
            "ok": False,
            "error": f"Login error: {type(exc).__name__}: {exc}",
            "details": {},
        }

    # UserGetRequest23
    user_get_request = f"""<?xml version="1.0" encoding="ISO-8859-1"?>
<BroadsoftDocument protocol="OCI" xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <sessionId xmlns="">{session_id}</sessionId>
  <command xsi:type="UserGetRequest23" xmlns="" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
    <userId>{user_id}</userId>
  </command>
</BroadsoftDocument>"""

    try:
        resp2 = client.service.processOCIMessage(user_get_request)  # type: ignore
        xml2 = _strip_default_ns(resp2)
        root2 = ET.fromstring(xml2)
        command2 = root2.find("command")
        if command2 is None:
            return {
                "ok": False,
                "error": "UserGet response missing <command>",
                "details": {"user_get_raw": resp2},
            }
        xsi_type2 = command2.attrib.get(
            "{http://www.w3.org/2001/XMLSchema-instance}type", ""
        )
        if "Error" in xsi_type2:
            summary2 = command2.findtext("summaryEnglish")
            return {
                "ok": False,
                "error": f"UserGetRequest23 failed: {summary2 or xsi_type2}",
                "details": {"user_get_raw": resp2},
            }
        # Example extraction
        bw_user_id = command2.findtext("userId")
        phone_number = command2.findtext("phoneNumber")
        return {
            "ok": True,
            "error": None,
            "details": {"userId": bw_user_id, "phoneNumber": phone_number},
        }
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return {
            "ok": False,
            "error": f"UserGet error: {type(exc).__name__}: {exc}",
            "details": {},
        }


# Map names to functions. For “manual” ones, just flag manual=True.
_APPTESTS = {
    "OpenClientServer": lambda h, td, **kw: _apptest_ocs(h, td),
    "BroadworksDms": _apptest_dms,
    "Xsi-Actions": _apptest_xsi_actions,
    "Xsi-Events": _apptest_xsi_events,
    "OCIOverSoap": _apptest_oci_over_soap,
    "BWCallCenter": lambda h, td, **kw: {
        "ok": False,
        "error": "manual",
        "details": {"manual": True},
    },
    "BWReceptionist": lambda h, td, **kw: {
        "ok": False,
        "error": "manual",
        "details": {"manual": True},
    },
    "OCIFiles": lambda h, td, **kw: {
        "ok": False,
        "error": "manual",
        "details": {"manual": True},
    },
    "BWCallSettingsWeb": lambda h, td, **kw: {
        "ok": False,
        "error": "manual",
        "details": {"manual": True},
    },
    "AuthenticationService": lambda h, td, **kw: {
        "ok": False,
        "error": "manual",
        "details": {"manual": True},
    },
    "CommPilot": lambda h, td, **kw: {
        "ok": False,
        "error": "manual",
        "details": {"manual": True},
    },
    "ModeratorClientApp": lambda h, td, **kw: {
        "ok": False,
        "error": "manual",
        "details": {"manual": True},
    },
    "PublicReporting": lambda h, td, **kw: {
        "ok": False,
        "error": "manual",
        "details": {"manual": True},
    },
    "NotificationPushServer": lambda h, td, **kw: {
        "ok": False,
        "error": "manual",
        "details": {"manual": True},
    },
}


@shared_task(name="apptest.run")
def apptest_run(data: Dict[str, Any], **options) -> Dict[str, Any]:
    """
    Run application tests per host.

    Args:
      data: the parsed contents of public_data.json, e.g.
        {
          "host1.example.com": {
            "applications": ["BroadworksDms","Xsi-Actions"],
            "test_data": {"dms_url": "...", "user_id": "...", "password": "..."}
          },
          ...
        }
      timeout: per-request timeout (seconds)
      verify_ssl: whether to verify HTTPS certificates

    Returns:
      {
        "summary": {"hosts": N, "apps_attempted": M, "duration_sec": float},
        "results": {
          "<hostname>": {
            "<AppName>": {"ok": bool, "error": str|None, "details": {...}},
            ...
          },
          ...
        }
      }
    """
    # Robust option parsing: accept kwargs without colliding with positionals
    timeout = float(options.get("timeout", 5.0))
    verify_ssl = bool(options.get("verify_ssl", True))

    started = time.time()
    results: Dict[str, Dict[str, Any]] = {}
    apps_attempted = 0

    for hostname in sorted(data.keys()):
        info = data.get(hostname) or {}
        applications = info.get("applications", [])
        test_data = info.get("test_data", {}) or {}
        per_host: Dict[str, Any] = {}

        for app_name in applications:
            apps_attempted += 1
            fn = _APPTESTS.get(app_name)
            if not fn:
                per_host[app_name] = {
                    "ok": False,
                    "error": "apptest missing",
                    "details": {},
                }
                continue

            try:
                # pass timeout/verify_ssl only to functions that accept them
                if fn in (
                    _apptest_dms,
                    _apptest_xsi_actions,
                    _apptest_xsi_events,
                    _apptest_oci_over_soap,
                ):
                    per_host[app_name] = fn(
                        hostname, test_data, timeout=timeout, verify_ssl=verify_ssl
                    )  # type: ignore[arg-type]
                else:
                    per_host[app_name] = fn(hostname, test_data)  # type: ignore[misc]
            except Exception as exc:  # pylint: disable=broad-exception-caught
                per_host[app_name] = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "details": {},
                }

        results[hostname] = per_host

    return {
        "summary": {
            "hosts": len(data),
            "apps_attempted": apps_attempted,
            "duration_sec": round(time.time() - started, 2),
            "features": {"have_zeep": HAVE_ZEEP},
        },
        "results": results,
    }


def _test_tcp(host: str, port: int, timeout: float) -> Tuple[bool, Optional[str]]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return False, f"{type(exc).__name__}: {exc}"


def _test_tls_orig(
    host: str,
    port: int,
    timeout: float,
    verify_ssl: bool,
) -> Dict[str, Any]:
    """
    Try a TLS handshake and pull cert CN + notAfter.
    Returns: {
        "loaded": bool,
        "cn": str|None,
        "not_after": str|None,
        "expires_utc": str|None,
        "error": str|None
    }
    """
    if verify_ssl:
        context = ssl.create_default_context()
    else:
        context = ssl._create_unverified_context()

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
                if not cert:
                    return {
                        "loaded": False,
                        "cn": None,
                        "not_after": None,
                        "expires_utc": None,
                        "error": "No certificate",
                    }
                subject = dict(x[0] for x in cert.get("subject", []))
                cn = subject.get("commonName")

                # cert["notAfter"] like 'Oct 29 04:00:00 2027 GMT'
                not_after = cert.get("notAfter")
                expires_utc = None
                if not_after:
                    try:
                        dt = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
                        dt = dt.replace(tzinfo=timezone.utc)
                        expires_utc = dt.isoformat()
                    except Exception:  # pylint: disable=broad-exception-caught
                        expires_utc = None

                return {
                    "loaded": True,
                    "cn": cn,
                    "not_after": not_after,
                    "expires_utc": expires_utc,
                    "error": None,
                }
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return {
            "loaded": False,
            "cn": None,
            "not_after": None,
            "expires_utc": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _test_tls(
    host: str,
    port: int,
    timeout: float,
    verify_ssl: bool,
) -> Dict[str, Any]:
    """
    Try a TLS handshake and pull cert CN + notAfter + issuer + serial number.
    Returns:
      {
        "loaded": bool,
        "cn": str|None,
        "not_after": str|None,
        "expires_utc": str|None,
        "issuer": str|None,
        "serial_number": str|None,
        "error": str|None
      }
    """
    if verify_ssl:
        context = ssl.create_default_context()
    else:
        context = ssl._create_unverified_context()

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            # SNI with server_hostname is fine even when verify_ssl=False
            with context.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
                if not cert:
                    return {
                        "loaded": False,
                        "cn": None,
                        "not_after": None,
                        "expires_utc": None,
                        "issuer": None,
                        "serial_number": None,
                        "error": "No certificate",
                    }

                # Subject CN
                subject = dict(x[0] for x in cert.get("subject", []))
                cn = subject.get("commonName")

                # Issuer (flattened)
                issuer_kv = dict(x[0] for x in cert.get("issuer", []))
                issuer = (
                    ", ".join(f"{k}={v}" for k, v in issuer_kv.items())
                    if issuer_kv
                    else None
                )

                # Serial number (hex string, if provided by Python/OpenSSL)
                serial_number = cert.get("serialNumber")

                # notAfter + ISO8601 UTC
                not_after = cert.get("notAfter")
                expires_utc = None
                if not_after:
                    try:
                        dt = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
                        dt = dt.replace(tzinfo=timezone.utc)
                        expires_utc = dt.isoformat()
                    except Exception:  # pylint: disable=broad-exception-caught
                        expires_utc = None

                return {
                    "loaded": True,
                    "cn": cn,
                    "not_after": not_after,
                    "expires_utc": expires_utc,
                    "issuer": issuer,
                    "serial_number": serial_number,
                    "error": None,
                }

    except Exception as exc:  # pylint: disable=broad-exception-caught
        return {
            "loaded": False,
            "cn": None,
            "not_after": None,
            "expires_utc": None,
            "issuer": None,
            "serial_number": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _process_host(
    hostname: str,
    info: Dict[str, Any],
    *,
    timeout: float,
    tls_ports: List[int],
    verify_ssl: bool,
) -> Dict[str, Any]:
    """
    Returns:
      {
        "ports": {
          "80":  {"open": true,  "error": null},
          "443": {
            "open": true,
            "error": null,
            "tls": {
                "loaded": true,
                "cn": "...",
                "not_after": "...",
                "expires_utc": "..."}
            },
          "2208":{"open": false, "error": "ConnectionRefusedError: ..."}
        },
        "failed_ports": [2208, ...]
      }
    """
    results: Dict[str, Any] = {"ports": {}, "failed_ports": []}
    ports: List[int] = list(info.get("ports", []))

    for port in ports:
        open_ok, err = _test_tcp(hostname, port, timeout)
        port_key = str(port)
        entry: Dict[str, Any] = {"open": open_ok, "error": err}
        if open_ok and port in tls_ports:
            entry["tls"] = _test_tls(hostname, port, timeout, verify_ssl)
            # consider TLS "failed" if handshake didn't load a cert
            if not entry["tls"].get("loaded"):
                results["failed_ports"].append(port)
        elif not open_ok:
            results["failed_ports"].append(port)
        results["ports"][port_key] = entry

    return results


@shared_task(name="porttest.run")
def porttest_run(
    data: Dict[str, Any],
    **options,
) -> Dict[str, Any]:
    """
    Check TCP connectivity for each host:port (and TLS on selected ports).

    Args:
      data: {
        "<host>": {
          "ports": [80, 443, 2208, 2209, 8011, 8012],
          ... (other fields ignored)
        },
        ...
      }
    Kwargs (optional):
      timeout: float = 5.0
      verify_ssl: bool = True
      tls_ports: List[int] = [443, 2209, 8012]
      max_workers: int = 10

    Returns:
      {
        "summary": {
          "hosts": N,
          "ports_checked": M,
          "duration_sec": float,
          "tls_ports": [...],
          "verify_ssl": bool
        },
        "results": {
          "<host>": {
            "ports": {
              "80": {"open": true, "error": null},
              "443": {"open": true, "error": null, "tls": {...}}
            },
            "failed_ports": [ ... ]
          }, ...
        }
      }
    """
    timeout = float(options.get("timeout", 5.0))
    verify_ssl = bool(options.get("verify_ssl", True))
    tls_ports: List[int] = list(options.get("tls_ports", [443, 2209, 8012]))
    max_workers = int(options.get("max_workers", 10))

    started = time.time()
    results: Dict[str, Any] = {}
    ports_checked = 0

    # fan-out with a thread pool (I/O bound)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for host, info in data.items():
            ports_checked += len(info.get("ports", []))
            futures[
                pool.submit(
                    _process_host,
                    host,
                    info,
                    timeout=timeout,
                    tls_ports=tls_ports,
                    verify_ssl=verify_ssl,
                )
            ] = host

        for fut in as_completed(futures):
            host = futures[fut]
            try:
                results[host] = fut.result()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                results[host] = {
                    "ports": {},
                    "failed_ports": [],
                    "error": f"{type(exc).__name__}: {exc}",
                }

    return {
        "summary": {
            "hosts": len(data),
            "ports_checked": ports_checked,
            "duration_sec": round(time.time() - started, 2),
            "tls_ports": tls_ports,
            "verify_ssl": verify_ssl,
        },
        "results": results,
    }
