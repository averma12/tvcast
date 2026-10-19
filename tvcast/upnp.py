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
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
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
            status["muted"] = tag(rendering(tv, "GetMute", timeout=timeout), "CurrentMute") == "1"
    return status


def has_caption_control(tv):
    """Whether the TV takes Samsung's caption extensions to RenderingControl. Other makes don't
    implement X_GetCaptionState or X_ControlCaption; they ignore the sec: subtitle tags instead."""
    return "Samsung" in tv.get("manufacturer", "")


def caption_files(escaped):
    """Parse caption-state XML, which the TV sends HTML-escaped inside its SOAP reply,
    into X_ControlCaption arguments: one dict per subtitle file."""
    if not escaped:
        return []
    files = []
    for res in ET.fromstring(html.unescape(escaped)):
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
    return caption_files(tag(x, "Captions")), caption_files(tag(x, "EnabledCaptions"))


def set_subtitles(tv, on):
    available = captions(tv)[0]
    if not available:
        raise RuntimeError("TV %s has no subtitle file loaded for this video" % tv["name"])
    # Only one subtitle file is ever served, so the first entry is ours.
    soap(tv, "RenderingControl", "X_ControlCaption", InstanceID=0,
         Operation="Enable" if on else "Disable", **available[0])
