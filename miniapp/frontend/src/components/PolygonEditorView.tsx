/**
 * PolygonEditorView — интерактивная карта с полигонами границ НП.
 *
 * Функционал:
 *  - Выбор региона из списка загруженных в БД
 *  - Просмотр полигонов на карте (Leaflet)
 *  - Выбор полигона → информация + кнопки редактирования/сброса
 *  - Редактирование вершин полигона (перетаскивание маркеров)
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
  const isEditingRef = useRef(false)

  // Синхронизируем ref с state для использования в замыканиях Leaflet
  useEffect(() => { isEditingRef.current = isEditing }, [isEditing])

  // --- Рефы Leaflet ---
  const mapContainerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<L.Map | null>(null)
  const geojsonLayerRef = useRef<L.GeoJSON | null>(null)
  const editLayerGroupRef = useRef<L.LayerGroup | null>(null)
  const vertexMarkersRef = useRef<L.Marker[]>([])
  const editPolygonRef = useRef<L.Polygon | null>(null)

  // ========================
  // Инициализация карты
  // ========================

  useEffect(() => {
    if (!mapContainerRef.current || mapRef.current) return

    const map = L.map(mapContainerRef.current, {
      zoomControl: true,
      attributionControl: false,
      preferCanvas: true, // Canvas для быстрого рендера тысяч полигонов
    }).setView([55.75, 37.62], 7)

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
    }).addTo(map)

    mapRef.current = map

    // Leaflet не всегда корректно определяет размеры контейнера
    // при lazy-loading / переключении вкладок — принудительно обновляем
    requestAnimationFrame(() => {
      map.invalidateSize()
    })

    // Слой для GeoJSON полигонов
    geojsonLayerRef.current = L.geoJSON(undefined, {
      style: (f) => featureStyle(f!),
      onEachFeature: (feature, layer) => {
        layer.on('click', () => {
          if (isEditingRef.current) return // Не переключаем полигон при редактировании
          const props = feature.properties as PolygonFeature['properties']
          setSelectedFeature({
            type: 'Feature',
            properties: props,
            geometry: feature.geometry,
          })
          haptic('light')
          // Зум к полигону
          const pathLayer = layer as unknown as L.Polyline
          if (pathLayer.getBounds) {
            map.fitBounds(pathLayer.getBounds(), { padding: [30, 30], maxZoom: 15 })
          }
        })
      },
    }).addTo(map)

    // Слой для редактирования (поверх GeoJSON)
    editLayerGroupRef.current = L.layerGroup().addTo(map)

    return () => {
      map.remove()
      mapRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

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
    cancelEdit()

    api.polygonGetGeojson(selectedRegion, true) // simplified для обзора
      .then(data => {
        if (!cancelled) {
          setGeojson(data)
          geojsonLayerRef.current?.clearLayers()
          geojsonLayerRef.current?.addData(data)
          // Зум к области региона
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
  // Редактирование
  // ========================

  const startEdit = useCallback(() => {
    if (!selectedFeature || !mapRef.current) return
    const geom = selectedFeature.geometry
    if (geom.type !== 'Polygon') {
      showAlert('Редактирование MultiPolygon пока не поддерживается')
      return
    }

    const coords = (geom as GeoJSON.Polygon).coordinates
    // GeoJSON: [lon, lat] → Leaflet: [lat, lon]
    const latLngs: L.LatLngTuple[] = coords[0].map(c => [c[1], c[0]] as L.LatLngTuple)

    // Создаём редактируемый полигон
    const polygon = L.polygon(latLngs, {
      color: '#ff9800',
      weight: 3,
      fillColor: '#ff9800',
      fillOpacity: 0.2,
    })

    editPolygonRef.current = polygon
    editLayerGroupRef.current?.addLayer(polygon)

    // Создаём вершинные маркеры
    const markers: L.Marker[] = []
    const vertexIcon = L.divIcon({
      className: '',
      html: '<div style="width:14px;height:14px;background:#2196f3;border:2px solid #fff;border-radius:50%;box-shadow:0 1px 3px rgba(0,0,0,.4);cursor:grab;"></div>',
      iconSize: [14, 14],
      iconAnchor: [7, 7],
    })

    const currentCoords = coords[0].map(c => [...c] as number[]) // Копия

    coords[0].forEach((c, i) => {
      const marker = L.marker([c[1], c[0]], {
        icon: vertexIcon,
        draggable: true,
        autoPan: true,
      })

      marker.on('drag', () => {
        const pos = marker.getLatLng()
        currentCoords[i] = [pos.lng, pos.lat] // Обновляем [lon, lat]
        // Обновляем полигон
        const newLatLngs: L.LatLngTuple[] = currentCoords.map(cc => [cc[1], cc[0]] as L.LatLngTuple)
        polygon.setLatLngs(newLatLngs)
        setEditCoords([currentCoords.map(cc => [...cc])]) // Копия для React state
      })

      marker.on('dragstart', () => {
        ;(marker.getElement()?.firstChild as HTMLElement)?.style.setProperty('cursor', 'grabbing')
      })

      marker.on('dragend', () => {
        ;(marker.getElement()?.firstChild as HTMLElement)?.style.setProperty('cursor', 'grab')
        setEditCoords([currentCoords.map(cc => [...cc])])
      })

      editLayerGroupRef.current?.addLayer(marker)
      markers.push(marker)
    })

    vertexMarkersRef.current = markers
    setEditCoords([coords[0].map(c => [...c])])
    setIsEditing(true)
    haptic('medium')
  }, [selectedFeature])

  const cancelEdit = useCallback(() => {
    editLayerGroupRef.current?.clearLayers()
    vertexMarkersRef.current = []
    editPolygonRef.current = null
    setEditCoords(null)
    setIsEditing(false)
  }, [])

  const saveEdit = useCallback(async () => {
    if (!selectedFeature || !editCoords) return
    setSaving(true)
    try {
      const geometry: GeoJSON.Polygon = {
        type: 'Polygon',
        coordinates: editCoords,
      }
      await api.polygonUpdateGeometry(selectedFeature.properties.id, geometry)
      haptic('success')
      cancelEdit()
      // Обновляем данные региона
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

  const resetPolygon = useCallback(async () => {
    if (!selectedFeature || !selectedRegion) return
    setSaving(true)
    try {
      await api.polygonReset(selectedRegion, selectedFeature.properties.id)
      haptic('success')
      cancelEdit()
      setSelectedFeature(null)
      // Обновляем данные региона
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
  }, [selectedFeature, selectedRegion, cancelEdit])

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

      {/* Карта */}
      <div className="rounded-xl overflow-hidden relative" style={{ border: '1px solid var(--tg-color-hint, #999)' }}>
        <div ref={mapContainerRef} style={{ height: '50vh', minHeight: 300, width: '100%' }} />
        {loadingMap && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/20">
            <span className="text-xs text-white">Загрузка полигонов...</span>
          </div>
        )}
      </div>

      {/* Информация о выбранном полигоне */}
      {selectedFeature && !isEditing && (
        <PolygonInfoPanel
          feature={selectedFeature}
          onEdit={startEdit}
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
              {editCoords[0].length} вершин
            </span>
          </div>
          <p className="text-xs opacity-60">
            Перетащите синие маркеры для изменения границ полигона.
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
  onEdit: () => void
  onReset: () => void
  saving: boolean
}

function PolygonInfoPanel({ feature, onEdit, onReset, saving }: InfoPanelProps) {
  const p = feature.properties
  const geom = feature.geometry
  const canEdit = geom.type === 'Polygon'
  const coordCount = geom.type === 'Polygon'
    ? geom.coordinates[0].length
    : geom.type === 'MultiPolygon'
      ? geom.coordinates.reduce((s, ring) => s + ring[0].length, 0)
      : 0

  return (
    <div className="tg-card space-y-2">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold truncate">
            {p.name || 'Без названия'}
          </h3>
          <div className="text-xs opacity-60 mt-0.5">
            {placeLabel(p.place_type) && <span>{placeLabel(p.place_type)} · </span>}
            {p.osm_type}/{p.osm_id} · {coordCount} вершин
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
            onClick={onEdit}
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
