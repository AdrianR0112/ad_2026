from flask import Flask, jsonify, request
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
import os
import re
import threading
import time
from zoneinfo import ZoneInfo
import certifi
import pytds
import requests

app = Flask(__name__)

# El bloqueo protege las solicitudes que lleguen a este proceso Python.
# No sincroniza automáticamente dos servidores Flask diferentes.
product_lock = threading.Lock()
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
RESEND_API_URL = "https://api.resend.com/emails"
ECUADOR_TIMEZONE = ZoneInfo("America/Guayaquil")


class ResendAPIError(Exception):
    """Error controlado al solicitar el envío de un correo a Resend."""


@app.after_request
def identificar_servidor(response):
    response.headers["X-Servidor"] = "Servidor-1"
    return response


def getConnection():
    return pytds.connect(
        server=os.environ["DB_SERVER"],
        database=os.environ["DB_DATABASE"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASS"],
        port=1433,
        row_strategy=pytds.namedtuple_row_strategy,
        cafile=certifi.where(),
        validate_host=True
    )


@contextmanager
def abrir_bd():
    """Cierra siempre el cursor y la conexión, incluso si hay errores."""
    conn = getConnection()
    try:
        cursor = conn.cursor()
        try:
            yield conn, cursor
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
    finally:
        conn.close()


def producto_a_diccionario(row):
    return {
        "id": int(row.id),
        "name": row.names,
        "price": float(row.price),
        "stock": int(row.stock),
        "version": int(row.version),
    }


def consultar_productos(cursor):
    cursor.execute(
        "SELECT id, names, price, stock, version FROM Products ORDER BY id"
    )
    return [producto_a_diccionario(row) for row in cursor.fetchall()]


def crear_resumen_productos(products):
    total_stock = sum(product["stock"] for product in products)
    inventory_value = sum(
        Decimal(str(product["price"])) * product["stock"]
        for product in products
    )
    return total_stock, inventory_value


def construir_correo_productos(products, fecha_consulta):
    total_stock, inventory_value = crear_resumen_productos(products)
    fecha_texto = fecha_consulta.strftime("%d/%m/%Y %H:%M:%S")
    return f"""
    <!doctype html>
    <html lang="es">
    <body style="margin:0;background:#f3f4f6;font-family:Arial,sans-serif;color:#111827;">
        <div style="max-width:680px;margin:0 auto;padding:32px 16px;">
            <div style="background:#ffffff;border-radius:16px;padding:30px;box-shadow:0 8px 24px rgba(15,23,42,.08);">
                <h1 style="margin:0 0 8px;color:#123c78;">Consulta del catálogo</h1>
                <p style="margin:0 0 24px;color:#4b5563;">
                    Resumen generado desde la API Flask y Azure SQL.
                </p>
                <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px;">
                    <div style="flex:1;min-width:150px;background:#eff6ff;border-radius:12px;padding:16px;">
                        <strong style="display:block;font-size:28px;color:#1d4ed8;">{len(products)}</strong>
                        Productos registrados
                    </div>
                    <div style="flex:1;min-width:150px;background:#ecfdf5;border-radius:12px;padding:16px;">
                        <strong style="display:block;font-size:28px;color:#047857;">{total_stock}</strong>
                        Unidades en stock
                    </div>
                </div>
                <p><strong>Valor estimado del inventario:</strong> ${inventory_value:.2f}</p>
                <p><strong>Fecha de consulta:</strong> {fecha_texto} (America/Guayaquil)</p>
                <p style="margin:24px 0 0;color:#6b7280;font-size:13px;">
                    Notificación automática generada mediante Resend.
                </p>
            </div>
        </div>
    </body>
    </html>
    """


def enviar_correo_resend(destinatario, products, fecha_consulta):
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        raise ResendAPIError("RESEND_API_KEY no está configurada en el servidor.")

    sender = os.environ.get(
        "RESEND_FROM",
        "Catálogo de Productos <onboarding@resend.dev>",
    )
    response = requests.post(
        RESEND_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": sender,
            "to": [destinatario],
            "subject": f"Consulta de productos - {fecha_consulta:%d/%m/%Y}",
            "html": construir_correo_productos(products, fecha_consulta),
        },
        timeout=20,
    )

    try:
        result = response.json()
    except ValueError:
        result = {}

    if not response.ok:
        detail = result.get("message", "Resend rechazó la solicitud de envío.")
        raise ResendAPIError(detail)

    return result


def validar_producto(data, requiere_id=False, requiere_version=False):
    """Valida el JSON y devuelve (datos_limpios, error)."""
    if not isinstance(data, dict):
        return None, "Debes enviar un objeto JSON válido."

    campos = ["name", "price", "stock"]
    if requiere_id:
        campos.append("id")
    if requiere_version:
        campos.append("version")

    faltantes = [campo for campo in campos if campo not in data]
    if faltantes:
        return None, "Faltan campos: " + ", ".join(faltantes)

    name = data["name"]
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
        return None, "name debe ser un texto de 1 a 100 caracteres."

    if isinstance(data["price"], bool):
        return None, "price debe ser un número."

    try:
        price = Decimal(str(data["price"]))
        if (
            not price.is_finite()
            or price < 0
            or price > Decimal("99999999.99")
            or price != price.quantize(Decimal("0.01"))
        ):
            return None, "price debe estar entre 0 y 99999999.99 y tener máximo 2 decimales."
    except (InvalidOperation, ValueError, TypeError):
        return None, "price debe ser un número válido."

    stock = data["stock"]
    if isinstance(stock, bool) or not isinstance(stock, int) or not (0 <= stock <= 2147483647):
        return None, "stock debe ser un entero no negativo."

    producto = {"name": name.strip(), "price": price, "stock": stock}

    if requiere_id:
        product_id = data["id"]
        if isinstance(product_id, bool) or not isinstance(product_id, int) or not (1 <= product_id <= 2147483647):
            return None, "id debe ser un entero positivo."
        producto["id"] = product_id

    if requiere_version:
        version = data["version"]
        if isinstance(version, bool) or not isinstance(version, int) or not (1 <= version <= 2147483647):
            return None, "version debe ser un entero positivo."
        producto["version"] = version

    return producto, None


def error_de_bd():
    app.logger.exception("Error en la API o en la base de datos")
    return jsonify({
        "mensaje": "Ocurrió un error interno al acceder a la base de datos."
    }), 500


# GET: obtener todos los productos
@app.route("/api/products", methods=["GET"])
@app.route("/api/productos", methods=["GET"])
def get_products():
    try:
        with abrir_bd() as (conn, cursor):
            products = consultar_productos(cursor)
        return jsonify(products), 200
    except Exception:
        return error_de_bd()


# POST: consultar productos y enviar el resumen por correo mediante Resend
@app.route("/api/products/email", methods=["POST"])
@app.route("/api/procesar", methods=["POST"])
def send_products_email():
    data = request.get_json(silent=True)
    destinatario = data.get("email", "").strip() if isinstance(data, dict) else ""

    if (
        not destinatario
        or len(destinatario) > 254
        or not EMAIL_PATTERN.fullmatch(destinatario)
    ):
        return jsonify({
            "mensaje": "Debes proporcionar un correo electrónico válido."
        }), 400

    try:
        with abrir_bd() as (conn, cursor):
            products = consultar_productos(cursor)

        fecha_consulta = datetime.now(ECUADOR_TIMEZONE)
        resend_result = enviar_correo_resend(
            destinatario,
            products,
            fecha_consulta,
        )
        total_stock, inventory_value = crear_resumen_productos(products)

        return jsonify({
            "procesamiento": "finalizado",
            "total_productos": len(products),
            "stock_total": total_stock,
            "valor_inventario": float(inventory_value),
            "fecha_consulta": fecha_consulta.isoformat(),
            "notificacion": "enviada",
            "message_id": resend_result.get("id"),
        }), 200

    except ResendAPIError as error:
        app.logger.exception("Error al enviar el correo mediante Resend")
        return jsonify({
            "mensaje": "No se pudo enviar el correo mediante Resend.",
            "detalle": str(error),
        }), 502
    except requests.RequestException:
        app.logger.exception("Error de red al contactar con Resend")
        return jsonify({
            "mensaje": "No se pudo contactar con el servicio de correo."
        }), 502
    except Exception:
        return error_de_bd()


# GET: obtener un producto por su identificador
@app.route("/api/products/<int:product_id>", methods=["GET"])
def get_product(product_id):
    try:
        with abrir_bd() as (conn, cursor):
            cursor.execute(
                "SELECT id, names, price, stock, version FROM Products WHERE id = ?",
                (product_id,),
            )
            row = cursor.fetchone()

            if row is None:
                return jsonify({
                    "mensaje": "Producto no encontrado"
                }), 404

            product = producto_a_diccionario(row)

        return jsonify(product), 200

    except Exception:
        return error_de_bd()


# GET: probar la conexión a Azure SQL
@app.route("/test-db", methods=["GET"])
def test_db():
    try:
        with abrir_bd() as (conn, cursor):
            cursor.execute("SELECT DB_NAME()")
            database = cursor.fetchone()[0]

        return jsonify({
            "mensaje": "Conexión exitosa",
            "base_datos": database
        }), 200

    except Exception:
        return error_de_bd()


# POST: insertar directamente en Products
@app.route("/api/products", methods=["POST"])
def create_product():
    datos, error = validar_producto(request.get_json(silent=True))

    if error:
        return jsonify({"mensaje": error}), 400

    try:
        with abrir_bd() as (conn, cursor):
            cursor.execute(
                "INSERT INTO Products (names, price, stock) VALUES (?, ?, ?)",
                (datos["name"], datos["price"], datos["stock"]),
            )
            conn.commit()

        return jsonify({
            "mensaje": "Producto creado exitosamente"
        }), 201

    except Exception:
        return error_de_bd()


# PUT: sin bloqueo
@app.route("/api/products/no_lock", methods=["PUT"])
def update_product():
    datos, error = validar_producto(
        request.get_json(silent=True),
        requiere_id=True
    )

    if error:
        return jsonify({"mensaje": error}), 400

    try:
        with abrir_bd() as (conn, cursor):
            # Pausa intencional para demostrar concurrencia.
            time.sleep(5)

            cursor.execute(
                """
                UPDATE Products
                SET names = ?,
                    price = ?,
                    stock = ?,
                    version = version + 1
                WHERE id = ?
                """,
                (
                    datos["name"],
                    datos["price"],
                    datos["stock"],
                    datos["id"]
                ),
            )

            if cursor.rowcount == 0:
                return jsonify({
                    "mensaje": "Producto no encontrado"
                }), 404

            conn.commit()

        return jsonify({
            "mensaje": "Producto actualizado sin bloqueo"
        }), 200

    except Exception:
        return error_de_bd()


# PUT: con bloqueo local
@app.route("/api/products/lock", methods=["PUT"])
def update_lock():
    datos, error = validar_producto(
        request.get_json(silent=True),
        requiere_id=True
    )

    if error:
        return jsonify({"mensaje": error}), 400

    try:
        with product_lock:
            print("Entra al bloqueo", flush=True)

            with abrir_bd() as (conn, cursor):
                time.sleep(5)

                cursor.execute(
                    """
                    UPDATE Products
                    SET names = ?,
                        price = ?,
                        stock = ?,
                        version = version + 1
                    WHERE id = ?
                    """,
                    (
                        datos["name"],
                        datos["price"],
                        datos["stock"],
                        datos["id"]
                    ),
                )

                if cursor.rowcount == 0:
                    return jsonify({
                        "mensaje": "Producto no encontrado"
                    }), 404

                conn.commit()

            print("Sale del bloqueo", flush=True)

        return jsonify({
            "mensaje": "Producto actualizado con bloqueo"
        }), 200

    except Exception:
        return error_de_bd()


# PUT: control de concurrencia mediante version
@app.route("/api/products/update_version", methods=["PUT"])
def update_version():
    datos, error = validar_producto(
        request.get_json(silent=True),
        requiere_id=True,
        requiere_version=True
    )

    if error:
        return jsonify({"mensaje": error}), 400

    try:
        with abrir_bd() as (conn, cursor):
            cursor.execute(
                """
                UPDATE Products
                SET names = ?,
                    price = ?,
                    stock = ?,
                    version = version + 1
                WHERE id = ? AND version = ?
                """,
                (
                    datos["name"],
                    datos["price"],
                    datos["stock"],
                    datos["id"],
                    datos["version"]
                ),
            )

            if cursor.rowcount == 0:
                cursor.execute(
                    "SELECT 1 FROM Products WHERE id = ?",
                    (datos["id"],)
                )

                if cursor.fetchone() is None:
                    return jsonify({
                        "mensaje": "Producto no encontrado"
                    }), 404

                return jsonify({
                    "mensaje": "Conflicto: la versión del producto cambió. Consulta de nuevo el producto."
                }), 409

            conn.commit()

        return jsonify({
            "mensaje": "Producto actualizado con control de versión",
            "version": datos["version"] + 1
        }), 200

    except Exception:
        return error_de_bd()


if __name__ == "__main__":
    # Solo para pruebas; en producción utilizar WSGI y HTTPS.
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
