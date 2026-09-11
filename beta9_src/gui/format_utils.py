def format_bytes(size_bytes):
    if size_bytes is None:
        return "-"
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


ARCHIVE_SIZE_PRESETS = (
    ("1 GB", 1 * 1024 ** 3),
    ("2 GB", 2 * 1024 ** 3),
    ("5 GB", 5 * 1024 ** 3),
    ("10 GB", 10 * 1024 ** 3),
)
