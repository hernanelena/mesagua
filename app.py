# ============================================
# APP: MAPA MESA DE AGUA
# Resumen territorial estilo XLS + PDF + Asignación espacial
# ============================================

import os
import json
import base64
from io import BytesIO
from datetime import datetime
import requests
import pandas as pd
import streamlit as st
import folium
from streamlit_folium import st_folium
from folium.plugins import LocateControl, Geocoder
import plotly.express as px
from branca.element import MacroElement
from jinja2 import Template
import math
from PIL import Image as PILImage, ImageDraw, ImageEnhance, ImageOps
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas
from functools import partial
from reportlab.platypus import Image as RLImage

# Shapely (point-in-polygon)
try:
    from shapely.geometry import shape, Point
    SHAPELY_OK = True
except Exception:
    SHAPELY_OK = False


def formatear_asistencia(row):
    # Recupera el valor original de la asistencia técnica
    val_raw = None
    for col in ['asistencia_t_cnica_de_la_obra', 'Asistencia_t_cnica_de_la_obra', 'asistencia']:
        if col in row and pd.notna(row[col]):
            val_raw = row[col]
            break

    txt_base = mapear_nombres_claros(val_raw, 'asistencia')

    # Verifica si solicita ampliar respuesta y si hay contenido en el detalle
    desea_ampliar = str(row.get('Desea_ampliar_su_respuesta', '')).strip().lower()
    detalle = row.get('Detalle_de_la_asistencia_t_cnica')

    if desea_ampliar == 'si' and pd.notna(detalle) and str(detalle).strip() not in ['', 'None', 'nan']:
        return f"{txt_base} ({str(detalle).strip()})"
    
    return txt_base


@st.cache_data(ttl=3600, show_spinner=False)
def obtener_foto_api(form_id, registro_id, id_adjunto, token):
    """Descarga la fotografía a demanda y la guarda en memoria por 1 hora."""
    if not (registro_id and id_adjunto and token):
        return None
    
    url_final = f"https://territorios.inta.gob.ar/api/v2/assets/{form_id}/data/{int(registro_id)}/attachments/{id_adjunto}/"
    headers_api = {"Authorization": f"Token {token}"}
    
    try:
        res = requests.get(url_final, headers=headers_api, timeout=6)
        if res.status_code == 200:
            content_type = res.headers.get('Content-Type', '').lower()
            
            # Si responde un objeto JSON con URL firmada
            if 'json' in content_type:
                meta = res.json()
                url_descarga = meta.get('download_url') or meta.get('file_url')
                if url_descarga:
                    res = requests.get(url_descarga, headers=headers_api, timeout=6)
            
            # Si el contenido obtenido es una imagen válida
            if res.status_code == 200 and 'image' in res.headers.get('Content-Type', '').lower():
                return res.content
    except (requests.exceptions.RequestException, Exception):
        return None
        
    return None


def latlon_to_tile(lat, lon, zoom):
    """Convierte coordenadas Lat/Lon a índices de Tile OSM/CartoDB."""
    lat_rad = math.radians(lat)
    n = 2.0 ** zoom
    xtile = int((lon + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return xtile, ytile

def tile_to_pix_offset(lat, lon, zoom, xtile, ytile):
    """Calcula el offset en píxeles exacto dentro del tile."""
    n = 2.0 ** zoom
    x_exact = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y_exact = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return int((x_exact - xtile) * 256), int((y_exact - ytile) * 256)


def generar_mapa_politico_bytes(lat: float, lon: float) -> bytes:
    """
    Genera un mapa de contexto con rutas, límites y etiquetas nítidas y contrastadas.
    """
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    
    try:
        # Encuadre para contexto regional/provincial
        delta_lat = 0.7 
        delta_lon = 1.1
        bbox = f"{lon - delta_lon},{lat - delta_lat},{lon + delta_lon},{lat + delta_lat}"
        
        # Se cambia a World_Street_Map para renderizar rutas, nombres y límites claros
        url_mapa = (
            f"https://services.arcgisonline.com/arcgis/rest/services/"
            f"World_Street_Map/MapServer/export?"
            f"bbox={bbox}&bboxSR=4326&imageSR=4326&size=1600,500&dpi=180&format=jpg&f=image"
        )
        
        r = requests.get(url_mapa, headers=headers, timeout=8)
        if r.status_code == 200 and len(r.content) > 1000:
            img = PILImage.open(BytesIO(r.content)).convert("RGB")
            
            # --- MEJORA DE NITIDEZ Y CONTRASTE ---
            img = ImageEnhance.Contrast(img).enhance(1.4)
            img = ImageEnhance.Color(img).enhance(1.2)
            
            draw = ImageDraw.Draw(img)
            cx, cy = img.width // 2, img.height // 2
            r_pin = 14
            
            # Marcador rojo resaltado con borde negro para el punto exacto
            draw.ellipse((cx - r_pin - 4, cy - r_pin - 4, cx + r_pin + 4, cy + r_pin + 4), fill="black")
            draw.ellipse((cx - r_pin, cy - r_pin, cx + r_pin, cy + r_pin), fill="#E63946")
            
            out = BytesIO()
            img.save(out, format='JPEG', quality=95)
            return out.getvalue()
    except Exception:
        pass

    return None


def escalar_imagen_proporcional(foto_bytes, max_ancho, max_alto):
    """
    Toma bytes de imagen, corrige la orientación EXIF (fotos verticales de celular)
    y genera un RLImage respetando la relación de aspecto sin deformarla.
    """
    try:
        img_pil = PILImage.open(BytesIO(foto_bytes))
        
        # --- AQUÍ SE CORRIGE LA ROTACIÓN AUTOMÁTICAMENTE ---
        img_pil = ImageOps.exif_transpose(img_pil)
        
        if img_pil.mode != 'RGB':
            img_pil = img_pil.convert('RGB')

        ancho_orig, alto_orig = img_pil.size
        
        # Calcular factor de escala para encajar en el contenedor
        escala_w = max_ancho / float(ancho_orig)
        escala_h = max_alto / float(alto_orig)
        escala = min(escala_w, escala_h)
        
        ancho_final = ancho_orig * escala
        alto_final = alto_orig * escala
        
        # Guardar la imagen rotada en un buffer para ReportLab
        out_buffer = BytesIO()
        img_pil.save(out_buffer, format='JPEG', quality=90)
        out_buffer.seek(0)
        
        return RLImage(out_buffer, width=ancho_final, height=alto_final)
    except Exception:
        return None


def construir_pdf_ficha_individual(seleccion, foto_bytes=None) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=1.5*cm,
        rightMargin=1.5*cm,
        topMargin=1.5*cm,
        bottomMargin=1.5*cm
    )
    styles = getSampleStyleSheet()
    
    # Estilos de texto
    titulo_header_style = ParagraphStyle(
        'HeaderTitulo',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=12,
        textColor=colors.HexColor('#1E3A8A'),
        alignment=1,
        spaceAfter=2
    )
    
    subtitulo_header_style = ParagraphStyle(
        'HeaderSubTitulo',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        textColor=colors.HexColor('#475569'),
        alignment=1,
        spaceAfter=2
    )

    titulo_ficha_style = ParagraphStyle(
        'TituloFicha',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=14,
        textColor=colors.HexColor('#1E3A8A'),
        alignment=1,
        spaceAfter=8
    )
    
    label_style = ParagraphStyle('LabelStyle', fontName='Helvetica-Bold', fontSize=8, textColor=colors.HexColor('#1E3A8A'), alignment=1)
    val_style = ParagraphStyle('ValStyle', fontName='Helvetica', fontSize=9, textColor=colors.black)

    elems = []

    # --- ENCABEZADO INSTITUCIONAL ---
    header_textos = [
        Paragraph("<b>MESA DEL AGUA PARA EL CHACO SALTEÑO</b>", titulo_header_style),
        Paragraph("Mapeo Digital de Obras e Infraestructura Hídrica", subtitulo_header_style),
        Paragraph("INTA • FUNDAPAZ • Gobierno de Salta", subtitulo_header_style)
    ]
    
    tabla_encabezado = Table([[header_textos]], colWidths=[18*cm])
    tabla_encabezado.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#F1F5F9')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CBD5E1')),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    
    elems.append(tabla_encabezado)
    elems.append(Spacer(1, 0.3*cm))

    # Título del documento
    elems.append(Paragraph("FICHA TÉCNICA DE RELEVAMIENTO", titulo_ficha_style))
    elems.append(Spacer(1, 0.2*cm))

    # --- DATOS DEL RELEVAMIENTO ---
    tecnologia_codigo = str(seleccion.get('tecnolog', 'otros')).strip()
    config_tec = mapa_config.get(tecnologia_codigo, mapa_config['otros'])
    titulo_ficha = config_tec['titulo']
    if tecnologia_codigo.lower() == 'otros' or titulo_ficha.lower() == 'otros':
        detalle_otros = seleccion.get('Detalle_otras_fuentes_de_agua')
        if pd.notna(detalle_otros) and str(detalle_otros).strip() != "":
            titulo_ficha = str(detalle_otros).strip()

    fecha_val = seleccion.get('fecha_limpia')
    fecha_str = fecha_val.strftime('%d/%m/%Y') if pd.notna(fecha_val) and isinstance(fecha_val, pd.Timestamp) else "No reg."

    # Obtención de Tratamiento del Agua
    realiza_tratamiento = seleccion.get('Realiza_tratamiento_del_agua_a', 'No reg.')
    cual_tratamiento = seleccion.get('Cual')

    detalles_data = [
        [Paragraph("Obra / Tecnología:", ParagraphStyle('L', fontName='Helvetica-Bold', fontSize=9, textColor=colors.HexColor('#1E3A8A'))), Paragraph(f"<b>{titulo_ficha}</b>", val_style)],
        [Paragraph("Fecha Relevamiento:", ParagraphStyle('L', fontName='Helvetica-Bold', fontSize=9, textColor=colors.HexColor('#1E3A8A'))), Paragraph(fecha_str, val_style)],
        [Paragraph("Provincia / Depto:", ParagraphStyle('L', fontName='Helvetica-Bold', fontSize=9, textColor=colors.HexColor('#1E3A8A'))), Paragraph(f"{seleccion.get('Provincia_final', 'No reg.')} - {seleccion.get('Departamento', 'No reg.')}", val_style)],
        [Paragraph("Estado de Obra:", ParagraphStyle('L', fontName='Helvetica-Bold', fontSize=9, textColor=colors.HexColor('#1E3A8A'))), Paragraph(str(seleccion.get('estado_txt', 'No reg.')), val_style)],
        [Paragraph("¿En Uso?:", ParagraphStyle('L', fontName='Helvetica-Bold', fontSize=9, textColor=colors.HexColor('#1E3A8A'))), Paragraph(str(seleccion.get('En_uso', 'No reg.')), val_style)],
        [Paragraph("Asistencia Técnica:", ParagraphStyle('L', fontName='Helvetica-Bold', fontSize=9, textColor=colors.HexColor('#1E3A8A'))), Paragraph(str(seleccion.get('asistencia_txt', 'No reg.')), val_style)],
        [Paragraph("Tipo de Usuario:", ParagraphStyle('L', fontName='Helvetica-Bold', fontSize=9, textColor=colors.HexColor('#1E3A8A'))), Paragraph(str(seleccion.get('usuario_txt', 'No reg.')), val_style)],
        [Paragraph("Familias Usuarias:", ParagraphStyle('L', fontName='Helvetica-Bold', fontSize=9, textColor=colors.HexColor('#1E3A8A'))), Paragraph(str(seleccion.get('Cantidad_de_familias_usuarias', 'No reg.')), val_style)],
        [Paragraph("Calidad del Agua:", ParagraphStyle('L', fontName='Helvetica-Bold', fontSize=9, textColor=colors.HexColor('#1E3A8A'))), Paragraph(str(seleccion.get('calidad_txt', 'No reg.')), val_style)],
        [Paragraph("Tratamiento:", ParagraphStyle('L', fontName='Helvetica-Bold', fontSize=9, textColor=colors.HexColor('#1E3A8A'))), Paragraph(str(realiza_tratamiento), val_style)],
    ]

    # Condicionado: Si realiza tratamiento = "si", muestra qué tratamiento realiza
    if str(realiza_tratamiento).strip().lower() in ['si', 'sí'] and pd.notna(cual_tratamiento) and str(cual_tratamiento).strip() not in ['', 'None', 'nan']:
        detalles_data.append([Paragraph("Cuál Tratamiento:", ParagraphStyle('L', fontName='Helvetica-Bold', fontSize=9, textColor=colors.HexColor('#1E3A8A'))), Paragraph(str(cual_tratamiento).strip(), val_style)])

    detalles_data.append([Paragraph("Coordenadas:", ParagraphStyle('L', fontName='Helvetica-Bold', fontSize=9, textColor=colors.HexColor('#1E3A8A'))), Paragraph(f"{float(seleccion.get('lat')):.5f}, {float(seleccion.get('lon')):.5f}", val_style)])

    if str(seleccion.get('En_uso', '')).lower() == 'no':
        prob_val = mapear_nombres_claros(seleccion.get('Problemas_asociados_al_No_uso'), 'problemas')
        if prob_val == "Otras":
            ampliar_txt = seleccion.get('Ampliar_la_respuesta')
            if pd.notna(ampliar_txt) and str(ampliar_txt).strip() not in ['', 'None', 'nan']:
                prob_val = f"Otras: {str(ampliar_txt).strip()}"
        if prob_val:
            detalles_data.append([Paragraph("Causa Inactividad:", ParagraphStyle('L', fontName='Helvetica-Bold', fontSize=9, textColor=colors.HexColor('#1E3A8A'))), Paragraph(prob_val, val_style)])

    tabla_detalles = Table(detalles_data, colWidths=[4.2*cm, 13.8*cm])
    tabla_detalles.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#F8FAFC')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CBD5E1')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    elems.append(tabla_detalles)
    elems.append(Spacer(1, 0.3*cm))

    # --- GENERACIÓN DE IMÁGENES Y MAPAS ---
    lat = float(seleccion.get('lat'))
    lon = float(seleccion.get('lon'))

    # 1. Foto de la obra
    img_foto_obj = escalar_imagen_proporcional(foto_bytes, 8.5 * cm, 5.0 * cm) if foto_bytes else None

    # 2. Mapa Satelital Detalle (Zoom)
    img_sat_obj = None
    try:
        delta_zoom = 0.005
        bbox_zoom = f"{lon - delta_zoom},{lat - delta_zoom},{lon + delta_zoom},{lat + delta_zoom}"
        url_sat = f"https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/export?bbox={bbox_zoom}&bboxSR=4326&imageSR=4326&size=600,600&dpi=150&format=jpg&f=image"
        r_sat = requests.get(url_sat, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        if r_sat.status_code == 200:
            img = PILImage.open(BytesIO(r_sat.content)).convert("RGB")
            draw = ImageDraw.Draw(img)
            cx, cy = img.width // 2, img.height // 2
            r = 12
            draw.ellipse((cx - r - 3, cy - r - 3, cx + r + 3, cy + r + 3), fill="black")
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill="#FFD700")  # Punto Amarillo
            out_sat = BytesIO()
            img.save(out_sat, format='JPEG', quality=95)
            img_sat_obj = escalar_imagen_proporcional(out_sat.getvalue(), 8.5 * cm, 5.0 * cm)
    except Exception:
        img_sat_obj = None

    # 3. Mapa de Ubicación Departamental / Provincial (Político)
    img_contexto_obj = None
    try:
        bytes_politico = generar_mapa_politico_bytes(lat, lon)
        if bytes_politico:
            img_contexto_obj = escalar_imagen_proporcional(bytes_politico, 18.0 * cm, 6.0 * cm)
    except Exception:
        img_contexto_obj = None

    # --- ESTRUCTURA DE TABLAS EN EL PDF ---
    fila_top_titulos = []
    fila_top_imgs = []

    if img_sat_obj:
        fila_top_titulos.append(Paragraph("<b>Vista Satelital (Detalle)</b>", label_style))
        fila_top_imgs.append(img_sat_obj)

    if img_foto_obj:
        fila_top_titulos.append(Paragraph("<b>Fotografía de la Obra</b>", label_style))
        fila_top_imgs.append(img_foto_obj)

    if fila_top_imgs:
        col_w = 18.0 / len(fila_top_imgs)
        t_top = Table([fila_top_titulos, fila_top_imgs], colWidths=[col_w * cm] * len(fila_top_imgs))
        t_top.setStyle(TableStyle([
            ('ALIGN', (0,0), (-1,-1), 'CENTER'),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#F1F5F9')),
            ('BOX', (0,0), (-1,-1), 0.5, colors.black),
            ('INNERGRID', (0,0), (-1,-1), 0.5, colors.black),
            ('TOPPADDING', (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ]))
        elems.append(t_top)
        elems.append(Spacer(1, 0.3*cm))

    # Fila Inferior: Ubicación Departamental
    if img_contexto_obj:
        t_bottom = Table([
            [Paragraph("<b>Ubicación Departamental / Provincial</b>", label_style)],
            [img_contexto_obj]
        ], colWidths=[18.0 * cm])
        t_bottom.setStyle(TableStyle([
            ('ALIGN', (0,0), (-1,-1), 'CENTER'),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#F1F5F9')),
            ('BOX', (0,0), (-1,-1), 0.5, colors.black),
            ('INNERGRID', (0,0), (-1,-1), 0.5, colors.black),
            ('TOPPADDING', (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ]))
        elems.append(t_bottom)

    doc.build(elems)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes


class BuscadorArgenmap(MacroElement):
    def __init__(self):
        super().__init__()
        self._template = Template(u"""
        {% macro script(this, kwargs) %}
            var geocoderControl = L.Control.extend({
                options: { position: 'topleft' },
                onAdd: function (map) {
                    var container = L.DomUtil.create('div', 'leaflet-bar leaflet-control');
                    container.style.backgroundColor = 'white';
                    container.style.position = 'relative';
                    container.style.borderRadius = '4px';
                    container.style.boxShadow = '0 1px 5px rgba(0,0,0,0.4)';
                    
                    var toggleBtn = L.DomUtil.create('a', '', container);
                    toggleBtn.href = '#';
                    toggleBtn.innerHTML = '🔍';
                    toggleBtn.style.display = 'block';
                    toggleBtn.style.width = '30px';
                    toggleBtn.style.height = '30px';
                    toggleBtn.style.lineHeight = '30px';
                    toggleBtn.style.textAlign = 'center';
                    toggleBtn.style.textDecoration = 'none';
                    toggleBtn.style.fontSize = '14px';
                    toggleBtn.style.color = '#333';

                    var searchBox = L.DomUtil.create('div', '', container);
                    searchBox.style.display = 'none';
                    searchBox.style.position = 'absolute';
                    searchBox.style.top = '0';
                    searchBox.style.left = '32px';
                    searchBox.style.backgroundColor = 'white';
                    searchBox.style.padding = '3px';
                    searchBox.style.borderRadius = '0 4px 4px 0';
                    searchBox.style.boxShadow = '2px 1px 5px rgba(0,0,0,0.2)';
                    
                    var input = L.DomUtil.create('input', '', searchBox);
                    input.type = 'text';
                    input.placeholder = 'Buscar localidad...';
                    input.style.width = '200px';
                    input.style.border = '1px solid #ccc';
                    input.style.outline = 'none';
                    input.style.padding = '4px 6px';
                    input.style.fontSize = '12px';
                    input.style.borderRadius = '3px';

                    var resultsList = L.DomUtil.create('div', '', searchBox);
                    resultsList.style.position = 'absolute';
                    resultsList.style.top = '100%';
                    resultsList.style.left = '0';
                    resultsList.style.width = '100%';
                    resultsList.style.maxHeight = '200px';
                    resultsList.style.overflowY = 'auto';
                    resultsList.style.backgroundColor = 'white';
                    resultsList.style.border = '1px solid #ccc';
                    resultsList.style.borderTop = 'none';
                    resultsList.style.boxShadow = '0px 4px 6px rgba(0,0,0,0.1)';
                    resultsList.style.display = 'none';
                    resultsList.style.zIndex = '1000';

                    L.DomEvent.disableClickPropagation(container);
                    L.DomEvent.disableScrollPropagation(resultsList);

                    toggleBtn.onclick = function(e) {
                        L.DomEvent.preventDefault(e);
                        if (searchBox.style.display === 'none') {
                            searchBox.style.display = 'block';
                            input.focus();
                        } else {
                            searchBox.style.display = 'none';
                            resultsList.style.display = 'none';
                        }
                    };

                    var timer = null;

                    input.oninput = function() {
                        clearTimeout(timer);
                        var query = input.value.trim();
                        if (query.length < 3) {
                            resultsList.innerHTML = '';
                            resultsList.style.display = 'none';
                            return;
                        }

                        timer = setTimeout(function() {
                            fetch('https://apis.datos.gob.ar/georef/api/localidades?nombre=' + encodeURIComponent(query) + '&max=5')
                                .then(response => response.json())
                                .then(data => {
                                    resultsList.innerHTML = '';
                                    if (data.localidades && data.localidades.length > 0) {
                                        resultsList.style.display = 'block';
                                        data.localidades.forEach(function(loc) {
                                            var item = document.createElement('div');
                                            item.style.padding = '6px 10px';
                                            item.style.cursor = 'pointer';
                                            item.style.fontSize = '11px';
                                            item.style.borderBottom = '1px solid #eee';
                                            item.innerHTML = '📍 <b>' + loc.nombre + '</b>, <span style="color:#666;">' + loc.departamento.nombre + ', ' + loc.provincia.nombre + '</span>';

                                            item.onmouseover = function() { item.style.backgroundColor = '#f0f4f8'; };
                                            item.onmouseout = function() { item.style.backgroundColor = 'white'; };

                                            item.onclick = function() {
                                                var lat = loc.centroide.lat;
                                                var lon = loc.centroide.lon;
                                                var mapTarget = {{this._parent.get_name()}};
                                                
                                                mapTarget.flyTo([lat, lon], 13);
                                                L.popup()
                                                    .setLatLng([lat, lon])
                                                    .setContent('<b>' + loc.nombre + '</b><br>' + loc.departamento.nombre + ', ' + loc.provincia.nombre)
                                                    .openOn(mapTarget);

                                                input.value = loc.nombre;
                                                resultsList.style.display = 'none';
                                            };
                                            resultsList.appendChild(item);
                                        });
                                    } else {
                                        resultsList.style.display = 'none';
                                    }
                                })
                                .catch(err => console.error('Error al autocompletar:', err));
                        }, 250);
                    };

                    document.addEventListener('click', function(e) {
                        if (!container.contains(e.target)) {
                            searchBox.style.display = 'none';
                            resultsList.style.display = 'none';
                        }
                    });

                    return container;
                }
            });
            var targetMap = {{this._parent.get_name()}};
            targetMap.addControl(new geocoderControl());
        {% endmacro %}
        """)


# 1. CONFIGURACIÓN DE PÁGINA
st.set_page_config(page_title="MAPA MESA DE AGUA", page_icon="🚰", layout="wide")

FORM_ID = "aHNGU6dn2MFGMpg9Y5M5sn"
TOKEN = st.secrets["KOBO_TOKEN"]
URL_MESAAGUA = f"https://territorios.inta.gob.ar/api/v2/assets/{FORM_ID}/data/?format=json"
HEADERS = {'Authorization': f'Token {TOKEN}'}

LOGO_PATH = os.path.join(os.path.dirname(__file__), "logo_mesa.png")
PDF_TOP_MARGIN = 3.2 * cm

# --- ESTILOS CSS ---
st.markdown(
    """
    <style>
        .block-container {
            padding-top: 1rem !important;
            padding-bottom: 1rem !important;
            padding-left: 1rem !important;
            max-width: 98% !important;
        }
        iframe { margin-top: 0px !important; }
        .ficha-header {
            background-color: #1E3A8A; color: white; padding: 10px; border-radius: 5px;
            text-align: center; margin-bottom: 15px; font-weight: bold;
        }
    </style>
    """,
    unsafe_allow_html=True
)

# --- MAPEO Y FUNCIONES AUXILIARES ---
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

def mapear_nombres_claros(valor, tipo):
    mapeos_maestros = {
        "asistencia": {
            "ong": "ONG", "instituci_n_nacionales": "Nación", "instituci_n_provinciales": "Provincia",
            "propio": "Propio", "otros": "Otros", "sin_asistencia": "Sin asistencia", "otro": "Otros"
        },
        "estado": {"bueno": "Bueno", "regular": "Regular", "malo": "Malo"},
        "calidad": {"buena": "Buena", "regular": "Regular", "mala": "Mala"},
        "usuario": {
            "csalud": "Centro de salud", "com_ind": "Comunidad indígena",
            "comunidad indígena": "Comunidad indígena", "comununidad indígena": "Comunidad indígena",
            "escuelas": "Escuelas", "escuela": "Escuelas", "prod_af": "Familia rural criolla",
            "furbanas": "Familias urbanas", "familia urbana": "Familias urbanas", "familias urbanas": "Familias urbanas",
        },
        "problemas": {
            "cantidad_calidad_del_agua": "Cantidad/Calidad del agua",
            "sistema_de_captaci_n__bomba__t": "Sistema de captación",
            "sistema_de_conducci_n__manguer": "Sistema de conducción",
            "sistema_de_almacenamiento__cis": "Sistema de almacenamiento",
            "otras": "Otras"
        }
    }
    if pd.isna(valor) or str(valor).strip() == "" or str(valor).strip().lower() in ["none", "nan"]:
        return ""
    v = str(valor).strip()
    v_clean = v.lower()
    if tipo == "problemas":
        for key, nombre in mapeos_maestros["problemas"].items():
            if key in v_clean:
                return nombre
        return "Otras" if v_clean else ""
    return mapeos_maestros.get(tipo, {}).get(v_clean, valor)

def generar_kml_desde_df(df, col_lat='lat', col_lon='lon', col_nombre='tecnologia_txt'):
    kml_header = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>Obras_Filtradas_INTA</name>
    <description>Exportación espacial de registros filtrados</description>"""
    kml_footer = """  </Document>\n</kml>"""
    orden_campos = [
        ('fecha_limpia', 'Fecha de relevamiento'), ('Provincia_final', 'Provincia'),
        ('tecnologia_txt', 'Tecnología'), ('Detalle_otras_fuentes_de_agua', 'Detalle otras fuentes de agua'),
        ('estado_txt', 'Estado de la obra'), ('En_uso', '¿En uso?'),
        ('prob_txt', 'Causas del no uso (problemas asociados)'), ('calidad_txt', 'Calidad del agua'),
        ('Realiza_tratamiento_del_agua_a', 'Realiza tratamiento'), ('Cual', '¿Cuál tratamiento?'),
        ('asistencia_txt', 'Asistencia técnica'), ('Detalle_de_la_asistencia_t_cnica', 'Detalle de la asistencia técnica'),
        ('usuario_txt', 'Tipo de usuario')
    ]
    placemarks = []
    for idx, row in df.iterrows():
        try:
            lat = float(row.get(col_lat))
            lon = float(row.get(col_lon))
        except (ValueError, TypeError):
            continue
        if pd.notna(lat) and pd.notna(lon):
            nombre_punto = str(row.get(col_nombre, f"Registro_{idx}"))
            html_desc = "<h3>Detalles de la obra</h3><table border='1' style='border-collapse:collapse; width:100%;'>"
            for col_key, etiqueta in orden_campos:
                val = row.get(col_key)
                
                # Manejo de fallback para columnas renombradas durante el filtrado
                if pd.isna(val) or val is None or str(val).strip() == "":
                    mapeo_alt = {
                        'fecha_limpia': 'Fecha_del_relevamiento', 
                        'Provincia_final': 'Provincia_api',
                        'asistencia_txt': 'asistencia', 
                        'estado_txt': 'estado',
                        'usuario_txt': 'usuario', 
                        'calidad_txt': 'calidad', 
                        'prob_txt': 'problemas_asociados_al_no_uso'
                    }
                    val = row.get(mapeo_alt.get(col_key, ''))

                if col_key in ['prob_txt', 'Problemas_asociados_al_No_uso']:
                    if str(row.get('En_uso', '')).lower() == 'si':
                        val = ""
                    else:
                        val_prob = mapear_nombres_claros(val, 'problemas')
                        if val_prob == "Otras":
                            ampliar_txt = row.get('Ampliar_la_respuesta')
                            if pd.notna(ampliar_txt) and str(ampliar_txt).strip() not in ['', 'None', 'nan']:
                                val = f"Otras: {str(ampliar_txt).strip()}"
                            else:
                                val = val_prob
                        else:
                            val = val_prob

                # Condicionar visualización de "¿Cuál tratamiento?" a si el tratamiento es 'sí'
                if col_key == 'Cual' and str(row.get('Realiza_tratamiento_del_agua_a', '')).lower() not in ['si', 'sí']:
                    val = ""

                try:
                    if pd.notna(val) and not isinstance(val, (list, dict, tuple)):
                        val_str = val.strftime('%d/%m/%Y') if isinstance(val, pd.Timestamp) else str(val).strip()
                        if val_str and val_str.lower() not in ['none', 'no reg.', 'nan']:
                            html_desc += f"<tr><td style='padding:4px;'><b>{etiqueta}</b></td><td style='padding:4px;'>{val_str}</td></tr>"
                except Exception:
                    continue
            html_desc += "</table>"
            nombre_clean = nombre_punto.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            pm = f"    <Placemark>\n      <name>{nombre_clean}</name>\n      <description><![CDATA[{html_desc}]]></description>\n      <Point>\n        <coordinates>{lon},{lat},0</coordinates>\n      </Point>\n    </Placemark>"
            placemarks.append(pm)
    return kml_header + "\n" + "\n".join(placemarks) + "\n" + kml_footer

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
            if isinstance(geo, list) and len(geo) >= 2:
                return float(geo[0]), float(geo[1])
            return None, None
        res = df.apply(obtener_gps, axis=1)
        df['lat'], df['lon'] = zip(*res)
        df = df.dropna(subset=['lat', 'lon']).copy()

        col_prov = next((c for c in df.columns if c.lower() == 'provincia'), None)
        if col_prov:
            df['Provincia_api'] = df[col_prov].astype(str).str.strip().str.lower().map({'salta': 'Salta', 'jujuy': 'Jujuy'})
        else:
            df['Provincia_api'] = None
        return df
    except Exception as e:
        st.error(f"Error al cargar datos: {e}")
        return pd.DataFrame()

@st.cache_data(show_spinner=False)
def cargar_geojson_deptos(path_geojson: str):
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
        return [f for f in feats if f['prov'] in ('Salta', 'Jujuy')]
    except Exception:
        return []

def asignar_depto_por_punto(df_pts: pd.DataFrame, deptos_features: list) -> pd.DataFrame:
    if not SHAPELY_OK or not deptos_features:
        df_out = df_pts.copy()
        df_out['Departamento'] = 'Sin asignar'
        df_out['Provincia_geo'] = None
        df_out['Provincia_final'] = df_out['Provincia_api'].fillna('Sin asignar')
        return df_out

    df = df_pts.copy()
    
    idx_prov = {
        'Salta': [f for f in deptos_features if f['prov'] == 'Salta'],
        'Jujuy': [f for f in deptos_features if f['prov'] == 'Jujuy']
    }
    
    lats = df['lat'].to_list()
    lons = df['lon'].to_list()
    provis_api = df.get('Provincia_api', pd.Series([None]*len(df))).to_list()
    
    prov_cols, dep_cols = [], []

    for lat, lon, prov_api in zip(lats, lons, provis_api):
        p = Point(lon, lat)
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

# --- REPORTLAB PDF GENERATOR ---
def _header_canvas(c: canvas.Canvas, doc, fecha_desde=None, fecha_hasta=None):
    page_width, page_height = landscape(A4)
    header_baseline_y = page_height - 1.8 * cm 
    center_of_page_x = page_width / 2.0
    left_margin, right_margin = 1.5 * cm, 1.5 * cm
    
    try:
        c.drawImage(LOGO_PATH, x=left_margin, y=header_baseline_y - 70, width=180, height=140, preserveAspectRatio=True, mask='auto')
    except Exception:
        pass

    c.setFont("Helvetica-Bold", 18)
    c.drawCentredString(center_of_page_x, header_baseline_y + 0.3 * cm, "RELEVAMIENTO DE DATOS")
    c.setFont("Helvetica", 9)
    c.drawCentredString(center_of_page_x, header_baseline_y - 0.2 * cm, datetime.now().strftime("Generado: %d/%m/%Y %H:%M"))

    rango_str = f"Periodo de información: {fecha_desde.strftime('%d/%m/%Y')} al {fecha_hasta.strftime('%d/%m/%Y')}" if fecha_desde and fecha_hasta else "Periodo de información: Todos los registros"
    c.setFont("Helvetica-Oblique", 9)
    c.drawCentredString(center_of_page_x, header_baseline_y - 0.65 * cm, rango_str)

    c.setLineWidth(1.2)
    c.setStrokeColor(colors.HexColor('#1E3A8A'))
    c.line(left_margin, header_baseline_y - 1.05 * cm, page_width - right_margin, header_baseline_y - 1.05 * cm)

def construir_pdf_xls(tec_por_prov, asis_por_prov, usu_por_prov, est_por_prov, cal_por_prov, prob_por_prov, fecha_desde=None, fecha_hasta=None) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=1*cm, rightMargin=1*cm, topMargin=PDF_TOP_MARGIN, bottomMargin=1.2*cm)
    styles = getSampleStyleSheet()
    elems = [Spacer(1, 0.1*cm)]

    def tabla_rl(df, titulo):
        if titulo:
            elems.append(Paragraph(f"<b>{titulo}</b>", styles['Heading3']))
        data = [["Departamento"] + [str(c) for c in df.columns]]
        for idx, row in df.iterrows():
            fila = []
            for v in row.tolist():
                try: fila.append(int(v))
                except Exception: fila.append(0 if v is None or (isinstance(v, float) and pd.isna(v)) else v)
            data.append([str(idx)] + fila)
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1E3A8A')), ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'), ('FONTSIZE', (0,0), (-1,0), 9),
            ('ALIGN', (0,0), (-1,-1), 'CENTER'), ('GRID', (0,0), (-1,-1), 0.25, colors.grey),
            ('FONTSIZE', (0,1), (-1,-1), 8), ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('BOTTOMPADDING', (0,0), (-1,-1), 3), ('TOPPADDING', (0,0), (-1,-1), 3),
        ]))
        elems.extend([t, Spacer(1, 0.4*cm)])

    elems.append(Paragraph("Informe generado a partir de la base de datos actualizada", styles['Heading1']))
    elems.append(Spacer(1, 0.25*cm))

    secciones = [
        ("1.- Tecnologías", tec_por_prov), ("2.- Asistencia Técnica", asis_por_prov),
        ("3.- Usuarios", usu_por_prov), ("4.- Estado Funcional de las Obras", est_por_prov),
        ("5.- Calidad del Agua", cal_por_prov), ("6.- Causas de Inactividad (Obras sin uso)", prob_por_prov)
    ]

    for idx, (titulo_sec, dic_prov) in enumerate(secciones):
        elems.append(Paragraph(f"<b>{titulo_sec}</b>", styles['Heading2']))
        for prov in ["Salta", "Jujuy"]:
            elems.append(Paragraph(f"<b>Provincia: {prov}</b>", styles['Heading4']))
            dfp = dic_prov.get(prov, pd.DataFrame())
            if not dfp.empty and dfp.shape[0] > 0:
                tabla_rl(dfp, "")
            else:
                elems.append(Paragraph("Sin registros", styles['Normal']))
                elems.append(Spacer(1, 0.2*cm))
        if idx < len(secciones) - 1:
            elems.append(PageBreak())

    header_callback = partial(_header_canvas, fecha_desde=fecha_desde, fecha_hasta=fecha_hasta)
    doc.build(elems, onFirstPage=header_callback, onLaterPages=header_callback)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes

def construir_xlsx(tec_por_prov, asis_por_prov, usu_por_prov, est_por_prov, cal_por_prov, prob_por_prov) -> bytes:
    xls_buffer = BytesIO()
    with pd.ExcelWriter(xls_buffer, engine="openpyxl") as writer:
        for prov in ["Salta", "Jujuy"]:
            tec_por_prov.get(prov, pd.DataFrame()).to_excel(writer, sheet_name=f"TEC_{prov}")
            asis_por_prov.get(prov, pd.DataFrame()).to_excel(writer, sheet_name=f"ASIS_{prov}")
            usu_por_prov.get(prov, pd.DataFrame()).to_excel(writer, sheet_name=f"USU_{prov}")
            est_por_prov.get(prov, pd.DataFrame()).to_excel(writer, sheet_name=f"ESTADO_{prov}")
            cal_por_prov.get(prov, pd.DataFrame()).to_excel(writer, sheet_name=f"CALIDAD_{prov}")
            prob_por_prov.get(prov, pd.DataFrame()).to_excel(writer, sheet_name=f"NO_USO_{prov}")
    xls_buffer.seek(0)
    return xls_buffer.getvalue()

# === BOTÓN VISTA GENERAL Y RENDERIZADO DEL MAPA/FICHA ===
class BotonVistaGeneral(MacroElement):
    def __init__(self, bounds):
        super().__init__()
        self.bounds = bounds
        self._template = Template(u"""
        {% macro script(this, kwargs) %}
            var btnGeneral = L.control({position: 'topleft'});
            btnGeneral.onAdd = function (map) {
                var div = L.DomUtil.create('div', 'leaflet-bar leaflet-control');
                div.innerHTML = '<a href="#" title="Volver a vista general" style="font-size: 18px; display: flex; justify-content: center; align-items: center; width: 34px; height: 34px; text-decoration: none; background-color: white; color: #1E3A8A;">🗺️</a>';
                div.onclick = function(e){
                    e.preventDefault();
                    var mapTarget = {{this._parent.get_name()}};
                    var bounds = {{this.bounds}};
                    mapTarget.fitBounds(bounds, {padding: [20, 20]});
                };
                return div;
            };
            btnGeneral.addTo({{this._parent.get_name()}});
        {% endmacro %}
        """)

def renderizar_mapa_y_ficha(df_filtrado, deptos_features):
    if not df_filtrado.empty:
        def obtener_col(df, posibles_nombres):
            for col in posibles_nombres:
                if col in df.columns: return df[col]
            return pd.Series(index=df.index, dtype=object)

        # Configuración de columnas con nombres claros
        df_filtrado['tecnologia_txt'] = df_filtrado['tecnolog'].apply(lambda x: mapa_config.get(str(x), mapa_config["otros"])["titulo"])
        df_filtrado['estado_txt'] = obtener_col(df_filtrado, ['estado_de_la_obra', 'Estado_de_la_obra', 'estado']).apply(lambda x: mapear_nombres_claros(x, 'estado'))
        df_filtrado['calidad_txt'] = obtener_col(df_filtrado, ['calidad_del_agua', 'Calidad_del_agua', 'calidad']).apply(lambda x: mapear_nombres_claros(x, 'calidad'))
        
        df_filtrado['asistencia_txt'] = df_filtrado.apply(formatear_asistencia, axis=1)
        
        df_filtrado['usuario_txt'] = obtener_col(df_filtrado, ['usuario', 'Usuario']).apply(lambda x: mapear_nombres_claros(x, 'usuario'))
        df_filtrado['prob_txt'] = obtener_col(df_filtrado, ['problemas_asociados_al_no_uso', 'Problemas_asociados_al_No_uso', 'causas_del_no_uso']).apply(lambda x: mapear_nombres_claros(x, 'problemas'))
        
        df_geo = asignar_depto_por_punto(df_filtrado, deptos_features)

        # 1. RECUPERAR EL CLIC ACTIVO
        if "mapa_agua" in st.session_state and st.session_state["mapa_agua"]:
            map_state = st.session_state["mapa_agua"]
            if map_state.get("last_object_clicked"):
                st.session_state["last_selected_point"] = map_state["last_object_clicked"]

        last_clicked = st.session_state.get("last_selected_point")

        if last_clicked:
            seleccion_df = df_filtrado[
                (abs(df_filtrado['lat'] - last_clicked['lat']) < 0.0005) & 
                (abs(df_filtrado['lon'] - last_clicked['lng']) < 0.0005)
            ]
            if seleccion_df.empty:
                st.session_state["last_selected_point"] = None
                last_clicked = None

        # 2. VISTA FIJA / DINÁMICA
        if last_clicked:
            centro_inicial = [last_clicked['lat'], last_clicked['lng']]
            zoom_inicial = 15  
        else:
            centro_inicial = [df_filtrado['lat'].mean(), df_filtrado['lon'].mean()]
            zoom_inicial = 8

        m = folium.Map(location=centro_inicial, zoom_start=zoom_inicial, tiles=None)

        sw = [float(df_filtrado['lat'].min()), float(df_filtrado['lon'].min())]
        ne = [float(df_filtrado['lat'].max()), float(df_filtrado['lon'].max())]
        bounds = [sw, ne]

        BuscadorArgenmap().add_to(m)
        BotonVistaGeneral(bounds).add_to(m)

        folium.TileLayer("https://wms.ign.gob.ar/geoserver/gwc/service/tms/1.0.0/capabaseargenmap@EPSG%3A3857@png/{z}/{x}/{-y}.png", attr='IGN', name='Argenmap (IGN)', overlay=False).add_to(m)
        folium.TileLayer("https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}", attr='Google Satélite', name='Google Satélite', overlay=False).add_to(m)
        folium.LayerControl(position='topright', collapsed=False).add_to(m)
        LocateControl(flyTo=True).add_to(m)

        # 3. CONSTRUCCIÓN DE MARCADORES
        for _, reg in df_filtrado.iterrows():
            tec_key = str(reg.get('tecnolog', 'otros')).strip()
            conf = mapa_config.get(tec_key, mapa_config["otros"])
            titulo_display = conf["titulo"]

            if tec_key.lower() == "otros" or titulo_display.lower() == "otros":
                detalle = str(reg.get("Detalle_otras_fuentes_de_agua", "")).strip()
                if detalle and detalle != "None":
                    titulo_display = detalle

            f_str = reg['fecha_limpia'].strftime('%d/%m/%Y') if pd.notna(reg['fecha_limpia']) else "No reg."
            val_en_uso = str(reg.get('En_uso', 'No reg.'))

            pop_html = f"<div style='font-family:Arial; min-width:180px;'><b style='color:{conf['hex']}; font-size:13px;'>{titulo_display.upper()}</b><br><span style='margin-top:4px; display:block;'><b>Fecha:</b> {f_str}</span><b>En Uso:</b> {val_en_uso}</div>"

            es_seleccionado = False
            if last_clicked:
                dist_lat = abs(reg['lat'] - last_clicked['lat'])
                dist_lon = abs(reg['lon'] - last_clicked['lng'])
                if dist_lat < 0.0002 and dist_lon < 0.0002:
                    es_seleccionado = True

            if es_seleccionado:
                folium.CircleMarker(
                    location=[reg['lat'], reg['lon']], 
                    radius=18, 
                    color='#FFD700', 
                    fill=True, 
                    fill_color='#FFEA00', 
                    fill_opacity=0.8, 
                    weight=4
                ).add_to(m)

            folium.Marker(
                [reg['lat'], reg['lon']], 
                popup=folium.Popup(pop_html, max_width=250), 
                icon=folium.Icon(color=conf["color"], icon='tint')
            ).add_to(m)

        leyenda_html = """<div style="position: fixed; bottom: 50px; right: 20px; width: auto; max-width: 220px; background-color: #ffffff !important; z-index: 9999; font-size: 12px !important; padding: 10px; border-radius: 8px; box-shadow: 0 4px 15px rgba(0,0,0,0.4);"><details style="cursor: pointer;"><summary style="font-size: 13px !important; color: #1E3A8A !important; font-weight: bold !important;">⚙️ Leyenda</summary><div style="margin-top: 10px;">"""
        for item in mapa_config.values():
            leyenda_html += f"""<div style="display: flex; align-items: center; margin-bottom: 5px;"><i style="background: {item['hex']} !important; width: 14px; height: 14px; margin-right: 8px; border-radius: 50%;"></i><span style="color: #111111 !important; font-size: 12px !important;">{item['titulo']}</span></div>"""
        leyenda_html += "</div></details></div>"
        m.get_root().html.add_child(folium.Element(leyenda_html))

        # --- ESTRUCTURA UNIFICADA EN COLUMNAS ---
        col_mapa, col_ficha = st.columns([3, 1])

        # COLUMNA 1: Mapa interactivo
        with col_mapa:
            with st.container(border=True):
                st_folium(
                    m, 
                    width="100%", 
                    height=480, 
                    key="mapa_agua", 
                    returned_objects=["last_object_clicked"]
                )

        # COLUMNA 2: Panel lateral de la ficha técnica
        with col_ficha:
            st.markdown('<div class="ficha-header">DATOS DEL RELEVAMIENTO</div>', unsafe_allow_html=True)
            punto_activo = st.session_state.get("last_selected_point")

            if punto_activo:
                lat, lon = punto_activo['lat'], punto_activo['lng']
                seleccion_df = df_filtrado[(abs(df_filtrado['lat'] - lat) < 0.0002) & (abs(df_filtrado['lon'] - lon) < 0.0002)]

                if not seleccion_df.empty:
                    seleccion = seleccion_df.iloc[0]
                    tecnologia_codigo = str(seleccion.get('tecnolog', 'otros')).strip()
                    config_tec = mapa_config.get(tecnologia_codigo, mapa_config['otros'])
                    titulo_ficha = config_tec['titulo']

                    if tecnologia_codigo.lower() == 'otros' or titulo_ficha.lower() == 'otros':
                        detalle_otros = seleccion.get('Detalle_otras_fuentes_de_agua')
                        if pd.notna(detalle_otros) and str(detalle_otros).strip() != "":
                            titulo_ficha = str(detalle_otros).strip()

                    titulo_expander = f"📋 **:blue[{titulo_ficha.upper()}]**"

                    with st.expander(titulo_expander, expanded=True):
                        fecha_val = seleccion.get('fecha_limpia')
                        fecha_str = fecha_val.strftime('%d/%m/%Y') if pd.notna(fecha_val) and isinstance(fecha_val, pd.Timestamp) else "No reg."
                        st.write(f"**📅 Fecha:** {fecha_str}")
                        st.write(f"**🏗️ Estado de Obra:** {seleccion.get('estado_txt', 'No reg.')}")
                        st.write(f"**🛠️ Asistencia Técnica:** {seleccion.get('asistencia_txt', 'No reg.')}")
                        st.write(f"**✅ En Uso:** {seleccion.get('En_uso', 'No reg.')}")

                        if str(seleccion.get('En_uso', '')).lower() == 'no':
                            prob_val = mapear_nombres_claros(seleccion.get('Problemas_asociados_al_No_uso'), 'problemas')
                            if prob_val == "Otras":
                                ampliar_txt = seleccion.get('Ampliar_la_respuesta')
                                if pd.notna(ampliar_txt) and str(ampliar_txt).strip() not in ['', 'None', 'nan']:
                                    prob_val = f"Otras: {str(ampliar_txt).strip()}"
                            if prob_val and prob_val.strip() != "":
                                st.write(f"**⚠️ Causa de inactividad:** {prob_val}")

                        st.write(f"**🧔 Tipo de Usuario:** {seleccion.get('usuario_txt', 'No reg.')}")
                        st.write(f"**👨‍👩‍👧‍👦 Familias usuarias:** {seleccion.get('Cantidad_de_familias_usuarias', 'No reg.')}")
                        st.write(f"**🧪 Calidad de Agua:** {seleccion.get('calidad_txt', 'No reg.')}")
                        
                        # --- SECCIÓN TRATAMIENTO DEL AGUA ---
                        tratamiento_val = seleccion.get('Realiza_tratamiento_del_agua_a', 'No reg.')
                        st.write(f"**🧼 Tratamiento:** {tratamiento_val}")
                        if str(tratamiento_val).strip().lower() in ['si', 'sí']:
                            qual_trat = seleccion.get('Cual')
                            if pd.notna(qual_trat) and str(qual_trat).strip() not in ['', 'None', 'nan']:
                                st.write(f"**❓ Cuál tratamiento:** {str(qual_trat).strip()}")

                    foto_bytes_para_pdf = None
                    with st.expander("🖼️ **:blue[Ver Fotografía]**", expanded=False):
                        registro_id = seleccion.get('_id') if pd.notna(seleccion.get('_id')) else seleccion.get('id')
                        adjuntos = seleccion.get('_attachments', [])
                        if isinstance(adjuntos, str) and adjuntos.strip() != "":
                            try: adjuntos = json.loads(adjuntos)
                            except Exception: adjuntos = []

                        id_adjunto = adjuntos[0].get('id') if isinstance(adjuntos, list) and len(adjuntos) > 0 else None

                        if registro_id and id_adjunto:
                            with st.spinner("Cargando imagen..."):
                                foto_bytes_para_pdf = obtener_foto_api(FORM_ID, registro_id, id_adjunto, TOKEN)
                            
                            if foto_bytes_para_pdf:
                                st.image(foto_bytes_para_pdf, caption="Fotografía de la obra", use_container_width=True)
                            else:
                                st.warning("No se pudo obtener la imagen.")
                        else:
                            st.info("Sin foto disponible para este registro.")

                    st.markdown("---")

                    st.download_button(
                        label="📄 Descargar Ficha Técnica en PDF",
                        data=lambda: construir_pdf_ficha_individual(
                            seleccion, 
                            foto_bytes=obtener_foto_api(FORM_ID, seleccion.get('_id'), id_adjunto, TOKEN)
                        ),
                        file_name=f"ficha_relevamiento_{int(seleccion.get('_id', 0))}.pdf",
                        mime="application/pdf",
                        use_container_width=True
                    )

                else:
                    st.warning("No se encontraron datos para este punto.")
            else:
                st.info("💡 Haz clic en un marcador para ver la ficha técnica.")

        return df_geo
    return None


# ======================= EJECUCIÓN PRINCIPAL =======================

# 1. ENCABEZADO
st.markdown(
    """
    <div style="text-align: left; padding: 10px; background-color: transparent;">
        <h1 style="color: #1E3A8A; font-weight: bold; margin-bottom: 5px; font-size: calc(1.6rem + 1.5vw); line-height: 1.1;">
            🚰 Mesa del Agua
        </h1>
        <p style="color: #555555; font-size: calc(1.0rem + 0.4vw); font-weight: 500; margin-top: 0; opacity: 0.9;">
            Mapeo digital de obras de agua en el Chaco Salteño
        </p>
    </div>
    """, 
    unsafe_allow_html=True
)

# 2. CARGA DE DATOS Y GEOJSON
df_raw = cargar_datos()
deptos_features = cargar_geojson_deptos("deptos.geojson") if SHAPELY_OK else []

if not df_raw.empty:
    df_raw = asignar_depto_por_punto(df_raw, deptos_features)

    # 3. BARRA LATERAL (FILTROS)
    with st.sidebar:
        try:
            with open("logox3.png", "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode()
            logo_src = f"data:image/png;base64,{encoded_string}"
        except Exception:
            logo_src = ""

        if logo_src:
            st.html(f"""<div style="text-align: center; margin-top: 15px; margin-bottom: 15px;"><img src="{logo_src}" width="180" style="filter: drop-shadow(0px 4px 6px rgba(0,0,0,0.3)); max-width: 90%; height: auto;" title="Mesa del Agua"></div>""")
        
        st.markdown("---")
        
        with st.expander("🔍 Filtros de Búsqueda", expanded=False):
            fecha_desde = st.date_input("Fecha Inicio", value=df_raw['fecha_limpia'].min().date())
            fecha_hasta = st.date_input("Fecha Fin", value=df_raw['fecha_limpia'].max().date())
            
            opciones_prov = ["Todas"] + sorted([p for p in df_raw['Provincia_api'].dropna().unique() if p])
            prov_filtro = st.selectbox("Provincia", opciones_prov)
            
            df_prov_temp = df_raw[df_raw['Provincia_api'] == prov_filtro] if prov_filtro != "Todas" else df_raw.copy()
            
            opciones_depto = ["Todos"]
            if 'Departamento' in df_prov_temp.columns:
                opciones_depto += sorted([d for d in df_prov_temp['Departamento'].dropna().unique() if d and d != 'Sin asignar'])
            depto_filtro = st.selectbox("Departamento", opciones_depto)
            
            listado_tec = ["Todas"] + [v["titulo"] for v in mapa_config.values()]
            tec_filtro = st.selectbox("Tecnología", listado_tec)
            
            opciones_uso = ["Todos"]
            if 'En_uso' in df_raw.columns:
                opciones_uso += sorted(df_raw['En_uso'].dropna().unique().tolist())
            uso_filtro = st.selectbox("¿En Uso?", opciones_uso)

        st.markdown("---")
        st.markdown("""<div style="text-align: center; font-size: 11px; opacity: 0.8;"><strong>Mesa del Agua para el Chaco Salteño</strong><br><span>Versión 1.3.0 (2026)</span><br><a href="https://creativecommons.org/licenses/by-nc-sa/4.0/deed.es" target="_blank" style="color: #1E3A8A; font-weight: bold;">Licencia CC BY-NC-SA 4.0</a></div>""", unsafe_allow_html=True)
        st.markdown(" ") 
        
        with st.sidebar.expander("👥 Créditos"):
            st.markdown("""<div style="font-size: 12px; line-height: 1.4;"><strong>Desarrollado por:</strong><br>Lic. Hernán Elena / INTA EEA Salta<br><br><strong>Mesa del Agua:</strong><ul style="margin: 4px 0 0 0; padding-left: 18px; font-size: 11px;"><li style="margin-bottom: 2px;">INTA Centro Regional Salta-Jujuy</li><li style="margin-bottom: 2px;">FUNDAPAZ</li><li style="margin-bottom: 2px;">Organizaciones de la Mesa del Agua</li><li style="margin-bottom: 2px;">Gobierno de Salta</li></ul></div>""", unsafe_allow_html=True)

    # 4. APLICACIÓN DE FILTROS AL DATAFRAME
    mask = (df_raw['fecha_limpia'].dt.date >= fecha_desde) & (df_raw['fecha_limpia'].dt.date <= fecha_hasta)
    if prov_filtro != "Todas":
        mask &= (df_raw['Provincia_api'] == prov_filtro)
    if depto_filtro != "Todos" and 'Departamento' in df_raw.columns:
        mask &= (df_raw['Departamento'] == depto_filtro)
    if tec_filtro != "Todas":
        tec_key_buscada = next(k for k, v in mapa_config.items() if v["titulo"] == tec_filtro)
        mask &= (df_raw['tecnolog'] == tec_key_buscada)
    if uso_filtro != "Todos":
        mask &= (df_raw['En_uso'] == uso_filtro)
        
    df_filtrado = df_raw[mask].copy()

    # 5. RENDERIZAR MAPA Y FICHA
    df_geo = renderizar_mapa_y_ficha(df_filtrado, deptos_features)

    # 6. DASHBOARD DE ESTADÍSTICAS Y TABLAS
    if not df_filtrado.empty and df_geo is not None:
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

        with t1:
            c_pie1, c_pie2 = st.columns(2)
            with c_pie1:
                fig1 = px.pie(df_filtrado, names='tecnologia_txt', title="Porcentaje de Tecnologías", color='tecnologia_txt', color_discrete_map=colores_tecnologias, hole=0.3)
                fig1.update_traces(hovertemplate="%{label}<br>Porcentaje: %{percent}")
                st.plotly_chart(fig1, use_container_width=True)
            with c_pie2:
                fig2 = px.pie(df_filtrado, names='estado_txt', title="Estado de la Obra")
                fig2.update_traces(hovertemplate="%{label}<br>Cantidad: %{value}")
                st.plotly_chart(fig2, use_container_width=True)

        with t2:
            c_pie3, c_bar1 = st.columns(2)
            with c_pie3:
                fig3 = px.pie(df_filtrado, names='calidad_txt', title="Calidad de Agua")
                fig3.update_traces(hovertemplate="%{label}<br>Total: %{value}")
                st.plotly_chart(fig3, use_container_width=True)
            with c_bar1:
                asistencia_data = df_filtrado['asistencia_txt'].value_counts().reset_index()
                fig4 = px.bar(asistencia_data, x='asistencia_txt', y='count', title="Asistencia Técnica", labels={'count': 'Obras', 'asistencia_txt': 'Origen'})
                fig4.update_traces(hovertemplate="Tipo: %{x}<br>Total: %{y}")
                st.plotly_chart(fig4, use_container_width=True)

        with t3:
            usuario_data = df_filtrado['usuario_txt'].value_counts().reset_index()
            fig5 = px.bar(usuario_data, x='count', y='usuario_txt', orientation='h', title="Tipos de Usuarios", labels={'count': 'Registros', 'usuario_txt': 'Categoría'})
            fig5.update_traces(hovertemplate="Usuario: %{y}<br>Cantidad: %{x}")
            st.plotly_chart(fig5, use_container_width=True)

        with t4:
            df_no_uso = df_filtrado[df_filtrado['En_uso'].astype(str).str.lower().str.contains('no', na=False)].copy()
            if not df_no_uso.empty:
                df_no_uso['prob_txt'] = df_no_uso['Problemas_asociados_al_No_uso'].apply(lambda x: mapear_nombres_claros(x, 'problemas'))
                prob_data = df_no_uso['prob_txt'].value_counts().reset_index()
                fig6 = px.bar(prob_data, x='count', y='prob_txt', orientation='h', title="Causas del No Uso (Obras Inactivas)", color='count', color_continuous_scale='Reds', labels={'count': 'Frecuencia', 'prob_txt': 'Motivo detectado'})
                fig6.update_layout(yaxis={'categoryorder': 'total ascending'})
                fig6.update_traces(hovertemplate="Problema: %{y}<br>Obras afectadas: %{x}")
                st.plotly_chart(fig6, use_container_width=True)
            else:
                st.success("✨ ¡Genial! Según los filtros aplicados, todas las obras están en uso.")

        # AUXILIARES MATRIZ TERRITORIAL
        def _orden_tecnologias(): return ["Cisterna de consumo", "Pozo somero", "Pozo profundo", "Cisterna productiva", "Tanque australiano", "Represa", "Red de distribución", "Madrejones", "Otros"]
        def _orden_asistencia(): return ["ONG", "Nación", "Provincia", "Propio", "Otros", "Sin asistencia"]
        def _orden_usuarios(): return ["Comunidad indígena", "Familia rural criolla", "Escuelas", "Familias urbanas"]
        def _orden_estado(): return ["Bueno", "Regular", "Malo"]
        def _orden_calidad(): return ["Buena", "Regular", "Mala"]
        def _orden_problemas(): return ["Cantidad/Calidad del agua", "Sistema de captación", "Sistema de conducción", "Sistema de almacenamiento", "Otras"]

        def _matriz_por_provincia(df_geo_local, columna_categoria, orden_columnas):
            out = {}
            for prov in ["Salta", "Jujuy"]:
                dfp = df_geo_local[df_geo_local["Provincia_final"].fillna("Sin asignar") == prov].copy()
                if dfp.empty:
                    out[prov] = pd.DataFrame(columns=orden_columnas, index=[])
                    continue
                cat_series = dfp[columna_categoria].fillna("Otros")
                g = dfp.assign(cat=cat_series).groupby(["Departamento", "cat"]).size().unstack(fill_value=0)
                for c in orden_columnas:
                    if c not in g.columns: g[c] = 0
                g = g[orden_columnas]
                g.loc["Totales"] = g.sum()
                if "Totales" in g.index:
                    g = pd.concat([g.drop(index=["Totales"]).sort_index(), g.loc[["Totales"]]])
                out[prov] = g
            return out

        with t5:
            st.subheader("Resumen Territorial")
            df_geo['Provincia_final'] = df_geo['Provincia_final'].fillna('Sin asignar')
            df_geo['Departamento'] = df_geo['Departamento'].fillna('Sin asignar')
            df_geo['usuario_xls'] = df_geo['usuario_txt']
            df_geo.loc[~df_geo['usuario_xls'].isin(_orden_usuarios()), 'usuario_xls'] = 'Otros'

            tec_por_prov = _matriz_por_provincia(df_geo.assign(tecnologia_txt=df_geo['tecnologia_txt'].fillna("Otros")), "tecnologia_txt", _orden_tecnologias())
            asis_por_prov = _matriz_por_provincia(df_geo.assign(asistencia_txt=df_geo['asistencia_txt'].fillna("Sin asistencia")), "asistencia_txt", _orden_asistencia())
            usu_por_prov = _matriz_por_provincia(df_geo, "usuario_xls", _orden_usuarios() + ["Otros"])
            est_por_prov = _matriz_por_provincia(df_geo.assign(estado_txt=df_geo['estado_txt'].fillna("Malo")), "estado_txt", _orden_estado())
            cal_por_prov = _matriz_por_provincia(df_geo.assign(calidad_txt=df_geo['calidad_txt'].fillna("Mala")), "calidad_txt", _orden_calidad())

            df_no_uso_geo = df_geo[df_geo['En_uso'].astype(str).str.lower().str.contains('no', na=False)].copy()
            if not df_no_uso_geo.empty:
                df_no_uso_geo['prob_txt'] = df_no_uso_geo['Problemas_asociados_al_No_uso'].apply(lambda x: mapear_nombres_claros(x, 'problemas'))
                prob_por_prov = _matriz_por_provincia(df_no_uso_geo, "prob_txt", _orden_problemas())
            else:
                prob_por_prov = {p: pd.DataFrame() for p in ["Salta", "Jujuy"]}

            st.info(f"📊 **Control de Consistencia:** Total registros procesados: **{len(df_geo)}** | Obras inactivas identificadas: **{len(df_no_uso_geo)}**")
            st.markdown("---")

            st.markdown("### Descargas")
            col_pdf, col_xls, col_kml = st.columns(3)

            with col_pdf:
                pdf_bytes = construir_pdf_xls(tec_por_prov, asis_por_prov, usu_por_prov, est_por_prov, cal_por_prov, prob_por_prov, fecha_desde, fecha_hasta)
                st.download_button("📥 Descargar Informe PDF", data=pdf_bytes, file_name="informe_mesa_agua.pdf", mime="application/pdf", use_container_width=True)

            with col_xls:
                xlsx_bytes = construir_xlsx(tec_por_prov, asis_por_prov, usu_por_prov, est_por_prov, cal_por_prov, prob_por_prov)
                st.download_button("📥 Descargar Resumen XLSX", data=xlsx_bytes, file_name="resumen_mesa_agua.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)

            with col_kml:
                if not df_filtrado.empty:
                    kml_string = generar_kml_desde_df(df=df_filtrado, col_lat='lat', col_lon='lon', col_nombre='tecnologia_txt')
                    st.download_button("🗺️ Descargar en KML", data=kml_string, file_name="obras_filtradas.kml", mime="application/vnd.google-earth.kml+xml", use_container_width=True)
                else:
                    st.warning("No hay registros para exportar en KML.")

        st.markdown("---")
        with st.expander("ℹ️ Información sobre la Mesa de Agua"):
            st.markdown("""
La Mesa de Agua ha promovido el mapeo e integración de más de 300 obras de agua en estos departamentos. La base de datos generada, sistematiza la localización, tipo de tecnología, población beneficiaria y estado funcional de cada obra, y se actualiza periódicamente con la colaboración de INTA, FUNDAPAZ, ONG, Gobierno Provincial, municipios y comunidades.

El mapeo digital de obras de agua en el Chaco Salteño, es una iniciativa impulsada por la Mesa de Agua con el objetivo de relevar, sistematizar y visualizar de manera accessible las obras de agua existentes y en desarrollo en el territorio. Este instrumento busca contribuir a una gestión más eficiente, equitativa y transparente del acceso al agua, poniendo en valor el conocimiento construido colectivamente.
        
Propósito: Fortalecer el plan de seguimiento de obras, ya que está concebida como una base de datos viva, con actualización en línea y con capacidad para analizar el uso, el estado y la calidad de las obras construidas.

**Equipo de trabajo:** INTA, FUNDAPAZ, ONG, Gobierno Provincial, municipios y comunidades.
Para más información: elena.hernan@inta.gob.ar
""", unsafe_allow_html=True)
    else:
        st.warning("No hay datos para los filtros seleccionados.")
else:
    st.error("No se pudieron cargar los datos.")
