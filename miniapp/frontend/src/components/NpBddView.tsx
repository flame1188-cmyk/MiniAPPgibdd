/**
 * NpBddView — вкладка «НП БДД» (Национальный проект «Безопасные дорожные движения»).
 *
 * Показывает:
 *  1. Селектор региона + переключатель linear/horizontal для линии плана.
 *  2. 4 KPI-карточки: Тр факт (YTD), Тр прогноз (на конец года), План, Отклонение.
 *  3. График 1: динамика Тр 2023→2030 (факт + прогноз + план).
 *  4. График 2: кумулятивный Тр по месяцам текущего года (факт + прогноз + план).
 *  5. Кнопка «Заморозить год» для админ-действий.
 */
import { useEffect, useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api, type NpBddData, type NpBddRegion } from '@/lib/api'
import { haptic, showAlert, showConfirm } from '@/lib/telegram'
import { cn } from '@/lib/utils'

const MONTH_SHORT = [
  'Янв', 'Фев', 'Мар', 'Апр', 'Май', 'Июн',
  'Июл', 'Авг', 'Сен', 'Окт', 'Ноя', 'Дек',
]

const STATUS_COLORS: Record<string, string> = {
  ok: 'text-green-600 dark:text-green-400',
  warning: 'text-yellow-600 dark:text-yellow-400',
  danger: 'text-red-600 dark:text-red-400',
}

const STATUS_LABELS: Record<string, string> = {
  ok: 'Выполняется',
  warning: 'На грани',
  danger: 'Угроза срыва',
}

interface NpBddViewProps {
  // placeholder для будущей интеграции, пока нет
}

export function NpBddView(_: NpBddViewProps = {}) {
  const queryClient = useQueryClient()
  const [selectedRegion, setSelectedRegion] = useState<string>('')
  const [planLineMode, setPlanLineMode] = useState<'linear' | 'horizontal'>('linear')

  // --- Список регионов ---
  const regionsQuery = useQuery({
    queryKey: ['np-bdd-regions'],
    queryFn: api.npBddListRegions,
    staleTime: 30 * 60 * 1000, // 30 минут
  })

  // Автовыбор первого региона
  useEffect(() => {
    if (!selectedRegion && regionsQuery.data && regionsQuery.data.length > 0) {
      setSelectedRegion(regionsQuery.data[0].code)
    }
  }, [regionsQuery.data, selectedRegion])

  // --- Настройки региона (для подхвата plan_line_mode) ---
  const settingsQuery = useQuery({
    queryKey: ['np-bdd-settings', selectedRegion],
    queryFn: () => api.npBddGetSettings(selectedRegion),
    enabled: !!selectedRegion,
    staleTime: 60 * 60 * 1000,
  })

  useEffect(() => {
    if (settingsQuery.data?.plan_line_mode) {
      setPlanLineMode(settingsQuery.data.plan_line_mode)
    }
  }, [settingsQuery.data])

  // --- Главный payload ---
  const dataQuery = useQuery({
    queryKey: ['np-bdd-data', selectedRegion, planLineMode],
    queryFn: () => api.npBddGetData(selectedRegion, planLineMode),
    enabled: !!selectedRegion,
    staleTime: 5 * 60 * 1000, // 5 минут на клиенте
    retry: 1,
  })

  // --- Список замороженных лет ---
  const frozenQuery = useQuery({
    queryKey: ['np-bdd-frozen', selectedRegion],
    queryFn: () => api.npBddListFrozen(selectedRegion),
    enabled: !!selectedRegion,
    staleTime: 60 * 1000,
  })

  // --- Мутация: переключение plan_line_mode ---
  const updateSettingsMutation = useMutation({
    mutationFn: (mode: 'linear' | 'horizontal') =>
      api.npBddUpdateSettings(selectedRegion, mode),
    onSuccess: (data) => {
      haptic('light')
      setPlanLineMode(data.plan_line_mode)
      // Инвалидируем data, чтобы перетянуть с новым режимом
      queryClient.invalidateQueries({ queryKey: ['np-bdd-data', selectedRegion] })
    },
    onError: (err: Error) => {
      haptic('error')
      showAlert(`Не удалось сохранить настройку: ${err.message}`)
    },
  })

  // --- Мутация: заморозка года ---
  const freezeMutation = useMutation({
    mutationFn: ({ year, note }: { year: number; note?: string }) =>
      api.npBddFreezeYear(selectedRegion, year, note),
    onSuccess: () => {
      haptic('success')
      queryClient.invalidateQueries({ queryKey: ['np-bdd-frozen', selectedRegion] })
      queryClient.invalidateQueries({ queryKey: ['np-bdd-data', selectedRegion] })
    },
    onError: (err: Error) => {
      haptic('error')
      showAlert(`Не удалось заморозить год: ${err.message}`)
    },
  })

  // --- Мутация: разморозка ---
  const unfreezeMutation = useMutation({
    mutationFn: (year: number) => api.npBddUnfreezeYear(selectedRegion, year),
    onSuccess: () => {
      haptic('light')
      queryClient.invalidateQueries({ queryKey: ['np-bdd-frozen', selectedRegion] })
      queryClient.invalidateQueries({ queryKey: ['np-bdd-data', selectedRegion] })
    },
    onError: (err: Error) => {
      haptic('error')
      showAlert(`Не удалось разморозить: ${err.message}`)
    },
  })

  const handleTogglePlanLine = () => {
    const newMode = planLineMode === 'linear' ? 'horizontal' : 'linear'
    updateSettingsMutation.mutate(newMode)
  }

  const handleFreeze = async (year: number) => {
    const ok = await showConfirm(
      `Заморозить ${year} год?\n\nПосле заморозки данные за этот год не будут пересчитываться. ` +
      `Используйте это после финализации данных ГИБДД (обычно через 2-3 месяца после окончания года).`
    )
    if (!ok) return
    freezeMutation.mutate({ year })
  }

  const handleUnfreeze = async (year: number) => {
    const ok = await showConfirm(`Разморозить ${year} год?`)
    if (!ok) return
    unfreezeMutation.mutate(year)
  }

  // --- Подготовка данных для графиков ---

  // График 1: точки по годам 2023..2030.
  // Для каждой точки: год, fact (если есть), plan.
  const chart1Data = useMemo(() => {
    if (!dataQuery.data) return []
    const d = dataQuery.data
    const years = Object.keys(d.plan_series).sort()
    return years.map((year) => {
      const planVal = d.plan_series[year]
      let factVal: number | null = null
      if (d.history[year]) {
        factVal = d.history[year].tr
      } else if (d.current_year.year.toString() === year) {
        factVal = d.current_year.tr_forecast_full_year
      }
      return {
        year,
        plan: planVal,
        fact: factVal,
        isForecast: d.current_year.year.toString() === year,
      }
    })
  }, [dataQuery.data])

  // График 2: кумулятивный Тр по месяцам.
  const chart2Data = useMemo(() => {
    if (!dataQuery.data) return []
    const mc = dataQuery.data.current_year.monthly_chart
    return mc.months.map((m) => {
      const key = String(m)
      const actual = mc.tr_actual_cumulative[key]
      const forecast = mc.tr_forecast_cumulative[key]
      const plan = mc.plan_cumulative[key]
      return {
        month: MONTH_SHORT[m - 1] || `М${m}`,
        // Используем null для отсутствующих значений (recharts пропустит их)
        fact: actual !== undefined ? actual : null,
        forecast: forecast !== undefined ? forecast : null,
        plan,
      }
    })
  }, [dataQuery.data])

  // --- Загрузка состояний ---
  if (regionsQuery.isLoading) {
    return <div className="tg-card">Загрузка справочника регионов…</div>
  }
  if (regionsQuery.isError) {
    return (
      <div className="tg-card text-red-600 dark:text-red-400">
        Не удалось загрузить список регионов: {(regionsQuery.error as Error).message}
      </div>
    )
  }
  if (!regionsQuery.data || regionsQuery.data.length === 0) {
    return (
      <div className="tg-card">
        Нет данных по регионам. Попросите администратора загрузить Excel-файлы
        Ктс и плановых значений Тр.
      </div>
    )
  }

  return (
    <div className="space-y-3">
      {/* Заголовок вкладки */}
      <div className="tg-card">
        <h2 className="text-lg font-semibold mb-1">НП БДД</h2>
        <p className="text-sm text-tg-hint">
          Мониторинг показателя Тр (погибших на 10 000 ТС) в рамках
          национального проекта «Безопасные дорожные движения».
        </p>
      </div>

      {/* Селектор региона + переключатель плана */}
      <div className="tg-card space-y-3">
        <div>
          <label className="tg-section-header block mb-1">Регион</label>
          <select
            className="tg-input w-full"
            value={selectedRegion}
            onChange={(e) => { haptic('light'); setSelectedRegion(e.target.value) }}
          >
            {regionsQuery.data.map((r: NpBddRegion) => (
              <option key={r.code} value={r.code}>{r.name}</option>
            ))}
          </select>
        </div>

        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="tg-section-header text-xs">Линия плана (график 2)</div>
            <div className="text-sm text-tg-hint mt-0.5">
              {planLineMode === 'linear'
                ? 'Линейный рост от 0 до годового плана'
                : 'Горизонтальная линия на уровне годового плана'}
            </div>
          </div>
          <button
            className="tg-button !py-2 !px-3 text-sm"
            onClick={handleTogglePlanLine}
            disabled={updateSettingsMutation.isPending}
          >
            {planLineMode === 'linear' ? 'Линейный' : 'Горизонтальный'}
          </button>
        </div>
      </div>

      {/* KPI-карточки + графики */}
      {dataQuery.isLoading && <div className="tg-card">Загрузка данных…</div>}
      {dataQuery.isError && (
        <div className="tg-card text-red-600 dark:text-red-400">
          Не удалось загрузить данные: {(dataQuery.error as Error).message}
        </div>
      )}
      {dataQuery.data && (
        <NpBddContent
          data={dataQuery.data}
          chart1Data={chart1Data}
          chart2Data={chart2Data}
          frozenYears={frozenQuery.data ?? []}
          onFreeze={handleFreeze}
          onUnfreeze={handleUnfreeze}
          freezePending={freezeMutation.isPending}
          unfreezePending={unfreezeMutation.isPending}
        />
      )}
    </div>
  )
}

// ============================================================
// Контент с KPI и графиками
// ============================================================

interface NpBddContentProps {
  data: NpBddData
  chart1Data: Array<{ year: string; plan: number; fact: number | null; isForecast: boolean }>
  chart2Data: Array<{ month: string; fact: number | null; forecast: number | null; plan: number }>
  frozenYears: Array<{ year: number; tr: number; deaths: number; frozen_at?: string; note?: string }>
  onFreeze: (year: number) => void
  onUnfreeze: (year: number) => void
  freezePending: boolean
  unfreezePending: boolean
}

function NpBddContent({
  data, chart1Data, chart2Data, frozenYears,
  onFreeze, onUnfreeze, freezePending, unfreezePending,
}: NpBddContentProps) {
  const { kpi, region, current_year } = data

  // Замороженные годы как Set для быстрой проверки
  const frozenSet = useMemo(() => new Set(frozenYears.map((f) => f.year)), [frozenYears])

  // Годы для кнопок заморозки: 2023, 2024, ..., текущий год - 1
  const freezableYears = useMemo(() => {
    const currentYear = current_year.year
    const years: number[] = []
    for (let y = 2023; y < currentYear; y++) years.push(y)
    return years
  }, [current_year.year])

  return (
    <>
      {/* 4 KPI-карточки */}
      <div className="grid grid-cols-2 gap-2">
        <KpiCard
          label={`Тр факт (${current_year.months_actual.length} мес)`}
          value={kpi.tr_actual_ytd.toFixed(3)}
          hint={`${current_year.deaths_ytd} погибших YTD`}
        />
        <KpiCard
          label="Тр прогноз (конец года)"
          value={kpi.tr_forecast_full_year.toFixed(3)}
          hint={`≈ ${current_year.deaths_forecast_full_year} погибших`}
          highlight={kpi.status}
        />
        <KpiCard
          label={`План ${current_year.year}`}
          value={kpi.tr_plan.toFixed(3)}
          hint="Из паспорта НП БДД"
        />
        <KpiCard
          label="Отклонение от плана"
          value={`${kpi.deviation_pct > 0 ? '+' : ''}${kpi.deviation_pct}%`}
          hint={STATUS_LABELS[kpi.status]}
          highlight={kpi.status}
        />
      </div>

      {/* График 1: динамика 2023→2030 */}
      <div className="tg-card">
        <h3 className="font-semibold mb-1">Динамика Тр 2023 → 2030</h3>
        <p className="text-xs text-tg-hint mb-3">
          Факт (история + прогноз текущего года) и плановые значения из паспорта НП БДД.
        </p>
        <div className="h-64">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chart1Data} margin={{ top: 5, right: 10, bottom: 5, left: -10 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--tg-color-hint, #999)" opacity={0.3} />
              <XAxis dataKey="year" tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} />
              <Tooltip
                contentStyle={{
                  background: 'var(--tg-color-section-bg, #fff)',
                  border: '1px solid var(--tg-color-hint, #ccc)',
                  borderRadius: '8px',
                  fontSize: '12px',
                }}
                formatter={(v: unknown) =>
                  typeof v === 'number' ? v.toFixed(3) : (v != null ? String(v) : '—')
                }
              />
              <Legend wrapperStyle={{ fontSize: '11px' }} />
              <Line
                type="monotone"
                dataKey="plan"
                name="План"
                stroke="var(--tg-color-link, #2481cc)"
                strokeWidth={2}
                strokeDasharray="5 5"
                dot={{ r: 3 }}
              />
              <Line
                type="monotone"
                dataKey="fact"
                name="Факт / прогноз"
                stroke="var(--tg-color-destructive, #ff3b30)"
                strokeWidth={2.5}
                dot={({ cx, cy, payload }) => {
                  if (cy === null || cy === undefined) return <></>
                  const isForecast = payload?.isForecast
                  return (
                    <circle
                      key={payload?.year}
                      cx={cx}
                      cy={cy}
                      r={4}
                      fill={isForecast ? 'var(--tg-color-link, #2481cc)' : 'var(--tg-color-destructive, #ff3b30)'}
                      stroke="var(--tg-color-bg, #fff)"
                      strokeWidth={1.5}
                    />
                  )
                }}
                connectNulls
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
        <div className="text-xs text-tg-hint mt-2">
          ● Красная точка — факт, ● синяя точка — прогноз на конец текущего года.
        </div>
      </div>

      {/* График 2: кумулятивный Тр по месяцам текущего года */}
      <div className="tg-card">
        <h3 className="font-semibold mb-1">
          Текущий {current_year.year}: кумулятивный Тр по месяцам
        </h3>
        <p className="text-xs text-tg-hint mb-3">
          Сплошная — факт (прошедшие месяцы), пунктир — прогноз (будущие), линия плана — {data.current_year.monthly_chart.plan_line_mode === 'linear' ? 'линейный рост' : 'горизонталь'}.
        </p>
        <div className="h-64">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chart2Data} margin={{ top: 5, right: 10, bottom: 5, left: -10 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--tg-color-hint, #999)" opacity={0.3} />
              <XAxis dataKey="month" tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} />
              <Tooltip
                contentStyle={{
                  background: 'var(--tg-color-section-bg, #fff)',
                  border: '1px solid var(--tg-color-hint, #ccc)',
                  borderRadius: '8px',
                  fontSize: '12px',
                }}
                formatter={(v: unknown) =>
                  typeof v === 'number' ? v.toFixed(3) : (v != null ? String(v) : '—')
                }
              />
              <Legend wrapperStyle={{ fontSize: '11px' }} />
              <Line
                type="monotone"
                dataKey="fact"
                name="Факт (кум.)"
                stroke="var(--tg-color-destructive, #ff3b30)"
                strokeWidth={2.5}
                dot={{ r: 3 }}
                connectNulls
              />
              <Line
                type="monotone"
                dataKey="forecast"
                name="Прогноз (кум.)"
                stroke="var(--tg-color-link, #2481cc)"
                strokeWidth={2}
                strokeDasharray="5 5"
                dot={{ r: 3 }}
                connectNulls
              />
              <Line
                type="monotone"
                dataKey="plan"
                name={`План (${data.current_year.monthly_chart.plan_line_mode === 'linear' ? 'линейн.' : 'гориз.'})`}
                stroke="var(--tg-color-hint, #999)"
                strokeWidth={1.5}
                strokeDasharray="2 2"
                dot={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Замороженные годы */}
      <div className="tg-card">
        <h3 className="font-semibold mb-2">Заморозка лет</h3>
        <p className="text-xs text-tg-hint mb-3">
          После финализации данных ГИБДД (через 2-3 месяца после окончания года)
          заморозьте год, чтобы он не пересчитывался.
        </p>

        {frozenYears.length > 0 && (
          <div className="space-y-1 mb-3">
            <div className="tg-section-header text-xs">Заморожено:</div>
            {frozenYears.map((f) => (
              <div
                key={f.year}
                className="flex items-center justify-between gap-2 py-1 px-2 rounded-lg bg-tg-secondary-bg"
              >
                <div className="text-sm">
                  <span className="font-medium">{f.year}</span>{' '}
                  <span className="text-tg-hint">
                    Тр={f.tr.toFixed(3)}, {f.deaths} погибших
                  </span>
                  {f.note && <div className="text-xs text-tg-hint italic">{f.note}</div>}
                </div>
                <button
                  className="text-xs text-red-600 dark:text-red-400"
                  onClick={() => onUnfreeze(f.year)}
                  disabled={unfreezePending}
                >
                  Разморозить
                </button>
              </div>
            ))}
          </div>
        )}

        <div className="tg-section-header text-xs mb-1">Доступно для заморозки:</div>
        <div className="flex flex-wrap gap-1">
          {freezableYears.map((y) => {
            const isFrozen = frozenSet.has(y)
            const hist = data.history[String(y)]
            return (
              <button
                key={y}
                className={cn(
                  'px-2 py-1 rounded-lg text-xs',
                  isFrozen
                    ? 'bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300'
                    : 'bg-tg-secondary-bg hover:opacity-80'
                )}
                onClick={() => !isFrozen && onFreeze(y)}
                disabled={isFrozen || freezePending || !hist}
                title={hist ? `Тр=${hist.tr}, ${hist.deaths} погибших` : 'Нет данных за год'}
              >
                {y}
                {isFrozen && ' ✓'}
                {!isFrozen && hist && `: ${hist.tr.toFixed(2)}`}
              </button>
            )
          })}
        </div>
      </div>

      {/* Подвал: расчёт-время */}
      <div className="text-xs text-tg-hint text-center">
        Регион: {region.name} ({region.code}) · расчёт от {new Date(data.calculated_at).toLocaleString('ru-RU')}
      </div>
    </>
  )
}

// ============================================================
// KPI-карточка
// ============================================================

interface KpiCardProps {
  label: string
  value: string
  hint?: string
  highlight?: 'ok' | 'warning' | 'danger'
}

function KpiCard({ label, value, hint, highlight }: KpiCardProps) {
  return (
    <div
      className={cn(
        'tg-card !mb-0 flex flex-col justify-between',
        highlight === 'ok' && 'border-l-4 border-green-500',
        highlight === 'warning' && 'border-l-4 border-yellow-500',
        highlight === 'danger' && 'border-l-4 border-red-500',
      )}
    >
      <div>
        <div className="tg-section-header text-xs">{label}</div>
        <div
          className={cn(
            'text-xl font-bold mt-0.5',
            highlight && STATUS_COLORS[highlight],
          )}
        >
          {value}
        </div>
      </div>
      {hint && <div className="text-xs text-tg-hint mt-1">{hint}</div>}
    </div>
  )
}
