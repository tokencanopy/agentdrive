"""The private viewer surface — the console's iframe, on its own origin.

The other adapter over `agentdrive.rendering` (the public surface is the
first). Serves only the credential-free shell, the viewer-session document/
byte endpoints, and an allowlisted asset set; host-gated to VIEWER_BASE_URL
by `HostSurfaceMiddleware` so the isolated origin exposes nothing else.
"""
