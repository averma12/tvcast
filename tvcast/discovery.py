"""Find UPnP/DLNA media renderers (TVs) on the local network and describe them as TV records.

A TV record is a JSON-able dict: id (the renderer's UDN), name, manufacturer, model, ip (host of
the description URL), location (the description URL) and services, which maps "AVTransport" and,
when the TV has it, "RenderingControl" to {"type": exact serviceType, "url": absolute control URL}.
"""
import http.client
import socket
import time
import urllib.parse
import urllib.request

from .upnp import MAX_XML, log, parse_xml

SSDP_ADDR = ("239.255.255.250", 1900)
SEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: %s:%d\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 1\r\n"  # devices answer after a random delay of up to MX seconds
    "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
    "\r\n" % SSDP_ADDR
).encode()
SEARCHES = 3  # SSDP is plain UDP, so repeat the search across the window to ride out lost packets
DESCRIBE_TIMEOUT = 3
NS = {"d": "urn:schemas-upnp-org:device-1-0"}
SERVICES = ("AVTransport", "RenderingControl")


def discover(timeout=3):
    """Search the network for `timeout` seconds; return the TV records found, sorted by name.

    A device whose description can't be read is logged and skipped, so it can't hide the others.
    """
    tvs = {}
    for location in sorted(search(timeout)):
        try:
            tv = describe(location)
        except RuntimeError as e:
            log("skipping a device found on the network: %s" % e)
            continue
        tvs[tv["id"]] = tv
    return sorted(tvs.values(), key=lambda tv: tv["name"].lower())


def search(timeout):
    """Send SSDP M-SEARCHes for MediaRenderers; return the description URLs that answer."""
    locations = set()
    start = time.monotonic()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for i in range(1, SEARCHES + 1):
            try:
                sock.sendto(SEARCH, SSDP_ADDR)
            except OSError as e:
                # macOS Local Network privacy fails LAN sends with EHOSTUNREACH ("No route to
                # host") until the user allows the "find devices on your local network" alert
                # that the first attempt raises.
                raise RuntimeError(
                    "SSDP search on %s:%d failed: %s; if macOS asked to allow local network"
                    " access, allow it and search again" % (SSDP_ADDR + (e,)))
            # Listen until the next search is due.
            while True:
                left = start + timeout * i / SEARCHES - time.monotonic()
                if left <= 0:
                    break
                sock.settimeout(left)
                try:
                    location = ssdp_location(sock.recv(65535))
                except socket.timeout:
                    break
                if location:
                    locations.add(location)
    return locations


def ssdp_location(response):
    """The LOCATION header of an SSDP response (bytes), or None."""
    for line in response.decode("utf-8", "replace").splitlines()[1:]:
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == "location":
            return value.strip()
    return None


def describe(location, timeout=DESCRIBE_TIMEOUT):
    """Fetch the device description at `location`; return the TV record of the MediaRenderer in
    it, which is either the root device or one embedded in its deviceList."""
    # urlopen also opens file:// and ftp:// URLs, and every URL here comes from a device on the
    # network; only plain HTTP is UPnP.
    if urllib.parse.urlsplit(location).scheme != "http":
        raise RuntimeError("device description URL %s is not an http:// URL" % location)
    try:
        with urllib.request.urlopen(location, timeout=timeout) as resp:
            text = resp.read(MAX_XML).decode("utf-8-sig")  # UPnP mandates UTF-8
    except (OSError, ValueError, http.client.HTTPException) as e:
        raise RuntimeError("reading device description %s failed: %s" % (location, e))
    root = parse_xml(text, "device description %s" % location)
    base = _text(root, "d:URLBase") or location
    host = urllib.parse.urlsplit(location).hostname
    for device in root.iter("{%s}device" % NS["d"]):
        if ":device:MediaRenderer:" not in _text(device, "d:deviceType"):
            continue
        services = {}
        for service in device.findall("d:serviceList/d:service", NS):
            service_type = _text(service, "d:serviceType")  # urn:schemas-upnp-org:service:<Name>:<version>
            name = service_type.split(":")[3] if service_type.startswith("urn:schemas-upnp-org:service:") else ""
            if name in SERVICES:
                url = urllib.parse.urljoin(base, _text(service, "d:controlURL"))
                # Any device on the LAN can answer a search with any description, so control URLs
                # must stay on the device's own host; otherwise it could aim this Mac's SOAP
                # requests (stream URLs and all) at another machine.
                parts = urllib.parse.urlsplit(url)
                if parts.scheme != "http" or parts.hostname != host:
                    raise RuntimeError("device description %s gives control URL %s, which is not http:// on %s"
                                       % (location, url, host))
                services[name] = {"type": service_type, "url": url}
        if "AVTransport" not in services:
            continue
        udn = _text(device, "d:UDN")
        if not udn:
            raise RuntimeError("device description %s gives the MediaRenderer no UDN" % location)
        return {
            "id": udn,
            "name": _text(device, "d:friendlyName"),
            "manufacturer": _text(device, "d:manufacturer"),
            "model": _text(device, "d:modelName"),
            "ip": host,
            "location": location,
            "services": services,
        }
    raise RuntimeError("device %s is not a UPnP MediaRenderer with an AVTransport service" % location)


def _text(element, path):
    return element.findtext(path, "", NS).strip()
