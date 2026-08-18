/**
 * 基础设置中的滑块滑动方式切换项。
 *
 * 功能：
 * 1. 展示浏览器自动 / 真实鼠标 / CDP真机Chrome 三种方式
 * 2. 切换后立即持久化并反馈结果
 */
import { useState } from 'react'
import { Loader2 } from 'lucide-react'

import {
  normalizeCaptchaFailureNotifyThreshold,
  normalizeSliderMode,
  updateCaptchaFailureNotifyThreshold,
  updateSliderMode,
} from '@/api/sliderModeSettings'
import { useUIStore } from '@/store/uiStore'
import { getApiErrorMessage } from '@/utils/apiError'
import type { SliderMode } from '@/types'

interface SliderModeSettingProps {
  value: unknown
  onSaved: (mode: SliderMode) => void
  failureNotifyThreshold: unknown
  onThresholdSaved: (threshold: number) => void
}

const MODE_LABELS: Record<SliderMode, string> = {
  browser: '浏览器自动滑动',
  real_mouse: '真实鼠标滑动',
  chrome_cdp: 'CDP真机Chrome+鼠标',
}

export function SliderModeSetting({
  value,
  onSaved,
  failureNotifyThreshold,
  onThresholdSaved,
}: SliderModeSettingProps) {
  const { addToast } = useUIStore()
  const [saving, setSaving] = useState(false)
  const [thresholdSaving, setThresholdSaving] = useState(false)
  const currentMode = normalizeSliderMode(value)
  const currentThreshold = normalizeCaptchaFailureNotifyThreshold(failureNotifyThreshold)

  const handleChange = async (mode: SliderMode) => {
    if (saving || mode === currentMode) {
      return
    }

    try {
      setSaving(true)
      const result = await updateSliderMode(mode)
      if (!result.success) {
        addToast({ type: 'error', message: result.message || '滑块滑动方式切换失败' })
        return
      }

      onSaved(mode)
      addToast({
        type: 'success',
        message: `已切换为${MODE_LABELS[mode]}，后续滑块实时生效`,
      })
    } catch (error) {
      addToast({
        type: 'error',
        message: getApiErrorMessage(error, '滑块滑动方式切换失败'),
      })
    } finally {
      setSaving(false)
    }
  }

  const handleThresholdSave = async (rawValue: string) => {
    const threshold = normalizeCaptchaFailureNotifyThreshold(rawValue)
    if (thresholdSaving || threshold === currentThreshold) {
      return
    }

    try {
      setThresholdSaving(true)
      const result = await updateCaptchaFailureNotifyThreshold(threshold)
      if (!result.success) {
        addToast({ type: 'error', message: result.message || '连续滑块失败通知配置保存失败' })
        return
      }

      onThresholdSaved(threshold)
      addToast({
        type: 'success',
        message: threshold > 0
          ? `已设置连续超过${threshold}次滑块失败时通知`
          : '已关闭连续滑块失败通知',
      })
    } catch (error) {
      addToast({
        type: 'error',
        message: getApiErrorMessage(error, '连续滑块失败通知配置保存失败'),
      })
    } finally {
      setThresholdSaving(false)
    }
  }

  return (
    <div className="border-t border-slate-100 dark:border-slate-700">
      <div className="flex items-center justify-between gap-4 py-3">
        <div className="min-w-0">
          <p className="font-medium text-slate-900 dark:text-slate-100">滑块滑动方式</p>
          <p className="text-sm text-slate-500 dark:text-slate-400">
            推荐「CDP真机Chrome+鼠标」：连接本机已登录闲鱼的 Chrome，物理鼠标滑动，通过率更高。
            使用前请先运行 scripts/start-chrome-cdp.ps1 启动调试 Chrome。
          </p>
        </div>
        <div className="relative shrink-0">
          <select
            aria-label="滑块滑动方式"
            value={currentMode}
            disabled={saving}
            onChange={(event) => void handleChange(event.target.value as SliderMode)}
            className="input-ios w-56 pr-9 disabled:cursor-not-allowed disabled:opacity-60"
          >
            <option value="browser">浏览器自动滑动</option>
            <option value="real_mouse">真实鼠标滑动</option>
            <option value="chrome_cdp">CDP真机Chrome+鼠标</option>
          </select>
          {saving && (
            <Loader2 className="pointer-events-none absolute right-8 top-1/2 h-4 w-4 -translate-y-1/2 animate-spin text-blue-500" />
          )}
        </div>
      </div>
      <div className="flex items-center justify-between gap-4 py-3 border-t border-slate-100 dark:border-slate-700">
        <div className="min-w-0">
          <p className="font-medium text-slate-900 dark:text-slate-100">连续滑块失败通知</p>
          <p className="text-sm text-slate-500 dark:text-slate-400">
            连续失败超过此次数后，通过当前账号已配置的通知渠道发送一次提醒；0表示不通知。
          </p>
        </div>
        <div className="relative shrink-0">
          <input
            aria-label="连续滑块失败通知次数"
            type="number"
            min={0}
            max={1000}
            step={1}
            defaultValue={currentThreshold}
            key={currentThreshold}
            disabled={thresholdSaving}
            onBlur={(event) => void handleThresholdSave(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.currentTarget.blur()
              }
            }}
            className="input-ios w-28 pr-8 text-center disabled:cursor-not-allowed disabled:opacity-60"
          />
          {thresholdSaving && (
            <Loader2 className="pointer-events-none absolute right-2 top-1/2 h-4 w-4 -translate-y-1/2 animate-spin text-blue-500" />
          )}
        </div>
      </div>
    </div>
  )
}
