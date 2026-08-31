/**
 * PolygonEditorView — интерактивная карта с полигонами границ НП.
 *
 * Функционал:
 *  - Выбор региона из списка загруженных в БД
 *  - Просмотр полигонов на карте (Leaflet)
 *  - Выбор полигона → информация + кнопки редактирования/сброса
 *  - Редактирование вершин полигона (перетаскивание маркеров)
 *  - Добавление вершин (клик по середине ребра)
 *  - Поддержка MultiPolygon (редактирование отдельных частей)
 *  - Рисование нового полигона на карте
 *  - Сохранение изменений в БД
 *  - Сброс к оригинальной версии из OSM
 */
import { useState, useEffect, useRef, useCallback } from 'react'
import L from 'leaflet'
import { api, ApiError } from '@/lib/api'
import { haptic, showAlert } from '@/lib/telegram'

// Leaflet CSS — импортируем здесь, чтобы не зависеть от глобальных стилей
import 'leaflet/dist/leaflet.css'

// ========================
// Типы
// ========================

interface PolygonRegion {
  region_code: string
  polygon_count: number
}

interface PolygonFeature {
  type: 'Feature'
  properties: {
    id: number
    osm_type: string
    osm_id: number
    name: string
    place_type: string | null
    is_edited: boolean
    edited_at: string | null
    edited_by: number | null
  }
  geometry: GeoJSON.Geometry
}

// Названия типов населённых пунктов для отображения
const PLACE_LABELS: Record<string, string> = {
  city: 'Город',
  town: 'Посёлок',
  village: 'Деревня',
  hamlet: 'Хутор',
  suburb: 'Пригород',
  locality: 'Населённый пункт',
}

function placeLabel(t: string | null): string {
  return (t && PLACE_LABELS[t]) || t || ''
}

// ========================
// Иконки маркеров
// ========================

const VERTEX_ICON = L.divIcon({
  className: '',
  html: '<div style="width:14px;height:14px;background:#2196f3;border:2px solid #fff;border-radius:50%;box-shadow:0 1px 3px rgba(0,0,0,.4);cursor:grab;"></div>',
  iconSize: [14, 14],
  iconAnchor: [7, 7],
})

const MIDPOINT_ICON = L.divIcon({
  className: 'vertex-midpoint-icon',
  html: '<div style="width:12px;height:12px;background:rgba(33,150,243,0.35);border:2px solid rgba(255,255,255,0.9);border-radius:50%;cursor:pointer;transition:transform .15s,background .15s;"></div>',
  iconSize: [12, 12],
  iconAnchor: [6, 6],
})

const DRAW_VERTEX_ICON = L.divIcon({
  className: '',
  html: '<div style="width:14px;height:14px;background:#4caf50;border:2px solid #fff;border-radius:50%;box-shadow:0 1px 3px rgba(0,0,0,.4);cursor:grab;"></div>',
  iconSize: [14, 14],
  iconAnchor: [7, 7],
})

const DRAW_CLOSE_ICON = L.divIcon({
  className: '',
  html: '<div style="width:18px;height:18px;background:#4caf50;border:3px solid #fff;border-radius:50%;box-shadow:0 0 0 2px rgba(76,175,80,.3);cursor:pointer;"></div>',
  iconSize: [18, 18],
  iconAnchor: [9, 9],
})

// ========================
// Компонент
// ========================

export function PolygonEditorView() {
  // --- Данные ---
  const [regions, setRegions] = useState<PolygonRegion[]>([])
  const [selectedRegion, setSelectedRegion] = useState<string>('')
  const [geojson, setGeojson] = useState<GeoJSON.FeatureCollection | null>(null)
  const [selectedFeature, setSelectedFeature] = useState<PolygonFeature | null>(null)

  // --- Состояния UI ---
  const [loading, setLoading] = useState(true)
  const [loadingMap, setLoadingMap] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  // --- Редактирование ---
  const [isEditing, setIsEditing] = useState(false)
  const [editCoords, setEditCoords] = useState<number[][][] | null>(null) // [ring][vertex][lon, lat]
  const [editGeomType, setEditGeomType] = useState<'Polygon' | 'MultiPolygon'>('Polygon')
  const [editPartIndex, setEditPartIndex] = useState(0)
  const isEditingRef = useRef(false)

  // --- Выбор части MultiPolygon в инфо-панели ---
  const [selectedPartIndex, setSelectedPartIndex] = useState(0)

  // --- Рисование нового полигона ---
  const [isDrawing, setIsDrawing] = useState(false)
  const [drawPoints, setDrawPoints] = useState<number[][]>([]) // [lon, lat][]
  const [showNewPolyForm, setShowNewPolyForm] = useState(false)
  const [newPolyName, setNewPolyName] = useState('')
  const [newPolyPlaceType, setNewPolyPlaceType] = useState('city')
  const isDrawingRef = useRef(false)

  // --- Рефы Leaflet ---
  const mapContainerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<L.Map | null>(null)
  const geojsonLayerRef = useRef<L.GeoJSON | null>(null)
  const editLayerGroupRef = useRef<L.LayerGroup | null>(null)
  const vertexMarkersRef = useRef<L.Marker[]>([])
  const midPointMarkersRef = useRef<L.Marker[]>([])
  const editPolygonRef = useRef<L.Polygon | null>(null)

  // --- Мутабельные рефы для редактирования ---
  const editCurrentCoordsRef = useRef<number[][]>([]) // [lon, lat][]
  const editOriginalGeomRef = useRef<GeoJSON.Geometry | null>(null)
  const editGeomTypeRef = useRef<'Polygon' | 'MultiPolygon'>('Polygon')
  const editPartIndexRef = useRef(0)

  // --- Мутабельные рефы для рисования ---
  const drawingLayerRef = useRef<L.LayerGroup | null>(null)
  const drawingPolylineRef = useRef<L.Polyline | null>(null)
  const drawingFillRef = useRef<L.Polygon | null>(null)
  const drawingMarkersRef = useRef<L.Marker[]>([])
  const drawLatLngsRef = useRef<L.LatLng[]>([])
  const mapClickHandlerRef = useRef<((e: L.LeafletMouseEvent) => void) | null>(null)

  // Функция-реф для вставки вершины (чтобы избежать stale closures)
  const insertVertexRef = useRef<(index: number, coord: number[]) => void>(() => {})

  // Синхронизируем refs с state
  useEffect(() => { isEditingRef.current = isEditing }, [isEditing])
  useEffect(() => { isDrawingRef.current = isDrawing }, [isDrawing])

  // ========================
  // Исправление Tailwind preflight для Leaflet + стили midpoint
  // ========================

  useEffect(() => {
    const id = 'leaflet-tailwind-fix'
    if (document.getElementById(id)) return
    const style = document.createElement('style')
    style.id = id
    style.textContent = [
      '.leaflet-container img{max-width:none!important;max-height:none!important}',
      '.leaflet-container .leaflet-tile-pane img{width:256px;height:256px}',
      '.vertex-midpoint-icon div:hover{transform:scale(1.5)!important;background:rgba(33,150,243,.9)!important}',
    ].join('')
    document.head.appendChild(style)
  }, [])

  // ========================
  // Инициализация карты
  // ========================

  useEffect(() => {
    if (loading) return
    const el = mapContainerRef.current
    if (!el || mapRef.current) return

    if (el.getBoundingClientRect().width === 0 || el.getBoundingClientRect().height === 0) {
      const ro = new ResizeObserver((entries) => {
        const r = entries[0].contentRect
        if (r.width > 0 && r.height > 0) {
          ro.disconnect()
          initMap(el)
        }
      })
      ro.observe(el)
      return () => ro.disconnect()
    }

    initMap(el)

    function initMap(container: HTMLDivElement) {
      const map = L.map(container, {
        zoomControl: true,
        attributionControl: false,
        preferCanvas: true,
      }).setView([55.75, 37.62], 7)

      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19,
      }).addTo(map)

      mapRef.current = map

      const ro2 = new ResizeObserver(() => map.invalidateSize())
      ro2.observe(container)
      const timer = setTimeout(() => map.invalidateSize(), 300)

      geojsonLayerRef.current = L.geoJSON(undefined, {
        style: (f) => featureStyle(f!),
        onEachFeature: (feature, layer) => {
          layer.on('click', () => {
            if (isEditingRef.current || isDrawingRef.current) return
            const props = feature.properties as PolygonFeature['properties']
            setSelectedFeature({
              type: 'Feature',
              properties: props,
              geometry: feature.geometry,
            })
            setSelectedPartIndex(0)
            haptic('light')
            const pathLayer = layer as unknown as L.Polyline
            if (pathLayer.getBounds) {
              map.fitBounds(pathLayer.getBounds(), { padding: [30, 30], maxZoom: 15 })
            }
          })
        },
      }).addTo(map)

      editLayerGroupRef.current = L.layerGroup().addTo(map)
      drawingLayerRef.current = L.layerGroup().addTo(map)

      return () => {
        clearTimeout(timer)
        ro2.disconnect()
        map.remove()
        mapRef.current = null
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading])

  // ========================
  // Загрузка регионов
  // ========================

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api.polygonListRegions()
      .then(data => {
        if (!cancelled) {
          setRegions(data)
          if (data.length === 1) {
            setSelectedRegion(data[0].region_code)
          }
        }
      })
      .catch(err => {
        if (!cancelled) setError(err instanceof ApiError ? err.detail : 'Ошибка загрузки')
      })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  // ========================
  // Загрузка GeoJSON региона
  // ========================

  useEffect(() => {
    if (!selectedRegion) {
      setGeojson(null)
      return
    }
    let cancelled = false
    setLoadingMap(true)
    setError('')
    setSelectedFeature(null)
    setSelectedPartIndex(0)
    cancelEdit()
    cancelDrawing()

    api.polygonGetGeojson(selectedRegion, true)
      .then(data => {
        if (!cancelled) {
          setGeojson(data)
          geojsonLayerRef.current?.clearLayers()
          geojsonLayerRef.current?.addData(data)
          if (data.features.length > 0 && mapRef.current) {
            const bounds = geojsonLayerRef.current!.getBounds()
            mapRef.current.fitBounds(bounds, { padding: [10, 10] })
          }
        }
      })
      .catch(err => {
        if (!cancelled) setError(err instanceof ApiError ? err.detail : 'Ошибка загрузки полигонов')
      })
      .finally(() => { if (!cancelled) setLoadingMap(false) })
    return () => { cancelled = true }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedRegion])

  // ========================
  // Зум к выбранной части MultiPolygon
  // ========================

  useEffect(() => {
    if (!selectedFeature || !mapRef.current) return
    const geom = selectedFeature.geometry
    if (geom.type !== 'MultiPolygon') return
    const mp = geom as GeoJSON.MultiPolygon
    const part = mp.coordinates[selectedPartIndex]
    if (!part || !part[0]) return
    const bounds = L.latLngBounds(part[0].map(c => [c[1], c[0]] as L.LatLngTuple))
    mapRef.current.fitBounds(bounds, { padding: [30, 30], maxZoom: 15 })
  }, [selectedPartIndex, selectedFeature])

  // ============================================================
  // РЕДАКТИРОВАНИЕ ПОЛИГОНА
  // ============================================================

  // --- Обновить позиции midpoint-маркеров ---
  function updateMidPointPositions() {
    const ring = editCurrentCoordsRef.current
    const mids = midPointMarkersRef.current
    // Для N вершин (замкнутое кольцо: первая == последняя) — N-1 рёбер
    for (let i = 0; i < mids.length; i++) {
      if (i < ring.length - 1) {
        const mid: [number, number] = [
          (ring[i][0] + ring[i + 1][0]) / 2,
          (ring[i][1] + ring[i + 1][1]) / 2,
        ]
        mids[i].setLatLng([mid[1], mid[0]])
      }
    }
  }

  // --- Создать midpoint-маркеры на рёбрах ---
  function createMidPointMarkers() {
    const ring = editCurrentCoordsRef.current
    const layerGroup = editLayerGroupRef.current
    if (!layerGroup || ring.length < 2) return

    // Количество рёбер = length - 1 (кольцо замкнуто: ring[last] === ring[0])
    const edgeCount = ring.length - 1
    for (let i = 0; i < edgeCount; i++) {
      const mid: [number, number] = [
        (ring[i][0] + ring[i + 1][0]) / 2,
        (ring[i][1] + ring[i + 1][1]) / 2,
      ]
      const marker = L.marker([mid[1], mid[0]], { icon: MIDPOINT_ICON })
      marker.on('click', () => {
        insertVertexRef.current(i + 1, [mid[0], mid[1]])
      })
      layerGroup.addLayer(marker)
      midPointMarkersRef.current.push(marker)
    }
  }

  // --- Полная пересборка слоя редактирования ---
  function rebuildEditLayer() {
    const layerGroup = editLayerGroupRef.current
    if (!layerGroup) return

    layerGroup.clearLayers()
    vertexMarkersRef.current = []
    midPointMarkersRef.current = []
    editPolygonRef.current = null

    const ring = editCurrentCoordsRef.current
    if (ring.length < 2) return

    // Создаём редактируемый полигон
    const latLngs: L.LatLngTuple[] = ring.map(c => [c[1], c[0]])
    const polygon = L.polygon(latLngs, {
      color: '#ff9800',
      weight: 3,
      fillColor: '#ff9800',
      fillOpacity: 0.2,
    })
    editPolygonRef.current = polygon
    layerGroup.addLayer(polygon)

    // Создаём вершинные маркеры
    const vMarkers: L.Marker[] = []
    ring.forEach((c, i) => {
      const marker = L.marker([c[1], c[0]], {
        icon: VERTEX_ICON,
        draggable: true,
        autoPan: true,
      })

      marker.on('drag', () => {
        const pos = marker.getLatLng()
        editCurrentCoordsRef.current[i] = [pos.lng, pos.lat]
        const newLatLngs = editCurrentCoordsRef.current.map(cc => [cc[1], cc[0]] as L.LatLngTuple)
        polygon.setLatLngs(newLatLngs)
        updateMidPointPositions()
      })

      marker.on('dragstart', () => {
        ;(marker.getElement()?.firstChild as HTMLElement)?.style.setProperty('cursor', 'grabbing')
      })

      marker.on('dragend', () => {
        ;(marker.getElement()?.firstChild as HTMLElement)?.style.setProperty('cursor', 'grab')
        // Обновляем замыкающую вершину (последняя == первая)
        editCurrentCoordsRef.current[editCurrentCoordsRef.current.length - 1] = [
          ...editCurrentCoordsRef.current[0],
        ]
        const newLatLngs = editCurrentCoordsRef.current.map(cc => [cc[1], cc[0]] as L.LatLngTuple)
        polygon.setLatLngs(newLatLngs)
        updateMidPointPositions()
        setEditCoords([editCurrentCoordsRef.current.map(cc => [...cc])])
      })

      layerGroup.addLayer(marker)
      vMarkers.push(marker)
    })

    vertexMarkersRef.current = vMarkers

    // Создаём midpoint-маркеры на рёбрах
    createMidPointMarkers()

    // Обновляем React-стейт
    setEditCoords([ring.map(c => [...c])])
  }

  // --- Вставить вершину в указанную позицию ---
  insertVertexRef.current = (index: number, coord: number[]) => {
    const ring = editCurrentCoordsRef.current
    ring.splice(index, 0, [coord[0], coord[1]])
    // Обновляем замыкающую вершину
    ring[ring.length - 1] = [...ring[0]]
    haptic('light')
    rebuildEditLayer()
  }

  // --- Войти в режим редактирования ---
  const enterEditMode = useCallback((partIndex?: number) => {
    if (!selectedFeature || !mapRef.current) return
    const geom = selectedFeature.geometry
    const pi = partIndex ?? 0

    let ringCoords: number[][]
    if (geom.type === 'Polygon') {
      editGeomTypeRef.current = 'Polygon'
      editPartIndexRef.current = 0
      editOriginalGeomRef.current = geom
      ringCoords = geom.coordinates[0]
    } else if (geom.type === 'MultiPolygon') {
      editGeomTypeRef.current = 'MultiPolygon'
      editPartIndexRef.current = pi
      editOriginalGeomRef.current = geom
      ringCoords = geom.coordinates[pi][0]
    } else {
      showAlert('Неподдерживаемый тип геометрии')
      return
    }

    // Глубокая копия для редактирования
    editCurrentCoordsRef.current = ringCoords.map(c => [...c])

    setEditGeomType(editGeomTypeRef.current)
    setEditPartIndex(pi)
    setIsEditing(true)

    // Скрываем оригинальный полигон из geojsonLayer (не удаляя его)
    // Чтобы он не перекрывался с редактируемым
    geojsonLayerRef.current?.eachLayer((layer) => {
      const f = (layer as any).feature as GeoJSON.Feature | undefined
      if (!f || !f.properties) return
      if ((f.properties as any).id === selectedFeature.properties.id) {
        ;(layer as L.Path).setStyle({ fillOpacity: 0, opacity: 0, weight: 0 })
      }
    })

    rebuildEditLayer()
    haptic('medium')
  }, [selectedFeature])

  // --- Выйти из режима редактирования ---
  const cancelEdit = useCallback(() => {
    // Восстанавливаем стиль оригинального полигона
    if (selectedFeature && geojsonLayerRef.current) {
      geojsonLayerRef.current.eachLayer((layer) => {
        const f = (layer as any).feature as GeoJSON.Feature | undefined
        if (!f || !f.properties) return
        if ((f.properties as any).id === selectedFeature.properties.id) {
          const style = featureStyle(f)
          ;(layer as L.Path).setStyle(style)
        }
      })
    }
    editLayerGroupRef.current?.clearLayers()
    vertexMarkersRef.current = []
    midPointMarkersRef.current = []
    editPolygonRef.current = null
    editCurrentCoordsRef.current = []
    editOriginalGeomRef.current = null
    setEditCoords(null)
    setIsEditing(false)
  }, [selectedFeature])

  // --- Сохранить отредактированный полигон ---
  const saveEdit = useCallback(async () => {
    if (!selectedFeature || !editCoords) return
    setSaving(true)
    try {
      let geometry: GeoJSON.Geometry
      if (editGeomTypeRef.current === 'Polygon') {
        geometry = { type: 'Polygon', coordinates: editCoords }
      } else {
        // MultiPolygon: заменяем отредактированную часть
        const orig = editOriginalGeomRef.current as GeoJSON.MultiPolygon
        const newCoords = orig.coordinates.map((part, i) =>
          i === editPartIndexRef.current ? editCoords : part
        )
        geometry = { type: 'MultiPolygon', coordinates: newCoords }
      }
      await api.polygonUpdateGeometry(selectedFeature.properties.id, geometry)
      haptic('success')
      cancelEdit()
      setSelectedFeature(null)
      if (selectedRegion) {
        const data = await api.polygonGetGeojson(selectedRegion, true)
        setGeojson(data)
        geojsonLayerRef.current?.clearLayers()
        geojsonLayerRef.current?.addData(data)
      }
    } catch (err) {
      haptic('error')
      showAlert(err instanceof ApiError ? err.detail : 'Ошибка сохранения')
    } finally {
      setSaving(false)
    }
  }, [selectedFeature, editCoords, selectedRegion, cancelEdit])

  // --- Сбросить полигон к оригиналу ---
  const resetPolygon = useCallback(async () => {
    if (!selectedFeature || !selectedRegion) return
    setSaving(true)
    try {
      await api.polygonReset(selectedRegion, selectedFeature.properties.id)
      haptic('success')
      setSelectedFeature(null)
      const data = await api.polygonGetGeojson(selectedRegion, true)
      setGeojson(data)
      geojsonLayerRef.current?.clearLayers()
      geojsonLayerRef.current?.addData(data)
    } catch (err) {
      haptic('error')
      showAlert(err instanceof ApiError ? err.detail : 'Ошибка сброса')
    } finally {
      setSaving(false)
    }
  }, [selectedFeature, selectedRegion])

  // ============================================================
  // РИСОВАНИЕ НОВОГО ПОЛИГОНА
  // ============================================================

  // --- Обновить визуализацию рисования (линия + заливка) ---
  function updateDrawingVisuals() {
    const pts = drawLatLngsRef.current
    const layerGroup = drawingLayerRef.current
    if (!layerGroup || pts.length === 0) return

    // Основная линия
    const latLngs: L.LatLngTuple[] = pts.map(p => [p.lat, p.lng])
    if (drawingPolylineRef.current) {
      drawingPolylineRef.current.setLatLngs(latLngs)
    } else {
      drawingPolylineRef.current = L.polyline(latLngs, {
        color: '#4caf50', weight: 2, dashArray: '5,5',
      })
      layerGroup.addLayer(drawingPolylineRef.current)
    }

    // Заливка-превью (после 3+ точек)
    if (pts.length >= 3) {
      if (drawingFillRef.current) {
        drawingFillRef.current.setLatLngs(latLngs)
      } else {
        drawingFillRef.current = L.polygon(latLngs, {
          color: '#4caf50', weight: 2, fillColor: '#4caf50', fillOpacity: 0.1,
        })
        layerGroup.addLayer(drawingFillRef.current)
        // Линию помещаем поверх заливки
        drawingPolylineRef.current?.bringToFront()
      }
    } else if (drawingFillRef.current) {
      layerGroup.removeLayer(drawingFillRef.current)
      drawingFillRef.current = null
    }
  }

  // --- Добавить точку при рисовании ---
  function addDrawPoint(latlng: L.LatLng) {
    const pts = drawLatLngsRef.current
    const layerGroup = drawingLayerRef.current
    if (!layerGroup) return

    pts.push(latlng)

    if (pts.length === 1) {
      // Первая точка — маркер-закрыватель
      const marker = L.marker(latlng, { icon: DRAW_CLOSE_ICON, draggable: true, zIndexOffset: 1000 })
      marker.on('click', () => {
        if (drawLatLngsRef.current.length >= 3) {
          closeDrawPolygon()
        } else {
          showAlert('Нужно минимум 3 точки для замыкания полигона')
        }
      })
      marker.on('drag', () => {
        drawLatLngsRef.current[0] = marker.getLatLng()
        updateDrawingVisuals()
      })
      layerGroup.addLayer(marker)
      drawingMarkersRef.current.push(marker)
    } else {
      // Обычная точка
      const marker = L.marker(latlng, { icon: DRAW_VERTEX_ICON, draggable: true })
      const idx = pts.length - 1
      marker.on('drag', () => {
        drawLatLngsRef.current[idx] = marker.getLatLng()
        updateDrawingVisuals()
      })
      layerGroup.addLayer(marker)
      drawingMarkersRef.current.push(marker)
    }

    // Первый маркер имеет повышенный z-index (задан через iconSize + zIndexOffset в DRAW_CLOSE_ICON)
    // Для маркеров Leaflet нет bringToFront — используем zIndexOffset при создании

    updateDrawingVisuals()
    setDrawPoints(pts.map(p => [p.lng, p.lat]))
  }

  // --- Начать рисование ---
  const startDrawing = useCallback(() => {
    if (isEditing) cancelEdit()
    const map = mapRef.current
    if (!map) return

    isDrawingRef.current = true
    setIsDrawing(true)
    setSelectedFeature(null)
    setShowNewPolyForm(false)
    setNewPolyName('')
    setNewPolyPlaceType('city')

    drawingLayerRef.current?.clearLayers()
    drawingPolylineRef.current = null
    drawingFillRef.current = null
    drawingMarkersRef.current = []
    drawLatLngsRef.current = []

    map.doubleClickZoom.disable()

    const handler = (e: L.LeafletMouseEvent) => {
      if (!isDrawingRef.current) return
      addDrawPoint(e.latlng)
    }
    mapClickHandlerRef.current = handler
    map.on('click', handler)

    haptic('light')
  }, [isEditing, cancelEdit])

  // --- Замкнуть полигон ---
  function closeDrawPolygon() {
    isDrawingRef.current = false
    const map = mapRef.current
    if (mapClickHandlerRef.current && map) {
      map.off('click', mapClickHandlerRef.current)
      mapClickHandlerRef.current = null
    }
    map?.doubleClickZoom.enable()

    // Убираем маркеры, оставляем только заливку как превью
    drawingMarkersRef.current.forEach(m => drawingLayerRef.current?.removeLayer(m))
    drawingMarkersRef.current = []
    drawingPolylineRef.current = null

    setIsDrawing(false)
    setShowNewPolyForm(true)
    haptic('medium')
  }

  // --- Отменить рисование ---
  const cancelDrawing = useCallback(() => {
    isDrawingRef.current = false
    const map = mapRef.current
    if (mapClickHandlerRef.current && map) {
      map.off('click', mapClickHandlerRef.current)
      mapClickHandlerRef.current = null
    }
    map?.doubleClickZoom.enable()

    drawingLayerRef.current?.clearLayers()
    drawingPolylineRef.current = null
    drawingFillRef.current = null
    drawingMarkersRef.current = []
    drawLatLngsRef.current = []

    setIsDrawing(false)
    setDrawPoints([])
    setShowNewPolyForm(false)
    setNewPolyName('')
    setNewPolyPlaceType('city')
  }, [])

  // --- Сохранить новый полигон ---
  const saveNewPolygon = useCallback(async () => {
    if (!selectedRegion || drawLatLngsRef.current.length < 3) {
      showAlert('Нужно минимум 3 точки')
      return
    }
    const name = newPolyName.trim() || 'Без названия'
    const coordinates: number[][][] = [
      // Замыкаем кольцо
      [...drawLatLngsRef.current.map(p => [p.lng, p.lat]),
        [drawLatLngsRef.current[0].lng, drawLatLngsRef.current[0].lat]],
    ]
    const geometry: GeoJSON.Polygon = {
      type: 'Polygon',
      coordinates,
    }

    setSaving(true)
    try {
      await api.polygonCreate(selectedRegion, geometry, name, newPolyPlaceType)
      haptic('success')
      cancelDrawing()
      // Обновляем карту
      const data = await api.polygonGetGeojson(selectedRegion, true)
      setGeojson(data)
      geojsonLayerRef.current?.clearLayers()
      geojsonLayerRef.current?.addData(data)
    } catch (err) {
      haptic('error')
      showAlert(err instanceof ApiError ? err.detail : 'Ошибка создания полигона')
    } finally {
      setSaving(false)
    }
  }, [selectedRegion, newPolyName, newPolyPlaceType, cancelDrawing])

  // ========================
  // Render
  // ========================

  if (loading) {
    return <div className="tg-card text-center py-8"><span className="text-xs opacity-50">Загрузка...</span></div>
  }

  if (error && !geojson) {
    return (
      <div className="tg-card">
        <div className="text-sm" style={{ color: 'var(--tg-color-destructive, #ff3b30)' }}>{error}</div>
      </div>
    )
  }

  return (
    <div className="space-y-3">
      {/* Заголовок */}
      <div className="tg-card">
        <h2 className="text-sm font-semibold mb-2">Редактор карты</h2>
        <p className="text-xs opacity-60">
          Просмотр и редактирование границ населённых пунктов из OSM.
          Нажмите на полигон для просмотра информации и редактирования.
        </p>
      </div>

      {/* Селектор региона */}
      {regions.length > 1 && (
        <div className="tg-card">
          <label className="text-xs font-medium mb-1.5 block opacity-70">Регион</label>
          <select
            className="w-full rounded-lg px-3 py-2 text-sm"
            style={{
              backgroundColor: 'var(--tg-color-bg, #fff)',
              color: 'var(--tg-color-text, #000)',
              border: '1px solid var(--tg-color-hint, #999)',
            }}
            value={selectedRegion}
            onChange={e => setSelectedRegion(e.target.value)}
          >
            <option value="">— Выберите регион —</option>
            {regions.map(r => (
              <option key={r.region_code} value={r.region_code}>
                {r.region_code} ({r.polygon_count} полигонов)
              </option>
            ))}
          </select>
        </div>
      )}

      {/* Тулбар: добавить полигон */}
      {!isEditing && !isDrawing && !showNewPolyForm && selectedRegion && (
        <div className="flex gap-2">
          <button
            className="flex-1 py-2 rounded-lg text-sm font-medium"
            style={{
              backgroundColor: 'rgba(76,175,80,0.12)',
              color: '#4caf50',
              border: '1px solid rgba(76,175,80,0.3)',
            }}
            onClick={startDrawing}
          >
            + Добавить полигон
          </button>
        </div>
      )}

      {/* Тулбар рисования */}
      {isDrawing && (
        <div className="tg-card flex items-center gap-2">
          <span className="text-xs flex-1" style={{ color: '#4caf50' }}>
            {drawPoints.length === 0
              ? 'Нажмите на карту для добавления первой вершины'
              : drawPoints.length < 3
                ? `${drawPoints.length} вершин. Ещё ${3 - drawPoints.length} до минимума.`
                : `${drawPoints.length} вершин. Нажмите зелёный маркер для замыкания.`
            }
          </span>
          <button
            className="px-3 py-1.5 rounded-lg text-xs font-medium"
            style={{
              backgroundColor: 'var(--tg-color-section-bg, #fff)',
              border: '1px solid var(--tg-color-hint, #999)',
            }}
            onClick={cancelDrawing}
          >
            Отмена
          </button>
        </div>
      )}

      {/* Карта */}
      <div className="rounded-xl overflow-hidden relative" style={{ border: '1px solid var(--tg-color-hint, #999)' }}>
        <div
          ref={mapContainerRef}
          style={{
            height: isDrawing ? '60vh' : '50vh',
            minHeight: 300,
            width: '100%',
            cursor: isDrawing ? 'crosshair' : undefined,
          }}
        />
        {loadingMap && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/20">
            <span className="text-xs text-white">Загрузка полигонов...</span>
          </div>
        )}
      </div>

      {/* Форма нового полигона */}
      {showNewPolyForm && (
        <div className="tg-card space-y-3">
          <h3 className="text-sm font-semibold" style={{ color: '#4caf50' }}>
            Новый полигон
          </h3>
          <div>
            <label className="text-xs font-medium mb-1 block opacity-70">Название</label>
            <input
              type="text"
              className="w-full rounded-lg px-3 py-2 text-sm"
              style={{
                backgroundColor: 'var(--tg-color-bg, #fff)',
                color: 'var(--tg-color-text, #000)',
                border: '1px solid var(--tg-color-hint, #999)',
              }}
              placeholder="Название населённого пункта"
              value={newPolyName}
              onChange={e => setNewPolyName(e.target.value)}
            />
          </div>
          <div>
            <label className="text-xs font-medium mb-1 block opacity-70">Тип</label>
            <select
              className="w-full rounded-lg px-3 py-2 text-sm"
              style={{
                backgroundColor: 'var(--tg-color-bg, #fff)',
                color: 'var(--tg-color-text, #000)',
                border: '1px solid var(--tg-color-hint, #999)',
              }}
              value={newPolyPlaceType}
              onChange={e => setNewPolyPlaceType(e.target.value)}
            >
              {Object.entries(PLACE_LABELS).map(([k, v]) => (
                <option key={k} value={k}>{v}</option>
              ))}
            </select>
          </div>
          <div className="text-xs opacity-50">
            {drawPoints.length} вершин
          </div>
          <div className="flex gap-2">
            <button
              className="flex-1 py-2 rounded-lg text-sm font-medium text-white"
              style={{ backgroundColor: '#4caf50' }}
              disabled={saving || drawPoints.length < 3}
              onClick={saveNewPolygon}
            >
              {saving ? 'Сохранение...' : 'Сохранить'}
            </button>
            <button
              className="flex-1 py-2 rounded-lg text-sm font-medium"
              style={{
                backgroundColor: 'var(--tg-color-section-bg, #fff)',
                border: '1px solid var(--tg-color-hint, #999)',
              }}
              onClick={cancelDrawing}
            >
              Отмена
            </button>
          </div>
        </div>
      )}

      {/* Информация о выбранном полигоне */}
      {selectedFeature && !isEditing && !isDrawing && !showNewPolyForm && (
        <PolygonInfoPanel
          feature={selectedFeature}
          selectedPartIndex={selectedPartIndex}
          onPartChange={setSelectedPartIndex}
          onEdit={enterEditMode}
          onReset={resetPolygon}
          saving={saving}
        />
      )}

      {/* Панель редактирования */}
      {isEditing && editCoords && (
        <div className="tg-card space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold" style={{ color: '#ff9800' }}>
              Редактирование: {selectedFeature?.properties.name || 'Без названия'}
            </h3>
            <span className="text-xs opacity-50">
              {editCoords[0].length - 1} вершин
              {editGeomType === 'MultiPolygon' && ` · Часть ${editPartIndex + 1}`}
            </span>
          </div>
          <p className="text-xs opacity-60">
            Перетащите синие маркеры для изменения границ.
            Нажмите на полупрозрачный маркер на ребре для добавления вершины.
          </p>
          <div className="flex gap-2">
            <button
              className="flex-1 py-2 rounded-lg text-sm font-medium text-white"
              style={{ backgroundColor: 'var(--tg-color-button, #2481cc)' }}
              disabled={saving}
              onClick={saveEdit}
            >
              {saving ? 'Сохранение...' : 'Сохранить'}
            </button>
            <button
              className="flex-1 py-2 rounded-lg text-sm font-medium"
              style={{
                backgroundColor: 'var(--tg-color-section-bg, #fff)',
                border: '1px solid var(--tg-color-hint, #999)',
              }}
              onClick={cancelEdit}
            >
              Отмена
            </button>
          </div>
        </div>
      )}

      {/* Счётчик */}
      {geojson && (
        <div className="text-center text-xs opacity-40">
          {geojson.features.length} полигонов
          {selectedFeature && !isEditing && (
            <> · Выбран: <b>{selectedFeature.properties.name || 'Без названия'}</b></>
          )}
        </div>
      )}
    </div>
  )
}

// ========================
// Подкомпоненты
// ========================

function featureStyle(feature: GeoJSON.Feature): L.PathOptions {
  const props = feature.properties as PolygonFeature['properties']
  return {
    color: props.is_edited ? '#ff9800' : '#2196f3',
    weight: 1.5,
    fillColor: props.is_edited ? '#ff9800' : '#2196f3',
    fillOpacity: 0.1,
  }
}

interface InfoPanelProps {
  feature: PolygonFeature
  selectedPartIndex: number
  onPartChange: (i: number) => void
  onEdit: (partIndex?: number) => void
  onReset: () => void
  saving: boolean
}

function PolygonInfoPanel({ feature, selectedPartIndex, onPartChange, onEdit, onReset, saving }: InfoPanelProps) {
  const p = feature.properties
  const geom = feature.geometry
  const isMulti = geom.type === 'MultiPolygon'
  const canEdit = geom.type === 'Polygon' || isMulti

  let coordCount = 0
  let partCount = 1
  if (geom.type === 'Polygon') {
    coordCount = geom.coordinates[0].length
  } else if (geom.type === 'MultiPolygon') {
    partCount = geom.coordinates.length
    coordCount = geom.coordinates.reduce((s, part) => s + part[0].length, 0)
  }

  return (
    <div className="tg-card space-y-2">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold truncate">
            {p.name || 'Без названия'}
          </h3>
          <div className="text-xs opacity-60 mt-0.5">
            {placeLabel(p.place_type) && <span>{placeLabel(p.place_type)} · </span>}
            {p.osm_type}/{p.osm_id}
            {isMulti ? ` · MultiPolygon (${partCount} ${partCount === 1 ? 'часть' : partCount < 5 ? 'части' : 'частей'})` : ''}
            {' · '}{coordCount} вершин
          </div>
        </div>
        {p.is_edited && (
          <span
            className="shrink-0 text-xs px-2 py-0.5 rounded-full"
            style={{ backgroundColor: 'rgba(255,152,0,0.15)', color: '#ff9800' }}
          >
            Изменён
          </span>
        )}
      </div>

      {/* Селектор части для MultiPolygon */}
      {isMulti && partCount > 1 && (
        <div>
          <label className="text-xs font-medium mb-1 block opacity-70">Часть для редактирования</label>
          <select
            className="w-full rounded-lg px-3 py-2 text-sm"
            style={{
              backgroundColor: 'var(--tg-color-bg, #fff)',
              color: 'var(--tg-color-text, #000)',
              border: '1px solid var(--tg-color-hint, #999)',
            }}
            value={selectedPartIndex}
            onChange={e => onPartChange(Number(e.target.value))}
          >
            {(geom as GeoJSON.MultiPolygon).coordinates.map((part, i) => (
              <option key={i} value={i}>
                Часть {i + 1} ({part[0].length} вершин)
              </option>
            ))}
          </select>
        </div>
      )}

      {p.is_edited && p.edited_at && (
        <div className="text-xs opacity-50">
          Отредактирован: {new Date(p.edited_at).toLocaleString('ru-RU')}
        </div>
      )}

      <div className="flex gap-2 pt-1">
        {canEdit && (
          <button
            className="flex-1 py-2 rounded-lg text-sm font-medium text-white"
            style={{ backgroundColor: 'var(--tg-color-button, #2481cc)' }}
            onClick={() => onEdit(selectedPartIndex)}
          >
            Редактировать
          </button>
        )}
        {p.is_edited && (
          <button
            className="flex-1 py-2 rounded-lg text-sm font-medium"
            style={{
              backgroundColor: 'var(--tg-color-section-bg, #fff)',
              border: '1px solid var(--tg-color-hint, #999)',
            }}
            disabled={saving}
            onClick={onReset}
          >
            Сбросить
          </button>
        )}
      </div>
    </div>
  )
}
