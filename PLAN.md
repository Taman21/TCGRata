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

## Fase 3: puesta en marcha (estado)
Workflow: `.github/workflows/crawl.yml` (cron cada 6h + manual). El estado (`data.json.gz`) vive en el release `data` del repo (privado), no en git.

Pasos únicos:
1. `gh auth refresh -s workflow` (el token necesita el scope `workflow` para subir el workflow).
2. Subir el estado inicial para no perder historial: `gzip -c data.json > data.json.gz; gh release create data data.json.gz --title data --notes estado`.
3. Cloudflare (gratis): crear cuenta, API token con permiso "Cloudflare Pages: Edit", luego
   `gh secret set CLOUDFLARE_API_TOKEN` y `gh secret set CLOUDFLARE_ACCOUNT_ID`. Sin estos secretos el workflow igual crawlea y guarda datos, solo omite el deploy.
4. Probar: `gh workflow run crawl` y mirar `gh run watch`.

Notas:
- GitHub Pages no sirve: en cuenta Free no publica repos privados. Por eso Cloudflare Pages.
- El sitio en `*.pages.dev` es público aunque el repo sea privado. Para restringirlo: Cloudflare Access (gratis hasta 50 usuarios).
- Presupuesto: 2000 min/mes / (4 corridas/día * 30) = ~16 min por corrida. Si un ciclo tarda más, subir el intervalo en el cron.

## Estado (2026-11-03)
- Fase 0 medir: hecha. Fase 1 velocidad: hecha. Fase 2 datos + sitio: hecha (por juego/tipo, productos agrupados; ver `classify.py` y `aliases.json`).
- Fase 3 automatización: workflow lanzado en GitHub (repo público; la cuenta estuvo bloqueada por facturación). Falta confirmar guardado de estado y deploy a Cloudflare.
- Fase 4 confiabilidad: hecha.
  - Por tienda: `last_ok` (último crawl sin errores) y `fails` (fallos seguidos). Reintento si no llegó nada. Una tienda que antes tenía stock y responde 0 productos cuenta como fallo.
  - Sacar una tienda de `stores.txt` (comentarla) la retira: no se crawlea ni se muestra.
  - `python crawler.py --health` lista tiendas sin crawl limpio en 24 h; el workflow lo corre al final y queda rojo (correo de GitHub) si >10% están así.
  - La página marca como desactualizado el precio de una tienda con >24 h sin crawl limpio y no lo elige como "el más barato" si hay otro fresco.
- Pendiente: extras de UI (gráfico de historial de precio, favoritos, sección "Bajó de precio", insignia de preventa), subir cobertura de juego con `cat`, Fase 5 solo si la medición lo pide.
