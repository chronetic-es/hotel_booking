from datetime import date

from instance import mcp
from db import obtener_conexion_db
from validators import validar_fechas, validar_telefono, formatear_precio
from config import PRECIO_DESAYUNO_POR_NOCHE, PRECIO_TRANSPORTE_AEROPUERTO


def _parse_ids(raw: str, label: str) -> tuple[list[int], str]:
    """Parses a comma-separated string of ints. Returns (list, error_str)."""
    try:
        ids = [int(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError:
        return [], f"{label} debe ser una lista de IDs separados por coma (ej: '2,3')."
    if not ids:
        return [], f"Debe indicar al menos un tipo de habitación en {label}."
    return ids, ""


def _parse_mask(raw: str, expected_len: int) -> tuple[list[int], str]:
    """Parses an optional 0/1 mask string. Returns (mask_list, error_str)."""
    if not raw.strip():
        return [0] * expected_len, ""
    try:
        mask = [int(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError:
        return [], "extra_beds_mask debe ser una lista de '0' o '1' separados por coma."
    if len(mask) != expected_len:
        return [], "extra_beds_mask debe tener el mismo número de elementos que tipos_habitacion_ids."
    return mask, ""


def _calcular_total(
    tipo_rows: list,  # list of dicts with base_price, extra_bed_price; indexed same as mask
    mask: list[int],
    noches: int,
    desayuno: bool,
    transporte: bool,
) -> float:
    """Calculates booking total from room types, extra bed mask, and add-ons."""
    total = 0.0
    num_habitaciones = len(tipo_rows)
    for i, tipo in enumerate(tipo_rows):
        total += noches * float(tipo["base_price"])
        if mask[i] and tipo["extra_bed_price"] is not None:
            total += noches * float(tipo["extra_bed_price"])
    if desayuno:
        total += noches * PRECIO_DESAYUNO_POR_NOCHE * num_habitaciones
    if transporte:
        total += PRECIO_TRANSPORTE_AEROPUERTO
    return total


@mcp.tool()
async def crear_reserva(
    nombre_completo: str,
    telefono: str,
    fecha_entrada: str,
    fecha_salida: str,
    tipos_habitacion_ids: str,
    extra_beds_mask: str = "",
    desayuno: bool = False,
    transporte: bool = False,
) -> str:
    """Crea la reserva y asigna las habitaciones físicas.
    tipos_habitacion_ids: IDs separados por coma, uno por habitación (ej: '2,3').
    extra_beds_mask: '0' o '1' por habitación indicando cama supletoria (ej: '1,0'). Vacío = sin camas supletorias.
    Acepta opciones de desayuno incluido y transporte desde el aeropuerto."""
    error_fecha = validar_fechas(fecha_entrada, fecha_salida)
    if error_fecha:
        return error_fecha

    error_tel = validar_telefono(telefono)
    if error_tel:
        return error_tel

    if not nombre_completo.strip():
        return "El nombre completo es obligatorio."

    ids, err = _parse_ids(tipos_habitacion_ids, "tipos_habitacion_ids")
    if err:
        return err

    mask, err = _parse_mask(extra_beds_mask, len(ids))
    if err:
        return err

    conn = await obtener_conexion_db()
    try:
        d_entrada = date.fromisoformat(fecha_entrada)
        d_salida = date.fromisoformat(fecha_salida)
        noches = (d_salida - d_entrada).days

        async with conn.transaction():
            # Validate all room types and check extra_bed rules
            tipo_rows = []
            for i, tipo_id in enumerate(ids):
                tipo = await conn.fetchrow(
                    "SELECT id, name, base_price, extra_bed_available, extra_bed_price FROM RoomTypes WHERE id = $1",
                    tipo_id,
                )
                if not tipo:
                    return f"No encontré el tipo de habitación con id {tipo_id}. Puede consultar las opciones disponibles."
                if mask[i] and not tipo["extra_bed_available"]:
                    return f"La {tipo['name']} no admite cama supletoria."
                tipo_rows.append(tipo)

            # Find one available physical room per slot (excluding already claimed in this transaction)
            claimed_ids: list[int] = []
            habitacion_ids: list[int] = []
            for i, tipo in enumerate(tipo_rows):
                room_id = await conn.fetchval(
                    """
                    SELECT id FROM Rooms WHERE room_type_id = $1
                    AND NOT (id = ANY($4::int[]))
                    AND id NOT IN (
                        SELECT ra.room_id FROM RoomAssignments ra JOIN Bookings b ON ra.booking_id = b.id
                        WHERE (b.check_in_date, b.check_out_date) OVERLAPS ($2, $3)
                        AND b.status != 'Cancelled'
                    ) LIMIT 1
                    """,
                    tipo["id"], d_entrada, d_salida, claimed_ids,
                )
                if not room_id:
                    return f"Lo siento, no quedan habitaciones disponibles de tipo {tipo['name']} para las fechas indicadas."
                claimed_ids.append(room_id)
                habitacion_ids.append(room_id)

            total = _calcular_total(tipo_rows, mask, noches, desayuno, transporte)

            u_id = await conn.fetchval(
                """
                INSERT INTO Users (full_name, phone) VALUES ($1, $2)
                ON CONFLICT (phone) DO UPDATE SET full_name = $1 RETURNING id
                """,
                nombre_completo.strip(),
                telefono,
            )

            b_id = await conn.fetchval(
                """
                INSERT INTO Bookings (user_id, check_in_date, check_out_date, total_amount, status, desayuno_incluido, transporte_aeropuerto)
                VALUES ($1, $2, $3, $4, 'Confirmed', $5, $6) RETURNING id
                """,
                u_id, d_entrada, d_salida, total, desayuno, transporte,
            )

            for room_id, has_extra_bed in zip(habitacion_ids, mask):
                await conn.execute(
                    "INSERT INTO RoomAssignments (booking_id, room_id, extra_bed) VALUES ($1, $2, $3)",
                    b_id, room_id, bool(has_extra_bed),
                )

            # Build confirmation text
            habitaciones_desc = []
            for i, tipo in enumerate(tipo_rows):
                desc = tipo["name"]
                if mask[i]:
                    desc += " (con cama supletoria)"
                habitaciones_desc.append(desc)
            rooms_str = ", ".join(habitaciones_desc)

            extras_desc = []
            if desayuno:
                extras_desc.append("desayuno incluido")
            if transporte:
                extras_desc.append("transporte desde el aeropuerto")
            extras_str = ", con " + " y ".join(extras_desc) if extras_desc else ""

            num_hab = len(ids)
            return (
                f"Reserva confirmada. Número de reserva: {b_id}. "
                f"Nombre: {nombre_completo.strip()}. "
                f"{num_hab} habitación{'es' if num_hab > 1 else ''}: {rooms_str}{extras_str}. "
                f"Entrada el {fecha_entrada}, salida el {fecha_salida}. "
                f"Total: {formatear_precio(total)}."
            )
    except Exception:
        return "No se pudo completar la reserva. Por favor, inténtelo de nuevo."
    finally:
        await conn.close()


@mcp.tool()
async def obtener_reservas_cliente(telefono: str) -> str:
    """Devuelve todas las reservas activas de un cliente dado su número de teléfono."""
    error_tel = validar_telefono(telefono)
    if error_tel:
        return error_tel

    conn = await obtener_conexion_db()
    try:
        # Fetch bookings without room join to avoid duplicate rows
        bookings = await conn.fetch(
            """
            SELECT b.id, b.check_in_date, b.check_out_date, b.total_amount,
                   b.desayuno_incluido, b.transporte_aeropuerto
            FROM Bookings b
            JOIN Users u ON b.user_id = u.id
            WHERE u.phone = $1 AND b.status NOT IN ('Cancelled', 'Completed')
            ORDER BY b.check_in_date
            """,
            telefono,
        )

        if not bookings:
            return "No encontré reservas activas para ese número de teléfono."

        partes = []
        for b in bookings:
            # Fetch room assignments for this booking
            asignaciones = await conn.fetch(
                """
                SELECT rt.name AS tipo, ra.extra_bed
                FROM RoomAssignments ra
                JOIN Rooms r ON ra.room_id = r.id
                JOIN RoomTypes rt ON r.room_type_id = rt.id
                WHERE ra.booking_id = $1
                ORDER BY rt.base_price
                """,
                b["id"],
            )
            rooms_desc = []
            for a in asignaciones:
                desc = a["tipo"]
                if a["extra_bed"]:
                    desc += " (cama supletoria)"
                rooms_desc.append(desc)
            rooms_str = ", ".join(rooms_desc) if rooms_desc else "sin habitación asignada"

            extras = []
            if b["desayuno_incluido"]:
                extras.append("desayuno incluido")
            if b["transporte_aeropuerto"]:
                extras.append("transporte aeropuerto")
            extras_str = ", " + " y ".join(extras) if extras else ""

            num_hab = len(asignaciones)
            partes.append(
                f"Reserva número {b['id']}: {num_hab} habitación{'es' if num_hab > 1 else ''} "
                f"({rooms_str}){extras_str}, "
                f"entrada el {b['check_in_date']}, salida el {b['check_out_date']}, "
                f"total {formatear_precio(float(b['total_amount']))}."
            )

        plural = len(bookings) != 1
        return (
            f"Tiene {len(bookings)} reserva{'s' if plural else ''} activa{'s' if plural else ''}. "
            + " ".join(partes)
        )
    finally:
        await conn.close()


@mcp.tool()
async def modificar_reserva(
    reserva_id: int,
    nueva_fecha_entrada: str = "",
    nueva_fecha_salida: str = "",
    nuevo_tipos_habitacion_ids: str = "",
    nuevo_extra_beds_mask: str = "",
    nuevo_desayuno: str = "",
    nuevo_transporte: str = "",
) -> str:
    """Modifica las fechas, los tipos de habitación, las camas supletorias o los servicios adicionales de una reserva.
    nuevo_tipos_habitacion_ids: nueva lista de tipos separados por coma (ej: '2,3'). Vacío = sin cambio.
    nuevo_extra_beds_mask: nueva máscara de camas supletorias (ej: '1,0'). Vacío = sin cambio.
    nuevo_desayuno: 'true' o 'false' para cambiar el desayuno incluido. Vacío = sin cambio.
    nuevo_transporte: 'true' o 'false' para cambiar el transporte desde el aeropuerto. Vacío = sin cambio."""
    cambios = any([
        nueva_fecha_entrada, nueva_fecha_salida, nuevo_tipos_habitacion_ids,
        nuevo_extra_beds_mask, nuevo_desayuno != "", nuevo_transporte != "",
    ])
    if not cambios:
        return "Debe indicar al menos un cambio."

    conn = await obtener_conexion_db()
    try:
        # Fetch booking status and services
        reserva = await conn.fetchrow(
            "SELECT id, check_in_date, check_out_date, status, desayuno_incluido, transporte_aeropuerto FROM Bookings WHERE id = $1",
            reserva_id,
        )
        if not reserva:
            return f"No encontré ninguna reserva con el número {reserva_id}."
        if reserva["status"] in ("Cancelled", "Completed"):
            return f"La reserva número {reserva_id} está {reserva['status'].lower()} y no puede modificarse."

        # Fetch current room assignments
        asignaciones_actuales = await conn.fetch(
            """
            SELECT ra.room_id, ra.extra_bed, rt.id AS tipo_id, rt.name AS tipo_nombre,
                   rt.base_price, rt.extra_bed_available, rt.extra_bed_price
            FROM RoomAssignments ra
            JOIN Rooms r ON ra.room_id = r.id
            JOIN RoomTypes rt ON r.room_type_id = rt.id
            WHERE ra.booking_id = $1
            ORDER BY rt.base_price
            """,
            reserva_id,
        )

        f_entrada = nueva_fecha_entrada if nueva_fecha_entrada else str(reserva["check_in_date"])
        f_salida = nueva_fecha_salida if nueva_fecha_salida else str(reserva["check_out_date"])

        error_fecha = validar_fechas(f_entrada, f_salida)
        if error_fecha:
            return error_fecha

        d_entrada = date.fromisoformat(f_entrada)
        d_salida = date.fromisoformat(f_salida)
        noches = (d_salida - d_entrada).days

        desayuno = (nuevo_desayuno.lower() == "true") if nuevo_desayuno != "" else reserva["desayuno_incluido"]
        transporte = (nuevo_transporte.lower() == "true") if nuevo_transporte != "" else reserva["transporte_aeropuerto"]

        changing_rooms = bool(nuevo_tipos_habitacion_ids.strip())
        changing_mask = bool(nuevo_extra_beds_mask.strip())

        async with conn.transaction():
            if changing_rooms:
                # Parse new room types and mask
                ids, err = _parse_ids(nuevo_tipos_habitacion_ids, "nuevo_tipos_habitacion_ids")
                if err:
                    return err
                mask, err = _parse_mask(nuevo_extra_beds_mask, len(ids))
                if err:
                    return err

                # Validate types and extra bed rules
                tipo_rows = []
                for i, tipo_id in enumerate(ids):
                    tipo = await conn.fetchrow(
                        "SELECT id, name, base_price, extra_bed_available, extra_bed_price FROM RoomTypes WHERE id = $1",
                        tipo_id,
                    )
                    if not tipo:
                        return f"No encontré el tipo de habitación con id {tipo_id}."
                    if mask[i] and not tipo["extra_bed_available"]:
                        return f"La {tipo['name']} no admite cama supletoria."
                    tipo_rows.append(tipo)

                # Find available rooms (excluding current booking)
                claimed_ids: list[int] = []
                habitacion_ids: list[int] = []
                for i, tipo in enumerate(tipo_rows):
                    room_id = await conn.fetchval(
                        """
                        SELECT id FROM Rooms WHERE room_type_id = $1
                        AND NOT (id = ANY($5::int[]))
                        AND id NOT IN (
                            SELECT ra.room_id FROM RoomAssignments ra JOIN Bookings b ON ra.booking_id = b.id
                            WHERE b.id != $4
                            AND (b.check_in_date, b.check_out_date) OVERLAPS ($2, $3)
                            AND b.status != 'Cancelled'
                        ) LIMIT 1
                        """,
                        tipo["id"], d_entrada, d_salida, reserva_id, claimed_ids,
                    )
                    if not room_id:
                        return f"No hay habitaciones disponibles de tipo {tipo['name']} para las fechas indicadas."
                    claimed_ids.append(room_id)
                    habitacion_ids.append(room_id)

                total = _calcular_total(tipo_rows, mask, noches, desayuno, transporte)

                await conn.execute("DELETE FROM RoomAssignments WHERE booking_id = $1", reserva_id)
                for room_id, has_extra_bed in zip(habitacion_ids, mask):
                    await conn.execute(
                        "INSERT INTO RoomAssignments (booking_id, room_id, extra_bed) VALUES ($1, $2, $3)",
                        reserva_id, room_id, bool(has_extra_bed),
                    )

                habitaciones_desc = []
                for i, tipo in enumerate(tipo_rows):
                    desc = tipo["name"]
                    if mask[i]:
                        desc += " (con cama supletoria)"
                    habitaciones_desc.append(desc)
                rooms_str = ", ".join(habitaciones_desc)
                num_hab = len(ids)

            elif changing_mask:
                # Only changing extra_bed flags on existing room assignments
                mask, err = _parse_mask(nuevo_extra_beds_mask, len(asignaciones_actuales))
                if err:
                    return err

                tipo_rows = list(asignaciones_actuales)
                for i, (a, has_extra_bed) in enumerate(zip(asignaciones_actuales, mask)):
                    if has_extra_bed and not a["extra_bed_available"]:
                        return f"La {a['tipo_nombre']} no admite cama supletoria."

                # Check availability for date changes (current rooms must still be free)
                if nueva_fecha_entrada or nueva_fecha_salida:
                    for a in asignaciones_actuales:
                        conflict = await conn.fetchval(
                            """
                            SELECT COUNT(*) FROM RoomAssignments ra JOIN Bookings b ON ra.booking_id = b.id
                            WHERE ra.room_id = $1 AND b.id != $4
                            AND (b.check_in_date, b.check_out_date) OVERLAPS ($2, $3)
                            AND b.status != 'Cancelled'
                            """,
                            a["room_id"], d_entrada, d_salida, reserva_id,
                        )
                        if conflict:
                            return f"Una de las habitaciones asignadas no está disponible para las nuevas fechas."

                total = _calcular_total(tipo_rows, mask, noches, desayuno, transporte)

                # Update extra_bed flags on existing assignments
                for a, has_extra_bed in zip(asignaciones_actuales, mask):
                    await conn.execute(
                        "UPDATE RoomAssignments SET extra_bed = $1 WHERE booking_id = $2 AND room_id = $3",
                        bool(has_extra_bed), reserva_id, a["room_id"],
                    )

                rooms_desc = []
                for a, has_extra_bed in zip(asignaciones_actuales, mask):
                    desc = a["tipo_nombre"]
                    if has_extra_bed:
                        desc += " (con cama supletoria)"
                    rooms_desc.append(desc)
                rooms_str = ", ".join(rooms_desc)
                num_hab = len(asignaciones_actuales)

            else:
                # Only dates/services changed — keep existing rooms, check availability if dates changed
                mask = [1 if a["extra_bed"] else 0 for a in asignaciones_actuales]
                tipo_rows = list(asignaciones_actuales)

                if nueva_fecha_entrada or nueva_fecha_salida:
                    for a in asignaciones_actuales:
                        conflict = await conn.fetchval(
                            """
                            SELECT COUNT(*) FROM RoomAssignments ra JOIN Bookings b ON ra.booking_id = b.id
                            WHERE ra.room_id = $1 AND b.id != $4
                            AND (b.check_in_date, b.check_out_date) OVERLAPS ($2, $3)
                            AND b.status != 'Cancelled'
                            """,
                            a["room_id"], d_entrada, d_salida, reserva_id,
                        )
                        if conflict:
                            return f"Una de las habitaciones asignadas no está disponible para las nuevas fechas."

                total = _calcular_total(tipo_rows, mask, noches, desayuno, transporte)

                rooms_desc = []
                for a in asignaciones_actuales:
                    desc = a["tipo_nombre"]
                    if a["extra_bed"]:
                        desc += " (con cama supletoria)"
                    rooms_desc.append(desc)
                rooms_str = ", ".join(rooms_desc)
                num_hab = len(asignaciones_actuales)

            await conn.execute(
                """
                UPDATE Bookings
                SET check_in_date = $1, check_out_date = $2, total_amount = $3,
                    desayuno_incluido = $4, transporte_aeropuerto = $5
                WHERE id = $6
                """,
                d_entrada, d_salida, total, desayuno, transporte, reserva_id,
            )

        extras = []
        if desayuno:
            extras.append("desayuno incluido")
        if transporte:
            extras.append("transporte aeropuerto")
        extras_str = ", con " + " y ".join(extras) if extras else ""

        return (
            f"La reserva número {reserva_id} ha sido actualizada. "
            f"{num_hab} habitación{'es' if num_hab > 1 else ''}: {rooms_str}{extras_str}. "
            f"Nueva entrada el {f_entrada}, salida el {f_salida}. "
            f"Nuevo total: {formatear_precio(total)}."
        )
    except Exception:
        return "No se pudo modificar la reserva. Por favor, inténtelo de nuevo."
    finally:
        await conn.close()


@mcp.tool()
async def cancelar_reserva(reserva_id: int) -> str:
    """Cancela una reserva dado su identificador."""
    conn = await obtener_conexion_db()
    try:
        estado = await conn.fetchval(
            "SELECT status FROM Bookings WHERE id = $1", reserva_id
        )

        if estado is None:
            return f"No encontré ninguna reserva con el número {reserva_id}."
        if estado == "Cancelled":
            return f"La reserva número {reserva_id} ya estaba cancelada."
        if estado == "Completed":
            return f"La reserva número {reserva_id} ya está completada y no puede cancelarse."

        await conn.execute("UPDATE Bookings SET status = 'Cancelled' WHERE id = $1", reserva_id)
        return f"La reserva número {reserva_id} ha sido cancelada correctamente."
    finally:
        await conn.close()
