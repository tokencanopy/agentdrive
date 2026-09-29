"""The shared rendering seam.

One renderer (`render.py`) and one byte-safety layer (`safety.py`) behind two
adapters: the anonymous public surface (`agentdrive.public`) and the private
console viewer (`agentdrive.viewer`). The adapters own authorization and
response policy — who may see the document and which CSP/cache headers ride
on it; this package owns everything about turning untrusted artifact bytes
into something a browser can safely show: MIME classification, escaping,
size limits, PDF structure, the download fallback, filename sanitization,
and active-content handling.
"""
