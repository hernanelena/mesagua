# ============================================
# APP: MAPA MESA DE AGUA
# Resumen territorial estilo XLS + PDF + Asignación espacial
# ============================================

import os
import streamlit as st
import pandas as pd
import requests
import folium
from streamlit_folium import st_folium
from folium.plugins import LocateControl
from datetime import datetime
import plotly.express as px

# ---- NUEVO: Geo + PDF/XLSX ----
import json
from io import BytesIO

# Shapely (point-in-polygon)
try:
    from shapely.geometry import shape, Point
    SHAPELY_OK = True
except Exception:
    SHAPELY_OK = False

# ReportLab para PDF
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas  # para dibujar encabezado en todas las páginas

# 1. CONFIGURACIÓN
st.set_page_config(page_title="MAPA MESA DE AGUA", layout="wide")

FORM_ID = "aHNGU6dn2MFGMpg9Y5M5sn"
TOKEN = st.secrets["KOBO_TOKEN"]

URL_MESAAGUA = f"https://territorios.inta.gob.ar/api/v2/assets/{FORM_ID}/data/?format=json"
HEADERS = {'Authorization': f'Token {TOKEN}'}

# Ruta del logo (debe estar junto al script)
LOGO_PATH = os.path.join(os.path.dirname(__file__), "logo_mesa.png")

# ===== Margen superior del PDF (usado por el doc) =====
PDF_TOP_MARGIN = 3.2 *cm  # subilo a 5*cm si agrandás más el logo

# --- ESTILOS CSS ---
st.markdown(
    """
    <style>
        .titulo-responsive {
            text-align: center; color: #1E3A8A; font-weight: bold; padding: 10px;
            font-size: calc(1.5rem + 1.2vw); line-height: 1.2;
        }
        .ficha-header {
            background-color: #1E3A8A; color: white; padding: 10px; border-radius: 5px;
            text-align: center; margin-bottom: 15px; font-weight: bold;
        }
    </style>
    """,
    unsafe_allow_html=True
)

# --- FUNCIÓN PARA PASAR A NOMBRES CLAROS ---
def mapear_nombres_claros(valor, tipo):
    mapeos_maestros = {
        "asistencia": {
            "ong": "ONG", "instituci_n_nacionales": "Nación", "instituci_n_provinciales": "Provincia",
            "propio": "Propio", "otros": "Otros", "sin_asistencia": "Sin asistencia", "otro": "Otros"
        },
        "estado": {"bueno": "Bueno", "regular": "Regular", "malo": "Malo"},
        "calidad": {"buena": "Buena", "regular": "Regular", "mala": "Mala"},
        "usuario": {
            # normalizo a la taxonomía del XLS
            "csalud": "Centro de salud",
            "com_ind": "Comunidad indígena",
            "comunidad indígena": "Comunidad indígena",
            "comununidad indígena": "Comunidad indígena",
            "escuelas": "Escuelas",
            "escuela": "Escuelas",
            "prod_af": "Familia rural criolla",
            "furbanas": "Familias urbanas",
            "familia urbana": "Familias urbanas",
            "familias urbanas": "Familias urbanas",
        },
        "problemas": {
            "cantidad_calidad_del_agua": "Cantidad/Calidad del agua",
            "sistema_de_captaci_n__bomba__t": "Sistema de captación (bomba, techo colector, toma)",
            "sistema_de_conducci_n__manguer": "Sistema de conducción (mangueras, cañerias)",
            "sistema_de_almacenamiento__cis": "Sistema de almacenamiento (cisterna, tanque)"
        }
    }
    v = "" if pd.isna(valor) else str(valor).strip()
    v_clean = v.lower()
    if tipo == "problemas":
        return mapeos_maestros["problemas"].get(v_clean, "Otras")
    return mapeos_maestros.get(tipo, {}).get(v_clean, valor if valor not in [None, ""] else "Otros")


@st.cache_data(ttl=60)
def cargar_datos():
    try:
        r = requests.get(URL_MESAAGUA, headers=HEADERS)
        data = r.json()
        df = pd.json_normalize(data.get('results', []))
        df.columns = [c.split('.')[-1].split('/')[-1] for c in df.columns]

        # Fecha
        col_fecha = next((c for c in df.columns if 'fecha' in c.lower() or 'relevamiento' in c.lower()), None)
        df['fecha_limpia'] = pd.to_datetime(df[col_fecha], errors='coerce') if col_fecha else pd.to_datetime(datetime.now())

        # GPS
        def obtener_gps(row):
            geo = row.get('_geolocation')
            if isinstance(geo, list) and len(geo) >= 2:
                return float(geo[0]), float(geo[1])
            return None, None
        res = df.apply(obtener_gps, axis=1)
        df['lat'], df['lon'] = zip(*res)
        df = df.dropna(subset=['lat', 'lon']).copy()

        # Provincia declarada en la API (clave 'salta'/'jujuy')
        col_prov = next((c for c in df.columns if c.lower() == 'provincia'), None)
        if col_prov:
            df['Provincia_api'] = (
                df[col_prov].astype(str).str.strip().str.lower()
                .map({'salta': 'Salta', 'jujuy': 'Jujuy'})
            )
        else:
            df['Provincia_api'] = None

        return df
    except Exception as e:
        st.error(f"Error: {e}")
        return pd.DataFrame()


# --- Cargar GEOJSON de Departamentos (Salta/Jujuy) ---
@st.cache_data(show_spinner=False)
def cargar_geojson_deptos(path_geojson: str):
    """
    Lee tu GeoJSON de departamentos y devuelve una lista:
    [{'prov': 'Salta', 'nam': 'La Viña', 'geom': shapely_geom}, ...]
    """
    try:
        with open(path_geojson, 'r', encoding='utf-8') as f:
            gj = json.load(f)
        feats = []
        for feat in gj.get('features', []):
            props = feat.get('properties', {}) or {}
            prov = props.get('prov') or props.get('Provincia') or props.get('province') or ''
            depto = props.get('nam') or props.get('depto') or props.get('name') or ''
            geom = shape(feat.get('geometry'))
            feats.append({'prov': str(prov).strip().title(), 'nam': str(depto).strip(), 'geom': geom})
        # Quedarnos con Salta/Jujuy
        feats = [f for f in feats if f['prov'] in ('Salta', 'Jujuy')]
        return feats
    except FileNotFoundError:
        st.error("No se encontró 'deptos.geojson'. Dejalo junto a la app o ajustá la ruta en cargar_geojson_deptos().")
    except Exception as e:
        st.error(f"Error al leer el geojson de departamentos: {e}")
    return []


def asignar_depto_por_punto(df_pts: pd.DataFrame, deptos_features: list) -> pd.DataFrame:
    """
    Para cada (lat, lon) encuentra el departamento. Usa Provincia_api para acotar.
    """
    if not SHAPELY_OK:
        st.error("Falta instalar 'shapely' para ubicar puntos en departamentos (pip install shapely).")
        df_out = df_pts.copy()
        df_out['Departamento'] = 'Sin asignar'
        df_out['Provincia_geo'] = None
        df_out['Provincia_final'] = df_out['Provincia_api'].fillna('Sin asignar')
        return df_out

    df = df_pts.copy()
    # Index por provincia
    idx_prov = {
        'Salta': [f for f in deptos_features if f['prov'] == 'Salta'],
        'Jujuy': [f for f in deptos_features if f['prov'] == 'Jujuy']
    }

    prov_cols, dep_cols = [], []
    for _, r in df.iterrows():
        p = Point(r['lon'], r['lat'])
        prov_api = r.get('Provincia_api')
        candidatos = idx_prov.get(prov_api, idx_prov['Salta'] + idx_prov['Jujuy'])
        prov_gj, depto = None, None
        for feat in candidatos:
            try:
                if feat['geom'].contains(p) or feat['geom'].intersects(p):
                    prov_gj = feat['prov']
                    depto = feat['nam']
                    break
            except Exception:
                continue
        prov_cols.append(prov_gj)
        dep_cols.append(depto)

    df['Provincia_geo'] = prov_cols
    df['Departamento'] = pd.Series(dep_cols, index=df.index).fillna('Sin asignar')
    df['Provincia_final'] = df['Provincia_api'].fillna(df['Provincia_geo']).fillna('Sin asignar')
    return df


# --- CONFIG MAPA ---
mapa_config = {
    "cisterna_de_consumo": {"titulo": "Cisterna de consumo", "color": "blue", "hex": "#0067A5"},
    "AUTOMATIC": {"titulo": "Cisterna productiva", "color": "cadetblue", "hex": "#436975"},
    "AUTOMATIC_4": {"titulo": "Pozo somero", "color": "green", "hex": "#228B22"},
    "AUTOMATIC_1": {"titulo": "Pozo profundo", "color": "darkgreen", "hex": "#006400"},
    "represa": {"titulo": "Represa", "color": "orange", "hex": "#FF8C00"},
    "red_de_distribuci_n": {"titulo": "Red de distribución", "color": "purple", "hex": "#800080"},
    "AUTOMATIC_2": {"titulo": "Tanque australiano", "color": "red", "hex": "#B22222"},
    "madrejones": {"titulo": "Madrejones", "color": "gray", "hex": "#696969"},
    "otros": {"titulo": "Otros", "color": "black", "hex": "#333333"}
}
colores_tecnologias = {v["titulo"]: v["hex"] for v in mapa_config.values()}


def buscar_v(registro, keywords):
    for col in registro.index:
        if any(k.lower() in col.lower() for k in keywords):
            val = registro[col]
            if pd.isnull(val) or str(val).lower() == 'none':
                return "No reg."
            if isinstance(val, (pd.Timestamp, datetime)):
                return val.strftime('%d/%m/%Y')
            return str(val)
    return "No reg."


# ============ ENCABEZADO PDF: LOGO ARRIBA DERECHA + TÍTULO/FECHA AL LADO ============
def _header_canvas(c: canvas.Canvas, doc):
    page_width, page_height = landscape(A4)
    
    # ----- Parámetros de Diseño -----
    # Definimos una línea de base central para alinear verticalmente logo y texto
    header_baseline_y = page_height - 1.8 * cm 
    
    # Coordenada X del centro exacto de la página (para centrar el título con el papel)
    center_of_page_x = page_width / 2.0
    
    left_margin = 1.5 * cm
    right_margin = 1.5 * cm
    
    # 1. *** LOGO (A la Izquierda) ***
    # Mantenemos el logo en su posición, sin cambios.
    logo_w = 180
    logo_h = 140  
    x_logo = left_margin
    y_logo = header_baseline_y - (logo_h / 2.0)
    
    try:
        c.drawImage(
            LOGO_PATH,
            x=x_logo,
            y=y_logo,
            width=logo_w,
            height=logo_h,
            preserveAspectRatio=True,
            mask='auto'
        )
    except Exception:
        pass

    # 2. *** TÍTULO Y FECHA (Centrados RESPECTO A LA PÁGINA TOTAL) ***
    # Al usar 'center_of_page_x', el texto se mueve hacia la izquierda,
    # ignorando al logo y centrándose con el papel.

    # -- Título --
    title_font = "Helvetica-Bold"
    title_size = 20
    title_text = "RELEVAMIENTO DE DATOS"
    
    c.setFont(title_font, title_size)
    y_title = header_baseline_y + 0.2 * cm
    
    # ¡AQUÍ ESTÁ EL CAMBIO IMPORTANTE!
    c.drawCentredString(center_of_page_x, y_title, title_text)

    # -- Fecha/Generado (Justo debajo del título y también centrada en la página) --
    date_font = "Helvetica"
    date_size = 10
    date_text = datetime.now().strftime("Generado: %d/%m/%Y %H:%M")
    
    c.setFont(date_font, date_size)
    y_date = y_title - 0.7 * cm
    
    # ¡AQUÍ TAMBIÉN!
    c.drawCentredString(center_of_page_x, y_date, date_text)

    # 3. *** LÍNEA SEPARADORA (El límite de seguridad) ***
    # La mantenemos igual, de margen a margen.
    y_line = y_date - 0.6 * cm
    
    c.setLineWidth(1.2)
    c.setStrokeColor(colors.HexColor('#1E3A8A'))
    c.line(left_margin, y_line, page_width - right_margin, y_line)


# ============ CONSTRUCTOR DE PDF (USA EL HEADER + SALTOS DE PÁGINA) ============
def construir_pdf_xls(tec_por_prov, asis_por_prov, usu_por_prov) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=1*cm,
        rightMargin=1*cm,
        topMargin=PDF_TOP_MARGIN,
        bottomMargin=1.2*cm
    )
    styles = getSampleStyleSheet()
    elems = []

    # --- Aire inicial ---
    elems.append(Spacer(1, 0.1*cm))

    # ---- Helper de tabla con estilo ----
    def tabla_rl(df, titulo):
        if titulo:
            elems.append(Paragraph(f"<b>{titulo}</b>", styles['Heading3']))

        data = [["Departamento"] + [str(c) for c in df.columns]]
        for idx, row in df.iterrows():
            fila = []
            for v in row.tolist():
                try:
                    fila.append(int(v))
                except Exception:
                    if v is None or (isinstance(v, float) and pd.isna(v)):
                        fila.append(0)
                    else:
                        fila.append(v)
            data.append([str(idx)] + fila)

        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1E3A8A')),
            ('TEXTCOLOR',  (0,0), (-1,0), colors.white),
            ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE',   (0,0), (-1,0), 10),
            ('ALIGN',      (0,0), (-1,-1), 'CENTER'),
            ('GRID',       (0,0), (-1,-1), 0.25, colors.grey),
            ('FONTSIZE',   (0,1), (-1,-1), 9),
            ('VALIGN',     (0,0), (-1,-1), 'MIDDLE'),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ('TOPPADDING',    (0,0), (-1,-1), 4),
        ]))
        elems.extend([t, Spacer(1, 0.45*cm)])

    # =======================
    # Sección 1: Tecnologías
    # =======================
    elems.append(Paragraph("Informe generado a partir de la base de datos acualizada", styles['Heading1']))
    elems.append(Spacer(1, 0.25*cm))

    elems.append(Paragraph("<b>1.- Tecnologías</b>", styles['Heading2']))
    for prov in ["Salta", "Jujuy"]:
        elems.append(Paragraph(f"<b>Provincia: {prov}</b>", styles['Heading4']))
        dfp = tec_por_prov.get(prov, pd.DataFrame())
        if not dfp.empty:
            tabla_rl(dfp, "")
        else:
            elems.append(Paragraph("Sin registros", styles['Normal']))
            elems.append(Spacer(1, 0.2*cm))

    # <<< SALTO DE PÁGINA ANTES DE LA SECCIÓN 2 >>>
    elems.append(PageBreak())

    # ==============================
    # Sección 2: Asistencia Técnica
    # ==============================
    elems.append(Paragraph("<b>2.- Asistencia Técnica</b>", styles['Heading2']))
    for prov in ["Salta", "Jujuy"]:
        elems.append(Paragraph(f"<b>Provincia: {prov}</b>", styles['Heading4']))
        dfp = asis_por_prov.get(prov, pd.DataFrame())
        if not dfp.empty:
            tabla_rl(dfp, "")
        else:
            elems.append(Paragraph("Sin registros", styles['Normal']))
            elems.append(Spacer(1, 0.2*cm))

    # <<< SALTO DE PÁGINA ANTES DE LA SECCIÓN 3 >>>
    elems.append(PageBreak())

    # =================
    # Sección 3: Usuarios
    # =================
    elems.append(Paragraph("<b>3.- Usuarios</b>", styles['Heading2']))
    for prov in ["Salta", "Jujuy"]:
        elems.append(Paragraph(f"<b>Provincia: {prov}</b>", styles['Heading4']))
        dfp = usu_por_prov.get(prov, pd.DataFrame())
        if not dfp.empty:
            tabla_rl(dfp, "")
        else:
            elems.append(Paragraph("Sin registros", styles['Normal']))
            elems.append(Spacer(1, 0.2*cm))

    # --- Build con encabezado en todas las páginas ---
    doc.build(
        elems,
        onFirstPage=_header_canvas,
        onLaterPages=_header_canvas
    )
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes


# ============ CONSTRUCTOR DE XLSX (SIN CAMBIOS ESTRUCTURALES) ============
def construir_xlsx(tec_por_prov, asis_por_prov, usu_por_prov) -> bytes:
    xls_buffer = BytesIO()
    with pd.ExcelWriter(xls_buffer, engine="openpyxl") as writer:
        for prov in ["Salta", "Jujuy"]:
            tec_por_prov.get(prov, pd.DataFrame()).to_excel(writer, sheet_name=f"TEC_{prov}")
            asis_por_prov.get(prov, pd.DataFrame()).to_excel(writer, sheet_name=f"ASIS_{prov}")
            usu_por_prov.get(prov, pd.DataFrame()).to_excel(writer, sheet_name=f"USU_{prov}")
    xls_buffer.seek(0)
    return xls_buffer.getvalue()


# ======================= APP =======================
st.markdown('<div class="titulo-responsive">📍 Monitoreo Mesa de Agua</div>', unsafe_allow_html=True)

# Cargar datos
df_raw = cargar_datos()

# Cargar deptos
deptos_features = cargar_geojson_deptos("deptos.geojson") if SHAPELY_OK else []

# ====== FILTROS ======
if not df_raw.empty:
    with st.expander("🔍 Opciones de Filtro", expanded=False):
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            fecha_desde = st.date_input("Inicio", value=df_raw['fecha_limpia'].min().date())
        with c2:
            fecha_hasta = st.date_input("Fin", value=df_raw['fecha_limpia'].max().date())
        with c3:
            listado_tec = ["Todas"] + [v["titulo"] for v in mapa_config.values()]
            tec_filtro = st.selectbox("Tecnología", listado_tec)
        with c4:
            opciones_uso = ["Todos"]
            if 'En_uso' in df_raw.columns:
                opciones_uso += sorted(df_raw['En_uso'].dropna().unique().tolist())
            uso_filtro = st.selectbox("¿En Uso?", opciones_uso)

    mask = (df_raw['fecha_limpia'].dt.date >= fecha_desde) & (df_raw['fecha_limpia'].dt.date <= fecha_hasta)
    if tec_filtro != "Todas":
        tec_key_buscada = next(k for k, v in mapa_config.items() if v["titulo"] == tec_filtro)
        mask &= (df_raw['tecnolog'] == tec_key_buscada)
    if uso_filtro != "Todos":
        mask &= (df_raw['En_uso'] == uso_filtro)
    df_filtrado = df_raw[mask].copy()
else:
    df_filtrado = pd.DataFrame()

# ====== MAPA ======
if not df_filtrado.empty:
    # Preparar columnas de texto
    df_filtrado['tecnologia_txt'] = df_filtrado['tecnolog'].apply(lambda x: mapa_config.get(str(x), mapa_config["otros"])["titulo"])
    df_filtrado['estado_txt'] = df_filtrado.get('Estado_de_la_obra', pd.Series(index=df_filtrado.index)).apply(lambda x: mapear_nombres_claros(x, 'estado'))
    df_filtrado['calidad_txt'] = df_filtrado.get('Calidad_del_agua', pd.Series(index=df_filtrado.index)).apply(lambda x: mapear_nombres_claros(x, 'calidad'))
    df_filtrado['asistencia_txt'] = df_filtrado.get('Asistencia_t_cnica_de_la_obra', pd.Series(index=df_filtrado.index)).apply(lambda x: mapear_nombres_claros(x, 'asistencia'))
    df_filtrado['usuario_txt'] = df_filtrado.get('Usuario', pd.Series(index=df_filtrado.index)).apply(lambda x: mapear_nombres_claros(x, 'usuario'))

    # Asignación espacial
    df_geo = asignar_depto_por_punto(df_filtrado, deptos_features)

    # --- MAPA ---
    m = folium.Map(location=[df_filtrado['lat'].mean(), df_filtrado['lon'].mean()], zoom_start=8, tiles=None)
    folium.TileLayer(
        tiles="https://wms.ign.gob.ar/geoserver/gwc/service/tms/1.0.0/capabaseargenmap@EPSG%3A3857@png/{z}/{x}/{-y}.png",
        attr='IGN', name='Argenmap (IGN)', overlay=False
    ).add_to(m)
    folium.TileLayer(
        tiles="https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
        attr='Google', name='Google Satélite', overlay=False
    ).add_to(m)
    folium.LayerControl(position='topright', collapsed=False).add_to(m)
    LocateControl(flyTo=True).add_to(m)

    for _, reg in df_filtrado.iterrows():
        tec_key = str(reg.get('tecnolog', 'otros'))
        conf = mapa_config.get(tec_key, mapa_config["otros"])
        titulo_display = conf["titulo"]
        if tec_key.lower() == "otros":
            detalle = buscar_v(reg, ["Detalle_otras_fuentes_de_agua"])
            if detalle != "No reg.":
                titulo_display = detalle
        f_str = reg['fecha_limpia'].strftime('%d/%m/%Y')
        pop_html = (
            f"<div style='font-family:Arial; min-width:180px;'>"
            f"<b style='color:{conf['hex']}'>{titulo_display.upper()}</b><br>"
            f"<b>Fecha:</b> {f_str}<br><b>En Uso:</b> {reg.get('En_uso','-')}</div>"
        )
        folium.Marker(
            [reg['lat'], reg['lon']],
            popup=folium.Popup(pop_html, max_width=250),
            icon=folium.Icon(color=conf["color"], icon='tint')
        ).add_to(m)

    salida_mapa = st_folium(m, width="100%", height=600, key="mapa_agua", returned_objects=["last_object_clicked"])

    with st.sidebar:
        st.markdown('<div class="ficha-header">DATOS DEL RELEVAMIENTO</div>', unsafe_allow_html=True)
        punto_click = salida_mapa.get("last_object_clicked")
        if punto_click:
            lat, lon = punto_click['lat'], punto_click['lng']
            seleccion_df = df_filtrado[(abs(df_filtrado['lat'] - lat) < 0.001) & (abs(df_filtrado['lon'] - lon) < 0.001)]
            if not seleccion_df.empty:
                seleccion = seleccion_df.iloc[0]
                foto_url = next((seleccion[c] for c in df_filtrado.columns if 'URL' in c.upper() and isinstance(seleccion[c], str) and seleccion[c].startswith('http')), None)
                if foto_url:
                    st.image(foto_url, use_container_width=True)
                st.markdown(f"### {mapa_config.get(str(seleccion.get('tecnolog')), mapa_config['otros'])['titulo']}")
                datos_ficha = {
                    "📅 Fecha": ["fecha_limpia", None],
                    "🏗️ Estado de Obra": ["Estado_de_la_obra", "estado"],
                    "💧 Otras Fuentes": ["Detalle_otras_fuentes_de_agua", None],
                    "🛠️ Asistencia Técnica": ["Asistencia_t_cnica_de_la_obra", "asistencia"],
                    "✅ En Uso": ["En_uso", None],
                    "⚠️ Problemas": ["Problemas_asociados_al_No_uso", "problemas"],
                    "🧔 Usuario": ["Usuario", "usuario"],
                    "👨‍👩‍👧‍👦 Familias": ["Cantidad_de_familias_usuarias", None],
                    "🧪 Calidad Agua": ["Calidad_del_agua", "calidad"],
                    "🧼 Tratamiento": ["Realiza_treatment_del_agua_a", None],
                    "❓ Cuál tratamiento": ["Cual", None]
                }
                for etiqueta, (kws, tipo_mapa) in datos_ficha.items():
                    valor = buscar_v(seleccion, [kws])
                    if tipo_mapa:
                        valor = mapear_nombres_claros(valor, tipo_mapa)
                    st.write(f"**{etiqueta}:** {valor}")
            else:
                st.warning("No se encontraron datos para este punto.")
        else:
            st.info("💡 Haz clic en un marcador para ver la ficha técnica.")

    # ====== DASHBOARD DE ESTADÍSTICAS ======
    st.markdown("---")
    st.markdown("### 📊 Tablero de Resumen")
    st.write(f"✅ Registros filtrados: {len(df_filtrado)}")

    t1, t2, t3, t4, t5 = st.tabs([
        "🏗️ Tecnologías y Estado",
        "💧 Calidad y Asistencia",
        "👥 Usuarios",
        "⚠️ Problemas (No Uso)",
        "📍 Informe"
    ])

    # ---- Tab 1: Tecnologías y Estado
    with t1:
        c_pie1, c_pie2 = st.columns(2)
        with c_pie1:
            fig1 = px.pie(
                df_filtrado, names='tecnologia_txt', title="Porcentaje de Tecnologías",
                color='tecnologia_txt', color_discrete_map=colores_tecnologias, hole=0.3
            )
            fig1.update_traces(hovertemplate="%{label}<br>Porcentaje: %{percent}")
            st.plotly_chart(fig1, use_container_width=True)
        with c_pie2:
            fig2 = px.pie(df_filtrado, names='estado_txt', title="Estado de la Obra")
            fig2.update_traces(hovertemplate="%{label}<br>Cantidad: %{value}")
            st.plotly_chart(fig2, use_container_width=True)

    # ---- Tab 2: Calidad y Asistencia
    with t2:
        c_pie3, c_bar1 = st.columns(2)
        with c_pie3:
            fig3 = px.pie(df_filtrado, names='calidad_txt', title="Calidad de Agua")
            fig3.update_traces(hovertemplate="%{label}<br>Total: %{value}")
            st.plotly_chart(fig3, use_container_width=True)
        with c_bar1:
            asistencia_data = df_filtrado['asistencia_txt'].value_counts().reset_index()
            fig4 = px.bar(
                asistencia_data, x='asistencia_txt', y='count', title="Asistencia Técnica",
                labels={'count': 'Obras', 'asistencia_txt': 'Origen'}
            )
            fig4.update_traces(hovertemplate="Tipo: %{x}<br>Total: %{y}")
            st.plotly_chart(fig4, use_container_width=True)

    # ---- Tab 3: Usuarios
    with t3:
        usuario_data = df_filtrado['usuario_txt'].value_counts().reset_index()
        fig5 = px.bar(
            usuario_data, x='count', y='usuario_txt', orientation='h', title="Tipos de Usuarios",
            labels={'count': 'Registros', 'usuario_txt': 'Categoría'}
        )
        fig5.update_traces(hovertemplate="Usuario: %{y}<br>Cantidad: %{x}")
        st.plotly_chart(fig5, use_container_width=True)

    # ---- Tab 4: Problemas (No Uso)
    with t4:
        df_no_uso = df_filtrado[df_filtrado['En_uso'].astype(str).str.lower().str.contains('no', na=False)].copy()
        if not df_no_uso.empty:
            df_no_uso['prob_txt'] = df_no_uso['Problemas_asociados_al_No_uso'].apply(lambda x: mapear_nombres_claros(x, 'problemas'))
            prob_data = df_no_uso['prob_txt'].value_counts().reset_index()
            fig6 = px.bar(
                prob_data, x='count', y='prob_txt', orientation='h',
                title="Causas del No Uso (Obras Inactivas)",
                color='count', color_continuous_scale='Reds',
                labels={'count': 'Frecuencia', 'prob_txt': 'Motivo detectado'}
            )
            fig6.update_layout(yaxis={'categoryorder': 'total ascending'})
            fig6.update_traces(hovertemplate="Problema: %{y}<br>Obras afectadas: %{x}")
            st.plotly_chart(fig6, use_container_width=True)
        else:
            st.success("✨ ¡Genial! Según los filtros aplicados, todas las obras están en uso.")

    # ----------------------------
    # Tab 5: Resumen territorial
    # ----------------------------
    def _orden_tecnologias():
        # Orden tomado de tu XLS
        return [
            "Cisterna de consumo",
            "Pozo somero",
            "Pozo profundo",
            "Cisterna productiva",
            "Tanque australiano",
            "Represa",
            "Red de distribución",
            "Otros"
        ]

    def _orden_asistencia():
        return ["ONG", "Nación", "Provincia", "Propio", "Otros", "Sin asistencia"]

    def _orden_usuarios():
        return ["Comunidad indígena", "Familia rural criolla", "Escuelas", "Familias urbanas"]

    def _matriz_por_provincia(df_geo_local, columna_categoria, orden_columnas):
        """
        Devuelve {prov: DataFrame} con:
          - Filas: Departamentos + 'Totales'
          - Columnas: orden_columnas (faltantes = 0)
        Conteo robusto (size).
        """
        out = {}
        for prov in ["Salta", "Jujuy"]:
            dfp = df_geo_local[df_geo_local["Provincia_final"].fillna("Sin asignar") == prov].copy()
            if dfp.empty:
                out[prov] = pd.DataFrame(columns=orden_columnas, index=[])
                continue

            cat_series = dfp[columna_categoria].fillna("Otros")
            g = (
                dfp.assign(cat=cat_series)
                   .groupby(["Departamento", "cat"])
                   .size()
                   .unstack(fill_value=0)
            )

            # asegurar todas las columnas
            for c in orden_columnas:
                if c not in g.columns:
                    g[c] = 0
            g = g[orden_columnas]

            # Totales
            g.loc["Totales"] = g.sum()

            # ordenar filas alfabéticamente y dejar Totales al final
            if "Totales" in g.index:
                g = pd.concat([g.drop(index=["Totales"]).sort_index(), g.loc[["Totales"]]])

            out[prov] = g
        return out

    with t5:
        st.subheader("Resumen Territorial")

        # Etiquetas limpias
        df_geo['Provincia_final'] = df_geo['Provincia_final'].fillna('Sin asignar')
        df_geo['Departamento'] = df_geo['Departamento'].fillna('Sin asignar')

        # Usuarios -> mapear a las 4 categorías del XLS, resto = "Otros"
        df_geo['usuario_xls'] = df_geo['usuario_txt']
        df_geo.loc[~df_geo['usuario_xls'].isin(_orden_usuarios()), 'usuario_xls'] = 'Otros'

        # --- 1) Tecnologías ---
        tec_por_prov = _matriz_por_provincia(
            df_geo.assign(tecnologia_txt=df_geo['tecnologia_txt'].fillna("Otros")),
            columna_categoria="tecnologia_txt",
            orden_columnas=_orden_tecnologias()
        )
        st.markdown("**1.- Tecnologías**")
        for prov in ["Salta", "Jujuy"]:
            st.markdown(f"**Provincia: {prov}**")
            if tec_por_prov[prov].empty:
                st.info("Sin registros para los filtros aplicados.")
            else:
                st.dataframe(tec_por_prov[prov], use_container_width=True)
        st.markdown("---")

        # --- 2) Asistencia Técnica ---
        asis_por_prov = _matriz_por_provincia(
            df_geo.assign(asistencia_txt=df_geo['asistencia_txt'].fillna("Sin asistencia")),
            columna_categoria="asistencia_txt",
            orden_columnas=_orden_asistencia()
        )
        st.markdown("**2.- Asistencia Técnica**")
        for prov in ["Salta", "Jujuy"]:
            st.markdown(f"**Provincia: {prov}**")
            if asis_por_prov[prov].empty:
                st.info("Sin registros para los filtros aplicados.")
            else:
                st.dataframe(asis_por_prov[prov], use_container_width=True)
        st.markdown("---")

        # --- 3) Usuarios ---
        usu_por_prov = _matriz_por_provincia(
            df_geo,
            columna_categoria="usuario_xls",
            orden_columnas=_orden_usuarios() + ["Otros"]
        )
        st.markdown("**3.- Usuarios**")
        for prov in ["Salta", "Jujuy"]:
            st.markdown(f"**Provincia: {prov}**")
            if usu_por_prov[prov].empty:
                st.info("Sin registros para los filtros aplicados.")
            else:
                st.dataframe(usu_por_prov[prov], use_container_width=True)
        st.markdown("---")

        # --- Indicador de consistencia de totales (auditoría rápida) ---
        total_reg = len(df_geo)
        total_tec = 0
        for prov in ["Salta", "Jujuy"]:
            if not tec_por_prov[prov].empty and "Totales" in tec_por_prov[prov].index:
                total_tec += tec_por_prov[prov].loc["Totales"].sum()
        st.caption(f"Total de registros (filtros aplicados): **{total_reg}** ")

        # ============ Descargas (PDF / XLSX) ============
        st.markdown("### Descargas")
        col_pdf, col_xls = st.columns(2)

        with col_pdf:
            pdf_bytes = construir_pdf_xls(tec_por_prov, asis_por_prov, usu_por_prov)
            st.download_button(
                label="📥 Descargar Informe PDF",
                data=pdf_bytes,
                file_name="informe_mesa_agua.pdf",
                mime="application/pdf",
                use_container_width=True
            )

        with col_xls:
            xlsx_bytes = construir_xlsx(tec_por_prov, asis_por_prov, usu_por_prov)
            st.download_button(
                label="📥 Descargar Resumen XLSX",
                data=xlsx_bytes,
                file_name="resumen_mesa_agua.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )

    # --- INFO INSTITUCIONAL COMPLETA ---
    st.markdown("---")

    INFO_MESA_MD = """
La Mesa de Agua ha promovido el mapeo e integración de más de 300 obras de agua en estos departamentos. La base de datos generada, sistematiza la localización, tipo de tecnología, población beneficiaria y estado funcional de cada obra, y se actualiza periódicamente con la colaboración de INTA, FUNDAPAZ, ONG, Gobierno Provincial, municipios y comunidades.

El mapeo digital de obras de agua en el Chaco Salteño, es una iniciativa impulsada por la Mesa de Agua con el objetivo de relevar, sistematizar y visualizar de manera accesible las obras de agua existentes y en desarrollo en el territorio. Este instrumento busca contribuir a una gestión más eficiente, equitativa y transparente del acceso al agua, poniendo en valor el conocimiento construido colectivamente.
        
Propósito: Fortalecer el plan de seguimiento de obras, ya que está concebida como una base de datos viva, con actualización en línea y con capacidad para analizar el uso, el estado y la calidad de las obras construidas.

**Equipo de trabajo:**
INTA, FUNDAPAZ, ONG, Gobierno Provincial, municipios y comunidades

Para más información, podés contactarnos en: elena.hernan@inta.gob.ar
"""

    with st.expander("ℹ️ Información sobre la Mesa de Agua"):
        st.markdown(INFO_MESA_MD, unsafe_allow_html=True)

else:
    st.warning("No hay datos para los filtros seleccionados.")
