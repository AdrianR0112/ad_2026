# API de productos

API Flask que consulta productos en Azure SQL y permite enviar por correo un
resumen del catálogo mediante Resend.

## Configuración de Resend

Configura estas variables de entorno en Render:

```text
RESEND_API_KEY=re_...
RESEND_FROM=Catálogo de Productos <onboarding@resend.dev>
```

`onboarding@resend.dev` sirve para pruebas y solo permite enviar al correo de la
cuenta de Resend. Para aceptar cualquier destinatario, verifica un dominio nuevo
en Resend y cambia `RESEND_FROM` por una dirección de ese dominio.

## Envío del resumen

```http
POST /api/products/email
Content-Type: application/json

{
  "email": "destinatario@ejemplo.com"
}
```

El correo incluye la cantidad de productos, el stock total, el valor estimado
del inventario y la fecha de consulta.
