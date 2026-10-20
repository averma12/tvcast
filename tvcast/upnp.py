"""UPnP control of a TV's media renderer (SOAP), Samsung's caption extensions, and shared helpers.

Every call takes a TV record from discovery; SOAP goes to the control URL and exact
serviceType the TV advertised, since both vary between makes and service versions.
"""
import contextlib
import html
import http.client
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

SOAP_TIMEOUT = 8
MAX_XML = 1 << 20  # bytes read of a device's XML; real descriptions and replies are a few KB
DLNA_FEATURES = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"


def log(message):
    print("%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


def hms(seconds):
    if seconds is None:
        return "?"
    seconds = int(seconds)
    return "%d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60, seconds % 60)


def seconds_of(text):
    """Parse a UPnP H:MM:SS[.fff] time; None for NOT_IMPLEMENTED or a missing value."""
    m = re.fullmatch(r"(\d+):(\d\d):(\d\d(?:\.\d+)?)", text or "")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None


def tag(xml, name):
    m = re.search(r"<%s>(.*?)</%s>" % (name, name), xml, re.S)
    return m[1] if m else None


def parse_xml(text, what):
    """Parse XML that a device on the LAN sent; what names it in errors."""
    # Entities can only be declared in a DTD, and the expat bundled with Apple's Python 3.9 has
    # no guard against entity-expansion bombs. UPnP documents never have a DTD.
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise RuntimeError("%s has a DTD, which UPnP doesn't use" % what)
    try:
        return ET.fromstring(text)
    except ET.ParseError as e:
        raise RuntimeError("%s is not valid XML: %s" % (what, e))


def soap(tv, service, action, timeout=SOAP_TIMEOUT, **args):
    """Invoke action on the TV's service and return the response XML."""
    svc = tv["services"].get(service)
    if svc is None:
        raise RuntimeError("TV %s has no %s service" % (tv["name"], service))
    body = "".join("<%s>%s</%s>" % (k, escape(str(v)), k) for k, v in args.items())
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        '<u:%s xmlns:u="%s">%s</u:%s>'
        "</s:Body></s:Envelope>" % (action, svc["type"], body, action)
    )
    req = urllib.request.Request(
        svc["url"],
        data=envelope.encode(),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": '"%s#%s"' % (svc["type"], action),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(MAX_XML).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read(MAX_XML).decode(errors="replace")
        raise RuntimeError("%s on TV %s failed: HTTP %d: %s"
                           % (action, tv["name"], e.code, tag(detail, "errorDescription") or detail.strip()))
    except (OSError, http.client.HTTPException) as e:  # URLError, timeouts, refusals, non-HTTP replies
        raise RuntimeError("%s on TV %s failed: %s" % (action, tv["name"], e))


def transport(tv, action, **args):
    return soap(tv, "AVTransport", action, InstanceID=0, **args)


def rendering(tv, action, **args):
    return soap(tv, "RenderingControl", action, InstanceID=0, Channel="Master", **args)


def transport_status(tv, timeout=SOAP_TIMEOUT):
    """The TV's transport state, position, duration and track URI."""
    pos = transport(tv, "GetPositionInfo", timeout=timeout)
    return {
        "state": tag(transport(tv, "GetTransportInfo", timeout=timeout), "CurrentTransportState"),
        "position": seconds_of(tag(pos, "RelTime")),
        "duration": seconds_of(tag(pos, "TrackDuration")),
        "uri": tag(pos, "TrackURI"),
    }


def tv_status(tv, timeout):
    """transport_status plus volume and mute. Those stay None when the TV has no RenderingControl
    or fails to report them, since an optional reading must not make the TV look unreachable."""
    status = dict(transport_status(tv, timeout), volume=None, muted=None)
    if "RenderingControl" in tv["services"]:
        with contextlib.suppress(RuntimeError):
            volume = tag(rendering(tv, "GetVolume", timeout=timeout), "CurrentVolume")
            status["volume"] = int(volume) if volume and volume.isdigit() else None
        with contextlib.suppress(RuntimeError):
            muted = tag(rendering(tv, "GetMute", timeout=timeout), "CurrentMute")
            status["muted"] = {"1": True, "0": False, "true": True, "false": False}.get(muted)
    return status


def is_samsung(tv):
    """Whether the TV is a Samsung, whose player has Samsung's DLNA extensions: X_GetCaptionState
    and X_ControlCaption to show subtitle files mid-movie, and video/x-mkv for Matroska. Other
    players (Android TV renderers such as Sony's, Philips' and Kodi) take a subtitle file only
    with the video and expect the registered video/x-matroska."""
    return "Samsung" in tv.get("manufacturer", "")


def caption_files(escaped, what):
    """Parse caption-state XML, which the TV sends HTML-escaped inside its SOAP reply,
    into X_ControlCaption arguments: one dict per subtitle file. what names it in errors."""
    if not escaped:
        return []
    files = []
    for res in parse_xml(html.unescape(escaped), what):
        for info in res.iter("captionFileInfo"):
            # Insertion order matters: UPnP arguments must follow the order in the TV's SCPD.
            files.append({
                "Name": info.get("name"),
                "ResourceURI": res.get("resUri"),
                "CaptionURI": info.get("uri"),
                "CaptionType": info.get("captionType"),
                "Language": (info.get("language") or "").strip(),
                "Encoding": info.get("encoding"),
            })
    return files


def captions(tv):
    """(available, enabled) subtitle files as a Samsung TV's player reports them."""
    x = soap(tv, "RenderingControl", "X_GetCaptionState", InstanceID=0)
    what = "caption state from TV %s" % tv["name"]
    return caption_files(tag(x, "Captions"), what), caption_files(tag(x, "EnabledCaptions"), what)


def set_caption(tv, on, media_url, caption_url, name):
    """Show or hide the SubRip file at caption_url over the video playing from media_url.

    A Samsung player registers only the first subtitle file sent with a video, but Enable makes it
    fetch and draw any caption URL at once, so this also switches files mid-movie. Disable leaves
    nothing on screen, not even tracks embedded in the video."""
    soap(tv, "RenderingControl", "X_ControlCaption", InstanceID=0, Operation="Enable" if on else "Disable",
         Name=name, ResourceURI="dlna" + media_url[len("http"):], CaptionURI=caption_url,
         CaptionType="SRT", Language="", Encoding="UTF-8")
