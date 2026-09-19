import React, { useState } from 'react'
import { api, handleActError } from '../api'
import { useStore } from '../store'

// 章节通关结算卡：展示奖励交接（通关金币 + 营地休整），点击进入下一章。
// next_chapter 是幂等行动：重复点击/重试由 request_id 与服务端 pending_next 双重拦截。
export default function ChapterClearView({ view }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const runId = useStore((s) => s.runId)
  const applyRun = useStore((s) => s.applyRun)
  const exp = view.expedition
  if (!exp) return null

  async function advance() {
    setBusy(true); setErr('')
    try {
      const res = await api.act(runId, { action: 'next_chapter' })
      applyRun(res.run)
    } catch (e) {
      setErr(await handleActError(e, runId, applyRun))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="overlay">
      <div className="rewardcard panel">
        <h2>🚩 第 {exp.chapter} 章通关！</h2>
        <p className="chapterhint">
          牌组、锻造成长与遗物将携带进入下一章；开章时获得通关金币并在营地休整恢复生命。
        </p>
        <div className="fieldrow">
          <button className="primary" onClick={advance} disabled={busy}>
            {busy ? '开章中…' : `进入第 ${exp.chapter + 1} / ${exp.total_chapters} 章 →`}
          </button>
        </div>
        {err && <div className="error">{err}</div>}
      </div>
    </div>
  )
}
