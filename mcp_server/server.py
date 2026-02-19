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

middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=[
            "mcp-protocol-version",
            "mcp-session-id",
            "Authorization",
            "Content-Type",
        ],
        expose_headers=["mcp-session-id"],
    )
]

app = mcp.http_app(middleware=middleware)


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
async def calcular_presupuesto(fecha_entrada: str, fecha_salida: str, tipo_habitacion: str) -> str:
    """Calcula el costo total de una estancia sin realizar la reserva."""
    conn = await obtener_conexion_db()
    try:
        tipo = await conn.fetchrow("SELECT base_price FROM RoomTypes WHERE name ILIKE $1", f"%{tipo_habitacion}%")
        if not tipo:
            return f"No encontré el tipo de habitación '{tipo_habitacion}'."

        noches = calcular_noches(fecha_entrada, fecha_salida)
        total = noches * tipo['base_price']
        return f"Para {noches} noches, el total sería de ${total:.2f} ($ {tipo['base_price']} por noche)."
    finally:
        await conn.close()

@mcp.tool()
async def verificar_disponibilidad(fecha_entrada: str, fecha_salida: str, tipo_habitacion: str) -> str:
    """Verifica si hay habitaciones libres de un tipo específico."""
    conn = await obtener_conexion_db()
    try:
        tipo = await conn.fetchrow("SELECT id FROM RoomTypes WHERE name ILIKE $1", f"%{tipo_habitacion}%")
        if not tipo: return f"Tipo '{tipo_habitacion}' no válido."

        d_entrada = date.fromisoformat(fecha_entrada)
        d_salida = date.fromisoformat(fecha_salida)

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
        cantidad = await conn.fetchval(query, tipo['id'], d_entrada, d_salida)

        if cantidad > 0:
            return f"Sí, hay {cantidad} habitaciones '{tipo_habitacion}' disponibles."
        return f"Lo siento, no hay disponibilidad para {tipo_habitacion} en esas fechas."
    finally:
        await conn.close()
@mcp.tool()
async def crear_reserva(nombre_completo: str, email: str, fecha_entrada: str, fecha_salida: str, tipo_habitacion: str) -> str:
    """Crea la reserva y asigna una habitación física automáticamente."""
    conn = await obtener_conexion_db()
    try:
        async with conn.transaction():
            tipo = await conn.fetchrow("SELECT id, base_price FROM RoomTypes WHERE name ILIKE $1", f"%{tipo_habitacion}%")
            if not tipo: return "Tipo de habitación no encontrado."

            habitacion_id = await conn.fetchval("""
                SELECT id FROM Rooms WHERE room_type_id = $1 AND id NOT IN (
                    SELECT ra.room_id FROM RoomAssignments ra JOIN Bookings b ON ra.booking_id = b.id
                    WHERE (b.check_in_date, b.check_out_date) OVERLAPS ($2::date, $3::date) AND b.status != 'Cancelled'
                ) LIMIT 1
            """, tipo['id'], fecha_entrada, fecha_salida)

            if not habitacion_id: return "Ya no quedan habitaciones físicas disponibles para esas fechas."

            noches = calcular_noches(fecha_entrada, fecha_salida)
            total = noches * tipo['base_price']

            u_id = await conn.fetchval("""
                INSERT INTO Users (full_name, email) VALUES ($1, $2)
                ON CONFLICT (email) DO UPDATE SET full_name = $1 RETURNING id
            """, nombre_completo, email)

            b_id = await conn.fetchval("""
                INSERT INTO Bookings (user_id, check_in_date, check_out_date, total_amount, status)
                VALUES ($1, $2, $3, $4, 'Confirmed') RETURNING id
            """, u_id, date.fromisoformat(fecha_entrada), date.fromisoformat(fecha_salida), total)

            await conn.execute("INSERT INTO RoomAssignments (booking_id, room_id) VALUES ($1, $2)", b_id, habitacion_id)

            return f"¡Reserva #{b_id} confirmada! Habitación asignada. Total: ${total:.2f}."
    except Exception as e:
        return f"Error: {str(e)}"
    finally:
        await conn.close()

@mcp.tool()
async def cancelar_reserva(reserva_id: int, email: str) -> str:
    """Cancela una reserva existente verificando el email del cliente."""
    conn = await obtener_conexion_db()
    try:
        valido = await conn.fetchval("""
            SELECT b.id FROM Bookings b JOIN Users u ON b.user_id = u.id
            WHERE b.id = $1 AND u.email = $2
        """, reserva_id, email)

        if not valido: return "No encontré una reserva con ese ID y email."

        await conn.execute("UPDATE Bookings SET status = 'Cancelled' WHERE id = $1", reserva_id)
        return f"La reserva #{reserva_id} ha sido cancelada exitosamente."
    finally:
        await conn.close()
