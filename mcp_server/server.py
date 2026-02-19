import httpx
import asyncpg
import os
from dotenv import load_dotenv
from datetime import date
from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from pydantic import AnyHttpUrl


load_dotenv()


mcp = FastMCP("HotelBookings")


async def obtener_conexion_db():
    return await asyncpg.connect(os.getenv("DATABASE_URL"))

@mcp.tool()
async def obtener_opciones_habitacion() -> str:
    """Lista los tipos de habitaciones disponibles y sus precios."""
    conn = await obtener_conexion_db()
    try:
        filas = await conn.fetch("SELECT name, base_price, description FROM RoomTypes")
        opciones = [f"{f['name']} (${f['base_price']}/noche): {f['description']}" for f in filas]
        return "Opciones disponibles:\n" + "\n".join(opciones)
    finally:
        await conn.close()

@mcp.tool()
async def verificar_disponibilidad(fecha_entrada: str, fecha_salida: str, tipo_habitacion: str) -> str:
    """
    Verifica si hay habitaciones disponibles de un tipo específico para ciertas fechas.
    Las fechas deben estar en formato YYYY-MM-DD.
    """
    conn = await obtener_conexion_db()
    try:
        # 1. Buscar el ID del tipo de habitación (busqueda flexible por nombre)
        tipo = await conn.fetchrow(
            "SELECT id FROM RoomTypes WHERE name ILIKE $1", f"%{tipo_habitacion}%"
        )
        if not tipo:
            return f"No pude encontrar el tipo de habitación: {tipo_habitacion}."

        # 2. Consulta para ver cuántas habitaciones NO están reservadas en ese rango
        query = """
            SELECT COUNT(*) FROM Rooms r
            WHERE r.room_type_id = $1
            AND r.id NOT IN (
                SELECT ra.room_id FROM RoomAssignments ra
                JOIN Bookings b ON ra.booking_id = b.id
                WHERE (b.check_in_date, b.check_out_date) OVERLAPS ($2::date, $3::date)
                AND b.status != 'Cancelled'
            )
        """
        cantidad = await conn.fetchval(query, tipo['id'], fecha_entrada, fecha_salida)

        if cantidad > 0:
            return f"Sí, tenemos {cantidad} habitaciones de tipo {tipo_habitacion} disponibles."
        return f"Lo siento, la habitación {tipo_habitacion} está agotada para esas fechas."
    finally:
        await conn.close()

@mcp.tool()
async def crear_reserva(nombre_completo: str, email: str, fecha_entrada: str, fecha_salida: str, tipo_habitacion: str) -> str:
    """Finaliza y guarda la reserva en la base de datos."""
    conn = await obtener_conexion_db()
    try:
        async with conn.transaction():
            # 1. Asegurar que el usuario existe (basado en el email)
            usuario_id = await conn.fetchval(
                """INSERT INTO Users (full_name, email) VALUES ($1, $2)
                   ON CONFLICT (email) DO UPDATE SET full_name = $1 RETURNING id""",
                nombre_completo, email
            )

            # 2. Obtener el ID del tipo de habitación
            tipo_id = await conn.fetchval("SELECT id FROM RoomTypes WHERE name ILIKE $1", f"%{tipo_habitacion}%")

            # 3. Crear la reserva
            reserva_id = await conn.fetchval(
                """INSERT INTO Bookings (user_id, check_in_date, check_out_date, status)
                   VALUES ($1, $2, $3, 'Confirmed') RETURNING id""",
                usuario_id, date.fromisoformat(fecha_entrada), date.fromisoformat(fecha_salida)
            )

            return f"¡Éxito! La reserva #{reserva_id} ha sido confirmada para {nombre_completo}."
    except Exception as e:
        return f"Error al crear la reserva: {str(e)}"
    finally:
        await conn.close()

@mcp.tool()
async def get_weather(city: str) -> str:
    """Get the current temperature for a city."""
    geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1"

    async with httpx.AsyncClient() as client:
        geo_res = await client.get(geo_url)
        data = geo_res.json()

        if not data.get("results"):
            return f"Error: Could not find city {city}."

        lat = data["results"][0]["latitude"]
        lon = data["results"][0]["longitude"]

        weather_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
        weather_res = await client.get(weather_url)
        temp = weather_res.json()["current_weather"]["temperature"]

        return f"The current temperature in {city} is {temp}°C."

app = mcp.http_app()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["mcp-session-id"],
    allow_credentials=False,
)
