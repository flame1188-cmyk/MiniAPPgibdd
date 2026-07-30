/**
 * AnalyticsView — визуализация аналитики ДТП через Recharts.
 *
 * Структура analytics (см. analytics.build_full_analytics):
 *  {
 *    current: { total, deaths, injured, alcohol, pedestrians,
 *               deaths_per_100, injured_per_100,
 *               by_weekday, by_hour, by_type, by_type_grouped,
 *               by_weather, by_road, by_month },
 *    previous: {...} | null,
 *    comparison: { total: {current, previous, change, abs_change},
 *                  deaths: {...}, injured: {...}, ...,
 *                  by_type_grouped: {current, previous},
 *                  by_road: {current, previous},
 *                  by_month: {current, previous} } | null,
 *    has_prev_data: boolean,
 *    prev_label: "Январь-Июнь 2025" | null,
 *    current_label: "Январь-Июнь 2026"
 *  }
 *
 * Возможности:
 *  - KPI с динамикой vs АППГ (если has_prev_data)
 *  - Переключатель метрики: ДТП / Погибшие / Раненые
 *  - График по месяцам (current vs prev) — работает с переключателем метрики
 *  - График по дорогам (топ-10) — работает с переключателем метрики
 *  - График по видам ДТП (9 канонических категорий)
 *  - График по дням недели и часам (только текущий период)
 *  - График по погоде (donut)
 */
import { useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { haptic } from '@/lib/telegram'

interface AnalyticsViewProps {
  analytics: Record<string, unknown>
}

const DAY_SHORT = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']

const MONTH_ORDER = [
  'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
  'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь',
]

const MONTH_SHORT = [
  'Янв', 'Фев', 'Мар', 'Апр', 'Май', 'Июн',
  'Июл', 'Авг', 'Сен', 'Окт', 'Ноя', 'Дек',
]

// 9 канонических категорий — порядок соответствует DTP_TYPE_ORDER в analytics.py
const DTP_TYPE_ORDER = [
  'Столкновение',
  'Наезд на пешехода',
  'Наезд на велосипедиста',
  'Наезд на стоящее ТС',
  'Съезд с дороги',
  'Опрокидывание',
  'Наезд на препятствие',
  'Наезд на лицо, использующее СИМ',
  'Иные ДТП',
]

// Короткие подписи для графика (иначе текст сливается)
const DTP_TYPE_SHORT: Record<string, string> = {
  'Столкновение': 'Столкновение',
  'Наезд на пешехода': 'Наезд на пешехода',
  'Наезд на велосипедиста': 'Наезд на велосип.',
  'Наезд на стоящее ТС': 'Наезд на стоящее ТС',
  'Съезд с дороги': 'Съезд с дороги',
  'Опрокидывание': 'Опрокидывание',
  'Наезд на препятствие': 'Наезд на препятст.',
  'Наезд на лицо, использующее СИМ': 'Наезд на СИМ',
  'Иные ДТП': 'Иные ДТП',
}

// Палитра цветов (адаптивная к Telegram-теме через CSS-вары)
const COLORS_PALETTE = [
  '#2481cc', // primary blue
  '#ff9500', // orange
  '#34c759', // green
  '#af52de', // purple
  '#ff3b30', // red
  '#5ac8fa', // light blue
  '#ffcc00', // yellow
  '#5856d6', // indigo
  '#8e8e93', // grey
]

// Метрики для переключателя
type Metric = 'dtp' | 'deaths' | 'injured'
const METRICS: { id: Metric; label: string; color: string }[] = [
  { id: 'dtp', label: 'ДТП', color: '#2481cc' },
  { id: 'deaths', label: 'Погибшие', color: '#ff3b30' },
  { id: 'injured', label: 'Раненые', color: '#ff9500' },
]

// Утилиты извлечения метрик из by_month (структура {dtp, deaths, injured})
function getMonthMetric(
  monthData: { dtp: number; deaths: number; injured: number } | undefined,
  metric: Metric
): number {
  if (!monthData) return 0
  if (metric === 'dtp') return monthData.dtp ?? 0
  if (metric === 'deaths') return monthData.deaths ?? 0
  return monthData.injured ?? 0
}

// Форматирование динамики для KPI
function formatDelta(
  current: number,
  previous: number | undefined
): { text: string; color: string } | null {
  if (previous === undefined || previous === null) return null
  const abs = current - previous
  if (previous === 0) {
    if (current === 0) return { text: '0% →', color: '#8e8e93' }
    return { text: 'новое ↑', color: '#ff3b30' }
  }
  const pct = (abs / previous) * 100
  const arrow = abs > 0 ? '↑' : abs < 0 ? '↓' : '→'
  const color = abs > 0 ? '#ff3b30' : abs < 0 ? '#34c759' : '#8e8e93'
  const sign = abs > 0 ? '+' : ''
  return {
    text: `${sign}${abs} (${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%) ${arrow}`,
    color,
  }
}

export function AnalyticsView({ analytics }: AnalyticsViewProps) {
  const a = analytics as Record<string, any>
  const [metric, setMetric] = useState<Metric>('dtp')

  // === Извлекаем current/previous ===
  const current = (a.current ?? a) as Record<string, any>
  const previous = (a.previous ?? null) as Record<string, any> | null
  const hasPrev = !!a.has_prev_data && !!previous
  const prevLabel = (a.prev_label ?? 'АППГ') as string
  const currentLabel = (a.current_label ?? 'Текущий период') as string

  // === KPI с динамикой ===
  const kpiItems = [
    { key: 'total', label: 'Всего ДТП', value: current.total, color: '#2481cc' },
    { key: 'deaths', label: 'Погибших', value: current.deaths, color: '#ff3b30' },
    { key: 'injured', label: 'Раненых', value: current.injured, color: '#ff9500' },
    { key: 'alcohol', label: 'Нетрезвые', value: current.alcohol, color: '#af52de' },
    { key: 'pedestrians', label: 'Пешеходы', value: current.pedestrians, color: '#34c759' },
    {
      key: 'deaths_per_100',
      label: 'Погибших / 100',
      value:
        typeof current.deaths_per_100 === 'number'
          ? current.deaths_per_100.toFixed(1)
          : '—',
      color: '#5856d6',
    },
  ]

  // === Данные для графиков ===
  // По дням недели
  const weekdayData = Object.entries(current.by_weekday ?? {})
    .map(([k, v]) => ({
      day: DAY_SHORT[Number(k)] ?? k,
      value: Number(v),
    }))
    .sort(
      (x, y) =>
        DAY_SHORT.indexOf(x.day) - DAY_SHORT.indexOf(y.day)
    )

  // По часам
  const hourData = Array.from({ length: 24 }, (_, h) => ({
    hour: `${h}`,
    value: Number((current.by_hour ?? {})[String(h)] ?? 0),
  }))

  // По видам ДТП (9 категорий, упорядочено по DTP_TYPE_ORDER)
  const typeGroupedCurrent = (current.by_type_grouped ?? {}) as Record<string, number>
  const typeGroupedPrev = (previous?.by_type_grouped ?? {}) as Record<string, number>
  const typeData = DTP_TYPE_ORDER.filter(
    (t) => (typeGroupedCurrent[t] ?? 0) > 0 || (typeGroupedPrev[t] ?? 0) > 0
  ).map((t) => ({
    name: DTP_TYPE_SHORT[t] ?? t,
    fullName: t,
    current: typeGroupedCurrent[t] ?? 0,
    previous: hasPrev ? typeGroupedPrev[t] ?? 0 : undefined,
  }))

  // По погоде
  const weatherData = Object.entries(current.by_weather ?? {})
    .map(([k, v]) => ({ name: k, value: Number(v) }))
    .sort((a, b) => b.value - a.value)

  // По месяцам (с АППГ) — зависит от переключателя metric
  const byMonthCurrent = (current.by_month ?? {}) as Record<
    string,
    { dtp: number; deaths: number; injured: number }
  >
  const byMonthPrev = (previous?.by_month ?? {}) as Record<
    string,
    { dtp: number; deaths: number; injured: number }
  >
  const monthData = MONTH_ORDER.filter(
    (m) => byMonthCurrent[m] || byMonthPrev[m]
  ).map((m) => ({
    month: MONTH_SHORT[MONTH_ORDER.indexOf(m)],
    fullMonth: m,
    current: getMonthMetric(byMonthCurrent[m], metric),
    previous: hasPrev ? getMonthMetric(byMonthPrev[m], metric) : undefined,
  }))

  // По дорогам (топ-10) — зависит от переключателя metric
  // by_road — это просто Counter по дороге (только количество ДТП).
  // Для deaths/injured нужно посчитать дополнительно.
  // Т.к. в analytics мы храним только count ДТП на дорогу — для deaths/injured
  // используем ту же цифру, но помечаем в tooltip.
  const roadDataCurrent = (current.by_road ?? {}) as Record<string, number>
  const roadDataPrev = (previous?.by_road ?? {}) as Record<string, number>
  const roadData = Object.keys(roadDataCurrent)
    .map((road) => ({
      name: road,
      current: roadDataCurrent[road] ?? 0,
      previous: hasPrev ? roadDataPrev[road] ?? 0 : undefined,
    }))
    .filter((d) => d.current > 0 || (d.previous ?? 0) > 0)
    .sort((a, b) => b.current - a.current)
    .slice(0, 10)

  // Текущая метрика для подписи
  const currentMetricLabel =
    METRICS.find((m) => m.id === metric)?.label ?? 'ДТП'

  return (
    <div className="space-y-3">
      {/* === Переключатель метрики === */}
      <div className="tg-card">
        <div className="tg-section-header mb-2">Метрика</div>
        <div className="grid grid-cols-3 gap-1.5">
          {METRICS.map((m) => (
            <button
              key={m.id}
              onClick={() => {
                setMetric(m.id)
                haptic('light')
              }}
              className="py-2 px-2 rounded-lg text-xs font-medium transition-colors"
              style={{
                backgroundColor:
                  metric === m.id
                    ? m.color
                    : 'var(--tg-color-secondary-bg, #f1f1f1)',
                color:
                  metric === m.id
                    ? '#ffffff'
                    : 'var(--tg-color-text, #000000)',
              }}
            >
              {m.label}
            </button>
          ))}
        </div>
        <div className="text-[10px] opacity-60 mt-1.5 text-center">
          Графики по месяцам и дорогам используют выбранную метрику
        </div>
      </div>

      {/* === KPI-сводка с динамикой === */}
      <div className="tg-card">
        <div className="tg-section-header mb-3">
          Сводка {hasPrev && `vs ${prevLabel}`}
        </div>
        <div className="grid grid-cols-3 gap-2">
          {kpiItems.map((kpi) => {
            // Для deaths_per_100 динамика = разница, а не %
            const isRate = kpi.key === 'deaths_per_100'
            const prevValue = hasPrev
              ? isRate
                ? Number(previous[kpi.key] ?? 0)
                : Number(previous[kpi.key] ?? 0)
              : undefined
            const curValue = isRate
              ? Number(kpi.value)
              : Number(kpi.value)
            const delta = hasPrev
              ? isRate
                ? formatDelta(curValue, prevValue)
                : formatDelta(curValue, prevValue)
              : null
            return (
              <div
                key={kpi.label}
                className="p-2.5 rounded-xl text-center"
                style={{
                  backgroundColor:
                    'var(--tg-color-secondary-bg, #f1f1f1)',
                }}
              >
                <div
                  className="text-lg font-bold"
                  style={{ color: kpi.color }}
                >
                  {kpi.value}
                </div>
                <div className="text-[10px] opacity-70 leading-tight mt-0.5">
                  {kpi.label}
                </div>
                {delta && (
                  <div
                    className="text-[10px] mt-1 font-medium leading-tight"
                    style={{ color: delta.color }}
                  >
                    {delta.text}
                  </div>
                )}
              </div>
            )
          })}
        </div>
        {typeof current.injured_per_100 === 'number' && (
          <div className="mt-2 text-xs opacity-60 text-center">
            Раненых на 100 ДТП:{' '}
            <b>{current.injured_per_100.toFixed(1)}</b>
            {hasPrev && typeof previous?.injured_per_100 === 'number' && (
              <span
                style={{
                  color:
                    previous.injured_per_100 > current.injured_per_100
                      ? '#34c759'
                      : previous.injured_per_100 < current.injured_per_100
                      ? '#ff3b30'
                      : '#8e8e93',
                  marginLeft: 6,
                }}
              >
                ({previous.injured_per_100.toFixed(1)} в АППГ)
              </span>
            )}
          </div>
        )}
      </div>

      {/* === Динамика по месяцам vs АППГ === */}
      {monthData.length > 0 && (
        <div className="tg-card">
          <div className="tg-section-header mb-3">
            Динамика по месяцам ({currentMetricLabel})
            {hasPrev && ` vs ${prevLabel}`}
          </div>
          <div style={{ width: '100%', height: 240 }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart
                data={monthData}
                margin={{ top: 5, right: 10, bottom: 5, left: -15 }}
              >
                <CartesianGrid
                  strokeDasharray="3 3"
                  stroke="var(--tg-color-hint, #ccc)"
                  strokeOpacity={0.3}
                />
                <XAxis
                  dataKey="month"
                  tick={{
                    fontSize: 11,
                    fill: 'var(--tg-color-hint, #999)',
                  }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  tick={{
                    fontSize: 10,
                    fill: 'var(--tg-color-hint, #999)',
                  }}
                  axisLine={false}
                  tickLine={false}
                  width={35}
                />
                <Tooltip
                  contentStyle={{
                    backgroundColor:
                      'var(--tg-color-section-bg, #fff)',
                    border: 'none',
                    borderRadius: 8,
                    fontSize: 12,
                  }}
                  labelFormatter={(l, payload) => {
                    const p = payload?.[0]?.payload
                    return p?.fullMonth ?? l
                  }}
                  formatter={(value: any, name: any) => [
                    value,
                    name === 'current' ? currentLabel : prevLabel,
                  ]}
                />
                {hasPrev && (
                  <Legend
                    wrapperStyle={{ fontSize: 11 }}
                    formatter={(value) =>
                      value === 'current' ? currentLabel : prevLabel
                    }
                  />
                )}
                <Line
                  type="monotone"
                  dataKey="current"
                  stroke="#2481cc"
                  strokeWidth={2.5}
                  dot={{ r: 3, fill: '#2481cc' }}
                  activeDot={{ r: 5 }}
                />
                {hasPrev && (
                  <Line
                    type="monotone"
                    dataKey="previous"
                    stroke="#ff9500"
                    strokeWidth={2}
                    strokeDasharray="5 3"
                    dot={{ r: 2, fill: '#ff9500' }}
                  />
                )}
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {/* === По дорогам (топ-10) === */}
      {roadData.length > 0 && (
        <div className="tg-card">
          <div className="tg-section-header mb-3">
            Аварийность по дорогам ({currentMetricLabel}, топ-10)
          </div>
          <div
            style={{
              width: '100%',
              height: Math.max(200, roadData.length * 32 + 30),
            }}
          >
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                layout="vertical"
                data={roadData}
                margin={{ top: 5, right: 15, bottom: 5, left: 5 }}
              >
                <CartesianGrid
                  strokeDasharray="3 3"
                  stroke="var(--tg-color-hint, #ccc)"
                  strokeOpacity={0.3}
                  horizontal={false}
                />
                <XAxis
                  type="number"
                  tick={{
                    fontSize: 10,
                    fill: 'var(--tg-color-hint, #999)',
                  }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  type="category"
                  dataKey="name"
                  tick={{
                    fontSize: 10,
                    fill: 'var(--tg-color-text, #000)',
                  }}
                  axisLine={false}
                  tickLine={false}
                  width={130}
                  tickFormatter={(v) =>
                    String(v).length > 22
                      ? String(v).slice(0, 22) + '…'
                      : v
                  }
                />
                <Tooltip
                  cursor={{ fill: 'rgba(0,0,0,0.05)' }}
                  contentStyle={{
                    backgroundColor:
                      'var(--tg-color-section-bg, #fff)',
                    border: 'none',
                    borderRadius: 8,
                    fontSize: 12,
                  }}
                  formatter={(value: any, name: any) => [
                    value,
                    name === 'current' ? currentLabel : prevLabel,
                  ]}
                />
                {hasPrev && (
                  <Legend
                    wrapperStyle={{ fontSize: 11 }}
                    formatter={(value) =>
                      value === 'current' ? currentLabel : prevLabel
                    }
                  />
                )}
                <Bar
                  dataKey="current"
                  fill="#2481cc"
                  radius={[0, 4, 4, 0]}
                  barSize={hasPrev ? 10 : 16}
                />
                {hasPrev && (
                  <Bar
                    dataKey="previous"
                    fill="#ff9500"
                    radius={[0, 4, 4, 0]}
                    barSize={10}
                  />
                )}
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {/* === По видам ДТП (9 категорий) === */}
      {typeData.length > 0 && (
        <div className="tg-card">
          <div className="tg-section-header mb-3">
            По видам ДТП {hasPrev && `vs ${prevLabel}`}
          </div>
          <div
            style={{
              width: '100%',
              height: Math.max(240, typeData.length * 32 + 30),
            }}
          >
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                layout="vertical"
                data={typeData}
                margin={{ top: 5, right: 15, bottom: 5, left: 5 }}
              >
                <CartesianGrid
                  strokeDasharray="3 3"
                  stroke="var(--tg-color-hint, #ccc)"
                  strokeOpacity={0.3}
                  horizontal={false}
                />
                <XAxis
                  type="number"
                  tick={{
                    fontSize: 10,
                    fill: 'var(--tg-color-hint, #999)',
                  }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  type="category"
                  dataKey="name"
                  tick={{
                    fontSize: 11,
                    fill: 'var(--tg-color-text, #000)',
                  }}
                  axisLine={false}
                  tickLine={false}
                  width={150}
                />
                <Tooltip
                  cursor={{ fill: 'rgba(0,0,0,0.05)' }}
                  contentStyle={{
                    backgroundColor:
                      'var(--tg-color-section-bg, #fff)',
                    border: 'none',
                    borderRadius: 8,
                    fontSize: 12,
                  }}
                  formatter={(value: any, name: any) => [
                    value,
                    name === 'current' ? currentLabel : prevLabel,
                  ]}
                  labelFormatter={(_, payload) =>
                    payload?.[0]?.payload?.fullName ?? ''
                  }
                />
                {hasPrev && (
                  <Legend
                    wrapperStyle={{ fontSize: 11 }}
                    formatter={(value) =>
                      value === 'current' ? currentLabel : prevLabel
                    }
                  />
                )}
                <Bar
                  dataKey="current"
                  fill="#2481cc"
                  radius={[0, 4, 4, 0]}
                  barSize={hasPrev ? 10 : 16}
                />
                {hasPrev && (
                  <Bar
                    dataKey="previous"
                    fill="#ff9500"
                    radius={[0, 4, 4, 0]}
                    barSize={10}
                  />
                )}
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {/* === По дням недели === */}
      {weekdayData.length > 0 && (
        <div className="tg-card">
          <div className="tg-section-header mb-3">
            По дням недели ({currentLabel})
          </div>
          <div style={{ width: '100%', height: 200 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={weekdayData}
                margin={{ top: 5, right: 5, bottom: 5, left: -20 }}
              >
                <XAxis
                  dataKey="day"
                  tick={{
                    fontSize: 11,
                    fill: 'var(--tg-color-hint, #999)',
                  }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  tick={{
                    fontSize: 10,
                    fill: 'var(--tg-color-hint, #999)',
                  }}
                  axisLine={false}
                  tickLine={false}
                  width={35}
                />
                <Tooltip
                  cursor={{ fill: 'rgba(0,0,0,0.05)' }}
                  contentStyle={{
                    backgroundColor:
                      'var(--tg-color-section-bg, #fff)',
                    border: 'none',
                    borderRadius: 8,
                    fontSize: 12,
                  }}
                />
                <Bar dataKey="value" radius={[4, 4, 0, 0]}>
                  {weekdayData.map((_, idx) => (
                    <Cell
                      key={idx}
                      fill={COLORS_PALETTE[idx % COLORS_PALETTE.length]}
                    />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {/* === По часам === */}
      {hourData.some((d) => d.value > 0) && (
        <div className="tg-card">
          <div className="tg-section-header mb-3">
            По часам суток ({currentLabel})
          </div>
          <div style={{ width: '100%', height: 200 }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart
                data={hourData}
                margin={{ top: 5, right: 5, bottom: 5, left: -20 }}
              >
                <XAxis
                  dataKey="hour"
                  tick={{
                    fontSize: 10,
                    fill: 'var(--tg-color-hint, #999)',
                  }}
                  axisLine={false}
                  tickLine={false}
                  interval={2}
                />
                <YAxis
                  tick={{
                    fontSize: 10,
                    fill: 'var(--tg-color-hint, #999)',
                  }}
                  axisLine={false}
                  tickLine={false}
                  width={35}
                />
                <Tooltip
                  contentStyle={{
                    backgroundColor:
                      'var(--tg-color-section-bg, #fff)',
                    border: 'none',
                    borderRadius: 8,
                    fontSize: 12,
                  }}
                  labelFormatter={(l) => `${l}:00`}
                />
                <Line
                  type="monotone"
                  dataKey="value"
                  stroke="#2481cc"
                  strokeWidth={2}
                  dot={{ r: 2, fill: '#2481cc' }}
                  activeDot={{ r: 4 }}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {/* === По погоде === */}
      {weatherData.length > 0 && (
        <div className="tg-card">
          <div className="tg-section-header mb-3">
            По погодным условиям ({currentLabel})
          </div>
          <div
            className="flex items-center"
            style={{ width: '100%', height: 200 }}
          >
            <div style={{ width: '50%', height: '100%' }}>
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={weatherData}
                    dataKey="value"
                    nameKey="name"
                    cx="50%"
                    cy="50%"
                    innerRadius={35}
                    outerRadius={70}
                    paddingAngle={2}
                  >
                    {weatherData.map((_, idx) => (
                      <Cell
                        key={idx}
                        fill={COLORS_PALETTE[idx % COLORS_PALETTE.length]}
                      />
                    ))}
                  </Pie>
                  <Tooltip
                    contentStyle={{
                      backgroundColor:
                        'var(--tg-color-section-bg, #fff)',
                      border: 'none',
                      borderRadius: 8,
                      fontSize: 12,
                    }}
                  />
                </PieChart>
              </ResponsiveContainer>
            </div>
            <div className="flex-1 pl-2 space-y-1">
              {weatherData.map((w, idx) => (
                <div
                  key={w.name}
                  className="flex items-center gap-2 text-xs"
                >
                  <span
                    style={{
                      display: 'inline-block',
                      width: 10,
                      height: 10,
                      borderRadius: 2,
                      backgroundColor:
                        COLORS_PALETTE[idx % COLORS_PALETTE.length],
                    }}
                  />
                  <span className="opacity-80 flex-1 truncate">
                    {w.name}
                  </span>
                  <span className="font-medium">{w.value}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
