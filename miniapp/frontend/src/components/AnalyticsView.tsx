/**
 * AnalyticsView — визуализация аналитики ДТП через Recharts.
 *
 * Структура:
 *  1. KPI-сводка (6 карточек: total / deaths / injured / alcohol / pedestrians / deaths_per_100)
 *  2. ДТП по дням недели (Bar chart)
 *  3. ДТП по часам (Line chart с area)
 *  4. ДТП по видам (Horizontal Bar chart)
 *  5. ДТП по погоде (Donut chart)
 *
 * Цвета берутся из CSS-переменных Telegram Mini App,
 * чтобы поддерживать тёмную/светлую тему автоматически.
 *
 * Формат analytics (см. analytics.calculate_metrics):
 *  {
 *    total: 836, deaths: 131, injured: 1141,
 *    alcohol: 13, pedestrians: 226,
 *    deaths_per_100: 15.70, injured_per_100: 136.50,
 *    by_weekday: {"0": 126, "1": 116, ...},     // 0=Пн, 6=Вс
 *    by_hour:   {"0": 24, "1": 8, ...},          // 0..23
 *    by_type:   {"Столкновение": 386, "Наезд на пешехода": 215, ...},
 *    by_weather:{"Ясно": 678, "Пасмурно": 77, ...}
 *  }
 */
import {
  Bar,
  BarChart,
  Cell,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

interface AnalyticsViewProps {
  analytics: Record<string, unknown>
}

const DAY_SHORT = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']

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
]

export function AnalyticsView({ analytics }: AnalyticsViewProps) {
  const a = analytics as Record<string, any>

  // === KPI ===
  const kpis = [
    { label: 'Всего ДТП', value: a.total, color: '#2481cc' },
    { label: 'Погибших', value: a.deaths, color: '#ff3b30' },
    { label: 'Раненых', value: a.injured, color: '#ff9500' },
    { label: 'Нетрезвые', value: a.alcohol, color: '#af52de' },
    { label: 'Пешеходы', value: a.pedestrians, color: '#34c759' },
    {
      label: 'Погибших / 100',
      value: typeof a.deaths_per_100 === 'number' ? a.deaths_per_100.toFixed(1) : '—',
      color: '#5856d6',
    },
  ]

  // === Данные для графиков ===
  const weekdayData = Object.entries(a.by_weekday ?? {})
    .map(([k, v]) => ({
      day: DAY_SHORT[Number(k)] ?? k,
      value: Number(v),
    }))
    // Сортируем по Пн-Вс (0..6)
    .sort((x, y) => DAY_SHORT.indexOf(x.day) - DAY_SHORT.indexOf(y.day))

  const hourData = Array.from({ length: 24 }, (_, h) => ({
    hour: `${h}`,
    value: Number((a.by_hour ?? {})[String(h)] ?? 0),
  }))

  const typeData = Object.entries(a.by_type ?? {})
    .map(([k, v]) => ({ name: k, value: Number(v) }))
    .sort((a, b) => b.value - a.value)

  const weatherData = Object.entries(a.by_weather ?? {})
    .map(([k, v]) => ({ name: k, value: Number(v) }))
    .sort((a, b) => b.value - a.value)

  return (
    <div className="space-y-3">
      {/* === KPI-сводка === */}
      <div className="tg-card">
        <div className="tg-section-header mb-3">Сводка</div>
        <div className="grid grid-cols-3 gap-2">
          {kpis.map((kpi) => (
            <div
              key={kpi.label}
              className="p-2.5 rounded-xl text-center"
              style={{
                backgroundColor: 'var(--tg-color-secondary-bg, #f1f1f1)',
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
            </div>
          ))}
        </div>
        {typeof a.injured_per_100 === 'number' && (
          <div className="mt-2 text-xs opacity-60 text-center">
            Раненых на 100 ДТП: <b>{a.injured_per_100.toFixed(1)}</b>
          </div>
        )}
      </div>

      {/* === По дням недели === */}
      {weekdayData.length > 0 && (
        <div className="tg-card">
          <div className="tg-section-header mb-3">По дням недели</div>
          <div style={{ width: '100%', height: 200 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={weekdayData}
                margin={{ top: 5, right: 5, bottom: 5, left: -20 }}
              >
                <XAxis
                  dataKey="day"
                  tick={{ fontSize: 11, fill: 'var(--tg-color-hint, #999)' }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  tick={{ fontSize: 10, fill: 'var(--tg-color-hint, #999)' }}
                  axisLine={false}
                  tickLine={false}
                  width={35}
                />
                <Tooltip
                  cursor={{ fill: 'rgba(0,0,0,0.05)' }}
                  contentStyle={{
                    backgroundColor: 'var(--tg-color-section-bg, #fff)',
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
          <div className="tg-section-header mb-3">По часам суток</div>
          <div style={{ width: '100%', height: 200 }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart
                data={hourData}
                margin={{ top: 5, right: 5, bottom: 5, left: -20 }}
              >
                <XAxis
                  dataKey="hour"
                  tick={{ fontSize: 10, fill: 'var(--tg-color-hint, #999)' }}
                  axisLine={false}
                  tickLine={false}
                  interval={2}
                />
                <YAxis
                  tick={{ fontSize: 10, fill: 'var(--tg-color-hint, #999)' }}
                  axisLine={false}
                  tickLine={false}
                  width={35}
                />
                <Tooltip
                  contentStyle={{
                    backgroundColor: 'var(--tg-color-section-bg, #fff)',
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

      {/* === По видам ДТП === */}
      {typeData.length > 0 && (
        <div className="tg-card">
          <div className="tg-section-header mb-3">По видам ДТП</div>
          <div style={{ width: '100%', height: typeData.length * 28 + 20 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                layout="vertical"
                data={typeData}
                margin={{ top: 5, right: 15, bottom: 5, left: 5 }}
              >
                <XAxis
                  type="number"
                  tick={{ fontSize: 10, fill: 'var(--tg-color-hint, #999)' }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  type="category"
                  dataKey="name"
                  tick={{ fontSize: 10, fill: 'var(--tg-color-text, #000)' }}
                  axisLine={false}
                  tickLine={false}
                  width={140}
                />
                <Tooltip
                  cursor={{ fill: 'rgba(0,0,0,0.05)' }}
                  contentStyle={{
                    backgroundColor: 'var(--tg-color-section-bg, #fff)',
                    border: 'none',
                    borderRadius: 8,
                    fontSize: 12,
                  }}
                />
                <Bar dataKey="value" radius={[0, 4, 4, 0]}>
                  {typeData.map((_, idx) => (
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

      {/* === По погоде === */}
      {weatherData.length > 0 && (
        <div className="tg-card">
          <div className="tg-section-header mb-3">По погодным условиям</div>
          <div className="flex items-center" style={{ width: '100%', height: 200 }}>
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
                      backgroundColor: 'var(--tg-color-section-bg, #fff)',
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
                <div key={w.name} className="flex items-center gap-2 text-xs">
                  <span
                    style={{
                      display: 'inline-block',
                      width: 10,
                      height: 10,
                      borderRadius: 2,
                      backgroundColor: COLORS_PALETTE[idx % COLORS_PALETTE.length],
                    }}
                  />
                  <span className="opacity-80 flex-1 truncate">{w.name}</span>
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
