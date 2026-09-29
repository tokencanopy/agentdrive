"""Pure redaction helpers shared by application and server access logs."""

_SECRET_TERMINATORS = frozenset('/?#\t\n\r "')


def redact_share_path(path: str, mount_prefix: str = "") -> str:
    """Replace a share credential segment while preserving the rest of the path.

    Bare public routes remain accepted when a mount is configured, so both
    `/s/{secret}` and `{mount}/s/{secret}` must be recognized. A mount is
    considered only when configured; this avoids treating an unrelated
    `/drive/s/...` path as a share URL in the empty-mount deployment.
    """
    prefixes = ["/s/"]
    normalized_mount = mount_prefix.rstrip("/")
    if normalized_mount:
        prefixes.append(f"{normalized_mount}/s/")

    # Longest first handles the otherwise ambiguous (though unusual) case
    # where the configured mount itself begins with `/s`.
    for prefix in sorted(prefixes, key=len, reverse=True):
        if not path.startswith(prefix):
            continue
        secret_end = len(prefix)
        while secret_end < len(path) and path[secret_end] not in _SECRET_TERMINATORS:
            secret_end += 1
        if secret_end == len(prefix):
            continue
        return f"{prefix}<redacted>{path[secret_end:]}"
    return path
