# Plan: crawler rápido, siempre actualizado, $0

Objetivo: precios frescos y sitio con 99% de uptime, sin pagar nada.

## Stack gratis

| Pieza | Opción | Límite a vigilar |
|---|---|---|
| Cron | GitHub Actions | Repo privado: 2000 min/mes. Cron puede retrasarse 5-15 min |
| Hosting | Cloudflare Pages (o GitHub Pages) | 25 MB por archivo |
| Datos | `.json.gz` por tienda (R2 10 GB si crece) | GitHub rechaza archivos > 100 MB |
| Alertas | Email de fallo de Actions | - |

## Fases

### Fase 0: medir
- Tiempo por tienda y conteo de errores en cada crawl.
- Salida: top 5 tiendas más lentas y las que fallan.

### Fase 1: velocidad en el script
- Sitemap: usar `lastmod`, re-visitar solo URLs cambiadas.
- Shopify y WooCommerce: páginas en paralelo.
- Escribir `data.json` una vez al final (o cada N tiendas), no tras cada tienda.
- Meta: ciclo completo de horas a minutos.

### Fase 2: separar datos y reporte
- Un `.json.gz` por tienda más un índice liviano.
- `report.html` estático, carga datos por `fetch` (hoy embebe ~147 MB).

### Fase 3: automatización
- Workflow de Actions con cron, en paralelo por grupos de tiendas (matrix).
- Publica datos y HTML al hosting.
- Si una tienda falla, se conserva su último dato bueno.

### Fase 4: confiabilidad
- Por tienda: `last_ok` y `fallos_consecutivos`.
- Reporte muestra "actualizado hace Xh" por precio y marca lo viejo.
- Alerta si una tienda pasa 24h sin actualizar.

### Fase 5: opcional (solo si la medición lo pide)
- Tiers de prioridad, SQLite, discovery semanal automático.

## Riesgos
- Tiendas tras Cloudflare pueden bloquear IPs de GitHub Actions: marcarlas aparte.
- "Precio 100% actualizado" no existe con scraping: se muestra antigüedad en vez de prometerlo.
- Historial en git engorda: datos en rama aparte u R2, no en `main`.

## Preguntas abiertas
1. Frescura objetivo: 1h, 6h o diaria.
2. ¿Reporte solo personal o público?
3. ¿Cuenta de Cloudflare, o parto con GitHub Pages?
4. Historial a conservar: todo, 90 días o 1 año.
