"use client";

import { createContext, ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { applyUpdate, getUpdateStatus, UpdateStatus } from "../../lib/api";
import { getToken } from "../../lib/auth";
import { fmt } from "../../lib/utils";
import { useLanguage } from "../../context/LanguageContext";
import { useToast } from "../ui/toast";

/**
 * 更新进度：
 * pulling 下载新镜像 → restarting 助手容器重建面板 → done 新面板已就绪（随后刷新页面）
 * rolled_back 新版本没起来、已恢复旧容器；timeout 等太久，交给用户手动刷新
 */
export type UpdatePhase = "idle" | "pulling" | "restarting" | "done" | "up_to_date" | "rolled_back" | "failed" | "timeout";

interface SelfUpdateValue {
  status: UpdateStatus | null;
  checking: boolean;
  checkFailed: boolean;
  phase: UpdatePhase;
  /** 失败或回滚时的说明 */
  detail: string | null;
  updateAvailable: boolean;
  check: (refresh?: boolean) => Promise<void>;
  start: () => Promise<void>;
  reset: () => void;
}

const SelfUpdateContext = createContext<SelfUpdateValue | undefined>(undefined);

const POLL_MS = 2000;
const GIVE_UP_MS = 20 * 60_000;
// 新面板起来后，助手还要清理旧容器才退出，最多等这么久拿它的结果
const RESULT_WAIT_MS = 30_000;
const DONE_FLAG = "tg-update-done";

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
const identity = (s: UpdateStatus) => `${s.current.version}|${s.current.build_sha}|${s.current.built_at}`;

export function SelfUpdateProvider({ children }: { children: ReactNode }) {
  const { t } = useLanguage();
  const { addToast } = useToast();
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkFailed, setCheckFailed] = useState(false);
  const [phase, setPhase] = useState<UpdatePhase>("idle");
  const [detail, setDetail] = useState<string | null>(null);
  const tracking = useRef(false);
  const tRef = useRef(t);
  tRef.current = t;

  const finish = useCallback((next: UpdatePhase, message: string | null = null) => {
    tracking.current = false;
    setPhase(next);
    setDetail(message);
  }, []);

  /** 盯着服务端直到更新有结果；before 是开始更新前的状态 */
  const track = useCallback(
    async (token: string, before: UpdateStatus) => {
      if (tracking.current) return;
      tracking.current = true;
      const startedAt = Date.now();
      const seenResult = before.last_result?.finished_at ?? null;
      let restartedAt: number | null = null;

      while (tracking.current) {
        if (Date.now() - startedAt > GIVE_UP_MS) return finish("timeout");
        await sleep(POLL_MS);
        let s: UpdateStatus;
        try {
          s = await getUpdateStatus(token, { timeoutMs: 8000 });
        } catch {
          // 面板正在重启，连不上是正常的
          setPhase("restarting");
          continue;
        }
        setStatus(s);

        const result = s.last_result && s.last_result.finished_at !== seenResult ? s.last_result : null;
        if (result) {
          return result.ok ? finish("done") : finish("rolled_back", result.message);
        }
        if (s.job.state === "pulling") {
          setPhase("pulling");
          setDetail(s.job.message);
        } else if (s.job.state === "up_to_date") {
          return finish("up_to_date");
        } else if (s.job.state === "failed") {
          // 仓库拒绝拉取时原始报错没什么帮助，界面会换成说明
          return finish("failed", s.job.code === "not_pullable" ? null : s.job.message);
        } else if (s.job.state === "restarting") {
          setPhase("restarting");
        } else {
          // 状态回到 idle：已经是重启后的新进程，等助手的结果，等不到就看版本有没有变
          setPhase("restarting");
          restartedAt ??= Date.now();
          if (Date.now() - restartedAt > RESULT_WAIT_MS) {
            return identity(s) !== identity(before) ? finish("done") : finish("rolled_back", null);
          }
        }
      }
    },
    [finish]
  );

  const check = useCallback(async (refresh = false) => {
    const token = getToken();
    if (!token) return;
    setChecking(true);
    try {
      const s = await getUpdateStatus(token, { refresh });
      setStatus(s);
      setCheckFailed(false);
    } catch {
      setCheckFailed(true);
    } finally {
      setChecking(false);
    }
  }, []);

  const start = useCallback(async () => {
    const token = getToken();
    if (!token || !status || tracking.current) return;
    setDetail(null);
    setPhase("pulling");
    try {
      await applyUpdate(token);
    } catch (err: any) {
      // 别的页面已经点了更新：直接跟着看进度
      if (err?.code === "UPDATE_IN_PROGRESS") {
        track(token, status);
        return;
      }
      setPhase("idle");
      const key =
        err?.code === "TASKS_RUNNING"
          ? "update_tasks_running"
          : err?.code === "UPDATE_NOT_PULLABLE"
            ? "update_pull_denied"
            : "update_start_failed";
      addToast(tRef.current(key), "error");
      // 部署条件变了（比如 socket 被移除），刷新状态让界面换成手动更新的说明
      if (typeof err?.code === "string" && err.code.startsWith("UPDATE_")) check();
      return;
    }
    track(token, status);
  }, [status, track, addToast, check]);

  const reset = useCallback(() => {
    if (!tracking.current) {
      setPhase("idle");
      setDetail(null);
    }
  }, []);

  // 进入面板时查一次（服务端缓存 6 小时）；刷新页面时如果更新还在进行，接着盯
  useEffect(() => {
    const token = getToken();
    if (!token) return;
    let cancelled = false;
    getUpdateStatus(token)
      .then((s) => {
        if (cancelled) return;
        setStatus(s);
        if (s.job.state === "pulling" || s.job.state === "restarting") {
          setPhase(s.job.state);
          track(token, s);
        }
      })
      .catch(() => !cancelled && setCheckFailed(true));
    try {
      const done = sessionStorage.getItem(DONE_FLAG);
      if (done) {
        sessionStorage.removeItem(DONE_FLAG);
        addToast(fmt(tRef.current("update_done_toast"), { version: done }), "success");
      }
    } catch {}
    return () => {
      cancelled = true;
      tracking.current = false;
    };
  }, [track, addToast]);

  // 新面板就绪：记下版本，刷新页面加载新的前端
  useEffect(() => {
    if (phase !== "done") return;
    try {
      sessionStorage.setItem(DONE_FLAG, status?.current.version || "");
    } catch {}
    const timer = setTimeout(() => window.location.reload(), 1500);
    return () => clearTimeout(timer);
  }, [phase, status]);

  const updateAvailable = Boolean(status?.update_available) && phase !== "done";

  const value = useMemo<SelfUpdateValue>(
    () => ({ status, checking, checkFailed, phase, detail, updateAvailable, check, start, reset }),
    [status, checking, checkFailed, phase, detail, updateAvailable, check, start, reset]
  );

  return <SelfUpdateContext.Provider value={value}>{children}</SelfUpdateContext.Provider>;
}

export function useSelfUpdate() {
  const context = useContext(SelfUpdateContext);
  if (context === undefined) {
    throw new Error("useSelfUpdate must be used within a SelfUpdateProvider");
  }
  return context;
}
