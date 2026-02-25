from datetime import date, timedelta

from instance import mcp
from db import obtener_conexion_db
from validators import validar_fechas, calcular_noches, formatear_precio
from config import PRECIO_DESAYUNO_POR_NOCHE, PRECIO_TRANSPORTE_AEROPUERTO

_DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MESES_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
             "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


@mcp.tool()
async def obtener_fecha_actual() -> str:
    """Devuelve la fecha actual con el día de la semana en español, para calcular fechas relativas."""
    hoy = date.today()
    dia_semana = _DIAS_ES[hoy.weekday()]
    mes = _MESES_ES[hoy.month - 1]
    return (
        f"Hoy es {dia_semana}, {hoy.day} de {mes} de {hoy.year}. "
        f"Fecha ISO: {hoy.isoformat()}."
    )


@mcp.tool()
async def calcular_fecha(dias_desde_hoy: int) -> str:
    """Devuelve la fecha ISO y el día de la semana correspondiente a N días desde hoy.
    Úsalo para verificar cualquier fecha relativa: mañana=1, pasado mañana=2, 1 semana=7, etc."""
    d = date.today() + timedelta(days=dias_desde_hoy)
    dia = _DIAS_ES[d.weekday()]
    mes = _MESES_ES[d.month - 1]
    return f"{dia} {d.day} de {mes} de {d.year} → {d.isoformat()}"


@mcp.tool()
async def obtener_opciones_habitacion() -> str:
    """Lista los tipos de habitaciones disponibles, su capacidad, disponibilidad de cama supletoria y los servicios adicionales."""
    conn = await obtener_conexion_db()
    try:
        filas = await conn.fetch(
            "SELECT id, name, base_price, max_occupancy, extra_bed_available, extra_bed_price, description FROM RoomTypes ORDER BY base_price"
        )
        opciones = []
        for f in filas:
            extra_bed_available = f["extra_bed_available"]
            if extra_bed_available:
                extra_bed_price = float(f["extra_bed_price"])
                supletoria_info = (
                    f"Admite cama supletoria hasta {f['max_occupancy'] + 1} personas, "
                    f"suplemento de {formatear_precio(extra_bed_price)} por noche."
                )
            else:
                supletoria_info = "No admite cama supletoria."
            opciones.append(
                f"{f['name']} (id:{f['id']}), {formatear_precio(float(f['base_price']))} por noche, "
                f"capacidad {f['max_occupancy']} personas. {supletoria_info} {f['description']}"
            )
        addons = (
            f"También ofrecemos desayuno incluido por {formatear_precio(PRECIO_DESAYUNO_POR_NOCHE)} por habitación y noche, "
            f"y servicio de transporte desde el aeropuerto por {formatear_precio(PRECIO_TRANSPORTE_AEROPUERTO)}."
        )
        return "Tenemos " + str(len(opciones)) + " tipos de habitación. " + " ".join(opciones) + " " + addons
    finally:
        await conn.close()


@mcp.tool()
async def verificar_disponibilidad(
    fecha_entrada: str,
    fecha_salida: str,
    tipo_habitacion_id: int,
    num_habitaciones: int = 1,
) -> str:
    """Verifica si hay suficientes habitaciones libres de un tipo específico para las fechas indicadas.
    num_habitaciones indica cuántas se necesitan (por defecto 1)."""
    error = validar_fechas(fecha_entrada, fecha_salida)
    if error:
        return error

    conn = await obtener_conexion_db()
    try:
        tipo = await conn.fetchrow(
            "SELECT id, name FROM RoomTypes WHERE id = $1",
            tipo_habitacion_id,
        )
        if not tipo:
            return "No reconozco ese tipo de habitación. Puede consultar las opciones disponibles."

        d_entrada = date.fromisoformat(fecha_entrada)
        d_salida = date.fromisoformat(fecha_salida)

        query = """
            SELECT COUNT(*) FROM Rooms r
            WHERE r.room_type_id = $1
            AND r.id NOT IN (
                SELECT ra.room_id FROM RoomAssignments ra
                JOIN Bookings b ON ra.booking_id = b.id
                WHERE (b.check_in_date, b.check_out_date) OVERLAPS ($2, $3)
                AND b.status != 'Cancelled'
            )
        """
        cantidad = await conn.fetchval(query, tipo["id"], d_entrada, d_salida)
        nombre = tipo["name"]
        if cantidad == 0:
            return f"Lo sentimos, no hay habitaciones de tipo {nombre} disponibles para esas fechas."
        if cantidad < num_habitaciones:
            return (
                f"Solo hay {cantidad} habitación{'es' if cantidad > 1 else ''} de tipo {nombre} "
                f"disponible{'s' if cantidad > 1 else ''} para esas fechas, "
                f"pero se solicitan {num_habitaciones}."
            )
        return (
            f"Hay {cantidad} habitaciones de tipo {nombre} disponibles para esas fechas "
            f"(se solicitan {num_habitaciones})."
        )
    finally:
        await conn.close()


@mcp.tool()
async def calcular_presupuesto(
    fecha_entrada: str,
    fecha_salida: str,
    tipos_habitacion_ids: str,
    extra_beds_mask: str = "",
    desayuno: bool = False,
    transporte: bool = False,
) -> str:
    """Calcula el costo total de una estancia sin realizar la reserva.
    tipos_habitacion_ids: IDs de tipo de habitación separados por coma, uno por habitación (ej: "2,2,1").
    extra_beds_mask: '0' o '1' por habitación indicando si lleva cama supletoria (ej: "1,0,0"). Vacío = sin camas supletorias.
    Incluye opciones de desayuno y transporte."""
    error = validar_fechas(fecha_entrada, fecha_salida)
    if error:
        return error

    # Parse room type IDs
    try:
        ids = [int(x.strip()) for x in tipos_habitacion_ids.split(",") if x.strip()]
    except ValueError:
        return "El parámetro tipos_habitacion_ids debe ser una lista de IDs separados por coma (ej: '2,3')."
    if not ids:
        return "Debe indicar al menos un tipo de habitación."

    # Parse extra beds mask
    if extra_beds_mask.strip():
        try:
            mask = [int(x.strip()) for x in extra_beds_mask.split(",") if x.strip()]
        except ValueError:
            return "El parámetro extra_beds_mask debe ser '0' o '1' por habitación separados por coma."
        if len(mask) != len(ids):
            return "extra_beds_mask debe tener el mismo número de elementos que tipos_habitacion_ids."
    else:
        mask = [0] * len(ids)

    conn = await obtener_conexion_db()
    try:
        noches = calcular_noches(fecha_entrada, fecha_salida)
        desglose = []
        total = 0.0

        for i, tipo_id in enumerate(ids):
            tipo = await conn.fetchrow(
                "SELECT name, base_price, extra_bed_available, extra_bed_price FROM RoomTypes WHERE id = $1",
                tipo_id,
            )
            if not tipo:
                return f"No encontré el tipo de habitación con id {tipo_id}."

            base_price = float(tipo["base_price"])
            room_total = noches * base_price
            total += room_total
            desglose.append(
                f"{tipo['name']}: {noches} noches a {formatear_precio(base_price)} por noche, total {formatear_precio(room_total)}"
            )

            if mask[i]:
                if not tipo["extra_bed_available"]:
                    return f"La {tipo['name']} no admite cama supletoria."
                extra_bed_price = float(tipo["extra_bed_price"])
                eb_total = noches * extra_bed_price
                total += eb_total
                desglose.append(
                    f"Cama supletoria {tipo['name']}: {noches} noches a {formatear_precio(extra_bed_price)} por noche, total {formatear_precio(eb_total)}"
                )

        num_habitaciones = len(ids)
        if desayuno:
            desayuno_total = noches * PRECIO_DESAYUNO_POR_NOCHE * num_habitaciones
            total += desayuno_total
            desglose.append(
                f"Desayuno: {noches} noches, {num_habitaciones} habitaciones a {formatear_precio(PRECIO_DESAYUNO_POR_NOCHE)} por habitación y noche, total {formatear_precio(desayuno_total)}"
            )
        if transporte:
            total += PRECIO_TRANSPORTE_AEROPUERTO
            desglose.append(f"Transporte aeropuerto: {formatear_precio(PRECIO_TRANSPORTE_AEROPUERTO)}")

        return (
            f"Presupuesto para {noches} noches, {num_habitaciones} habitación{'es' if num_habitaciones > 1 else ''}. "
            + "; ".join(desglose)
            + f". Total: {formatear_precio(total)}."
        )
    finally:
        await conn.close()


@mcp.tool()
async def validar_capacidad(
    tipos_habitacion_ids: str,
    extra_beds_mask: str,
    num_personas: int,
) -> str:
    """Valida si la combinación de habitaciones y camas supletorias puede alojar al número de personas indicado.
    tipos_habitacion_ids: IDs separados por coma, uno por habitación (ej: "2,1").
    extra_beds_mask: '0' o '1' por habitación indicando si lleva cama supletoria (ej: "1,0"). Vacío = sin camas supletorias.
    Devuelve confirmación si la capacidad es suficiente, o un mensaje de error explicando el problema."""
    try:
        ids = [int(x.strip()) for x in tipos_habitacion_ids.split(",") if x.strip()]
    except ValueError:
        return "tipos_habitacion_ids debe ser una lista de IDs separados por coma."
    if not ids:
        return "Debe indicar al menos un tipo de habitación."

    if extra_beds_mask.strip():
        try:
            mask = [int(x.strip()) for x in extra_beds_mask.split(",") if x.strip()]
        except ValueError:
            return "extra_beds_mask debe ser una lista de '0' o '1' separados por coma."
        if len(mask) != len(ids):
            return "extra_beds_mask debe tener el mismo número de elementos que tipos_habitacion_ids."
    else:
        mask = [0] * len(ids)

    conn = await obtener_conexion_db()
    try:
        capacidad_total = 0
        desglose = []

        for i, tipo_id in enumerate(ids):
            tipo = await conn.fetchrow(
                "SELECT name, max_occupancy, extra_bed_available FROM RoomTypes WHERE id = $1",
                tipo_id,
            )
            if not tipo:
                return f"No encontré el tipo de habitación con id {tipo_id}."

            if mask[i] and not tipo["extra_bed_available"]:
                return (
                    f"La {tipo['name']} no admite cama supletoria. "
                    f"Por favor, ajuste la selección."
                )

            capacidad_hab = tipo["max_occupancy"] + (1 if mask[i] else 0)
            capacidad_total += capacidad_hab

            desc = f"{tipo['name']}: {tipo['max_occupancy']} persona{'s' if tipo['max_occupancy'] > 1 else ''}"
            if mask[i]:
                desc += " + 1 cama supletoria"
            desglose.append(desc)

        resumen = ", ".join(desglose)

        if capacidad_total >= num_personas:
            return (
                f"Capacidad suficiente. {len(ids)} habitación{'es' if len(ids) > 1 else ''}: {resumen}. "
                f"Capacidad total: {capacidad_total} personas para {num_personas} huésped{'es' if num_personas > 1 else ''}."
            )

        falta = num_personas - capacidad_total
        # Suggest adding extra beds to rooms that support it but don't have one yet
        sugerencias = []
        for j, tipo_id in enumerate(ids):
            if mask[j] == 0:
                t = await conn.fetchrow(
                    "SELECT name, extra_bed_available FROM RoomTypes WHERE id = $1", tipo_id
                )
                if t and t["extra_bed_available"]:
                    sugerencias.append(t["name"])

        mensaje = (
            f"Capacidad insuficiente. La selección cubre {capacidad_total} personas "
            f"({resumen}), pero se necesitan {num_personas} (faltan {falta}). "
        )
        if sugerencias:
            mensaje += (
                f"Podría añadir cama supletoria en: {', '.join(sugerencias)}. "
            )
        mensaje += "También puede añadir otra habitación o elegir tipos con mayor capacidad."
        return mensaje
    finally:
        await conn.close()
