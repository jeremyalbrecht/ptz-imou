"""ONVIF XML helpers: parsing, envelope building, velocity mapping."""

import os
import xml.etree.ElementTree as ET

from .constants import SOAP11, SOAP12


def find_element(root, local_name):
    """Find element by local name recursively, ignoring namespaces."""
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == local_name:
            return elem
    return None


def detect_operation(body_bytes):
    """Return (operation_name, element) from a SOAP body."""
    try:
        root = ET.fromstring(body_bytes)
    except ET.ParseError:
        return None, None
    for ns in (SOAP12, SOAP11):
        body = root.find(f"{{{ns}}}Body")
        if body is not None:
            for child in body:
                local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                return local, child
    return None, None


def soap_envelope(body_xml):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
        ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
        ' xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"'
        ' xmlns:tt="http://www.onvif.org/ver10/schema">'
        f'<s:Body>{body_xml}</s:Body>'
        '</s:Envelope>'
    )


def velocity_to_ptz(pan, tilt, zoom):
    """Map ONVIF velocity (-1..1) to DVRIP direction code and speed (1-8).

    Speed is capped at PTZ_MAX_SPEED (default 3) for small, controlled steps.
    """
    max_speed = int(os.environ.get("PTZ_MAX_SPEED", "3"))
    if abs(zoom) > 0.01:
        return (
            "ZoomTele" if zoom > 0 else "ZoomWide",
            max(1, min(max_speed, round(abs(zoom) * max_speed))),
        )
    has_pan, has_tilt = abs(pan) > 0.01, abs(tilt) > 0.01
    if has_pan and has_tilt:
        code = ("Right" if pan > 0 else "Left") + ("Up" if tilt > 0 else "Down")
    elif has_pan:
        code = "Right" if pan > 0 else "Left"
    elif has_tilt:
        code = "Up" if tilt > 0 else "Down"
    else:
        return None, 0
    return code, max(1, min(max_speed, round(max(abs(pan), abs(tilt)) * max_speed)))

