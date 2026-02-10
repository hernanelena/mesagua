import streamlit as st
import pandas as pd
import requests
import folium
from streamlit_folium import st_folium
from folium.plugins import LocateControl
from datetime import datetime
import plotly.express as px

# 1. CONFIGURACIÓN
st.set_page_config(page_title="MAPA MESA DE AGUA", layout="wide")

FORM_ID = "aHNGU6dn2MFGMpg9Y5M5sn"
TOKEN = st.secrets["KOBO_TOKEN"]
URL_MESAAGUA = f"https://territorios.inta.gob.ar/api/v2/assets/{FORM_ID}/data/?format=json"
HEADERS = {'Authorization': f'Token {TOKEN}'}

# --- ESTILOS CSS ---
st.markdown("""
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
""", unsafe_allow_html=True)

# --- FUNCIÓN PARA PASAR A NOMBRES CLAROS ---
def mapear_nombres_claros(valor, tipo):
    mapeos_maestros = {
        "asistencia": {
            "ong": "ONG", "instituci_n_nacionales": "Nación", "instituci_n_provinciales": "Provincia",
            "propio": "Propio", "otros": "Otros", "sin_asistencia": "Sin asistencia"
        },
        "estado": {"bueno": "Bueno", "regular": "Regular", "malo": "Malo"},
        "calidad": {"buena": "Buena", "regular": "Regular", "mala": "Mala"},
        "usuario": {
            "csalud": "Centro de salud", "com_ind": "Comununidad indígena",
            "escuelas": "Escuela", "prod_af": "Familia rural criolla", "furbanas": "Familia urbana"
        },
        "problemas": {
            "cantidad_calidad_del_agua": "Cantidad/Calidad del agua",
            "sistema_de_captaci_n__bomba__t": "Sistema de captación (bomba, techo colector, toma)",
            "sistema_de_conducci_n__manguer": "Sistema de conducción (mangueras, cañerias)",
            "sistema_de_almacenamiento__cis": "Sistema de almacenamiento (cisterna, tanque)"
        }
    }
    v_clean = str(valor).lower().strip()
    if tipo == "problemas":
        return mapeos_maestros["problemas"].get(v_clean, "Otras")
    return mapeos_maestros.get(tipo, {}).get(v_clean, valor)

@st.cache_data(ttl=60)
def cargar_datos():
    try:
        r = requests.get(URL_MESAAGUA, headers=HEADERS)
        data = r.json()
        df = pd.json_normalize(data.get('results', []))
        df.columns = [c.split('.')[-1].split('/')[-1] for c in df.columns]
        col_fecha = next((c for c in df.columns if 'fecha' in c.lower() or 'relevamiento' in c.lower()), None)
        df['fecha_limpia'] = pd.to_datetime(df[col_fecha], errors='coerce') if col_fecha else pd.to_datetime(datetime.now())
        def obtener_gps(row):
            geo = row.get('_geolocation')
            if isinstance(geo, list) and len(geo) >= 2: return float(geo[0]), float(geo[1])
            return None, None
        res = df.apply(obtener_gps, axis=1)
        df['lat'], df['lon'] = zip(*res)
        return df.dropna(subset=['lat', 'lon'])
    except Exception as e:
        st.error(f"Error: {e}")
        return pd.DataFrame()

df_raw = cargar_datos()

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
            if pd.isnull(val) or str(val).lower() == 'none': return "No reg."
            if isinstance(val, (pd.Timestamp, datetime)): return val.strftime('%d/%m/%Y')
            return str(val)
    return "No reg."

st.markdown('<div class="titulo-responsive">💧 Mesa de Agua</div>', unsafe_allow_html=True)

if not df_raw.empty:
    with st.expander("🔍 Opciones de Filtro", expanded=False):
        c1, c2, c3, c4 = st.columns(4)
        with c1: fecha_desde = st.date_input("Inicio", value=df_raw['fecha_limpia'].min().date())
        with c2: fecha_hasta = st.date_input("Fin", value=df_raw['fecha_limpia'].max().date())
        with c3:
            listado_tec = ["Todas"] + [v["titulo"] for v in mapa_config.values()]
            tec_filtro = st.selectbox("Tecnología", listado_tec)
        with c4:
            opciones_uso = ["Todos"]
            if 'En_uso' in df_raw.columns: opciones_uso += sorted(df_raw['En_uso'].dropna().unique().tolist())
            uso_filtro = st.selectbox("¿En Uso?", opciones_uso)

    mask = (df_raw['fecha_limpia'].dt.date >= fecha_desde) & (df_raw['fecha_limpia'].dt.date <= fecha_hasta)
    if tec_filtro != "Todas":
        tec_key_buscada = next(k for k, v in mapa_config.items() if v["titulo"] == tec_filtro)
        mask &= (df_raw['tecnolog'] == tec_key_buscada)
    if uso_filtro != "Todos": mask &= (df_raw['En_uso'] == uso_filtro)
    df_filtrado = df_raw[mask].copy()
else:
    df_filtrado = pd.DataFrame()

if not df_filtrado.empty:
    # --- PARTE DEL MAPA ---
    m = folium.Map(location=[df_filtrado['lat'].mean(), df_filtrado['lon'].mean()], zoom_start=8, tiles=None)
    folium.TileLayer(tiles="https://wms.ign.gob.ar/geoserver/gwc/service/tms/1.0.0/capabaseargenmap@EPSG%3A3857@png/{z}/{x}/{-y}.png", attr='IGN', name='Argenmap (IGN)', overlay=False).add_to(m)
    folium.TileLayer(tiles="https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}", attr='Google', name='Google Satélite', overlay=False).add_to(m)
    folium.LayerControl(position='topright', collapsed=False).add_to(m)
    LocateControl(flyTo=True).add_to(m)

    for _, reg in df_filtrado.iterrows():
        tec_key = str(reg.get('tecnolog', 'otros'))
        conf = mapa_config.get(tec_key, mapa_config["otros"])
        titulo_display = conf["titulo"]
        if tec_key.lower() == "otros":
            detalle = buscar_v(reg, ["Detalle_otras_fuentes_de_agua"])
            if detalle != "No reg.": titulo_display = detalle
        f_str = reg['fecha_limpia'].strftime('%d/%m/%Y')
        pop_html = f"<div style='font-family:Arial; min-width:180px;'><b style='color:{conf['hex']}'>{titulo_display.upper()}</b><br><b>Fecha:</b> {f_str}<br><b>En Uso:</b> {reg.get('En_uso','-')}</div>"
        folium.Marker([reg['lat'], reg['lon']], popup=folium.Popup(pop_html, max_width=250), icon=folium.Icon(color=conf["color"], icon='tint')).add_to(m)

    # --- LLAMADA AL MAPA CON returned_objects ---
    salida_mapa = st_folium(
        m, 
        width="100%", 
        height=600, 
        key="mapa_agua",
        returned_objects=["last_object_clicked"] 
    )

    with st.sidebar:
        st.markdown('<div class="ficha-header">DATOS DEL RELEVAMIENTO</div>', unsafe_allow_html=True)
        punto_click = salida_mapa.get("last_object_clicked")
        if punto_click:
            lat, lon = punto_click['lat'], punto_click['lng']
            seleccion_df = df_filtrado[(abs(df_filtrado['lat'] - lat) < 0.001) & (abs(df_filtrado['lon'] - lon) < 0.001)]
            
            if not seleccion_df.empty:
                seleccion = seleccion_df.iloc[0]
                foto_url = next((seleccion[c] for c in df_filtrado.columns if 'URL' in c.upper() and isinstance(seleccion[c], str) and seleccion[c].startswith('http')), None)
                if foto_url: st.image(foto_url, use_container_width=True)
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
                    if tipo_mapa: valor = mapear_nombres_claros(valor, tipo_mapa)
                    st.write(f"**{etiqueta}:** {valor}")
            else:
                st.warning("No se encontraron datos para este punto.")
        else:
            st.info("💡 Haz clic en un marcador para ver la ficha técnica.")

    # --- DASHBOARD DE ESTADÍSTICAS ---
    st.markdown("---")
    st.markdown("### 📊 Tablero de Resumen")
    st.write(f"✅ Registros filtrados: {len(df_filtrado)}")
    t1, t2, t3, t4 = st.tabs(["🏗️ Tecnologías y Estado", "💧 Calidad y Asistencia", "👥 Usuarios", "⚠️ Problemas (No Uso)"])

    with t1:
        c_pie1, c_pie2 = st.columns(2)
        with c_pie1:
            df_filtrado['tecnologia_txt'] = df_filtrado['tecnolog'].apply(lambda x: mapa_config.get(str(x), mapa_config["otros"])["titulo"])
            fig1 = px.pie(df_filtrado, names='tecnologia_txt', title="Porcentaje de Tecnologías", 
                          color='tecnologia_txt', color_discrete_map=colores_tecnologias, hole=0.3)
            fig1.update_traces(hovertemplate="%{label}<br>Porcentaje: %{percent}")
            st.plotly_chart(fig1, use_container_width=True)
        with c_pie2:
            df_filtrado['estado_txt'] = df_filtrado['Estado_de_la_obra'].apply(lambda x: mapear_nombres_claros(x, 'estado'))
            fig2 = px.pie(df_filtrado, names='estado_txt', title="Estado de la Obra")
            fig2.update_traces(hovertemplate="%{label}<br>Cantidad: %{value}")
            st.plotly_chart(fig2, use_container_width=True)

    with t2:
        c_pie3, c_bar1 = st.columns(2)
        with c_pie3:
            df_filtrado['calidad_txt'] = df_filtrado['Calidad_del_agua'].apply(lambda x: mapear_nombres_claros(x, 'calidad'))
            fig3 = px.pie(df_filtrado, names='calidad_txt', title="Calidad de Agua")
            fig3.update_traces(hovertemplate="%{label}<br>Total: %{value}")
            st.plotly_chart(fig3, use_container_width=True)
        with c_bar1:
            df_filtrado['asistencia_txt'] = df_filtrado['Asistencia_t_cnica_de_la_obra'].apply(lambda x: mapear_nombres_claros(x, 'asistencia'))
            asistencia_data = df_filtrado['asistencia_txt'].value_counts().reset_index()
            fig4 = px.bar(asistencia_data, x='asistencia_txt', y='count', title="Asistencia Técnica",
                          labels={'count':'Obras', 'asistencia_txt':'Origen'})
            fig4.update_traces(hovertemplate="Tipo: %{x}<br>Total: %{y}")
            st.plotly_chart(fig4, use_container_width=True)

    with t3:
        df_filtrado['usuario_txt'] = df_filtrado['Usuario'].apply(lambda x: mapear_nombres_claros(x, 'usuario'))
        usuario_data = df_filtrado['usuario_txt'].value_counts().reset_index()
        fig5 = px.bar(usuario_data, x='count', y='usuario_txt', orientation='h', title="Tipos de Usuarios",
                      labels={'count':'Registros', 'usuario_txt':'Categoría'})
        fig5.update_traces(hovertemplate="Usuario: %{y}<br>Cantidad: %{x}")
        st.plotly_chart(fig5, use_container_width=True)

    with t4:
        df_no_uso = df_filtrado[df_filtrado['En_uso'].astype(str).str.lower().str.contains('no', na=False)].copy()
        if not df_no_uso.empty:
            df_no_uso['prob_txt'] = df_no_uso['Problemas_asociados_al_No_uso'].apply(lambda x: mapear_nombres_claros(x, 'problemas'))
            prob_data = df_no_uso['prob_txt'].value_counts().reset_index()
            fig6 = px.bar(prob_data, x='count', y='prob_txt', orientation='h', 
                          title="Causas del No Uso (Obras Inactivas)",
                          color='count', color_continuous_scale='Reds',
                          labels={'count':'Frecuencia', 'prob_txt':'Motivo detectado'})
            fig6.update_layout(yaxis={'categoryorder':'total ascending'})
            fig6.update_traces(hovertemplate="Problema: %{y}<br>Obras afectadas: %{x}")
            st.plotly_chart(fig6, use_container_width=True)
        else:
            st.success("✨ ¡Genial! Según los filtros aplicados, todas las obras están en uso.")

    
else:
    st.warning("No hay datos para los filtros seleccionados.")



