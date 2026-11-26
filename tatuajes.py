# -*- coding: utf-8 -*-
"""
Tatoo CEMENER
Marcación de puntos de tatuaje sobre imágenes DICOM.

Requisitos: PyQt6, pydicom, numpy   (opcional: Pillow para generar el .ico)
    pip install PyQt6 pydicom numpy

Archivos que espera al lado del .py (o embebidos en el ejecutable):
    logo_cemener.png   -> splash de inicio + encabezado
    logo_cemener.ico   -> icono de ventana / barra de tareas (Windows)
"""

import ctypes
import json
import os
import re
import shutil
import sys
import tempfile
from collections import OrderedDict

import numpy as np
import pydicom

from datetime import datetime

from PyQt6.QtCore import (QObject, QPoint, QPointF, QRect, QRunnable, QSettings,
                          QSize, Qt, QThreadPool, QTimer, pyqtSignal)
from PyQt6.QtGui import (QColor, QFont, QIcon, QImage, QKeySequence, QPainter,
                         QPalette,
                         QPainterPath, QPen, QPixmap, QPolygonF, QRegion,
                         QShortcut)
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QFileDialog, QFrame,
                             QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QMainWindow,
                             QMessageBox, QPushButton, QRadioButton,
                             QScrollArea, QSpinBox, QSplashScreen, QToolButton,
                             QVBoxLayout, QWidget)

# ==========================================================================
# CONFIGURACIÓN
# ==========================================================================
APP_NOMBRE = "Tatoo CEMENER"
APP_VERSION = "2.2"
APP_ID = "cemener.tatoo.2.2"          # necesario para el icono en la barra de tareas

ARCHIVO_LOGO = "logo_cemener.png"
ARCHIVO_ICONO = "logo_cemener.ico"

# Tiempo mínimo del splash en ms. 0 = se cierra apenas la ventana está lista.
SPLASH_MS = 0

# Cuántas imágenes ya decodificadas se guardan en memoria para volver a ellas
# sin releer el disco/la red.
MAX_CACHE = 12

DIR_DICOM_DEFECTO = r"\\ARIADB\va_data$\filedata\DICOM"
# Donde se guardan las imágenes exportadas: una carpeta por paciente,
# "DNI - APELLIDO, NOMBRE", dentro de esta.
DIR_SALIDA_DEFECTO = r"\\nas.cemener.org.ar\Fisica\Imagenes de pacientes"

# Lista de pacientes ocultos. Se guarda en el perfil del usuario para que la
# ocultación sobreviva al cierre del programa. Nunca se toca la carpeta original.
ARCHIVO_OCULTOS = os.path.join(os.path.expanduser("~"), ".tatoo_cemener_ocultos.json")

# Nombres de paciente ya leídos de los DICOM, para no volver a abrir un archivo
# por carpeta cada vez que arranca el programa.
ARCHIVO_NOMBRES = os.path.join(os.path.expanduser("~"), ".tatoo_cemener_nombres.json")

# Solo se listan las imágenes cuyo archivo empieza con esto (reconstrucción 3D).
# Si una carpeta no tiene ninguna, se muestran todas.
PREFIJO_RECONSTRUCCION = "SC"

# Colores de tatuaje, en orden clínico
COLORES_TATUAJE = {
    "Negro · Referencia": QColor(0, 0, 0),
    "Rojo · Tratamiento 1ra etapa": QColor(220, 0, 0),
    "Verde · Tratamiento 2da etapa": QColor(0, 190, 0),
    "Azul · Tratamiento previo": QColor(0, 60, 235),
    "Amarillo · Auxiliar": QColor(250, 210, 0),
}

# Colores de la mira (líneas guía). Ya no hay cyan fijo: se elige acá.
COLORES_GUIA = {
    "Magenta": QColor(255, 0, 200),
    "Blanco": QColor(255, 255, 255),
    "Negro": QColor(0, 0, 0),
    "Naranja": QColor(255, 140, 0),
    "Verde lima": QColor(150, 255, 0),
}

# Todas las exportaciones se guardan con este nombre fijo.
NOMBRE_EXPORT = "Tatuaje"

# (etiqueta, extensión, formato Qt, calidad)
FORMATOS_EXPORT = [
    ("JPG", "jpg", "JPG", 95),
    ("PNG", "png", "PNG", -1),
    ("BMP", "bmp", "BMP", -1),
    ("TIFF", "tiff", "TIFF", -1),
]
FORMATO_DEFECTO = "jpg"


def ruta_recurso(nombre: str) -> str:
    """Devuelve la ruta del recurso, funcione como .py o empaquetado con PyInstaller."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, nombre)


def icono_color(color: QColor, lado: int = 16) -> QIcon:
    """Cuadradito de color para los desplegables."""
    pm = QPixmap(lado, lado)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(color)
    p.setPen(QPen(QColor("#5A6472"), 1))
    p.drawRoundedRect(1, 1, lado - 3, lado - 3, 4, 4)
    p.end()
    return QIcon(pm)


# ==========================================================================
# LECTURA DE DICOM
# ==========================================================================
def dicom_a_qimage(ds):
    """Convierte el pixel_array a QImage aplicando rescale, ventana y fotométrica."""
    arr = ds.pixel_array
    foto = str(getattr(ds, "PhotometricInterpretation", "MONOCHROME2")).upper()

    # Color (RGB / YBR)
    if arr.ndim == 3 and arr.shape[-1] == 3:
        if "YBR" in foto:
            try:
                from pydicom.pixels import convert_color_space
            except ImportError:
                from pydicom.pixel_data_handlers.util import convert_color_space
            arr = convert_color_space(arr, foto, "RGB")
        arr = np.ascontiguousarray(arr.astype(np.uint8))
        h, w, _ = arr.shape
        return QImage(arr.tobytes(), w, h, 3 * w, QImage.Format.Format_RGB888).copy()

    # Multiframe en escala de grises: tomamos el primer frame
    if arr.ndim == 3:
        arr = arr[0]

    arr = arr.astype(np.float32)
    pendiente = float(getattr(ds, "RescaleSlope", 1) or 1)
    corte = float(getattr(ds, "RescaleIntercept", 0) or 0)
    arr = arr * pendiente + corte

    centro = getattr(ds, "WindowCenter", None)
    ancho = getattr(ds, "WindowWidth", None)
    try:
        if centro is not None and ancho is not None:
            centro = float(centro[0]) if hasattr(centro, "__iter__") else float(centro)
            ancho = float(ancho[0]) if hasattr(ancho, "__iter__") else float(ancho)
            minimo, maximo = centro - ancho / 2.0, centro + ancho / 2.0
        else:
            raise ValueError
    except (TypeError, ValueError, IndexError):
        minimo, maximo = float(arr.min()), float(arr.max())

    if maximo - minimo < 1e-6:
        maximo = minimo + 1.0

    arr = np.clip((arr - minimo) / (maximo - minimo), 0.0, 1.0) * 255.0
    if foto == "MONOCHROME1":
        arr = 255.0 - arr

    arr = np.ascontiguousarray(arr.astype(np.uint8))
    h, w = arr.shape
    return QImage(arr.tobytes(), w, h, w, QImage.Format.Format_Grayscale8).copy()


def nombre_paciente(ds) -> str:
    valor = getattr(ds, "PatientName", None)
    if not valor:
        return "Paciente sin nombre"
    partes = [p.strip() for p in str(valor).split("^") if p.strip()]
    if len(partes) >= 2:
        return f"{partes[0]}, {partes[1]}"
    return partes[0] if partes else "Paciente sin nombre"


def leer_dicom(ruta: str):
    """Lee un DICOM y devuelve (QImage, metadatos). No toca la interfaz,
    así que puede ejecutarse en un hilo secundario."""
    ds = pydicom.dcmread(ruta, force=True)
    imagen = dicom_a_qimage(ds)

    espaciado = getattr(ds, "PixelSpacing", None) or getattr(ds, "ImagerPixelSpacing", None)
    meta = {
        "paciente": nombre_paciente(ds).upper(),
        "modalidad": str(getattr(ds, "Modality", "") or "").strip(),
        "fecha": str(getattr(ds, "StudyDate", "") or ""),
        "espaciado": float(espaciado[0]) if espaciado else None,
    }
    return imagen, meta


class SenalesLector(QObject):
    listo = pyqtSignal(str, object, object)


class LectorEnSegundoPlano(QRunnable):
    """Precarga la imagen siguiente mientras el usuario trabaja en la actual."""

    def __init__(self, ruta: str):
        super().__init__()
        self.setAutoDelete(False)   # lo libera la ventana, no el pool
        self.ruta = ruta
        self.senales = SenalesLector()

    def run(self):
        try:
            imagen, meta = leer_dicom(self.ruta)
        except Exception:
            imagen, meta = None, None
        self.senales.listo.emit(self.ruta, imagen, meta)


def es_reconstruccion(nombre_archivo: str) -> bool:
    return nombre_archivo.upper().startswith(PREFIJO_RECONSTRUCCION)


def leer_nombre_carpeta(ruta_carpeta: str):
    """Lee el nombre del paciente del primer DICOM legible de la carpeta.
    Solo lee el encabezado, no la imagen, así que es rápido."""
    try:
        with os.scandir(ruta_carpeta) as entradas:
            archivos = sorted(e.name for e in entradas
                              if e.is_file() and not e.name.startswith("."))
    except OSError:
        return None
    # primero los de reconstrucción, que son los que se van a abrir igual
    archivos.sort(key=lambda n: not es_reconstruccion(n))
    for nombre in archivos[:4]:
        try:
            ds = pydicom.dcmread(os.path.join(ruta_carpeta, nombre),
                                 stop_before_pixels=True, force=True,
                                 specific_tags=["PatientName"])
        except Exception:
            continue
        if getattr(ds, "PatientName", None):
            return nombre_paciente(ds)
    return None


class SenalesNombres(QObject):
    nombre = pyqtSignal(str, str, str)   # raíz, carpeta, nombre
    terminado = pyqtSignal(str)


class LectorNombres(QRunnable):
    """Recorre las carpetas de pacientes en segundo plano y va avisando el
    nombre de cada uno, así la lista aparece al instante y se completa sola."""

    def __init__(self, raiz: str, carpetas: list):
        super().__init__()
        self.setAutoDelete(False)
        self.raiz = raiz
        self.carpetas = carpetas
        self.cancelado = False
        self.senales = SenalesNombres()

    def run(self):
        for carpeta in self.carpetas:
            if self.cancelado:
                return
            nombre = leer_nombre_carpeta(os.path.join(self.raiz, carpeta))
            if nombre:
                self.senales.nombre.emit(self.raiz, carpeta, nombre)
        self.senales.terminado.emit(self.raiz)


# ==========================================================================
# WIDGETS AUXILIARES
# ==========================================================================
class SpinSinRueda(QSpinBox):
    """La ruedita del mouse no cambia el valor: desplaza el panel. Así no se
    modifica sin querer al pasar por arriba."""

    def wheelEvent(self, event):
        event.ignore()


class Seccion(QWidget):
    """Tarjeta plegable: un encabezado con el título y, al hacer clic, se
    muestra u oculta el contenido. Plegada muestra un resumen a la derecha."""

    def __init__(self, titulo: str, abierta: bool = False):
        super().__init__()
        self.setObjectName("tarjeta")
        # sin esto Qt no pinta el fondo/borde de la hoja de estilos en un QWidget propio
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        externa = QVBoxLayout(self)
        externa.setContentsMargins(14, 6, 14, 10)
        externa.setSpacing(4)

        fila = QHBoxLayout()
        fila.setSpacing(6)
        self.boton = QToolButton()
        self.boton.setObjectName("cabeceraSeccion")
        self.boton.setText(titulo.upper())
        self.boton.setCheckable(True)
        self.boton.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.boton.setCursor(Qt.CursorShape.PointingHandCursor)
        self.boton.toggled.connect(self._alternar)
        self.lbl_resumen = QLabel()
        self.lbl_resumen.setObjectName("resumenSeccion")
        self.lbl_resumen.setAlignment(Qt.AlignmentFlag.AlignRight |
                                      Qt.AlignmentFlag.AlignVCenter)
        fila.addWidget(self.boton)
        fila.addStretch(1)
        fila.addWidget(self.lbl_resumen)
        externa.addLayout(fila)

        self.contenido = QWidget()
        self.caja = QVBoxLayout(self.contenido)
        self.caja.setContentsMargins(0, 2, 0, 0)
        self.caja.setSpacing(8)
        externa.addWidget(self.contenido)

        # Cada vez que se abre el programa arranca como se indica acá
        # (no recuerda cómo quedó la vez anterior).
        self.boton.setChecked(abierta)
        self._alternar(abierta)

    def _alternar(self, abierta: bool):
        self.contenido.setVisible(abierta)
        self.lbl_resumen.setVisible(not abierta)
        self.boton.setArrowType(Qt.ArrowType.DownArrow if abierta
                                else Qt.ArrowType.RightArrow)

    def resumen(self, texto: str):
        self.lbl_resumen.setText(texto)


# ==========================================================================
# LIENZO
# ==========================================================================
class Lienzo(QWidget):
    """
    Muestra la imagen ajustada al widget y dibuja las marcas por encima.
    Las marcas NO se queman en el pixmap: se guardan en coordenadas de imagen,
    así que recortar, deshacer y exportar en resolución completa son operaciones
    no destructivas.
    """

    modo_terminado = pyqtSignal()
    estado_cambiado = pyqtSignal()
    cursor_movido = pyqtSignal(object)   # QPoint en coordenadas de imagen, o None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(560, 520)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

        self.pix_original = None          # QPixmap sin marcas
        self.vista = QRect()              # recorte activo, en coords de imagen
        self.marcas = []                  # dicts: x, y, color, tamano, ancho
        self.historial = []               # (marcas, vista)

        self.modo = "nav"                 # nav | recorte | tatuaje
        self.color_tatuaje = list(COLORES_TATUAJE.values())[0]
        self.color_guia = COLORES_GUIA["Magenta"]
        self.tamano = 10
        self.ancho = 2

        self._cache = None
        self._escala = 1.0
        self._offset = QPoint(0, 0)
        self._cursor = None
        self._arrastrando = False
        self._p1 = None
        self._p2 = None

    # ---------------- carga y vista ----------------
    def cargar(self, pixmap: QPixmap):
        self.pix_original = pixmap
        self.vista = QRect(0, 0, pixmap.width(), pixmap.height())
        self.marcas = []
        self.historial = []
        self._recalcular()
        self.update()
        self.estado_cambiado.emit()

    def limpiar(self):
        self.pix_original = None
        self.vista = QRect()
        self.marcas = []
        self.historial = []
        self._cache = None
        self.update()
        self.estado_cambiado.emit()

    def hay_imagen(self) -> bool:
        return self.pix_original is not None

    def _recalcular(self):
        if not self.pix_original or self.vista.isEmpty() or self.width() < 2:
            self._cache = None
            return
        recorte = self.pix_original.copy(self.vista)
        self._escala = min(self.width() / self.vista.width(),
                           self.height() / self.vista.height())
        destino = QSize(max(1, int(self.vista.width() * self._escala)),
                        max(1, int(self.vista.height() * self._escala)))
        self._cache = recorte.scaled(destino,
                                     Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
        self._offset = QPoint((self.width() - self._cache.width()) // 2,
                              (self.height() - self._cache.height()) // 2)

    def resizeEvent(self, event):
        self._recalcular()
        super().resizeEvent(event)

    # ---------------- conversión de coordenadas ----------------
    def a_imagen(self, punto: QPoint):
        if not self._cache:
            return None
        x = self.vista.x() + (punto.x() - self._offset.x()) / self._escala
        y = self.vista.y() + (punto.y() - self._offset.y()) / self._escala
        return QPoint(int(round(x)), int(round(y)))

    def _dentro(self, punto: QPoint) -> bool:
        if not self._cache:
            return False
        area = QRect(self._offset, self._cache.size())
        return area.contains(punto)

    # ---------------- dibujo ----------------
    def _pintar_marca(self, painter: QPainter, marca):
        pen = QPen(marca["color"])
        pen.setWidth(marca["ancho"])
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        painter.setPen(pen)
        x, y, t = marca["x"], marca["y"], marca["tamano"]
        painter.drawLine(x - t, y, x + t, y)
        painter.drawLine(x, y - t, x, y + t)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # fondo con esquinas redondeadas, para que el lienzo se lea como una
        # superficie más y no como un rectángulo pegado
        fondo = QPainterPath()
        fondo.addRoundedRect(0.0, 0.0, float(self.width()), float(self.height()), 12.0, 12.0)
        p.fillPath(fondo, QColor(PAL["lienzo"]))
        p.setPen(QPen(QColor(PAL["borde"]), 1))
        p.drawPath(fondo)
        p.setClipPath(fondo)

        if not self._cache:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            fuente = p.font()
            fuente.setPointSize(11)
            p.setFont(fuente)
            p.setPen(QColor(PAL["texto_ten"]))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "Sin estudio cargado\n\nCtrl+F para buscar un paciente")
            return

        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.drawPixmap(self._offset, self._cache)

        # marcas, en el sistema de coordenadas de la imagen
        if self.marcas:
            p.save()
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.translate(self._offset)
            p.scale(self._escala, self._escala)
            p.translate(-self.vista.x(), -self.vista.y())
            for marca in self.marcas:
                self._pintar_marca(p, marca)
            p.restore()

        area = QRect(self._offset, self._cache.size())

        # mira
        if self.modo == "tatuaje" and self._cursor and area.contains(self._cursor):
            cx, cy = self._cursor.x(), self._cursor.y()
            guia = QPen(self.color_guia, 1, Qt.PenStyle.DashLine)
            guia.setDashPattern([4, 4])
            p.setPen(guia)
            p.drawLine(area.left(), cy, area.right(), cy)
            p.drawLine(cx, area.top(), cx, area.bottom())

            # previsualización de la cruz real, con el color y tamaño elegidos
            t = max(2, int(self.tamano * self._escala))
            a = max(1, int(round(self.ancho * self._escala)))
            p.setPen(QPen(self.color_tatuaje, a, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.FlatCap))
            p.drawLine(cx - t, cy, cx + t, cy)
            p.drawLine(cx, cy - t, cx, cy + t)

        # rectángulo de recorte: solo en modo recorte
        if self.modo == "recorte" and self._arrastrando and self._p1 and self._p2:
            seleccion = QRect(self._p1, self._p2).normalized()
            # oscurecer todo lo que queda fuera de la selección
            fuera = QRegion(area).subtracted(QRegion(seleccion))
            p.save()
            p.setClipRegion(fuera)
            p.fillRect(area, QColor(0, 0, 0, 130))
            p.restore()
            p.setPen(QPen(QColor(PAL["acento_alto"]), 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(seleccion)

    def _region_mira(self, punto):
        if punto is None:
            return QRegion()
        margen = int(self.tamano * self._escala) + self.ancho + 6
        horizontal = QRect(0, punto.y() - margen, self.width(), 2 * margen)
        vertical = QRect(punto.x() - margen, 0, 2 * margen, self.height())
        return QRegion(horizontal).united(QRegion(vertical))

    # ---------------- mouse ----------------
    def mouseMoveEvent(self, event):
        punto = event.position().toPoint()
        anterior = self._cursor
        self._cursor = punto

        if self.modo == "tatuaje":
            # repintado parcial: solo las franjas de la mira -> se siente instantáneo
            self.update(self._region_mira(anterior).united(self._region_mira(punto)))
        elif self.modo == "recorte" and self._arrastrando:
            self._p2 = punto
            self.update()

        self.cursor_movido.emit(self.a_imagen(punto) if self._dentro(punto) else None)

    def leaveEvent(self, event):
        anterior = self._cursor
        self._cursor = None
        if self.modo == "tatuaje":
            self.update(self._region_mira(anterior))
        self.cursor_movido.emit(None)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self._cache:
            return
        punto = event.position().toPoint()
        if not self._dentro(punto):
            return

        if self.modo == "recorte":
            self._arrastrando = True
            self._p1 = punto
            self._p2 = punto
        elif self.modo == "tatuaje":
            self.agregar_marca(self.a_imagen(punto))

    def mouseReleaseEvent(self, event):
        if self.modo == "recorte" and self._arrastrando:
            self._arrastrando = False
            self._aplicar_recorte()
            self._p1 = self._p2 = None
            self.update()

    # ---------------- acciones ----------------
    def _guardar_estado(self):
        self.historial.append((list(self.marcas), QRect(self.vista)))
        if len(self.historial) > 60:
            self.historial.pop(0)

    def agregar_marca(self, punto_imagen: QPoint):
        if punto_imagen is None:
            return
        self._guardar_estado()
        self.marcas.append({
            "x": punto_imagen.x(), "y": punto_imagen.y(),
            "color": QColor(self.color_tatuaje),
            "tamano": self.tamano, "ancho": self.ancho,
        })
        self.update()
        self.estado_cambiado.emit()

    def _aplicar_recorte(self):
        p1 = self.a_imagen(self._p1)
        p2 = self.a_imagen(self._p2)
        if p1 is None or p2 is None:
            return
        nueva = QRect(p1, p2).normalized().intersected(
            QRect(0, 0, self.pix_original.width(), self.pix_original.height()))
        if nueva.width() < 16 or nueva.height() < 16:
            return
        self._guardar_estado()
        self.vista = nueva
        self.modo = "nav"
        self.unsetCursor()
        self._recalcular()
        self.update()
        self.modo_terminado.emit()
        self.estado_cambiado.emit()

    def deshacer(self) -> bool:
        if not self.historial:
            return False
        self.marcas, self.vista = self.historial.pop()
        self._recalcular()
        self.update()
        self.estado_cambiado.emit()
        return True

    def restablecer_vista(self):
        if not self.pix_original:
            return
        self._guardar_estado()
        self.vista = QRect(0, 0, self.pix_original.width(), self.pix_original.height())
        self._recalcular()
        self.update()
        self.estado_cambiado.emit()

    def borrar_marcas(self):
        if not self.marcas:
            return
        self._guardar_estado()
        self.marcas = []
        self.update()
        self.estado_cambiado.emit()

    def fijar_modo(self, modo: str):
        self.modo = modo
        if modo == "tatuaje":
            self.setCursor(Qt.CursorShape.BlankCursor)
        elif modo == "recorte":
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.unsetCursor()
        self.update()

    def render_export(self) -> QPixmap:
        """Vista actual con las marcas, a resolución original."""
        pm = self.pix_original.copy(self.vista)
        p = QPainter(pm)
        p.translate(-self.vista.x(), -self.vista.y())
        for marca in self.marcas:
            self._pintar_marca(p, marca)
        p.end()
        return pm


# ==========================================================================
# VENTANA PRINCIPAL
# ==========================================================================
# --------------------------------------------------------------------------
# PALETA
# Tema oscuro, como los visores de imagen médica: un panel claro al lado de
# una imagen en escala de grises altera cómo se percibe el gris de la imagen.
# --------------------------------------------------------------------------
PAL = {
    "fondo":      "#101317",   # fondo general de la ventana
    "lienzo":     "#0A0C0F",   # área de imagen, más oscura que todo lo demás
    "tarjeta":    "#191D23",   # superficie de los paneles
    "borde":      "#272D36",
    "borde_alto": "#39424E",
    "texto":      "#E6EAF0",
    "texto_sec":  "#98A2B0",
    "texto_ten":  "#5F6B7A",   # tenue: rótulos de sección
    "acento":     "#12A594",   # verde azulado, se lee bien sobre oscuro
    "acento_alto": "#16BFAC",
    "peligro":    "#D6605E",
}

def _flecha_png(ruta: str, hacia_abajo: bool):
    """Qt no dibuja las flechas nativas de combos y spinbox cuando el widget
    tiene borde propio por hoja de estilos: hay que darle la imagen."""
    pm = QPixmap(18, 18)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(PAL["texto_sec"]), 1.6,
                  Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                  Qt.PenJoinStyle.RoundJoin))
    if hacia_abajo:
        p.drawPolyline(QPolygonF([QPointF(5, 7), QPointF(9, 11), QPointF(13, 7)]))
    else:
        p.drawPolyline(QPolygonF([QPointF(5, 11), QPointF(9, 7), QPointF(13, 11)]))
    p.end()
    pm.save(ruta, "PNG")


def construir_estilos() -> str:
    """Arma la hoja de estilos. Necesita que exista un QApplication porque
    genera las imágenes de las flechas."""
    carpeta = os.path.join(tempfile.gettempdir(), "tatoo_cemener_ui")
    os.makedirs(carpeta, exist_ok=True)
    abajo = os.path.join(carpeta, "flecha_abajo.png")
    arriba = os.path.join(carpeta, "flecha_arriba.png")
    try:
        _flecha_png(abajo, True)
        _flecha_png(arriba, False)
    except Exception:
        abajo = arriba = ""
    # Qt siempre usa barras normales en url(), también en Windows
    valores = dict(PAL,
                   flecha_abajo=abajo.replace("\\", "/"),
                   flecha_arriba=arriba.replace("\\", "/"))
    return PLANTILLA_ESTILOS.format(**valores)


PLANTILLA_ESTILOS = """
QMainWindow, QWidget#panel {{ background: {fondo}; }}
QWidget#tarjeta {{
    background: {tarjeta}; border: 1px solid {borde}; border-radius: 12px;
}}

QLabel {{ color: {texto_sec}; font-size: 13px; }}
QLabel#tituloSeccion {{
    color: {texto_ten}; font-size: 10px; font-weight: 700;
    letter-spacing: 1.4px; text-transform: uppercase;
}}
QLabel#nombrePaciente {{
    color: {texto}; font-size: 20px; font-weight: 700; letter-spacing: 0.3px;
}}
QLabel#datosEstudio {{ color: {texto_ten}; font-size: 12px; }}
QLabel#estadoIzq {{ color: {texto_sec}; font-size: 12px; }}
QLabel#estadoDer {{
    color: {texto_ten}; font-size: 12px;
    font-family: "Consolas", "DejaVu Sans Mono", monospace;
}}
QFrame#encabezado {{ background: {tarjeta}; border-bottom: 1px solid {borde}; }}
QFrame#lineaAcento {{ background: {acento}; border: none; }}
QFrame#barraEstado {{ background: {tarjeta}; border-top: 1px solid {borde}; }}

QPushButton {{
    background: #212730; color: {texto}; border: 1px solid {borde_alto};
    border-radius: 8px; padding: 0 12px; min-height: 32px; font-size: 13px;
}}
QPushButton:hover {{ background: #2A313B; border-color: #4A5462; }}
QPushButton:pressed {{ background: #1A1F26; }}
QPushButton:disabled {{
    background: #171B21; color: #4A535F; border-color: #232932;
}}
QPushButton#primario {{
    background: {acento}; color: #04211E; border: 1px solid {acento};
    font-weight: 700;
}}
QPushButton#primario:hover {{ background: {acento_alto}; border-color: {acento_alto}; }}
QPushButton#primario:disabled {{
    background: #17342F; color: #4E6863; border-color: #1D3D38;
}}
QPushButton#peligro {{ color: {peligro}; border-color: #4A2E30; }}
QPushButton#peligro:hover {{ background: #2E1E20; border-color: #6B3E3F; }}
QPushButton:checked {{
    background: {acento}; color: #04211E; border-color: {acento}; font-weight: 700;
}}

QComboBox, QSpinBox {{
    background: #12161B; color: {texto}; border: 1px solid {borde_alto};
    border-radius: 8px; padding: 0 8px; min-height: 32px; font-size: 13px;
    selection-background-color: {acento};
}}
QComboBox:hover, QSpinBox:hover {{ border-color: #4A5462; }}
QComboBox:focus, QSpinBox:focus {{ border-color: {acento}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: url("{flecha_abajo}"); width: 18px; height: 18px; }}
QComboBox QAbstractItemView {{
    background: {tarjeta}; color: {texto}; border: 1px solid {borde_alto};
    border-radius: 8px; padding: 4px; outline: none;
    selection-background-color: {acento}; selection-color: #04211E;
}}
QSpinBox::up-button, QSpinBox::down-button {{
    subcontrol-origin: border; width: 20px;
    background: #1C222A; border-left: 1px solid {borde};
}}
QSpinBox::up-button {{ subcontrol-position: top right; border-top-right-radius: 7px; }}
QSpinBox::down-button {{ subcontrol-position: bottom right; border-bottom-right-radius: 7px; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: #2A313B; }}
QSpinBox::up-arrow {{ image: url("{flecha_arriba}"); width: 16px; height: 16px; }}
QSpinBox::down-arrow {{ image: url("{flecha_abajo}"); width: 16px; height: 16px; }}

QToolButton#cabeceraSeccion {{
    background: transparent; border: none; color: {texto_sec};
    font-size: 11px; font-weight: 700; letter-spacing: 1.4px;
    padding: 4px 0; min-height: 24px;
}}
QToolButton#cabeceraSeccion:hover {{ color: {texto}; }}
QLabel#resumenSeccion {{ color: {texto_ten}; font-size: 12px; }}
QLabel#carpetaActual {{ color: {texto_ten}; font-size: 11px; }}

QRadioButton {{ color: {texto}; font-size: 13px; spacing: 8px; padding: 2px 0; }}
QRadioButton:disabled {{ color: #4A535F; }}
QCheckBox {{ color: {texto}; font-size: 13px; spacing: 8px; padding: 2px 0; }}
QRadioButton::indicator {{
    width: 12px; height: 12px; border-radius: 8px;
    border: 2px solid #4A5462; background: #12161B;
}}
QRadioButton::indicator:hover {{ border-color: {texto_sec}; }}
QRadioButton::indicator:checked {{
    border: 2px solid {acento};
    background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
                                stop:0 {acento}, stop:0.5 {acento},
                                stop:0.6 #12161B, stop:1 #12161B);
}}

QPushButton#muestra {{
    min-height: 26px; max-height: 26px; min-width: 26px; max-width: 26px;
    padding: 0; border-radius: 6px; border: 2px solid {borde_alto};
}}
QPushButton#muestra:checked {{ border: 2px solid {texto}; }}

QLineEdit {{
    background: #12161B; color: {texto}; border: 1px solid {borde_alto};
    border-radius: 8px; padding: 0 8px; min-height: 30px; font-size: 13px;
}}
QLineEdit:focus {{ border-color: {acento}; }}

QListWidget {{
    background: #12161B; color: {texto}; border: 1px solid {borde_alto};
    border-radius: 8px; padding: 3px; font-size: 13px; outline: none;
}}
QListWidget::item {{ padding: 4px 6px; border-radius: 5px; }}
QListWidget::item:hover {{ background: #1E242C; }}
QListWidget::item:selected {{ background: {acento}; color: #04211E; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: #2E3641; border-radius: 5px; min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{ background: #3D4756; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QToolTip {{
    background: {tarjeta}; color: {texto}; border: 1px solid {borde_alto};
    border-radius: 6px; padding: 5px 8px;
}}
QMessageBox {{ background: {fondo}; }}
QMessageBox QLabel {{ color: {texto}; font-size: 13px; }}
"""


def paleta_oscura() -> QPalette:
    """Qt pinta algunos elementos (flechas de combos y spinbox, cursor de
    texto, selección) con la paleta del sistema, no con la hoja de estilos.
    Sin esto quedan oscuros sobre fondo oscuro."""
    p = QPalette()
    C = QPalette.ColorRole
    p.setColor(C.Window, QColor(PAL["fondo"]))
    p.setColor(C.WindowText, QColor(PAL["texto"]))
    p.setColor(C.Base, QColor("#12161B"))
    p.setColor(C.AlternateBase, QColor(PAL["tarjeta"]))
    p.setColor(C.Text, QColor(PAL["texto"]))
    p.setColor(C.Button, QColor("#212730"))
    p.setColor(C.ButtonText, QColor(PAL["texto"]))
    p.setColor(C.Highlight, QColor(PAL["acento"]))
    p.setColor(C.HighlightedText, QColor("#04211E"))
    p.setColor(C.ToolTipBase, QColor(PAL["tarjeta"]))
    p.setColor(C.ToolTipText, QColor(PAL["texto"]))
    p.setColor(C.PlaceholderText, QColor(PAL["texto_ten"]))
    gris = QColor("#4A535F")
    for rol in (C.Text, C.ButtonText, C.WindowText):
        p.setColor(QPalette.ColorGroup.Disabled, rol, gris)
    return p


class TatooApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NOMBRE} · v{APP_VERSION}")
        self.resize(1180, 780)

        # Preferencias que sobreviven al cierre (en Windows van al registro
        # del usuario): la carpeta DICOM elegida.
        self.ajustes = QSettings("CEMENER", "TatooCEMENER")

        self.directorio_raiz = DIR_DICOM_DEFECTO
        self.paciente_dir = ""
        self.ultimo_dir_export = ""
        self.ocultos = self._cargar_ocultos()
        self.nombres = self._cargar_nombres()
        self.espaciado_mm = None
        self.datos_estudio = ""

        self._cache_img = OrderedDict()
        self._precargando = {}
        self._hilos = QThreadPool()
        self._hilos.setMaxThreadCount(1)
        self._hilos_nombres = QThreadPool()
        self._hilos_nombres.setMaxThreadCount(1)
        self._lector_nombres = None
        # marcas tal como estaban en la última exportación, para saber si
        # quedó algo sin guardar
        self._marcas_exportadas = []
        # paciente de la imagen en pantalla (para la carpeta de salida)
        self._paciente_actual = ""
        self._carpeta_actual = ""

        self._construir_ui()
        self._atajos()
        self.setStyleSheet(construir_estilos())

        # El escaneo del directorio (que puede estar en la red) se hace después
        # de mostrar la ventana, así la app no queda congelada al abrir.
        QTimer.singleShot(0, self._escaneo_inicial)

    def _escaneo_inicial(self):
        """Abre la última carpeta usada; si no existe, la fija por defecto;
        si tampoco, pide elegir una (y queda guardada para la próxima)."""
        guardada = str(self.ajustes.value("carpeta_dicom", "") or "")
        for candidata in (guardada, DIR_DICOM_DEFECTO):
            if candidata and os.path.isdir(candidata):
                self.directorio_raiz = candidata
                self._estado("Leyendo la carpeta de pacientes…")
                QApplication.processEvents()
                self._listar_pacientes(candidata)
                return
        self._estado("No se encontró la carpeta DICOM. Elegí una.")
        self._mostrar_carpeta()
        self.elegir_directorio()

    # ---------------- construcción de la interfaz ----------------
    def _construir_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        raiz = QVBoxLayout(central)
        raiz.setContentsMargins(0, 0, 0, 0)
        raiz.setSpacing(0)

        raiz.addWidget(self._encabezado())

        linea = QFrame()
        linea.setObjectName("lineaAcento")
        linea.setFixedHeight(2)
        raiz.addWidget(linea)

        cuerpo = QHBoxLayout()
        cuerpo.setContentsMargins(16, 16, 16, 16)
        cuerpo.setSpacing(16)

        self.lienzo = Lienzo()
        self.lienzo.modo_terminado.connect(self._modo_cancelado)
        self.lienzo.estado_cambiado.connect(self._refrescar_controles)
        self.lienzo.cursor_movido.connect(self._mostrar_coordenadas)
        cuerpo.addWidget(self.lienzo, stretch=1)

        cuerpo.addWidget(self._panel_lateral())
        raiz.addLayout(cuerpo, stretch=1)

        raiz.addWidget(self._barra_estado())

    def _barra_estado(self) -> QFrame:
        marco = QFrame()
        marco.setObjectName("barraEstado")
        marco.setFixedHeight(30)
        fila = QHBoxLayout(marco)
        fila.setContentsMargins(16, 0, 16, 0)

        self.lbl_estado = QLabel("Listo.")
        self.lbl_estado.setObjectName("estadoIzq")
        self.lbl_coords = QLabel("")
        self.lbl_coords.setObjectName("estadoDer")

        fila.addWidget(self.lbl_estado)
        fila.addStretch()
        fila.addWidget(self.lbl_coords)
        return marco

    def _encabezado(self) -> QFrame:
        marco = QFrame()
        marco.setObjectName("encabezado")
        fila = QHBoxLayout(marco)
        fila.setContentsMargins(20, 14, 20, 14)
        fila.setSpacing(16)

        logo = QLabel()
        pm = QPixmap(ruta_recurso(ARCHIVO_LOGO))
        if not pm.isNull():
            logo.setPixmap(pm.scaledToHeight(
                40, Qt.TransformationMode.SmoothTransformation))
        else:
            logo.setText("CEMENER")
            logo.setStyleSheet(
                f"font-weight:800; color:{PAL['acento']}; font-size:17px;"
                "letter-spacing:2px;")
        fila.addWidget(logo)

        columna = QVBoxLayout()
        columna.setSpacing(3)
        self.lbl_paciente = QLabel("Sin estudio cargado")
        self.lbl_paciente.setObjectName("nombrePaciente")
        self.lbl_datos = QLabel("Ctrl+F para buscar un paciente")
        self.lbl_datos.setObjectName("datosEstudio")
        columna.addWidget(self.lbl_paciente)
        columna.addWidget(self.lbl_datos)
        fila.addLayout(columna)
        fila.addStretch()

        self.lbl_marca = QLabel(f"{APP_NOMBRE}\nv{APP_VERSION}")
        self.lbl_marca.setObjectName("datosEstudio")
        self.lbl_marca.setAlignment(Qt.AlignmentFlag.AlignRight |
                                    Qt.AlignmentFlag.AlignVCenter)
        fila.addWidget(self.lbl_marca)
        return marco

    def _panel_lateral(self) -> QWidget:
        contenedor = QWidget()
        contenedor.setObjectName("panel")
        col = QVBoxLayout(contenedor)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(10)

        # --- Estudio ---
        self.sec_estudio = Seccion("Estudio")
        caja = self.sec_estudio.caja

        fila = QHBoxLayout()
        self.lbl_carpeta = QLabel()
        self.lbl_carpeta.setObjectName("carpetaActual")
        self.lbl_carpeta.setMinimumWidth(10)
        self.btn_directorio = QPushButton("Cambiar…")
        self.btn_directorio.setToolTip("Elegir otra carpeta DICOM (Ctrl+O). "
                                       "Queda guardada para la próxima vez.")
        self.btn_directorio.clicked.connect(self.elegir_directorio)
        fila.addWidget(self.lbl_carpeta, stretch=1)
        fila.addWidget(self.btn_directorio)
        caja.addLayout(fila)

        self.txt_buscar = QLineEdit()
        self.txt_buscar.setPlaceholderText("Buscar por nombre o DNI…")
        self.txt_buscar.setClearButtonEnabled(True)
        self.txt_buscar.textChanged.connect(self._filtrar_pacientes)
        caja.addWidget(self.txt_buscar)

        self.lst_pacientes = QListWidget()
        self.lst_pacientes.setMinimumHeight(150)
        self.lst_pacientes.currentItemChanged.connect(self.cambiar_paciente)
        caja.addWidget(self.lst_pacientes, stretch=1)

        self.lbl_imagenes = QLabel("Imagen")
        caja.addWidget(self.lbl_imagenes)
        self.lst_archivos = QListWidget()
        self.lst_archivos.currentItemChanged.connect(self._cambiar_imagen)
        caja.addWidget(self.lst_archivos)
        self._ajustar_alto_archivos()

        fila = QHBoxLayout()
        self.btn_ocultar = QPushButton("Borrar paciente")
        self.btn_ocultar.setObjectName("peligro")
        self.btn_ocultar.setToolTip("Lo saca de la lista. La carpeta no se borra.")
        self.btn_ocultar.clicked.connect(self.ocultar_paciente)
        self.btn_restaurar = QPushButton("Ver todos")
        self.btn_restaurar.setToolTip("Vuelve a mostrar los pacientes ocultos.")
        self.btn_restaurar.clicked.connect(self.restaurar_pacientes)
        fila.addWidget(self.btn_ocultar)
        fila.addWidget(self.btn_restaurar)
        caja.addLayout(fila)
        col.addWidget(self.sec_estudio, stretch=1)

        # --- Vista ---
        self.sec_vista = Seccion("Vista")
        fila = QHBoxLayout()
        self.btn_recorte = QPushButton("Recortar (R)")
        self.btn_recorte.setCheckable(True)
        self.btn_recorte.clicked.connect(self.alternar_recorte)
        self.btn_vista_completa = QPushButton("Ver completa")
        self.btn_vista_completa.clicked.connect(self.lienzo.restablecer_vista)
        fila.addWidget(self.btn_recorte)
        fila.addWidget(self.btn_vista_completa)
        self.sec_vista.caja.addLayout(fila)
        col.addWidget(self.sec_vista)

        # --- Tatuajes ---
        self.sec_tatuajes = Seccion("Tatuajes")
        caja = self.sec_tatuajes.caja

        caja.addWidget(QLabel("Color del tatuaje"))
        self.grupo_color = QButtonGroup(self)
        self._colores_tatuaje = list(COLORES_TATUAJE.items())
        lista_colores = QVBoxLayout()
        lista_colores.setSpacing(0)
        for i, (nombre, color) in enumerate(self._colores_tatuaje):
            opcion = QRadioButton(nombre)
            opcion.setIcon(icono_color(color))
            opcion.setChecked(i == 0)
            self.grupo_color.addButton(opcion, i)
            lista_colores.addWidget(opcion)
        self.grupo_color.idToggled.connect(self._cambiar_color_tatuaje)
        caja.addLayout(lista_colores)

        grilla = QGridLayout()
        grilla.setHorizontalSpacing(10)
        grilla.setVerticalSpacing(4)
        grilla.addWidget(QLabel("Tamaño (px)"), 0, 0)
        grilla.addWidget(QLabel("Grosor (px)"), 0, 1)
        self.spin_tamano = SpinSinRueda()
        self.spin_tamano.setRange(2, 400)
        self.spin_tamano.setValue(10)
        self.spin_tamano.valueChanged.connect(self._cambiar_tamano)
        self.spin_ancho = SpinSinRueda()
        self.spin_ancho.setRange(1, 30)
        self.spin_ancho.setValue(2)
        self.spin_ancho.valueChanged.connect(self._cambiar_ancho)
        grilla.addWidget(self.spin_tamano, 1, 0)
        grilla.addWidget(self.spin_ancho, 1, 1)
        caja.addLayout(grilla)

        fila = QHBoxLayout()
        fila.setSpacing(6)
        fila.addWidget(QLabel("Mira"))
        fila.addStretch()
        self.grupo_guia = QButtonGroup(self)
        self._colores_guia = list(COLORES_GUIA.items())
        for i, (nombre, color) in enumerate(self._colores_guia):
            muestra = QPushButton()
            muestra.setObjectName("muestra")
            muestra.setCheckable(True)
            muestra.setChecked(i == 0)
            muestra.setToolTip(f"Mira {nombre.lower()}")
            muestra.setStyleSheet(f"background: {color.name()};")
            self.grupo_guia.addButton(muestra, i)
            fila.addWidget(muestra)
        self.grupo_guia.idToggled.connect(self._cambiar_color_guia)
        caja.addLayout(fila)

        self.btn_colocar = QPushButton("Colocar tatuaje (T)")
        self.btn_colocar.setObjectName("primario")
        self.btn_colocar.setCheckable(True)
        self.btn_colocar.clicked.connect(self.alternar_colocacion)
        caja.addWidget(self.btn_colocar)

        fila = QHBoxLayout()
        self.btn_deshacer = QPushButton("Deshacer")
        self.btn_deshacer.clicked.connect(self.deshacer)
        self.btn_limpiar = QPushButton("Borrar marcas")
        self.btn_limpiar.clicked.connect(self.lienzo.borrar_marcas)
        fila.addWidget(self.btn_deshacer)
        fila.addWidget(self.btn_limpiar)
        caja.addLayout(fila)
        col.addWidget(self.sec_tatuajes)

        # --- Salida ---
        self.sec_salida = Seccion("Salida")
        caja = self.sec_salida.caja
        caja.addWidget(QLabel("Formato"))
        self.grupo_formato = QButtonGroup(self)
        grilla = QGridLayout()
        grilla.setVerticalSpacing(2)
        for i, (etiqueta, extension, _, _) in enumerate(FORMATOS_EXPORT):
            opcion = QRadioButton(etiqueta)
            opcion.setChecked(extension == FORMATO_DEFECTO)
            self.grupo_formato.addButton(opcion, i)
            grilla.addWidget(opcion, i // 2, i % 2)
        self.grupo_formato.idToggled.connect(lambda *_: self._actualizar_destino())
        caja.addLayout(grilla)

        caja.addWidget(QLabel("Carpeta de salida"))
        fila = QHBoxLayout()
        self.lbl_salida = QLabel()
        self.lbl_salida.setObjectName("carpetaActual")
        self.lbl_salida.setMinimumWidth(10)
        self.btn_salida = QPushButton("Cambiar…")
        self.btn_salida.setToolTip("Carpeta donde se crea la carpeta de cada paciente. "
                                   "Queda guardada para la próxima vez.")
        self.btn_salida.clicked.connect(self.elegir_carpeta_salida)
        fila.addWidget(self.lbl_salida, stretch=1)
        fila.addWidget(self.btn_salida)
        caja.addLayout(fila)

        self.chk_borrar = QCheckBox("Borrar carpeta del paciente al exportar")
        self.chk_borrar.setToolTip(
            "Después de exportar bien la imagen, borra la carpeta DICOM del "
            "paciente (no se puede deshacer).")
        self.chk_borrar.setChecked(
            str(self.ajustes.value("borrar_tras_exportar", "false")).lower() in ("true", "1"))
        self.chk_borrar.toggled.connect(self._cambiar_borrado)
        caja.addWidget(self.chk_borrar)
        col.addWidget(self.sec_salida)

        # Si Estudio está abierta, la lista de pacientes se lleva el espacio
        # libre; si está plegada, el espacio queda abajo y todo sube.
        col.addStretch(0)
        indice_estudio = col.indexOf(self.sec_estudio)
        indice_hueco = col.count() - 1

        def repartir(abierta):
            col.setStretch(indice_estudio, 1 if abierta else 0)
            col.setStretch(indice_hueco, 0 if abierta else 1)
        self.sec_estudio.boton.toggled.connect(repartir)
        repartir(self.sec_estudio.boton.isChecked())

        # El botón de exportar queda siempre a la vista, aunque las secciones
        # estén plegadas.
        self.btn_exportar = QPushButton("Exportar imagen…")
        self.btn_exportar.setObjectName("primario")
        self.btn_exportar.clicked.connect(self.exportar)
        col.addWidget(self.btn_exportar)

        self.lbl_destino = QLabel()
        self.lbl_destino.setObjectName("datosEstudio")
        self.lbl_destino.setWordWrap(True)
        col.addWidget(self.lbl_destino)

        scroll = QScrollArea()
        scroll.setWidget(contenedor)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(346)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._mostrar_carpeta()
        self._refrescar_controles()
        return scroll

    def _atajos(self):
        def atajo(secuencia, funcion):
            QShortcut(QKeySequence(secuencia), self).activated.connect(funcion)

        atajo("Ctrl+Z", self.deshacer)
        atajo("Ctrl+O", self.elegir_directorio)
        atajo("Ctrl+E", self.exportar)
        atajo("T", lambda: self.btn_colocar.click())
        atajo("R", lambda: self.btn_recorte.click())
        atajo("Esc", self._cancelar_modo)
        atajo("Ctrl+F", self._ir_al_buscador)
        atajo("F11", self._alternar_pantalla_completa)

    def _alternar_pantalla_completa(self):
        """F11: pantalla completa sin barra de título; otra vez F11 vuelve a
        la ventana maximizada."""
        if self.isFullScreen():
            self.showMaximized()
        else:
            self.showFullScreen()

    def _ir_al_buscador(self):
        if not self.sec_estudio.boton.isChecked():
            self.sec_estudio.boton.setChecked(True)
        self.txt_buscar.setFocus()
        self.txt_buscar.selectAll()

    # ---------------- marcas sin exportar ----------------
    def _hay_marcas_sin_exportar(self) -> bool:
        return bool(self.lienzo.marcas) and self.lienzo.marcas != self._marcas_exportadas

    def _confirmar_descartar(self) -> bool:
        """Si hay marcas sin exportar, pregunta qué hacer. Devuelve True si se
        puede seguir (se exportaron o se eligió descartarlas)."""
        if not self._hay_marcas_sin_exportar():
            return True
        n = len(self.lienzo.marcas)
        caja = QMessageBox(self)
        caja.setIcon(QMessageBox.Icon.Warning)
        caja.setWindowTitle("Marcas sin exportar")
        caja.setText(f"Hay {n} marca{'s' if n != 1 else ''} sin exportar en "
                     f"{self.lbl_paciente.text()}.")
        caja.setInformativeText("Si seguís sin exportar, se pierden.")
        exportar = caja.addButton("Exportar ahora", QMessageBox.ButtonRole.AcceptRole)
        descartar = caja.addButton("Descartar", QMessageBox.ButtonRole.DestructiveRole)
        caja.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
        caja.setDefaultButton(exportar)
        caja.exec()
        elegido = caja.clickedButton()
        if elegido is exportar:
            return self.exportar()
        return elegido is descartar

    # ---------------- estado de la interfaz ----------------
    def _estado(self, texto: str):
        self.lbl_estado.setText(texto)

    def _refrescar_controles(self):
        hay = self.lienzo.hay_imagen()
        for boton in (self.btn_recorte, self.btn_vista_completa, self.btn_colocar,
                      self.btn_exportar, self.btn_limpiar):
            boton.setEnabled(hay)
        self.btn_deshacer.setEnabled(hay and bool(self.lienzo.historial))
        self.btn_ocultar.setEnabled(self.lst_pacientes.currentItem() is not None)
        self.btn_restaurar.setEnabled(bool(self.ocultos))
        self._actualizar_destino()
        self._actualizar_resumenes()

    def _formato_actual(self):
        indice = self.grupo_formato.checkedId()
        return FORMATOS_EXPORT[indice if indice >= 0 else 0]

    def _carpeta_salida(self) -> str:
        return str(self.ajustes.value("carpeta_salida", "") or DIR_SALIDA_DEFECTO)

    def _carpeta_paciente_salida(self):
        """Nombre de la carpeta de salida del paciente actual: 'DNI - APELLIDO, NOMBRE'."""
        # se usa el paciente de la imagen que está en pantalla, no el marcado
        # en la lista (al cambiar de paciente la lista ya apunta al nuevo)
        dni = self._carpeta_actual
        if not dni:
            return None
        nombre = self._paciente_actual
        texto = f"{dni} - {nombre}" if nombre else dni
        # sin caracteres que Windows no acepta en nombres de carpeta
        return re.sub(r'[\\/:*?"<>|]', "", texto).strip(" .")

    def _actualizar_destino(self):
        extension = self._formato_actual()[1]
        base = self._carpeta_salida()
        visible = base if len(base) <= 34 else "…" + base[-33:]
        self.lbl_salida.setText(visible)
        self.lbl_salida.setToolTip(base)
        sub = self._carpeta_paciente_salida() or "DNI - APELLIDO, NOMBRE"
        texto = f"Se guarda como {NOMBRE_EXPORT}.{extension} en «{sub}»."
        if self.chk_borrar.isChecked():
            texto += " Después se borra la carpeta DICOM del paciente."
        self.lbl_destino.setText(texto)
        self.sec_salida.resumen(extension.upper() +
                                (" · borra carpeta" if self.chk_borrar.isChecked() else ""))

    def _cambiar_borrado(self, marcado: bool):
        self.ajustes.setValue("borrar_tras_exportar", marcado)
        self._actualizar_destino()

    def elegir_carpeta_salida(self):
        carpeta = QFileDialog.getExistingDirectory(
            self, "Elegir la carpeta de salida", self._carpeta_salida())
        if carpeta:
            self.ajustes.setValue("carpeta_salida", carpeta)
            self._actualizar_destino()

    def _actualizar_resumenes(self):
        """Lo que se ve a la derecha del título cuando una sección está plegada."""
        item = self.lst_pacientes.currentItem()
        self.sec_estudio.resumen(item.text().split("  ·  ")[0] if item else "Sin paciente")
        color = self._colores_tatuaje[max(0, self.grupo_color.checkedId())][0]
        self.sec_tatuajes.resumen(
            f"{color.split(' · ')[0]} · {self.spin_tamano.value()} px · "
            f"{len(self.lienzo.marcas)} marcas")
        self.sec_vista.resumen("Recortada" if self.lienzo.hay_imagen() and
                               self.lienzo.vista != QRect(
                                   0, 0, self.lienzo.pix_original.width(),
                                   self.lienzo.pix_original.height())
                               else "Completa")

    def _mostrar_coordenadas(self, punto):
        if punto is None:
            self.lbl_coords.setText(f"{len(self.lienzo.marcas)} marcas")
            return
        texto = f"x {punto.x():>5}   y {punto.y():>5}"
        if self.espaciado_mm:
            mm = self.spin_tamano.value() * 2 * self.espaciado_mm
            texto += f"   ·   cruz {mm:.1f} mm"
        texto += f"   ·   {len(self.lienzo.marcas)} marcas"
        self.lbl_coords.setText(texto)

    def _cambiar_color_tatuaje(self, indice, marcado):
        if not marcado:
            return
        self.lienzo.color_tatuaje = self._colores_tatuaje[indice][1]
        self.lienzo.update()
        self._actualizar_resumenes()

    def _cambiar_color_guia(self, indice, marcado):
        if not marcado:
            return
        self.lienzo.color_guia = self._colores_guia[indice][1]
        self.lienzo.update()

    def _cambiar_tamano(self, valor):
        self.lienzo.tamano = valor
        self.lienzo.update()
        self._actualizar_resumenes()

    def _cambiar_ancho(self, valor):
        self.lienzo.ancho = valor
        self.lienzo.update()

    def _cancelar_modo(self):
        self.btn_colocar.setChecked(False)
        self.btn_recorte.setChecked(False)
        self.lienzo.fijar_modo("nav")

    def _modo_cancelado(self):
        self._cancelar_modo()

    def alternar_colocacion(self):
        if self.btn_colocar.isChecked():
            self.btn_recorte.setChecked(False)
            self.lienzo.fijar_modo("tatuaje")
            self._estado("Clic sobre la imagen para marcar. Esc para salir.")
        else:
            self.lienzo.fijar_modo("nav")

    def alternar_recorte(self):
        if self.btn_recorte.isChecked():
            self.btn_colocar.setChecked(False)
            self.lienzo.fijar_modo("recorte")
            self._estado("Arrastrá para elegir la zona a ampliar. Esc para salir.")
        else:
            self.lienzo.fijar_modo("nav")

    def deshacer(self):
        if not self.lienzo.deshacer():
            self._estado("No queda nada para deshacer.")

    # ---------------- archivos ----------------
    def elegir_directorio(self):
        if not self._confirmar_descartar():
            return
        carpeta = QFileDialog.getExistingDirectory(
            self, "Elegir la carpeta raíz de DICOM", self.directorio_raiz)
        if carpeta:
            self.directorio_raiz = carpeta
            self.ajustes.setValue("carpeta_dicom", carpeta)
            self._listar_pacientes(carpeta)

    def _mostrar_carpeta(self):
        ruta = self.directorio_raiz or ""
        visible = ruta if len(ruta) <= 34 else "…" + ruta[-33:]
        self.lbl_carpeta.setText(visible)
        self.lbl_carpeta.setToolTip(ruta)

    def _texto_paciente(self, carpeta: str) -> str:
        nombre = self.nombres.get(self._clave_oculto(carpeta))
        # el DNI queda al lado del nombre para distinguir homónimos
        return f"{nombre}  ·  {carpeta}" if nombre else carpeta

    def _listar_pacientes(self, ruta: str):
        try:
            with os.scandir(ruta) as entradas:
                todas = [(e.name, e.stat().st_mtime) for e in entradas if e.is_dir()]
        except OSError as e:
            QMessageBox.warning(self, "No se pudo leer la carpeta", str(e))
            return

        # los modificados más recientemente, primero
        todas.sort(key=lambda t: t[1], reverse=True)
        visibles = [(c, m) for c, m in todas
                    if self._clave_oculto(c) not in self.ocultos]
        escondidos = len(todas) - len(visibles)

        self.lst_pacientes.blockSignals(True)
        self.lst_pacientes.clear()
        for carpeta, mtime in visibles:
            item = QListWidgetItem(self._texto_paciente(carpeta))
            item.setData(Qt.ItemDataRole.UserRole, carpeta)
            fecha = datetime.fromtimestamp(mtime).strftime("%d/%m/%Y %H:%M")
            item.setToolTip(f"{carpeta}\nÚltima modificación: {fecha}")
            self.lst_pacientes.addItem(item)
        self.lst_pacientes.setCurrentRow(-1)
        self.lst_pacientes.blockSignals(False)
        self._filtrar_pacientes(self.txt_buscar.text())

        self.lst_archivos.blockSignals(True)
        self.lst_archivos.clear()
        self.lst_archivos.blockSignals(False)
        self._ajustar_alto_archivos()
        self.paciente_dir = ""
        self.lienzo.limpiar()
        self.lbl_paciente.setText("Sin estudio cargado")
        self.lbl_datos.setText("Ctrl+F para buscar un paciente")
        self._mostrar_carpeta()

        detalle = f" ({escondidos} oculto/s)" if escondidos else ""
        self._estado(f"{len(visibles)} pacientes en {ruta}{detalle}")
        self._leer_nombres(ruta, [c for c, _ in visibles])

    def _filtrar_pacientes(self, texto: str):
        texto = texto.strip().lower()
        for i in range(self.lst_pacientes.count()):
            item = self.lst_pacientes.item(i)
            item.setHidden(bool(texto) and texto not in item.text().lower())

    # ---------------- nombres de paciente ----------------
    def _leer_nombres(self, raiz: str, carpetas: list):
        if self._lector_nombres:
            self._lector_nombres.cancelado = True
        pendientes = [c for c in carpetas
                      if self._clave_oculto(c) not in self.nombres]
        if not pendientes:
            self._lector_nombres = None
            return
        lector = LectorNombres(raiz, pendientes)
        lector.senales.nombre.connect(self._nombre_leido)
        lector.senales.terminado.connect(lambda *_: self._guardar_nombres())
        self._lector_nombres = lector
        self._hilos_nombres.start(lector)

    def _nombre_leido(self, raiz: str, carpeta: str, nombre: str):
        clave = os.path.normcase(os.path.join(raiz, carpeta))
        self.nombres[clave] = nombre
        if raiz != self.directorio_raiz:
            return
        for i in range(self.lst_pacientes.count()):
            item = self.lst_pacientes.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == carpeta:
                item.setText(self._texto_paciente(carpeta))
                break
        self._filtrar_pacientes(self.txt_buscar.text())
        if self.lst_pacientes.currentItem():
            self._actualizar_resumenes()

    def _cargar_nombres(self) -> dict:
        try:
            with open(ARCHIVO_NOMBRES, "r", encoding="utf-8") as f:
                datos = json.load(f)
            return datos if isinstance(datos, dict) else {}
        except (OSError, ValueError):
            return {}

    def _guardar_nombres(self):
        try:
            with open(ARCHIVO_NOMBRES, "w", encoding="utf-8") as f:
                json.dump(self.nombres, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def closeEvent(self, event):
        if not self._confirmar_descartar():
            event.ignore()
            return
        if self._lector_nombres:
            self._lector_nombres.cancelado = True
        self._guardar_nombres()
        super().closeEvent(event)

    # ---------------- imágenes del paciente ----------------
    def _ajustar_alto_archivos(self):
        """La lista de imágenes ocupa solo lo que necesita (entre 1 y 4 filas)."""
        filas = min(max(self.lst_archivos.count(), 1), 4)
        alto_fila = self.lst_archivos.sizeHintForRow(0) if self.lst_archivos.count() else 26
        self.lst_archivos.setFixedHeight(filas * max(alto_fila, 26) + 10)

    def cambiar_paciente(self, actual=None, anterior=None):
        if anterior is not None and actual is not anterior and \
                not self._confirmar_descartar():
            # se cancela: vuelve a quedar marcado el paciente anterior
            self.lst_pacientes.blockSignals(True)
            self.lst_pacientes.setCurrentItem(anterior)
            self.lst_pacientes.blockSignals(False)
            return
        self.lst_archivos.blockSignals(True)
        self.lst_archivos.clear()
        self.lst_archivos.blockSignals(False)
        self._ajustar_alto_archivos()

        item = self.lst_pacientes.currentItem()
        if item is None:
            self.paciente_dir = ""
            self.lienzo.limpiar()
            self.lbl_paciente.setText("Sin estudio cargado")
            self.lbl_datos.setText("Ctrl+F para buscar un paciente")
            self._refrescar_controles()
            return

        self.paciente_dir = os.path.join(self.directorio_raiz,
                                         item.data(Qt.ItemDataRole.UserRole))
        self._paciente_actual = ""
        self._carpeta_actual = ""
        self._cache_img.clear()
        try:
            with os.scandir(self.paciente_dir) as entradas:
                archivos = sorted(e.name for e in entradas
                                  if e.is_file() and not e.name.startswith("."))
        except OSError as e:
            self.lienzo.limpiar()
            self._estado(f"No se pudo leer la carpeta: {e}")
            return

        if not archivos:
            self.lienzo.limpiar()
            self.lbl_imagenes.setText("Imagen")
            self._estado("Esa carpeta no tiene archivos.")
            return

        # Solo la reconstrucción 3D (archivos SC…). Si no hay, se muestran todos.
        reconstruccion = [a for a in archivos if es_reconstruccion(a)]
        if reconstruccion:
            archivos = reconstruccion
            self.lbl_imagenes.setText(f"Reconstrucción 3D ({len(archivos)})")
        else:
            self.lbl_imagenes.setText(
                f"Imagen ({len(archivos)}) · sin archivos {PREFIJO_RECONSTRUCCION}")

        self.lst_archivos.blockSignals(True)
        self.lst_archivos.addItems(archivos)
        self.lst_archivos.setCurrentRow(0)
        self.lst_archivos.blockSignals(False)
        self._ajustar_alto_archivos()
        self.cargar_dicom()

    def _cambiar_imagen(self, actual, anterior):
        if anterior is not None and not self._confirmar_descartar():
            self.lst_archivos.blockSignals(True)
            self.lst_archivos.setCurrentItem(anterior)
            self.lst_archivos.blockSignals(False)
            return
        if self.paciente_dir and os.path.isdir(self.paciente_dir):
            self.cargar_dicom()

    def cargar_dicom(self, *_):
        item = self.lst_archivos.currentItem()
        if item is None or not self.paciente_dir:
            return
        nombre = item.text()
        ruta = os.path.join(self.paciente_dir, nombre)

        if ruta in self._cache_img:
            imagen, meta = self._cache_img[ruta]
            self._cache_img.move_to_end(ruta)
        else:
            QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
            try:
                imagen, meta = leer_dicom(ruta)
            except Exception as e:
                self.lienzo.limpiar()
                self.lbl_paciente.setText("No se pudo abrir el archivo")
                self.lbl_datos.setText(nombre)
                self._estado(f"Error al leer {nombre}: {e}")
                return
            finally:
                QApplication.restoreOverrideCursor()
            self._guardar_en_cache(ruta, imagen, meta)

        self.lbl_paciente.setText(meta["paciente"])
        self._paciente_actual = meta["paciente"]
        self._carpeta_actual = os.path.basename(os.path.normpath(self.paciente_dir))
        self.espaciado_mm = meta["espaciado"]

        partes = [meta["modalidad"], f"{imagen.width()}×{imagen.height()} px"]
        fecha = meta["fecha"]
        if len(fecha) == 8:
            partes.append(f"{fecha[6:]}/{fecha[4:6]}/{fecha[:4]}")
        partes.append(nombre)
        self.datos_estudio = "  ·  ".join(p for p in partes if p)
        self.lbl_datos.setText(self.datos_estudio)

        self.lienzo.cargar(QPixmap.fromImage(imagen))
        self._marcas_exportadas = []
        self._cancelar_modo()
        self._estado(self.datos_estudio)
        self._precargar_siguiente()

    # ---------------- caché y precarga ----------------
    def _guardar_en_cache(self, ruta, imagen, meta):
        if imagen is None:
            return
        self._cache_img[ruta] = (imagen, meta)
        self._cache_img.move_to_end(ruta)
        while len(self._cache_img) > MAX_CACHE:
            self._cache_img.popitem(last=False)

    def _precargar_siguiente(self):
        """Mientras el usuario marca, va leyendo la imagen que sigue en la lista."""
        indice = self.lst_archivos.currentRow() + 1
        if indice >= self.lst_archivos.count() or not self.paciente_dir:
            return
        ruta = os.path.join(self.paciente_dir, self.lst_archivos.item(indice).text())
        if ruta in self._cache_img or ruta in self._precargando:
            return

        self._precargando[ruta] = lector = LectorEnSegundoPlano(ruta)
        lector.senales.listo.connect(self._precarga_lista)
        self._hilos.start(lector)

    def _precarga_lista(self, ruta, imagen, meta):
        self._precargando.pop(ruta, None)
        if imagen is not None and os.path.dirname(ruta) == self.paciente_dir:
            self._guardar_en_cache(ruta, imagen, meta)

    # ---------------- salida ----------------
    def exportar(self) -> bool:
        if not self.lienzo.hay_imagen():
            return False

        _, extension, formato, calidad = self._formato_actual()
        archivo = f"{NOMBRE_EXPORT}.{extension}"

        base = self._carpeta_salida()
        if not os.path.isdir(base):
            respuesta = QMessageBox.question(
                self, "Carpeta de salida no disponible",
                f"No se encuentra la carpeta de salida:\n{base}\n\n"
                "¿Querés elegir otra carpeta solo para esta exportación?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if respuesta != QMessageBox.StandardButton.Yes:
                return False
            base = QFileDialog.getExistingDirectory(
                self, "Elegir carpeta de salida", self.ultimo_dir_export or "")
            if not base:
                return False

        carpeta_paciente = self._carpeta_paciente_salida()
        if not carpeta_paciente:
            return False
        dni_carpeta = self._carpeta_actual
        carpeta = os.path.join(base, carpeta_paciente)
        try:
            os.makedirs(carpeta, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "No se pudo crear la carpeta",
                                f"{carpeta}\n\n{e}")
            return False

        ruta = os.path.join(carpeta, archivo)
        if os.path.exists(ruta):
            confirmar = QMessageBox.question(
                self, "Reemplazar archivo",
                f"Esa carpeta ya tiene un {archivo}. ¿Lo reemplazo?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if confirmar != QMessageBox.StandardButton.Yes:
                return False

        imagen = self.lienzo.render_export()
        if imagen.save(ruta, formato, calidad) and os.path.getsize(ruta) > 0:
            self.ultimo_dir_export = base
            self._marcas_exportadas = list(self.lienzo.marcas)
            self._estado(f"Guardado: {ruta}")
            if self.chk_borrar.isChecked():
                self._borrar_carpeta_paciente(dni_carpeta, ruta)
            return True
        QMessageBox.warning(self, "No se pudo guardar",
                            f"Revisá permisos de escritura en:\n{carpeta}")
        return False

    def _borrar_carpeta_paciente(self, carpeta: str, exportado: str):
        """Borra la carpeta DICOM del paciente (solo si está directamente
        dentro de la carpeta DICOM elegida) y lo saca de la lista."""
        raiz = os.path.normcase(os.path.normpath(self.directorio_raiz))
        ruta = os.path.normpath(os.path.join(self.directorio_raiz, carpeta))
        if (not carpeta or os.path.normcase(os.path.dirname(ruta)) != raiz
                or not os.path.isdir(ruta)):
            return
        self._cache_img.clear()
        self._precargando.clear()
        try:
            shutil.rmtree(ruta)
        except OSError as e:
            QMessageBox.warning(
                self, "No se pudo borrar la carpeta del paciente",
                f"La imagen se exportó bien en:\n{exportado}\n\n"
                f"pero no se pudo borrar:\n{ruta}\n\n{e}")
            return
        self.nombres.pop(self._clave_oculto(carpeta), None)
        self._quitar_paciente_de_lista(carpeta)
        self._estado(f"Guardado: {exportado}  ·  carpeta {carpeta} borrada")

    def _quitar_paciente_de_lista(self, carpeta: str):
        actual = self.lst_pacientes.currentItem()
        era_actual = actual is not None and actual.data(Qt.ItemDataRole.UserRole) == carpeta
        self.lst_pacientes.blockSignals(True)
        for i in range(self.lst_pacientes.count()):
            if self.lst_pacientes.item(i).data(Qt.ItemDataRole.UserRole) == carpeta:
                self.lst_pacientes.takeItem(i)
                break
        if era_actual:
            self.lst_pacientes.setCurrentRow(-1)
        self.lst_pacientes.blockSignals(False)
        if era_actual:
            self.lst_archivos.blockSignals(True)
            self.lst_archivos.clear()
            self.lst_archivos.blockSignals(False)
            self._ajustar_alto_archivos()
            self.paciente_dir = ""
            self._paciente_actual = ""
            self._carpeta_actual = ""
            self._marcas_exportadas = []
            self.lienzo.limpiar()
            self.lbl_paciente.setText("Sin estudio cargado")
            self.lbl_datos.setText("Ctrl+F para buscar un paciente")
        self._refrescar_controles()

    # ---------------- lista de pacientes ----------------
    def _cargar_ocultos(self) -> set:
        try:
            with open(ARCHIVO_OCULTOS, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except (OSError, ValueError):
            return set()

    def _guardar_ocultos(self):
        try:
            with open(ARCHIVO_OCULTOS, "w", encoding="utf-8") as f:
                json.dump(sorted(self.ocultos), f, ensure_ascii=False, indent=1)
        except OSError:
            pass  # si no se puede escribir, la ocultación vale solo esta sesión

    def _clave_oculto(self, carpeta_paciente: str) -> str:
        return os.path.normcase(os.path.join(self.directorio_raiz, carpeta_paciente))

    def ocultar_paciente(self):
        item = self.lst_pacientes.currentItem()
        if item is None:
            return
        if not self._confirmar_descartar():
            return
        paciente = item.data(Qt.ItemDataRole.UserRole)
        nombre_visible = item.text()

        confirmar = QMessageBox.question(
            self, "Borrar paciente de la lista",
            f"«{nombre_visible}» deja de aparecer en la lista.\n"
            "La carpeta y los archivos quedan intactos en el disco.\n\n"
            "Podés recuperarlo con «Ver todos».",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if confirmar != QMessageBox.StandardButton.Yes:
            return

        self.ocultos.add(self._clave_oculto(paciente))
        self._guardar_ocultos()
        self._listar_pacientes(self.directorio_raiz)
        self._estado(f"«{nombre_visible}» ya no aparece en la lista.")

    def restaurar_pacientes(self):
        if not self.ocultos or not self._confirmar_descartar():
            return
        confirmar = QMessageBox.question(
            self, "Ver todos los pacientes",
            f"Volver a mostrar {len(self.ocultos)} paciente(s) oculto(s)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if confirmar != QMessageBox.StandardButton.Yes:
            return
        self.ocultos.clear()
        self._guardar_ocultos()
        self._listar_pacientes(self.directorio_raiz)
        self._estado("Se restauró la lista completa de pacientes.")


# ==========================================================================
# ARRANQUE
# ==========================================================================
def main():
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NOMBRE)
    # Fusion respeta la hoja de estilos en todas las plataformas; el estilo
    # nativo de Windows ignora varias reglas y rompe el tema oscuro.
    app.setStyle("Fusion")
    app.setPalette(paleta_oscura())
    for familia in ("Segoe UI", "Inter", "DejaVu Sans"):
        if familia in QFont().families() or familia == "Segoe UI":
            app.setFont(QFont(familia, 10))
            break

    icono = ruta_recurso(ARCHIVO_ICONO)
    if not os.path.exists(icono):
        icono = ruta_recurso(ARCHIVO_LOGO)
    if os.path.exists(icono):
        app.setWindowIcon(QIcon(icono))

    splash = None
    logo = QPixmap(ruta_recurso(ARCHIVO_LOGO))
    if not logo.isNull():
        splash = QSplashScreen(logo.scaled(420, 420,
                                           Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))
        splash.show()
        app.processEvents()

    ventana = TatooApp()
    ventana.showMaximized()

    if splash:
        if SPLASH_MS > 0:
            QTimer.singleShot(SPLASH_MS, lambda: splash.finish(ventana))
        else:
            splash.finish(ventana)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()