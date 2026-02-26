from pathlib import Path

from instance import mcp

_INFO_FILE = Path(__file__).parent.parent / "hotel_info.txt"


@mcp.tool()
def obtener_informacion_hotel() -> str:
    """Devuelve la información general del hotel."""
    return _INFO_FILE.read_text(encoding="utf-8")
