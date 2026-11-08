-- Migración 0008 (transaccional): sector «Otros» en el catálogo fijo de sectores de las empresas. La aplica `app/migraciones` al
-- arrancar, una sola vez y en una transacción; no se ejecuta a mano.
--
-- QUÉ HACE. Añade el valor `OTROS` al tipo enumerado `sector_empresa`. SQLAlchemy guarda el NOMBRE del miembro de `SectorEmpresa`
-- (MINERIA, PETROLEO, ENERGIA y ahora OTROS). Ese único tipo lo usan `empresas.sector` y `documentos.sector` (la copia que la
-- ingesta conserva al reservar el documento), así que una sola sentencia cubre las dos columnas. La base vectorial guarda el sector
-- como VARCHAR(64) sin lista de valores, por lo que no necesita migración.
--
-- POR QUÉ ES IDEMPOTENTE Y NO DECLARA OBJETOS. `create_all` crea `sector_empresa` ANTES de las migraciones, con los miembros del
-- modelo de ese momento, y no la altera después: una base ya desplegada tiene los tres valores antiguos; una instalación nueva ya
-- trae `OTROS`. Los tipos no forman parte de la firma de esquema (solo tablas y vistas), así que no hay adopción posible: la
-- migración se aplica siempre una vez y `IF NOT EXISTS` admite que el valor ya exista. Es el mismo patrón de la 0005.
--
-- Requiere PostgreSQL >= 12 (ALTER TYPE ... ADD VALUE dentro de una transacción) y el tipo `sector_empresa`. No toca filas
-- existentes, no cambia ningún valor anterior ni lo convierte en predeterminado, y no desactiva ni recrea ninguna restricción.
-- Un valor añadido no puede usarse dentro de esta misma transacción; ninguna otra sentencia de este archivo lo usa.

ALTER TYPE sector_empresa ADD VALUE IF NOT EXISTS 'OTROS';
